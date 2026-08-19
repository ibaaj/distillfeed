from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .presentation import render_plain_text, render_summary_markdown


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PRESETS = {"best-unread", "catch-up", "today", "awaiting-ai", "starred", "everything", "custom"}
_READ_STATES = {"all", "unread", "read"}
_SAVED_STATES = {"all", "starred", "read-later"}
_AI_STATES = {"all", "scored", "pending", "not-sent"}
_DECISIONS = {"all", "keep", "drop"}
_SORTS = {"ai", "date", "local"}
_PAGE_SIZES = {10, 25, 50}


@dataclass(frozen=True)
class ReviewScope:
    kind: str
    scope_id: int
    title: str
    group_ids: tuple[int, ...]
    feed_ids: tuple[int, ...]
    is_arxiv: bool
    preference_group_id: int
    review_display_mode: str


def default_review_preset(scope: ReviewScope) -> str:
    """Return the no-query preset for this review scope.

    Ordinary RSS/Atom scopes behave like an article inbox and therefore expose
    every stored item by default.  The specialist arXiv workflow keeps its
    relevance-first defaults.
    """
    if not scope.is_arxiv:
        return "everything"
    return "catch-up" if scope.review_display_mode == "direct" else "best-unread"


def default_review_sort(scope: ReviewScope) -> str:
    """Use chronological ordering for ordinary feeds and AI ordering for arXiv."""
    return "ai" if scope.is_arxiv else "date"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def resolve_review_scope(
    connection: sqlite3.Connection,
    *,
    group_id: int | None = None,
    feed_id: int | None = None,
) -> ReviewScope:
    if (group_id is None) == (feed_id is None):
        raise ValueError("Choose exactly one group or feed review scope")
    if feed_id is not None:
        row = connection.execute(
            """SELECT f.id,f.group_id,f.title,f.xml_url,g.review_display_mode
                 FROM feeds f JOIN groups g ON g.id=f.group_id
                 WHERE f.id=? AND f.enabled=1""",
            (int(feed_id),),
        ).fetchone()
        if not row:
            raise LookupError("Feed not found")
        return ReviewScope(
            kind="feed", scope_id=int(row["id"]), title=str(row["title"]),
            group_ids=(int(row["group_id"]),), feed_ids=(int(row["id"]),),
            is_arxiv=str(row["xml_url"] or "").startswith("plugin://arxiv/"),
            preference_group_id=int(row["group_id"]),
            review_display_mode=(
                str(row["review_display_mode"] or "daily")
                if str(row["review_display_mode"] or "daily") in {"daily", "direct"}
                else "daily"
            ),
        )

    group = connection.execute(
        "SELECT id,title,review_display_mode FROM groups WHERE id=?", (int(group_id),)
    ).fetchone()
    if not group:
        raise LookupError("Group not found")
    descendants = connection.execute(
        """WITH RECURSIVE descendants(id) AS (
               SELECT ? UNION ALL
               SELECT g.id FROM groups g JOIN descendants d ON g.parent_id=d.id
           ) SELECT id FROM descendants ORDER BY id""",
        (int(group_id),),
    ).fetchall()
    group_ids = tuple(int(row["id"]) for row in descendants)
    marks = ",".join("?" for _ in group_ids)
    feeds = connection.execute(
        f"""SELECT id,xml_url FROM feeds
              WHERE enabled=1 AND group_id IN ({marks}) ORDER BY id""",
        group_ids,
    ).fetchall() if group_ids else []
    feed_ids = tuple(int(row["id"]) for row in feeds)
    is_arxiv = bool(feeds) and all(
        str(row["xml_url"] or "").startswith("plugin://arxiv/") for row in feeds
    )
    return ReviewScope(
        kind="group", scope_id=int(group["id"]), title=str(group["title"]),
        group_ids=group_ids, feed_ids=feed_ids, is_arxiv=is_arxiv,
        preference_group_id=int(group["id"]),
        review_display_mode=(
            str(group["review_display_mode"] or "daily")
            if str(group["review_display_mode"] or "daily") in {"daily", "direct"}
            else "daily"
        ),
    )


