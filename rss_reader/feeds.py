from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import feedparser

from .config import Config
from .db import connect, group_descendant_ids, transaction, utcnow
from .generated_feeds import is_generated_feed_url, read_generated_feed
from .net import read_limited_response, safe_external_url, safe_get, validate_http_url

LOGGER = logging.getLogger(__name__)
FUTURE_SOURCE_TOLERANCE = timedelta(hours=2)
_PROVIDER_CHALLENGE_MARKERS = (
    b"making sure you&#39;re not a bot",
    b"making sure you're not a bot",
    b"/.within.website/x/",
    b"anubis",
    b"captcha",
    b"cf-chl-",
)


class RefreshCancelled(Exception):
    """A queued feed fetch was skipped after a cooperative stop request."""


class RefreshDeferred(Exception):
    """A selected feed was intentionally deferred without making a request."""

    def __init__(self, message: str, *, kind: str = "retry-backoff", until: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.until = until


class FeedSourceFailure(RuntimeError):
    """A classified source response that is unsafe or unsuitable as a feed."""

    def __init__(
        self, message: str, *, kind: str, status_code: int | None = None,
        content_type: str | None = None, retry_hours: int | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.content_type = content_type
        self.retry_hours = retry_hours
        self.retry_at = retry_at


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in {"script", "style", "template", "noscript"}:
            self.suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "template", "noscript"} and self.suppressed_depth:
            self.suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.suppressed_depth:
            self.parts.append(data)


def plain_text(value: str | None) -> str:
    parser = TextExtractor()
    try:
        parser.feed(value or "")
    except Exception:
        return re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _entry_time(entry: Any) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=UTC).isoformat(timespec="seconds")
    return None


def _entry_datetime(entry: Any) -> datetime | None:
    value = _entry_time(entry)
    return datetime.fromisoformat(value) if value else None


def _stored_entry_times(entry: Any, discovered: datetime) -> tuple[str | None, str | None, str | None]:
    """Return display time, source time, and any source-date warning."""
    source = _entry_datetime(entry)
    source_text = source.isoformat(timespec="seconds") if source else None
    if source is not None and source > discovered + FUTURE_SOURCE_TOLERANCE:
        return discovered.isoformat(timespec="seconds"), source_text, "future-source-date"
    return source_text, source_text, None


def _content_kind(url: str | None) -> tuple[str, str]:
    """Classify only explicit, high-confidence media URL forms."""
    if not url:
        return "", ""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    segments = [part for part in parsed.path.split("/") if part]
    if (hostname == "youtube.com" or hostname.endswith(".youtube.com")) and (
        len(segments) >= 2 and segments[0].casefold() == "shorts" and segments[1]
    ):
        return "youtube-short", "explicit-url"
    return "", ""


def _looks_like_html(content: bytes, content_type: str | None) -> bool:
    media_type = str(content_type or "").split(";", 1)[0].strip().casefold()
    prefix = content.lstrip()[:256].lower()
    return media_type in {"text/html", "application/xhtml+xml"} or (
        prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")
    )


def _is_provider_challenge(content: bytes, content_type: str | None) -> bool:
    if not _looks_like_html(content, content_type):
        return False
    sample = content[:16_384].lower()
    return any(marker in sample for marker in _PROVIDER_CHALLENGE_MARKERS)


def _stable_id(feed_url: str, entry: Any) -> str:
    raw = entry.get("id") or entry.get("guid") or entry.get("link")
    if not raw:
        raw = "\x1f".join([feed_url, entry.get("title", ""), str(_entry_time(entry) or "")])
    return hashlib.sha256(str(raw).encode("utf-8", "replace")).hexdigest()


def _description(entry: Any) -> str:
    candidates = []
    if entry.get("summary"):
        candidates.append(entry["summary"])
    for content in entry.get("content", []):
        if content.get("value"):
            candidates.append(content["value"])
    return plain_text(max(candidates, key=len, default=""))


def _failure_delay(config: Config, failures: int) -> datetime:
    feeds = config.section("feeds")
    minutes = int(feeds["retry_base_minutes"]) * (2 ** max(0, failures - 1))
    return datetime.now(UTC) + timedelta(minutes=min(minutes, int(feeds["retry_max_hours"]) * 60))


