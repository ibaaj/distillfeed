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


class RefreshCancelled(Exception):
    """A queued feed fetch was skipped after a cooperative stop request."""


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
) -> tuple[bytes | None, str | None, str | None, str | None, int | None]:
    """Return content, link base, validators and status for one safe source."""
    options = config.section("feeds")
    source = str(feed["xml_url"])
    if is_generated_feed_url(source):
        content = read_generated_feed(config, source, int(options["max_response_bytes"]))
        return content, None, None, None, None
    validate_http_url(source, bool(options["allow_private_urls"]))
    with safe_get(
        source, headers=headers, timeout=int(options["timeout_seconds"]),
        allow_private=bool(options["allow_private_urls"]),
    ) as response:
        response.raise_for_status() if response.status_code != 304 else None
        if response.status_code == 304:
            return None, response.url, response.headers.get("ETag"), response.headers.get(
                "Last-Modified"
            ), 304
        content = read_limited_response(response, int(options["max_response_bytes"]))
        return (
            content, response.url, response.headers.get("ETag"),
            response.headers.get("Last-Modified"), int(response.status_code),
        )


def refresh_feed(connection, config: Config, feed, force: bool = False) -> int:
    now = datetime.now(UTC)
    if not force and feed["next_retry_at"] and datetime.fromisoformat(feed["next_retry_at"]) > now:
        LOGGER.info("Skipping feed %s until retry time %s", feed["title"], feed["next_retry_at"])
        return 0
    options = config.section("feeds")
    headers = {"User-Agent": str(options["user_agent"]), "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*;q=0.1"}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]
    LOGGER.info("Reading feed id=%s title=%r source=%s", feed["id"], feed["title"], feed["xml_url"])
    try:
        content, base_url, etag, last_modified, source_status = _read_feed_source(
            config, feed, headers,
        )
        if content is None and source_status == 304:
            connection.execute(
                """UPDATE feeds SET last_attempt_at=?, last_success_at=?, consecutive_failures=0,
                   next_retry_at=NULL, last_http_status=304, last_error=NULL WHERE id=?""",
                (utcnow(), utcnow(), feed["id"]),
            )
            return 0
        assert content is not None
        # Parsing bytes directly avoids treating missing HTTP metadata as fatal
        # and uses the same parser for remote and generated feed documents.
        parsed = feedparser.parse(content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"Feed parse error: {parsed.bozo_exception}")
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
                   last_http_status=?, last_error=NULL WHERE id=?""",
                (
                    title, html_url, etag, last_modified, utcnow(), utcnow(),
                    source_status, feed["id"],
                ),
            )
            for entry in entries:
                entry_link = str(entry.get("link", ""))
                item_url = safe_external_url(
                    urljoin(base_url, entry_link) if base_url else entry_link
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO items(
                           feed_id, stable_id, title, url, author, published_at,
                           discovered_at, description_text
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        feed["id"], _stable_id(feed["xml_url"], entry),
                        (plain_text(entry.get("title")) or "Untitled entry")[:1000], item_url,
                        plain_text(entry.get("author"))[:500] or None, _entry_time(entry), utcnow(), _description(entry),
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
        status = getattr(getattr(exc, "response", None), "status_code", None)
        connection.execute(
            """UPDATE feeds SET last_attempt_at=?, consecutive_failures=?, next_retry_at=?,
               last_http_status=?, last_error=? WHERE id=?""",
            (utcnow(), failures, _failure_delay(config, failures).isoformat(), status, str(exc)[:1000], feed["id"]),
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
            return {"attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0}
        marks = ",".join("?" for _ in group_ids)
        query += f" AND group_id IN ({marks})"
        params.extend(group_ids)
    feeds = connection.execute(query + " ORDER BY id", params).fetchall()
    stats = {"attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0}
    if not feeds:
        return stats

    options = config.section("feeds")
    max_workers = 1 if feed_id is not None else min(int(options["max_workers"]), len(feeds))
    per_host = int(options["max_workers_per_host"])
    host_locks: defaultdict[str, threading.BoundedSemaphore] = defaultdict(
        lambda: threading.BoundedSemaphore(per_host)
    )

    def worker(identifier: int, url: str) -> int:
        hostname = (urlparse(url).hostname or "").casefold()
        with host_locks[hostname], connect(config.database_path) as worker_connection:
            if cancel_requested and cancel_requested(worker_connection):
                raise RefreshCancelled()
            row = worker_connection.execute("SELECT * FROM feeds WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return 0
            return refresh_feed(worker_connection, config, row, force)

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
            except RefreshCancelled:
                stats["cancelled"] = True
            except Exception:
                stats["failed"] += 1
    return stats
