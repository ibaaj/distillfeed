from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .presentation import render_plain_text, render_summary_markdown
from .review_sql import query_context, filter_sql, order_keys, item_dict, COMPACT
from distillfeed_arxiv.dates import canonical_day_sql


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PRESETS = {"current-week", "current-month", "last-5-days", "best", "best-unread", "best-read", "today", "awaiting-ai", "starred", "everything", "custom"}
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


def default_review_preset(
    scope: ReviewScope, connection: sqlite3.Connection | None = None, *,
    preference: str = "adaptive", today: str | None = None,
) -> str:
    """Choose a nonempty time window for the scope, without hiding read items.

    Explicit URL filters always override this initial/reset default. Only source
    dates and feed identifiers are selected: no descriptions or AI joins are loaded.
    The same canonical UTC day policy is used as by review_sql.
    """
    if preference in _PRESETS and preference != "custom":
        return preference
    if connection is None:
        return "current-week"  # Compatibility for independent callers.
    if not scope.feed_ids:
        return "everything"
    day = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    marks = ",".join("?" for _ in scope.feed_ids)
    if _table_exists(connection, "distillfeed_arxiv_papers"):
        day_sql = canonical_day_sql(connection)
        join = "LEFT JOIN distillfeed_arxiv_papers ap ON ap.item_id=i.id"
    else:
        day_sql = "COALESCE(date(i.published_at),date(i.discovered_at),'undated')"
        join = ""
    future = "(f.xml_url NOT LIKE 'plugin://arxiv/%' AND julianday(i.published_at)>julianday(i.discovered_at)+2.0/24)"
    display = f"CASE WHEN {future} THEN i.discovered_at ELSE i.published_at END"
    day_sql = day_sql.replace("date(i.published_at)", f"date({display})")
    row = connection.execute(
        f"""SELECT MAX(day) FROM (
            SELECT {day_sql} AS day FROM items i JOIN feeds f ON f.id=i.feed_id
            {join} WHERE f.enabled=1 AND i.feed_id IN ({marks})
        ) WHERE day<>'undated' AND day<=?""", [*scope.feed_ids, day.isoformat()],
    ).fetchone()
    latest = str(row[0] or "")
    if latest >= (day - timedelta(days=day.weekday())).isoformat():
        return "current-week"
    if latest >= day.replace(day=1).isoformat():
        return "current-month"
    return "everything"


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


def preset_defaults(preset: str, *, default_min_ai: int = 70, today: str | None = None,
                    default_sort: str = "ai") -> dict[str, Any]:
    day = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    base: dict[str, Any] = {
        "read":"all", "saved":"all", "ai":"all", "decision":"all", "min_ai":0,
        "source":0,"from":"","to":"","sort":default_sort if default_sort in _SORTS else "ai",
    }
    if preset in {"best", "best-unread", "best-read"}:
        base.update(ai="scored",decision="keep",min_ai=default_min_ai,sort="ai")
        if preset != "best": base["read"] = "unread" if preset == "best-unread" else "read"
    elif preset == "current-week":
        base.update({"from":(day-timedelta(days=day.weekday())).isoformat(),"to":day.isoformat()})
    elif preset == "current-month":
        base.update({"from": day.replace(day=1).isoformat(), "to": day.isoformat()})
    elif preset in {"last-5-days", "catch-up"}:
        base.update({"from":(day-timedelta(days=4)).isoformat(),"to":day.isoformat()})
    elif preset == "today": base.update({"from":day.isoformat(),"to":day.isoformat()})
    elif preset == "awaiting-ai": base.update(ai="pending",sort="local")
    elif preset == "starred": base.update(saved="starred")
    return base


def parse_review_filters(
    values: Mapping[str, Any], *, default_min_ai: int = 70, today: str | None = None,
    default_preset: str = "best-unread", default_sort: str = "ai", default_page_size: int = 25,
) -> dict[str, Any]:
    fallback_preset = default_preset if default_preset in _PRESETS else "best-unread"
    fallback_sort = default_sort if default_sort in _SORTS else "ai"
    if values.get("preset") == "catch-up":
        # Upgrade old bookmarks without retaining their invisible unread-only filter.
        values = {k:v for k,v in values.items() if k not in {"read","from","to"}}
        values["preset"] = "last-5-days"
    preset = _choice(values.get("preset"), _PRESETS, fallback_preset)
    defaults = preset_defaults(
        preset, default_min_ai=default_min_ai, today=today, default_sort=fallback_sort,
    )
    page_size = _integer(values.get("page_size"), default_page_size, minimum=10, maximum=50)
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