def _future_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _entries_to_store(entries: list[Any], feed, options: dict[str, Any]) -> tuple[list[Any], bool]:
    """Apply a strict recent-history cap the first time a feed is retrieved."""
    limit = int(options["max_entries_per_feed_update"])
    initial_import = not bool(feed["last_success_at"])
    if not initial_import:
        return entries[:limit], False

    cutoff = datetime.now(UTC) - timedelta(days=int(options["initial_import_max_age_days"]))
    recent = [entry for entry in entries if _entry_datetime(entry) is None or _entry_datetime(entry) >= cutoff]
    # Bibliography feeds such as DBLP often publish valid updates whose dates are
    # normalized to the first day of a month or year.  A feed can therefore be
    # current while every entry falls just outside the age window.  Keep the
    # import bounded, but do not turn a valid non-empty feed into an empty one.
    if entries and not recent:
        LOGGER.info(
            "No entries met the initial age window for feed id=%s; using the bounded newest-entry fallback",
            feed["id"],
        )
        recent = list(entries)
    recent.sort(key=lambda entry: _entry_datetime(entry) or datetime.min.replace(tzinfo=UTC), reverse=True)
    initial_limit = min(limit, int(options["initial_import_max_entries_per_feed"]))
    return recent[:initial_limit], True


def _read_feed_source(
    config: Config, feed: Any, headers: dict[str, str],
) -> tuple[bytes | None, str | None, str | None, str | None, int | None, str | None]:
    """Return content, link base, validators, status, and content type."""
    options = config.section("feeds")
    source = str(feed["xml_url"])
    if is_generated_feed_url(source):
        content = read_generated_feed(config, source, int(options["max_response_bytes"]))
        return content, None, None, None, None, "application/xml"
    validate_http_url(source, bool(options["allow_private_urls"]))
    with safe_get(
        source, headers=headers, timeout=int(options["timeout_seconds"]),
        allow_private=bool(options["allow_private_urls"]),
    ) as response:
        response.raise_for_status() if response.status_code != 304 else None
        if response.status_code == 304:
            return None, response.url, response.headers.get("ETag"), response.headers.get(
                "Last-Modified"
            ), 304, response.headers.get("Content-Type")
        content = read_limited_response(response, int(options["max_response_bytes"]))
        return (
            content, response.url, response.headers.get("ETag"),
            response.headers.get("Last-Modified"), int(response.status_code),
            response.headers.get("Content-Type"),
        )


