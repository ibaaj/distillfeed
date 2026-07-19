from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from .ai_policy import effective_feed_mode, effective_group_modes
from .db import group_descendant_ids, utcnow


ACTIVE_STATES = ("waiting", "retry")


def sync_review_queue(connection) -> None:
    """Reconcile legacy eligibility with the durable evaluation queue."""
    now = utcnow()
    connection.execute(
        """INSERT OR IGNORE INTO ai_review_queue(
               item_id,state,queued_at,updated_at,available_at
           )
           SELECT i.id,
                  CASE WHEN i.summary_eligible=1 THEN 'waiting' ELSE 'archived' END,
                  i.discovered_at, ?, NULL
           FROM items i""",
        (now,),
    )
    connection.execute(
        """UPDATE ai_review_queue SET state='reviewed',updated_at=?,available_at=NULL,
                  claimed_run_id=NULL,last_error=NULL
           WHERE state<>'reviewed' AND EXISTS (
             SELECT 1 FROM ai_evaluations evaluation
             JOIN llm_runs lr ON lr.id=evaluation.llm_run_id
             WHERE evaluation.item_id=ai_review_queue.item_id
               AND evaluation.current=1 AND lr.status='success'
           )""",
        (now,),
    )
    connection.execute(
        """UPDATE ai_review_queue SET state='archived',updated_at=?,available_at=NULL,
                  claimed_run_id=NULL
           WHERE state NOT IN ('reviewed','processing')
             AND EXISTS (SELECT 1 FROM items i WHERE i.id=ai_review_queue.item_id
                         AND i.summary_eligible=0)""",
        (now,),
    )
    connection.execute(
        """UPDATE ai_review_queue SET state='waiting',updated_at=?,available_at=NULL,
                  claimed_run_id=NULL,last_error=NULL
           WHERE state='archived' AND EXISTS (
             SELECT 1 FROM items i WHERE i.id=ai_review_queue.item_id
             AND i.summary_eligible=1
           ) AND NOT EXISTS (
             SELECT 1 FROM ai_evaluations evaluation
             JOIN llm_runs lr ON lr.id=evaluation.llm_run_id
             WHERE evaluation.item_id=ai_review_queue.item_id
               AND evaluation.current=1 AND lr.status='success'
           )""",
        (now,),
    )
    # A process can disappear after claiming work. A stale claim is safe to
    # return to the queue because successful publication is transactional.
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    connection.execute(
        """UPDATE ai_review_queue SET state='retry',updated_at=?,available_at=?,
                  claimed_run_id=NULL,last_error='Previous review was interrupted'
           WHERE state='processing' AND updated_at<?""",
        (now, now, stale),
    )


def mark_processing(connection, item_ids: Iterable[int], run_id: int) -> None:
    identifiers = sorted({int(value) for value in item_ids})
    if not identifiers:
        return
    marks = ",".join("?" for _ in identifiers)
    connection.execute(
        f"""UPDATE ai_review_queue SET state='processing',updated_at=?,available_at=NULL,
                   claimed_run_id=?,attempts=attempts+1,last_error=NULL
            WHERE item_id IN ({marks}) AND state IN ('waiting','retry')
              AND NOT EXISTS (
                SELECT 1 FROM ai_item_preferences preference
                WHERE preference.item_id=ai_review_queue.item_id
                  AND preference.disposition='excluded'
              )""",
        [utcnow(), run_id, *identifiers],
    )


def mark_reviewed(connection, item_ids: Iterable[int], run_id: int) -> None:
    identifiers = sorted({int(value) for value in item_ids})
    if not identifiers:
        return
    marks = ",".join("?" for _ in identifiers)
    connection.execute(
        f"""UPDATE ai_review_queue SET state='reviewed',updated_at=?,available_at=NULL,
                   claimed_run_id=?,last_error=NULL WHERE item_id IN ({marks})""",
        [utcnow(), run_id, *identifiers],
    )


def mark_retry(connection, item_ids: Iterable[int], error: str, *, delay_minutes: int = 15) -> None:
    identifiers = sorted({int(value) for value in item_ids})
    if not identifiers:
        return
    marks = ",".join("?" for _ in identifiers)
    available = (datetime.now(UTC) + timedelta(minutes=max(0, delay_minutes))).isoformat(
        timespec="seconds"
    )
    connection.execute(
        f"""UPDATE ai_review_queue SET state='retry',updated_at=?,available_at=?,
                   claimed_run_id=NULL,last_error=? WHERE item_id IN ({marks})
              AND state='processing'""",
        [utcnow(), available, str(error)[:2000], *identifiers],
    )


def set_item_disposition(connection, item_ids: Iterable[int], disposition: str) -> int:
    if disposition not in {"default", "excluded"}:
        raise ValueError("AI item disposition must be default or excluded")
    identifiers = sorted({int(value) for value in item_ids})
    if not identifiers:
        return 0
    changed = 0
    for item_id in identifiers:
        if not connection.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            continue
        connection.execute(
            """INSERT INTO ai_item_preferences(item_id,disposition,updated_at)
               VALUES(?,?,?) ON CONFLICT(item_id) DO UPDATE SET
               disposition=excluded.disposition,updated_at=excluded.updated_at""",
            (item_id, disposition, utcnow()),
        )
        changed += 1
    return changed