def _integer(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _valid_day(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _DAY.fullmatch(text):
        raise ValueError("Dates must use YYYY-MM-DD")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Dates must be valid calendar dates") from exc
    return text


def preset_defaults(
    preset: str, *, default_min_ai: int = 70, today: str | None = None,
    default_sort: str = "ai",
) -> dict[str, Any]:
    day = today or datetime.now(UTC).date().isoformat()
    base: dict[str, Any] = {
        "read": "all", "saved": "all", "ai": "all", "decision": "all",
        "min_ai": 0, "source": 0, "from": "", "to": "",
        "sort": default_sort if default_sort in _SORTS else "ai",
    }
    if preset == "best-unread":
        base.update(read="unread", ai="scored", decision="keep", min_ai=default_min_ai, sort="ai")
    elif preset == "catch-up":
        base.update(read="unread")
    elif preset == "today":
        base.update(**{"from": day, "to": day})
    elif preset == "awaiting-ai":
        base.update(ai="pending", sort="local")
    elif preset == "starred":
        base.update(saved="starred")
    return base


def parse_review_filters(
    values: Mapping[str, Any], *, default_min_ai: int = 70, today: str | None = None,
    default_preset: str = "best-unread", default_sort: str = "ai",
) -> dict[str, Any]:
    fallback_preset = default_preset if default_preset in _PRESETS else "best-unread"
    fallback_sort = default_sort if default_sort in _SORTS else "ai"
    preset = _choice(values.get("preset"), _PRESETS, fallback_preset)
    defaults = preset_defaults(
        preset, default_min_ai=default_min_ai, today=today, default_sort=fallback_sort,
    )
    page_size = _integer(values.get("page_size"), 25, minimum=10, maximum=50)
    if page_size not in _PAGE_SIZES:
        page_size = 25
    query = str(values.get("q") or "").strip()[:200]
    source = _integer(values.get("source"), int(defaults["source"]), minimum=0, maximum=2_147_483_647)
    result = {
        "preset": preset,
        "q": query,
        "read": _choice(values.get("read"), _READ_STATES, str(defaults["read"])),
        "saved": _choice(values.get("saved"), _SAVED_STATES, str(defaults["saved"])),
        "ai": _choice(values.get("ai"), _AI_STATES, str(defaults["ai"])),
        "decision": _choice(values.get("decision"), _DECISIONS, str(defaults["decision"])),
        "min_ai": _integer(values.get("min_ai"), int(defaults["min_ai"]), minimum=0, maximum=100),
        "source": source,
        "from": _valid_day(values.get("from", defaults["from"])),
        "to": _valid_day(values.get("to", defaults["to"])),
        "sort": _choice(values.get("sort"), _SORTS, str(defaults["sort"])),
        "page_size": page_size,
    }
    if result["ai"] in {"pending", "not-sent"}:
        # AI thresholds and keep/drop decisions have no meaning before an AI
        # score exists.  Normalize conflicting bookmarked/manual URLs instead
        # of returning an unexpectedly empty view.
        result["min_ai"] = 0
        result["decision"] = "all"
    if result["from"] and result["to"] and result["from"] > result["to"]:
        raise ValueError("The start date must not be after the end date")
    return result


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def _review_rows(
    connection: sqlite3.Connection, scope: ReviewScope, *, minimum_relevance: int,
) -> list[dict[str, Any]]:
    if not scope.feed_ids:
        return []
    marks = ",".join("?" for _ in scope.feed_ids)
    has_arxiv = _table_exists(connection, "distillfeed_arxiv_papers")
    if has_arxiv:
        arxiv_select = """
            ap.item_id AS arxiv_item_id,ap.arxiv_id,ap.pdf_url,ap.local_score,
            ap.llm_score,ap.final_score,ap.decision AS arxiv_decision,
            ap.why AS arxiv_why,ap.tags_json AS arxiv_tags_json,
            ap.local_reasons_json,ap.evaluation_status
        """
        arxiv_join = "LEFT JOIN distillfeed_arxiv_papers ap ON ap.item_id=i.id"
    else:
        arxiv_select = """
            NULL AS arxiv_item_id,NULL AS arxiv_id,NULL AS pdf_url,NULL AS local_score,
            NULL AS llm_score,NULL AS final_score,NULL AS arxiv_decision,
            NULL AS arxiv_why,NULL AS arxiv_tags_json,NULL AS local_reasons_json,
            NULL AS evaluation_status
        """
        arxiv_join = ""
    rows = connection.execute(
        f"""SELECT i.id,i.feed_id,i.title,i.url,i.author,i.published_at,i.discovered_at,
                    i.description_text,i.summary_eligible,i.is_read,i.is_starred,i.is_read_later,
                    f.title AS feed_title,f.group_id,f.xml_url,f.llm_enabled,
                    eval.relevance AS ordinary_ai_score,eval.description AS ordinary_ai_summary,
                    eval.justification AS ordinary_ai_why,eval.story_cluster,
                    legacy.importance AS legacy_ai_score,legacy.description AS legacy_ai_summary,
                    legacy.justification AS legacy_ai_why,legacy.story_cluster AS legacy_story_cluster,
                    COALESCE((SELECT GROUP_CONCAT(t.name, ' · ') FROM item_tags it
                              JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id),'') AS user_tags,
                    {arxiv_select}
               FROM items i JOIN feeds f ON f.id=i.feed_id
               LEFT JOIN ai_evaluations eval ON eval.item_id=i.id AND eval.current=1
               LEFT JOIN summary_items legacy ON legacy.rowid=(
                   SELECT candidate.rowid FROM summary_items candidate
                   JOIN summaries candidate_summary ON candidate_summary.id=candidate.summary_id
                   JOIN llm_runs candidate_run ON candidate_run.id=candidate_summary.llm_run_id
                   WHERE candidate.item_id=i.id AND candidate.included=1
                     AND candidate_run.status='success'
                   ORDER BY candidate_run.id DESC LIMIT 1
               )
               {arxiv_join}
              WHERE f.enabled=1 AND i.feed_id IN ({marks})""",
        scope.feed_ids,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        is_arxiv = row["arxiv_item_id"] is not None or str(row["xml_url"] or "").startswith("plugin://arxiv/")
        arxiv_score = row["llm_score"]
        ordinary_score = row["ordinary_ai_score"]
        legacy_score = row["legacy_ai_score"]
        ai_score = (
            int(arxiv_score) if arxiv_score is not None
            else int(ordinary_score) if ordinary_score is not None
            else int(legacy_score) if legacy_score is not None
            else None
        )
        if ai_score is not None:
            ai_state = "scored"
        elif is_arxiv and str(row["evaluation_status"] or "") == "pending":
            ai_state = "pending"
        elif is_arxiv and str(row["evaluation_status"] or "") == "screened_out":
            ai_state = "not-sent"
        elif not is_arxiv and bool(row["llm_enabled"]) and bool(row["summary_eligible"]):
            ai_state = "pending"
        else:
            ai_state = "not-sent"
        decision = ""
        if ai_state == "scored":
            stored_decision = str(row["arxiv_decision"] or "").strip().lower() if is_arxiv else ""
            decision = stored_decision if stored_decision in {"keep", "drop"} else (
                "keep" if int(ai_score) >= minimum_relevance else "drop"
            )
        stamp = str(row["published_at"] or row["discovered_at"] or "")
        day = stamp[:10] if _DAY.fullmatch(stamp[:10]) else "undated"
        arxiv_tags = _json_list(row["arxiv_tags_json"])
        user_tags = [part.strip() for part in str(row["user_tags"] or "").split(" · ") if part.strip()]
        tags = list(dict.fromkeys([*arxiv_tags, *user_tags]))
        why = str(row["arxiv_why"] or row["ordinary_ai_why"] or row["legacy_ai_why"] or "").strip()
        ai_summary = str(row["ordinary_ai_summary"] or row["legacy_ai_summary"] or "").strip()
        story_cluster = str(row["story_cluster"] or row["legacy_story_cluster"] or "")
        search_text = " ".join([
            str(row["title"] or ""), str(row["author"] or ""), str(row["feed_title"] or ""),
            " ".join(tags), why, ai_summary, str(row["description_text"] or ""),
            str(row["arxiv_id"] or ""),
        ]).casefold()
        result.append({
            "id": int(row["id"]), "feed_id": int(row["feed_id"]), "feed_title": str(row["feed_title"]),
            "title": str(row["title"]), "url": str(row["url"] or ""), "author": str(row["author"] or ""),
            "published_at": stamp, "day": day, "description_text": str(row["description_text"] or ""),
            "is_read": bool(row["is_read"]), "is_starred": bool(row["is_starred"]),
            "is_read_later": bool(row["is_read_later"]), "tags": tags,
            "ai_state": ai_state, "ai_score": ai_score, "decision": decision,
            "local_score": int(row["local_score"]) if row["local_score"] is not None else None,
            "why": why, "ai_summary": ai_summary, "story_cluster": story_cluster,
            "is_arxiv": is_arxiv, "arxiv_id": str(row["arxiv_id"] or ""),
            "pdf_url": str(row["pdf_url"] or ""), "local_reasons": _json_list(row["local_reasons_json"]),
            "search_text": search_text,
        })
    return result


def _matches(item: dict[str, Any], filters: Mapping[str, Any]) -> bool:
    query = str(filters["q"] or "").casefold()
    if query and query not in item["search_text"]:
        return False
    read_state = filters["read"]
    if read_state == "unread" and item["is_read"]:
        return False
    if read_state == "read" and not item["is_read"]:
        return False
    saved = filters["saved"]
    if saved == "starred" and not item["is_starred"]:
        return False
    if saved == "read-later" and not item["is_read_later"]:
        return False
    if filters["ai"] != "all" and item["ai_state"] != filters["ai"]:
        return False
    if filters["decision"] != "all" and item["decision"] != filters["decision"]:
        return False
    if int(filters["source"] or 0) and item["feed_id"] != int(filters["source"]):
        return False
    minimum = int(filters["min_ai"] or 0)
    if minimum and (item["ai_score"] is None or int(item["ai_score"]) < minimum):
        return False
    if (filters["from"] or filters["to"]) and item["day"] == "undated":
        return False
    if filters["from"] and item["day"] < filters["from"]:
        return False
    if filters["to"] and item["day"] > filters["to"]:
        return False
    return True


def _timestamp_value(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0


def _cohort(item: Mapping[str, Any]) -> int:
    if item["ai_state"] == "scored" and item["decision"] == "keep":
        return 0
    if item["ai_state"] == "scored" and item["decision"] == "drop":
        return 1
    if item["ai_state"] == "scored":
        return 2
    if item["ai_state"] == "pending":
        return 3
    if item["ai_state"] == "not-sent":
        return 4
    return 5


def _sort_key(item: Mapping[str, Any], mode: str) -> tuple[int, ...]:
    score = int(item["ai_score"]) if item["ai_score"] is not None else -1
    local = int(item["local_score"]) if item["local_score"] is not None else -1
    stamp = _timestamp_value(str(item["published_at"] or ""))
    identifier = int(item["id"])
    if mode == "date":
        return (-stamp, -identifier)
    if mode == "local":
        return (-local, _cohort(item), -score, -stamp, -identifier)
    return (_cohort(item), -score, -local, -stamp, -identifier)


def _filters_signature(scope: ReviewScope, day: str, filters: Mapping[str, Any]) -> str:
    payload = {
        "scope": [scope.kind, scope.scope_id], "day": day,
        "filters": {key: filters[key] for key in sorted(filters) if key != "page_size"},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _encode_cursor(signature: str, key: tuple[int, ...]) -> str:
    payload = json.dumps({"v": 1, "sig": signature, "key": list(key)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str, signature: str) -> tuple[int, ...]:
    if not value or len(value) > 2048:
        raise ValueError("Invalid review cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("v") != 1 or payload.get("sig") != signature:
            raise ValueError
        key = payload.get("key")
        if not isinstance(key, list) or not all(isinstance(part, int) for part in key):
            raise ValueError
        return tuple(key)
    except Exception as exc:
        raise ValueError("The review cursor does not match the current filters") from exc


def _daily_briefs(connection: sqlite3.Connection, scope: ReviewScope) -> dict[str, dict[str, Any]]:
    if not scope.group_ids:
        return {}
    group_marks = ",".join("?" for _ in scope.group_ids)
    parameters: list[Any] = list(scope.group_ids)
    where = f"s.group_id IN ({group_marks})"
    if scope.kind == "feed":
        where = f"({where} OR s.scope_feed_id=?)"
        parameters.append(scope.scope_id)
    rows = connection.execute(
        f"""SELECT s.id,s.overview,s.sections_json,lr.model,lr.completed_at,
                    SUM(CASE WHEN si.included=1 THEN 1 ELSE 0 END) AS selected_count,
                    MIN(substr(COALESCE(i.published_at,i.discovered_at),1,10)) AS first_day,
                    MAX(substr(COALESCE(i.published_at,i.discovered_at),1,10)) AS last_day
               FROM summaries s JOIN llm_runs lr ON lr.id=s.llm_run_id
               JOIN summary_items si ON si.summary_id=s.id
               JOIN items i ON i.id=si.item_id
              WHERE lr.status='success' AND {where}
              GROUP BY s.id
             HAVING first_day=last_day
              ORDER BY lr.id DESC""",
        parameters,
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = str(row["first_day"] or "")
        if day in result or not _DAY.fullmatch(day):
            continue
        sections: list[dict[str, Any]] = []
        try:
            parsed = json.loads(str(row["sections_json"] or "[]"))
            if isinstance(parsed, list):
                sections = [section for section in parsed if isinstance(section, dict)]
        except (ValueError, json.JSONDecodeError):
            pass
        fragments: list[str] = []
        if str(row["overview"] or "").strip():
            fragments.append(str(row["overview"]).strip())
        for section in sections:
            heading = str(section.get("heading") or "").strip()
            body = str(section.get("body") or "").strip()
            if heading:
                fragments.append(f"## {heading}\n\n{body}" if body else f"## {heading}")
            elif body:
                fragments.append(body)
        result[day] = {
            "summary_id": int(row["id"]), "selected_count": int(row["selected_count"] or 0),
            "model": str(row["model"] or ""), "completed_at": str(row["completed_at"] or ""),
            "html": str(render_summary_markdown("\n\n".join(fragments))),
        }
    return result


def list_review_days(
    connection: sqlite3.Connection,
    scope: ReviewScope,
    filters: Mapping[str, Any],
    *,
    minimum_relevance: int,
) -> dict[str, Any]:
    all_items = _review_rows(connection, scope, minimum_relevance=minimum_relevance)
    source_ids = {item["feed_id"] for item in all_items}
    if int(filters["source"] or 0) and int(filters["source"]) not in source_ids:
        raise ValueError("The selected source is outside this review scope")
    filtered = [item for item in all_items if _matches(item, filters)]
    all_by_day: dict[str, list[dict[str, Any]]] = {}
    filtered_by_day: dict[str, list[dict[str, Any]]] = {}
    for item in all_items:
        all_by_day.setdefault(item["day"], []).append(item)
    for item in filtered:
        filtered_by_day.setdefault(item["day"], []).append(item)
    briefs = _daily_briefs(connection, scope)
    days: list[dict[str, Any]] = []
    for day in sorted(filtered_by_day, reverse=True):
        matching = filtered_by_day[day]
        complete_set = all_by_day.get(day, matching)
        days.append({
            "day": day,
            "total": len(complete_set), "matching": len(matching),
            "unread": sum(not item["is_read"] for item in complete_set),
            "scored": sum(item["ai_state"] == "scored" for item in complete_set),
            "pending": sum(item["ai_state"] == "pending" for item in complete_set),
            "not_sent": sum(item["ai_state"] == "not-sent" for item in complete_set),
            "kept": sum(item["decision"] == "keep" for item in complete_set),
            "complete": not any(not item["is_read"] for item in complete_set),
            "brief": briefs.get(day),
        })
    sources: list[dict[str, Any]] = []
    by_source: dict[int, dict[str, Any]] = {}
    for item in all_items:
        source = by_source.setdefault(item["feed_id"], {
            "id": item["feed_id"], "title": item["feed_title"], "total": 0,
        })
        source["total"] += 1
    sources = sorted(by_source.values(), key=lambda item: (str(item["title"]).casefold(), int(item["id"])))
    counts = {
        "total": len(all_items), "matching": len(filtered),
        "unread": sum(not item["is_read"] for item in all_items),
        "scored": sum(item["ai_state"] == "scored" for item in all_items),
        "pending": sum(item["ai_state"] == "pending" for item in all_items),
        "not_sent": sum(item["ai_state"] == "not-sent" for item in all_items),
        "starred": sum(item["is_starred"] for item in all_items),
    }
    default_open = next((day["day"] for day in days if not day["complete"]), days[0]["day"] if days else "")
    return {
        "scope": {
            "kind": scope.kind, "id": scope.scope_id, "title": scope.title,
            "is_arxiv": scope.is_arxiv,
            "preference_group_id": scope.preference_group_id,
            "review_display_mode": scope.review_display_mode,
            "default_preset": default_review_preset(scope),
            "default_sort": default_review_sort(scope),
        },
        "filters": dict(filters), "counts": counts, "sources": sources,
        "days": days, "default_open_day": default_open,
    }


def list_review_day_items(
    connection: sqlite3.Connection,
    scope: ReviewScope,
    day: str,
    filters: Mapping[str, Any],
    *,
    minimum_relevance: int,
    cursor: str = "",
) -> dict[str, Any]:
    if day != "undated":
        _valid_day(day)
    items = [
        item for item in _review_rows(connection, scope, minimum_relevance=minimum_relevance)
        if item["day"] == day and _matches(item, filters)
    ]
    mode = str(filters["sort"])
    items.sort(key=lambda item: _sort_key(item, mode))
    signature = _filters_signature(scope, day, filters)
    start = 0
    if cursor:
        key = _decode_cursor(cursor, signature)
        while start < len(items) and _sort_key(items[start], mode) <= key:
            start += 1
    page_size = int(filters["page_size"])
    selected = items[start:start + page_size]
    has_more = start + page_size < len(items)
    next_cursor = _encode_cursor(signature, _sort_key(selected[-1], mode)) if selected and has_more else ""
    compact: list[dict[str, Any]] = []
    for item in selected:
        compact.append({
            "id": item["id"], "feed_id": item["feed_id"], "feed_title": item["feed_title"],
            "title": item["title"], "url": item["url"], "author": item["author"],
            "published_at": item["published_at"], "day": item["day"],
            "is_read": item["is_read"], "is_starred": item["is_starred"],
            "is_read_later": item["is_read_later"], "tags": item["tags"],
            "ai_state": item["ai_state"], "ai_score": item["ai_score"],
            "decision": item["decision"], "local_score": item["local_score"],
            "why": item["why"][:280], "is_arxiv": item["is_arxiv"],
            "arxiv_id": item["arxiv_id"], "pdf_url": item["pdf_url"],
            "cohort": _cohort(item),
        })
    return {
        "day": day, "items": compact, "total": len(items),
        "next_cursor": next_cursor, "has_more": has_more,
    }


def _normalised_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _duplicates_source(summary: Any, source: Any) -> bool:
    summary_text = _normalised_text(summary)
    source_text = _normalised_text(source)
    if not summary_text or not source_text:
        return False
    if summary_text == source_text:
        return True
    # Older daily-summary rows sometimes contain a truncated abstract rather
    # than an independently generated item synopsis.  Treat a substantial
    # containment match as source duplication, but retain genuinely concise
    # AI summaries that merely reuse a short phrase from the paper.
    return min(len(summary_text), len(source_text)) >= 160 and (
        summary_text in source_text or source_text in summary_text
    )


def review_item_details(
    connection: sqlite3.Connection,
    scope: ReviewScope,
    item_id: int,
    *,
    minimum_relevance: int,
) -> dict[str, Any] | None:
    item = next((entry for entry in _review_rows(
        connection, scope, minimum_relevance=minimum_relevance,
    ) if int(entry["id"]) == int(item_id)), None)
    if item is None:
        return None
    stored = connection.execute(
        """SELECT si.description,si.justification
             FROM summary_items si JOIN summaries s ON s.id=si.summary_id
             JOIN llm_runs lr ON lr.id=s.llm_run_id
            WHERE si.item_id=? AND lr.status='success'
            ORDER BY lr.id DESC LIMIT 1""",
        (int(item_id),),
    ).fetchone()
    summary_text = item["ai_summary"] or (str(stored["description"] or "") if stored else "")
    source_text = item["description_text"]
    # Older arXiv releases stored the abstract verbatim in summary_items.  It is
    # source text, not an item-level AI summary, so suppress the duplicate block.
    if _duplicates_source(summary_text, source_text):
        summary_text = ""
    rationale = item["why"] or (str(stored["justification"] or "") if stored else "")
    local_reasons = item["local_reasons"]
    local_html = ""
    if local_reasons:
        local_html = "<ul>" + "".join(f"<li>{html.escape(reason)}</li>" for reason in local_reasons) + "</ul>"
    return {
        "id": item["id"], "title": item["title"],
        "summary_html": str(render_summary_markdown(summary_text)) if summary_text else "",
        "rationale_html": str(render_summary_markdown(rationale)) if rationale else "",
        "source_html": str(render_plain_text(source_text)),
        "source_label": "Paper abstract" if item["is_arxiv"] else "Source description",
        "local_rationale_html": local_html,
        "url": item["url"], "pdf_url": item["pdf_url"],
        "is_arxiv": item["is_arxiv"], "tags": item["tags"],
    }


def finish_review_day(
    connection: sqlite3.Connection,
    scope: ReviewScope,
    day: str,
) -> dict[str, Any]:
    if day != "undated":
        _valid_day(day)
    if not scope.feed_ids:
        return {"status": "ok", "day": day, "matched": 0, "changed": 0, "unread": 0}
    marks = ",".join("?" for _ in scope.feed_ids)
    day_expression = "substr(COALESCE(published_at,discovered_at),1,10)=?" if day != "undated" else "substr(COALESCE(published_at,discovered_at),1,10) NOT GLOB '????-??-??'"
    day_params: list[Any] = [day] if day != "undated" else []
    matched = int(connection.execute(
        f"SELECT COUNT(*) FROM items WHERE feed_id IN ({marks}) AND {day_expression}",
        [*scope.feed_ids, *day_params],
    ).fetchone()[0])
    cursor = connection.execute(
        f"""UPDATE items SET is_read=1
              WHERE is_read=0 AND feed_id IN ({marks}) AND {day_expression}""",
        [*scope.feed_ids, *day_params],
    )
    unread = int(connection.execute(
        f"""SELECT COUNT(*) FROM items
              WHERE is_read=0 AND feed_id IN ({marks}) AND {day_expression}""",
        [*scope.feed_ids, *day_params],
    ).fetchone()[0])
    return {
        "status": "ok", "day": day, "matched": matched,
        "changed": max(int(cursor.rowcount), 0), "unread": unread,
    }