def refresh_feed(connection, config: Config, feed, force: bool = False) -> int:
    now = datetime.now(UTC)
    retry_time = _future_time(feed["next_retry_at"])
    if not force and retry_time is not None and retry_time > now:
        LOGGER.info("Deferring feed %s until retry time %s", feed["title"], feed["next_retry_at"])
        raise RefreshDeferred(
            f"Feed is deferred until {feed['next_retry_at']}",
            kind=str(feed["last_error_kind"] or "retry-backoff"),
            until=str(feed["next_retry_at"]),
        )
    options = config.section("feeds")
    headers = {"User-Agent": str(options["user_agent"]), "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*;q=0.1"}
    # A manual force retry must make a real request for the current document.
    # This also avoids reusing validators captured around a captive-portal or
    # otherwise broken network session.
    if not force and feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if not force and feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]
    LOGGER.info("Reading feed id=%s title=%r source=%s", feed["id"], feed["title"], feed["xml_url"])
    source_status: int | None = None
    source_content_type: str | None = None
    try:
        content, base_url, etag, last_modified, source_status, source_content_type = _read_feed_source(
            config, feed, headers,
        )
        if content is None and source_status == 304:
            connection.execute(
                """UPDATE feeds SET last_attempt_at=?, last_success_at=?, consecutive_failures=0,
                   next_retry_at=NULL, last_http_status=304,last_content_type=?,
                   last_error_kind=NULL,last_error=NULL WHERE id=?""",
                (utcnow(), utcnow(), str(source_content_type or "")[:200] or None, feed["id"]),
            )
            return 0
        assert content is not None
        if _is_provider_challenge(content, source_content_type):
            raise FeedSourceFailure(
                "Source returned an anti-bot HTML challenge; RSS retrieval deferred",
                kind="provider-challenge", status_code=source_status,
                content_type=source_content_type, retry_hours=24,
            )
        # Parsing bytes directly avoids treating missing HTTP metadata as fatal
        # and uses the same parser for remote and generated feed documents.
        parsed = feedparser.parse(content)
        if parsed.bozo and not parsed.entries:
            kind = "unexpected-html" if _looks_like_html(content, source_content_type) else "malformed-feed"
            raise FeedSourceFailure(
                f"Feed parse error: {parsed.bozo_exception}", kind=kind,
                status_code=source_status, content_type=source_content_type,
            )
        if parsed.bozo:
            LOGGER.warning("Feed id=%s recovered with parse warning: %s", feed["id"], parsed.bozo_exception)
        entries, initial_import = _entries_to_store(list(parsed.entries), feed, options)
        added = 0
        with transaction(connection, immediate=True):
            title = (plain_text(parsed.feed.get("title")) or feed["title"])[:300]
            html_url = safe_external_url(parsed.feed.get("link")) or safe_external_url(feed["html_url"])
            connection.execute(
                """UPDATE feeds SET title=CASE WHEN title_locked=1 THEN title ELSE ? END,
                   html_url=?, etag=?, last_modified=?, last_attempt_at=?,
                   last_success_at=?, consecutive_failures=0, next_retry_at=NULL,
                   last_http_status=?,last_content_type=?,last_error_kind=NULL,last_error=NULL
                   WHERE id=?""",
                (
                    title, html_url, etag, last_modified, utcnow(), utcnow(),
                    source_status, str(source_content_type or "")[:200] or None, feed["id"],
                ),
            )
            for entry in entries:
                entry_link = str(entry.get("link", ""))
                item_url = safe_external_url(
                    urljoin(base_url, entry_link) if base_url else entry_link
                )
                discovered = datetime.now(UTC).replace(microsecond=0)
                published_at, source_published_at, date_warning = _stored_entry_times(entry, discovered)
                content_kind, content_kind_source = _content_kind(item_url)
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO items(
                           feed_id, stable_id, title, url, author, published_at,
                           source_published_at,discovered_at,date_warning,content_kind,
                           content_kind_source,description_text
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        feed["id"], _stable_id(feed["xml_url"], entry),
                        (plain_text(entry.get("title")) or "Untitled entry")[:1000], item_url,
                        plain_text(entry.get("author"))[:500] or None, published_at,
                        source_published_at, discovered.isoformat(timespec="seconds"), date_warning,
                        content_kind, content_kind_source, _description(entry),
                    ),
                )
                added += max(cursor.rowcount, 0)
        LOGGER.info(
            "Feed id=%s parsed=%d considered=%d new=%d initial_import=%s",
            feed["id"], len(parsed.entries), len(entries), added, initial_import,
        )
        return added
    except Exception as exc:
        failures = int(feed["consecutive_failures"]) + 1
        response = getattr(exc, "response", None)
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(response, "status_code", None)
        content_type = getattr(exc, "content_type", None) or source_content_type
        if not content_type and response is not None:
            content_type = getattr(response, "headers", {}).get("Content-Type")
        kind = getattr(exc, "kind", None)
        if not kind:
            if status is not None and int(status) >= 400:
                kind = "http-error"
            elif isinstance(exc, (OSError, TimeoutError)) or exc.__class__.__name__ in {
                "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
            }:
                kind = "network-failure"
            else:
                kind = "feed-error"
        retry_at = getattr(exc, "retry_at", None)
        retry_hours = getattr(exc, "retry_hours", None)
        if retry_at is None:
            retry_at = (
                datetime.now(UTC) + timedelta(hours=max(1, int(retry_hours)))
                if retry_hours is not None else _failure_delay(config, failures)
            )
        connection.execute(
            """UPDATE feeds SET last_attempt_at=?, consecutive_failures=?, next_retry_at=?,
               last_http_status=?,last_content_type=?,last_error_kind=?,last_error=? WHERE id=?""",
            (
                utcnow(), failures, retry_at.isoformat(), status,
                str(content_type or "")[:200] or None, str(kind)[:100], str(exc)[:1000], feed["id"],
            ),
        )
        LOGGER.error("Feed id=%s failed: %s", feed["id"], exc)
        LOGGER.debug("Feed id=%s traceback", feed["id"], exc_info=True)
        raise


