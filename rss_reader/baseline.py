from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from .ai_queue import sync_review_queue
from .config import Config
from .db import llm_enabled_group_ids


LOGGER = logging.getLogger(__name__)


def baseline_backlog(
    connection,
    config: Config,
    *,
    max_items: int | None = None,
    max_per_feed: int | None = None,
    max_age_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Keep a fair recent subset AI-ready without deleting stored entries."""
    llm = config.section("llm")
    workload_limit = {
        "focused": 80, "balanced": 200, "wide": 500,
    }.get(str(llm.get("review_workload", "balanced")), 200)
    item_limit = int(max_items if max_items is not None else workload_limit)
    feed_limit = int(max_per_feed if max_per_feed is not None else llm["max_entries_per_feed"])
    age_days = int(max_age_days if max_age_days is not None else llm["candidate_max_age_days"])
    if item_limit <= 0 or feed_limit <= 0 or age_days < 0:
        raise ValueError("Baseline limits must be positive; max age may be zero to disable it")
    cutoff = datetime.now(UTC) - timedelta(days=age_days) if age_days > 0 else None
    enabled_groups = llm_enabled_group_ids(connection)
    if not enabled_groups:
        return {"eligible": 0, "baselined": 0, "examined": 0}
    group_marks = ",".join("?" for _ in enabled_groups)
    rows = connection.execute(
        f"""SELECT i.id,i.feed_id,f.group_id,i.published_at,i.discovered_at
              FROM items i JOIN feeds f ON f.id=i.feed_id
             WHERE i.summary_eligible=1 AND f.llm_enabled=1
               AND f.group_id IN ({group_marks})
               AND NOT EXISTS (
                   SELECT 1 FROM ai_evaluations evaluation
                    WHERE evaluation.item_id=i.id AND evaluation.current=1
               )
             ORDER BY COALESCE(i.published_at,i.discovered_at) DESC,i.id DESC""",
        enabled_groups,
    ).fetchall()
    grouped: defaultdict[int, defaultdict[int, deque[int]]] = defaultdict(
        lambda: defaultdict(deque)
    )
    rejected: set[int] = set()
    for row in rows:
        item_id = int(row["id"])
        timestamp = row["published_at"] or row["discovered_at"]
        try:
            item_time = datetime.fromisoformat(timestamp) if timestamp else None
            if item_time and item_time.tzinfo is None:
                item_time = item_time.replace(tzinfo=UTC)
        except ValueError:
            item_time = None
        feed_queue = grouped[int(row["group_id"])][int(row["feed_id"])]
        if (cutoff and item_time and item_time < cutoff) or len(feed_queue) >= feed_limit:
            rejected.add(item_id)
        else:
            feed_queue.append(item_id)

    group_queues: dict[int, deque[int]] = {}
    for group_id, feeds in grouped.items():
        queue: deque[int] = deque()
        feed_queues = [values for _, values in sorted(feeds.items())]
        while any(feed_queues):
            for feed_queue in feed_queues:
                if feed_queue:
                    queue.append(feed_queue.popleft())
        group_queues[group_id] = queue
    kept: set[int] = set()
    group_order = deque(sorted(group_queues))
    while group_order and len(kept) < item_limit:
        group_id = group_order.popleft()
        kept.add(group_queues[group_id].popleft())
        if group_queues[group_id]:
            group_order.append(group_id)
    for queue in group_queues.values():
        rejected.update(queue)
    rejected.update(int(row["id"]) for row in rows if int(row["id"]) not in kept)
    if not dry_run and rejected:
        identifiers = sorted(rejected)
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start : start + 500]
            marks = ",".join("?" for _ in batch)
            connection.execute(
                f"UPDATE items SET summary_eligible=0 WHERE id IN ({marks})", batch,
            )
    if not dry_run:
        sync_review_queue(connection)
    LOGGER.info(
        "Backlog baseline eligible=%d baselined=%d dry_run=%s",
        len(kept), len(rejected), dry_run,
    )
    return {"eligible": len(kept), "baselined": len(rejected), "examined": len(rows)}