def queue_dashboard(
    connection, *, limit: int = 100, offset: int = 0,
    view: str = "ready", include_inactive: bool = False, query: str = "",
    maximum_age_days: int = 0, group_id: int | None = None, feed_id: int | None = None,
) -> dict[str, Any]:
    sync_review_queue(connection)
    if view not in {
        "ready", "processing", "retry", "on_request", "disabled", "expired",
        "inactive", "excluded", "all",
    }:
        raise ValueError("Unknown AI queue view")
    group_modes = effective_group_modes(connection)
    if group_id is not None and feed_id is not None:
        raise ValueError("Choose either a group or a feed queue scope")
    scope_clause = ""
    scope_parameters: list[int] = []
    if feed_id is not None:
        scope_clause = " AND f.id=?"
        scope_parameters = [int(feed_id)]
    elif group_id is not None:
        descendants = group_descendant_ids(connection, int(group_id))
        marks = ",".join("?" for _ in descendants) or "NULL"
        scope_clause = f" AND f.group_id IN ({marks})"
        scope_parameters = descendants
    rows = connection.execute(
        f"""SELECT q.*,i.title,i.url,i.published_at,i.discovered_at,i.summary_eligible,
                  f.id AS feed_id,f.title AS feed_title,
                  f.llm_enabled AS feed_llm_enabled,f.ai_mode AS feed_ai_mode,
                  g.id AS group_id,g.title AS group_title,g.llm_enabled AS group_llm_enabled,
                  g.ai_priority,g.ai_mode AS group_ai_mode,g.position AS group_position,
                  COALESCE(pref.disposition,'default') AS disposition
           FROM ai_review_queue q JOIN items i ON i.id=q.item_id
           JOIN feeds f ON f.id=i.feed_id JOIN groups g ON g.id=f.group_id
           LEFT JOIN ai_item_preferences pref ON pref.item_id=i.id
           WHERE q.state IN ('waiting','retry','processing'){scope_clause}
           ORDER BY CASE g.ai_priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1
                    WHEN 'low' THEN 2 WHEN 'manual' THEN 3 ELSE 4 END,
                    q.queued_at,i.id""",
        scope_parameters,
    ).fetchall()
    visible: list[dict[str, Any]] = []
    counts = {
        "ready": 0, "processing": 0, "retry": 0, "on_request": 0,
        "disabled": 0, "expired": 0, "inactive": 0, "excluded": 0,
    }
    by_group: dict[int, dict[str, Any]] = {}
    now = datetime.now(UTC)
    search = query.strip().casefold()
    for row in rows:
        item = dict(row)
        mode = effective_feed_mode(row, group_modes)
        state = str(row["state"])
        too_old = False
        if maximum_age_days > 0 and (row["published_at"] or row["discovered_at"]):
            try:
                timestamp = datetime.fromisoformat(row["published_at"] or row["discovered_at"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                too_old = timestamp < now - timedelta(days=maximum_age_days)
            except ValueError:
                pass
        if str(row["disposition"]) == "excluded":
            display_state = "excluded"
            counts["excluded"] += 1
        elif mode != "automatic":
            display_state = "on_request" if mode == "manual" else "disabled"
            counts["inactive"] += 1
            counts[display_state] += 1
            item["inactive_reason"] = "Only when this source is updated" if mode == "manual" else "This source never uses AI"
        elif too_old:
            display_state = "expired"
            counts["inactive"] += 1
            counts["expired"] += 1
            item["inactive_reason"] = f"Older than the {maximum_age_days}-day waiting limit"
        elif state == "retry" and row["available_at"]:
            try:
                display_state = "ready" if datetime.fromisoformat(row["available_at"]) <= now else "retry"
            except ValueError:
                display_state = "retry"
            counts[display_state] += 1
        elif state == "waiting":
            display_state = "ready"
            counts["ready"] += 1
        else:
            display_state = state
            counts[state] = counts.get(state, 0) + 1
        item["display_state"] = display_state
        matches_view = view == "all" or display_state == view
        if view == "inactive" and display_state in {"on_request", "disabled", "expired"}:
            matches_view = True
        if view == "ready" and include_inactive and display_state in {
            "on_request", "disabled", "expired",
        }:
            matches_view = True
        matches_search = not search or search in " ".join(
            str(item.get(key, "")) for key in ("title", "feed_title", "group_title")
        ).casefold()
        if matches_view and matches_search:
            visible.append(item)
        group = by_group.setdefault(
            int(row["group_id"]),
            {
                "id": int(row["group_id"]), "title": row["group_title"],
                "priority": row["ai_priority"], "ready": 0, "processing": 0,
                "retry": 0, "on_request": 0, "disabled": 0, "expired": 0,
                "inactive": 0, "excluded": 0,
            },
        )
        group[display_state if display_state in group else "inactive"] += 1
        if display_state in {"on_request", "disabled", "expired"}:
            group["inactive"] += 1
    total_states = {
        str(row["state"]): int(row["amount"])
        for row in connection.execute(
            "SELECT state,COUNT(*) AS amount FROM ai_review_queue GROUP BY state"
        ).fetchall()
    }
    page_start = max(0, int(offset))
    page_end = page_start + max(1, min(int(limit), 500))
    return {
        "items": visible[page_start:page_end], "counts": counts, "groups": list(by_group.values()),
        "total": len(visible), "all_total": sum(counts.values()),
        "reviewed": total_states.get("reviewed", 0),
        "archived": total_states.get("archived", 0),
    }