def refresh_all(
    connection, config: Config, feed_id: int | None = None,
    group_id: int | None = None, force: bool = False,
    cancel_requested: Callable[[Any], bool] | None = None,
) -> dict[str, Any]:
    if feed_id is not None and group_id is not None:
        raise ValueError("Choose either a group or a feed refresh scope")
    # plugin:// sources belong to installed plugins. HTTP(S) and explicitly
    # configured generated:// sources use the generic RSS worker.
    query = "SELECT * FROM feeds WHERE enabled=1 AND xml_url NOT LIKE 'plugin:%'"
    params: list[Any] = []
    if feed_id is not None:
        query += " AND id=?"
        params.append(feed_id)
    elif group_id is not None:
        group_ids = group_descendant_ids(connection, group_id)
        if not group_ids:
            return {
                "attempted": 0, "succeeded": 0, "failed": 0, "deferred": 0,
                "new_items": 0, "failure_kinds": {}, "feed_failures": [],
                "deferred_kinds": {}, "feed_deferred": [],
            }
        marks = ",".join("?" for _ in group_ids)
        query += f" AND group_id IN ({marks})"
        params.extend(group_ids)
    feeds = connection.execute(query + " ORDER BY id", params).fetchall()
    stats: dict[str, Any] = {
        "attempted": 0, "succeeded": 0, "failed": 0, "deferred": 0,
        "new_items": 0, "failure_kinds": {}, "feed_failures": [],
        "deferred_kinds": {}, "feed_deferred": [],
    }
    if not feeds:
        return stats

    options = config.section("feeds")
    max_workers = 1 if feed_id is not None else min(int(options["max_workers"]), len(feeds))
    per_host = int(options["max_workers_per_host"])
    host_locks: defaultdict[str, threading.BoundedSemaphore] = defaultdict(
        lambda: threading.BoundedSemaphore(per_host)
    )
    host_state_lock = threading.Lock()
    host_blocked_until: dict[str, datetime] = {}
    current_time = datetime.now(UTC)
    for selected_feed in feeds:
        if str(selected_feed["last_error_kind"] or "") != "provider-challenge":
            continue
        blocked_until = _future_time(selected_feed["next_retry_at"])
        hostname = (urlparse(str(selected_feed["xml_url"])).hostname or "").casefold()
        if hostname and blocked_until is not None and blocked_until > current_time:
            host_blocked_until[hostname] = max(
                blocked_until, host_blocked_until.get(hostname, blocked_until),
            )

    def host_deferral(hostname: str, *, bypass: bool) -> None:
        if bypass or not hostname:
            return
        with host_state_lock:
            blocked_until = host_blocked_until.get(hostname)
        if blocked_until is not None and blocked_until > datetime.now(UTC):
            raise RefreshDeferred(
                f"Source host is deferred after an anti-bot challenge until {blocked_until.isoformat()}",
                kind="provider-challenge", until=blocked_until.isoformat(),
            )

    def worker(identifier: int, url: str) -> int:
        hostname = (urlparse(url).hostname or "").casefold()
        # A force request may bypass host backoff only for an explicitly selected
        # single feed. A group-level force must not fan out against a challenged host.
        bypass_host_backoff = bool(force and feed_id is not None)
        host_deferral(hostname, bypass=bypass_host_backoff)
        with host_locks[hostname]:
            host_deferral(hostname, bypass=bypass_host_backoff)
            with connect(config.database_path) as worker_connection:
                if cancel_requested and cancel_requested(worker_connection):
                    raise RefreshCancelled()
                row = worker_connection.execute("SELECT * FROM feeds WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    return 0
                try:
                    return refresh_feed(worker_connection, config, row, force)
                except FeedSourceFailure as exc:
                    if exc.kind == "provider-challenge":
                        blocked_until = exc.retry_at
                        if blocked_until is None:
                            blocked_until = datetime.now(UTC) + timedelta(
                                hours=max(1, int(exc.retry_hours or 24))
                            )
                        if hostname:
                            with host_state_lock:
                                host_blocked_until[hostname] = max(
                                    blocked_until, host_blocked_until.get(hostname, blocked_until),
                                )
                        # The feed row already records the classified source response
                        # and its long retry time. At group-operation level this is a
                        # respectful source deferral, not a malformed-feed failure.
                        raise RefreshDeferred(
                            str(exc), kind=exc.kind, until=blocked_until.isoformat(),
                        ) from exc
                    raise

    stats["attempted"] = len(feeds)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="feed") as executor:
        futures = {
            executor.submit(worker, int(feed["id"]), str(feed["xml_url"])): int(feed["id"])
            for feed in feeds
        }
        for future in as_completed(futures):
            try:
                stats["new_items"] += future.result()
                stats["succeeded"] += 1
            except RefreshDeferred as exc:
                stats["deferred"] += 1
                kind = str(exc.kind or "retry-backoff")
                kinds = stats["deferred_kinds"]
                kinds[kind] = int(kinds.get(kind, 0)) + 1
                feed_id_value = futures[future]
                feed_row = next((row for row in feeds if int(row["id"]) == feed_id_value), None)
                if len(stats["feed_deferred"]) < 50:
                    stats["feed_deferred"].append({
                        "id": feed_id_value,
                        "title": str(feed_row["title"] if feed_row is not None else f"Feed {feed_id_value}"),
                        "kind": kind,
                        "until": exc.until,
                        "reason": str(exc)[:500],
                    })
            except RefreshCancelled:
                stats["cancelled"] = True
            except Exception as exc:
                stats["failed"] += 1
                kind = str(getattr(exc, "kind", "feed-error") or "feed-error")
                kinds = stats["failure_kinds"]
                kinds[kind] = int(kinds.get(kind, 0)) + 1
                feed_id_value = futures[future]
                feed_row = next((row for row in feeds if int(row["id"]) == feed_id_value), None)
                if len(stats["feed_failures"]) < 50:
                    stats["feed_failures"].append({
                        "id": feed_id_value,
                        "title": str(feed_row["title"] if feed_row is not None else f"Feed {feed_id_value}"),
                        "kind": kind,
                        "error": str(exc)[:500],
                    })
    return stats