def _review_rows(connection: sqlite3.Connection, scope: ReviewScope, *, minimum_relevance: int) -> list[dict[str, Any]]:
    # Compatibility helper for callers needing complete records. UI endpoints use
    # bounded SQL below and never call this all-history method.
    cte, params = query_context(connection, scope, minimum_relevance)
    rows = [item_dict(row) for row in connection.execute(cte+" SELECT * FROM r",params)]
    for row in rows:
        row["search_text"] = " ".join(str(row.get(k) or "") for k in
            ("title","author","feed_title","why","ai_summary","description_text","arxiv_id")).casefold()+" "+" ".join(row["tags"]).casefold()
    return rows


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


def _daily_briefs(connection: sqlite3.Connection, scope: ReviewScope, *, days: set[str] | None = None) -> dict[str, dict[str, Any]]:
    if not scope.group_ids:
        return {}
    group_marks = ",".join("?" for _ in scope.group_ids)
    parameters: list[Any] = list(scope.group_ids)
    where = f"s.group_id IN ({group_marks})"
    if scope.kind == "feed":
        where = f"({where} OR s.scope_feed_id=?)"
        parameters.append(scope.scope_id)
    has_arxiv = _table_exists(connection, "distillfeed_arxiv_papers")
    join = "LEFT JOIN distillfeed_arxiv_papers ap ON ap.item_id=i.id" if has_arxiv else ""
    day_expr = canonical_day_sql(connection) if has_arxiv else "COALESCE(date(i.published_at),date(i.discovered_at),'undated')"
    having = "first_day=last_day"
    if days is not None:
        if not days: return {}
        having += " AND first_day IN ("+",".join("?" for _ in days)+")"
        parameters.extend(sorted(days))
    rows = connection.execute(
        f"""SELECT s.id,s.overview,s.sections_json,lr.model,lr.completed_at,
                SUM(CASE WHEN si.included=1 THEN 1 ELSE 0 END) AS selected_count,
                MIN({day_expr}) AS first_day,MAX({day_expr}) AS last_day
            FROM summaries s JOIN llm_runs lr ON lr.id=s.llm_run_id
            JOIN summary_items si ON si.summary_id=s.id JOIN items i ON i.id=si.item_id
            {join} WHERE lr.status='success' AND {where}
            GROUP BY s.id HAVING {having} ORDER BY lr.id DESC""",parameters,
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


def list_review_days(connection: sqlite3.Connection, scope: ReviewScope, filters: Mapping[str, Any],
                     *, minimum_relevance: int, before: str = "", limit: int = 60) -> dict[str, Any]:
    if filters["source"] and int(filters["source"]) not in scope.feed_ids:
        raise ValueError("The selected source is outside this review scope")
    if before and before != "undated": _valid_day(before)
    limit = max(1,min(60,int(limit)))
    cte, params = query_context(connection,scope,minimum_relevance)
    match, args = filter_sql(filters)
    counts = dict(connection.execute(cte+f""" SELECT COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN {match} THEN 1 ELSE 0 END),0) AS matching,
        COALESCE(SUM(is_read=0),0) AS unread,COALESCE(SUM(ai_state='scored'),0) AS scored,
        COALESCE(SUM(ai_state='pending'),0) AS pending,COALESCE(SUM(ai_state='not-sent'),0) AS not_sent,
        COALESCE(SUM(is_starred),0) AS starred FROM r""",[*params,*args]).fetchone())
    boundary = ""
    boundary_args = []
    if before == "undated": boundary = "AND 0"
    elif before: boundary="AND (day<? OR day='undated')"; boundary_args=[before]
    rows = connection.execute(cte+f""" SELECT day,COUNT(*) AS total,
        SUM(CASE WHEN {match} THEN 1 ELSE 0 END) AS matching,
        SUM(is_read=0) AS unread,SUM(ai_state='scored') AS scored,
        SUM(ai_state='pending') AS pending,SUM(ai_state='not-sent') AS not_sent,
        SUM(decision='keep') AS kept
        FROM r WHERE 1 {boundary} GROUP BY day HAVING matching>0
        ORDER BY (day='undated') ASC,day DESC LIMIT ?""",[*params,*args,*boundary_args,limit+1]).fetchall()
    has_more=len(rows)>limit
    days=[dict(row) for row in rows[:limit]]
    briefs=_daily_briefs(connection,scope,days={day['day'] for day in days})
    for day in days:
        day.update(complete=not day['unread'],brief=briefs.get(day['day']))
    sources=[dict(row) for row in connection.execute(cte+" SELECT feed_id AS id,feed_title AS title,COUNT(*) AS total FROM r GROUP BY feed_id ORDER BY feed_title COLLATE NOCASE,feed_id",params)]
    default_open=next((day['day'] for day in days if day['day']!='undated'),"")
    return {"scope":{"kind":scope.kind,"id":scope.scope_id,"title":scope.title,"is_arxiv":scope.is_arxiv,
              "preference_group_id":scope.preference_group_id,"review_display_mode":scope.review_display_mode,
              "default_preset":default_review_preset(scope),"default_sort":default_review_sort(scope)},
            "filters":dict(filters),"counts":counts,"sources":sources,"days":days,
            "default_open_day":default_open,"has_more_days":has_more,
            "next_day_cursor":days[-1]['day'] if has_more and days else ""}


def list_review_day_items(connection: sqlite3.Connection, scope: ReviewScope, day: str,
                          filters: Mapping[str, Any], *, minimum_relevance: int,
                          cursor: str = "") -> dict[str, Any]:
    if day != "undated": _valid_day(day)
    if filters["source"] and int(filters["source"]) not in scope.feed_ids:
        raise ValueError("The selected source is outside this review scope")
    cte,params=query_context(connection,scope,minimum_relevance)
    match,args=filter_sql(filters)
    condition=f"day=? AND ({match})"
    bindings=[*params,day,*args]
    total=int(connection.execute(cte+f" SELECT COUNT(*) FROM r WHERE {condition}",bindings).fetchone()[0])
    keys=order_keys(str(filters['sort']))
    signature=_filters_signature(scope,day,filters)
    if cursor:
        key=_decode_cursor(cursor,signature)
        if len(key)!=len(keys): raise ValueError("Invalid review cursor sort key")
        condition+=f" AND ({','.join(keys)})>({','.join('?' for _ in keys)})"
        bindings.extend(key)
    page_size=int(filters['page_size'])
    rows=connection.execute(cte+f" SELECT {COMPACT} FROM r WHERE {condition} ORDER BY {','.join(keys)} LIMIT ?",[*bindings,page_size+1]).fetchall()
    more=len(rows)>page_size
    items=[item_dict(row) for row in rows[:page_size]]
    def key_for(item):
        score=int(item['ai_score']) if item['ai_score'] is not None else -1
        local=int(item['local_score']) if item['local_score'] is not None else -1
        stamp=int(item['stamp']); identifier=int(item['id'])
        if filters['sort']=='date': return (-stamp,-identifier)
        if filters['sort']=='local': return (-local,item['cohort'],-score,-stamp,-identifier)
        return (item['cohort'],-score,-local,-stamp,-identifier)
    next_cursor=_encode_cursor(signature,key_for(items[-1])) if more and items else ""
    for item in items: item.pop('stamp',None)
    return {'day':day,'items':items,'total':total,'has_more':more,'next_cursor':next_cursor}


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
    cte, params = query_context(connection, scope, minimum_relevance)
    row = connection.execute(cte + " SELECT * FROM r WHERE id=?", [*params, int(item_id)]).fetchone()
    item = item_dict(row) if row else None
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
        local_html = "<ul>" + "".join(f"<li>{html.escape(str(reason))}</li>" for reason in local_reasons) + "</ul>"
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


def finish_review_day(connection: sqlite3.Connection, scope: ReviewScope, day: str) -> dict[str, Any]:
    if day != "undated": _valid_day(day)
    cte,params=query_context(connection,scope,0)
    matched=int(connection.execute(cte+" SELECT COUNT(*) FROM r WHERE day=?",[*params,day]).fetchone()[0])
    connection.execute(cte+" UPDATE items SET is_read=1 WHERE is_read=0 AND id IN (SELECT id FROM r WHERE day=?)",[*params,day])
    changed=int(connection.execute('SELECT changes()').fetchone()[0])
    return {'status':'ok','day':day,'matched':matched,'changed':changed,'unread':0}
