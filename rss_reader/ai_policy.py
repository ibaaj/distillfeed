from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .db import group_descendant_ids, utcnow


WORKLOAD_LIMITS = {"focused": 80, "balanced": 200, "wide": 500}


def workload_limit(config: Config) -> int:
    return WORKLOAD_LIMITS.get(str(config.get("llm", "review_workload", "balanced")), 200)


def policy_snapshot(config: Config) -> dict[str, Any]:
    """Return the immutable settings that define one AI-summary operation."""
    llm = config.section("llm")
    return {
        "provider": str(llm.get("provider", "openai")),
        "api_key_env": str(llm.get("api_key_env", "OPENAI_API_KEY")),
        "base_url": str(llm.get("base_url", "")),
        "model": str(llm.get("model", "")),
        "reasoning_effort": str(llm.get("reasoning_effort", "none")),
        "language": str(config.get("app", "summary_language", "English")),
        "interests": str(config.get("app", "interest_profile", ""))[:2000],
        "workload": str(llm.get("review_workload", "balanced")),
        "maximum_items": workload_limit(config),
        "maximum_items_per_request": int(llm.get("max_entries_total", 160)),
        "maximum_items_per_feed": int(llm.get("max_entries_per_feed", 20)),
        "maximum_description_characters": int(llm.get("max_description_chars", 1500)),
        "maximum_age_days": int(llm.get("candidate_max_age_days", 30)),
        "rolling_digest_hours": int(llm.get("rolling_digest_hours", 24)),
        "minimum_relevance": int(llm.get("minimum_relevance", 70)),
        "maximum_summary_items": int(llm.get("maximum_summary_items", 25)),
        "maximum_input_characters": int(llm.get("max_input_chars", 400_000)),
        "maximum_output_tokens": int(llm.get("max_output_tokens", 16_000)),
        "estimated_output_tokens_per_item": int(
            llm.get("estimated_output_tokens_per_item", 120)
        ),
        "estimated_output_tokens_per_group": int(
            llm.get("estimated_output_tokens_per_group", 250)
        ),
        "output_token_safety_margin": int(llm.get("output_token_safety_margin", 1000)),
        "monthly_budget_usd": float(llm.get("monthly_budget_usd", 0)),
        "pricing": dict(llm.get("pricing", {})),
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def effective_group_modes(connection) -> dict[int, str]:
    rows = connection.execute(
        "SELECT id,parent_id,llm_enabled,ai_mode,ai_priority FROM groups"
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    cache: dict[int, str] = {}

    def resolve(group_id: int, trail: set[int] | None = None) -> str:
        if group_id in cache:
            return cache[group_id]
        trail = set() if trail is None else trail
        row = by_id.get(group_id)
        if row is None or group_id in trail:
            return "off"
        own = str(row["ai_mode"] or "automatic")
        # Keep old OPML/configuration files meaningful during the migration.
        if not bool(row["llm_enabled"]) or str(row["ai_priority"]) == "off":
            own = "off"
        elif str(row["ai_priority"]) == "manual" and own == "automatic":
            own = "manual"
        parent_id = row["parent_id"]
        if parent_id is not None:
            parent = resolve(int(parent_id), trail | {group_id})
            if parent == "off":
                own = "off"
            elif parent == "manual" and own == "automatic":
                own = "manual"
        cache[group_id] = own if own in {"automatic", "manual", "off"} else "automatic"
        return cache[group_id]

    return {identifier: resolve(identifier) for identifier in by_id}


def effective_feed_mode(row: Any, group_modes: dict[int, str]) -> str:
    parent = group_modes.get(int(row["group_id"]), "off")
    own = str(row["feed_ai_mode"] or "inherit")
    if not bool(row["feed_llm_enabled"]) or own == "off" or parent == "off":
        return "off"
    if parent == "manual" and own in {"inherit", "automatic"}:
        return "manual"
    if own == "manual":
        return "manual"
    return parent if own == "inherit" else "automatic"


def selected_payload_rows(
    connection, selected_ids: list[int], snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the exact bounded entry payload shared by planning and execution."""
    if not selected_ids:
        return []
    marks = ",".join("?" for _ in selected_ids)
    rows = connection.execute(
        f"""SELECT i.id AS item_id,i.feed_id,i.title,i.author,i.published_at,i.discovered_at,
                   i.description_text,f.title AS feed_title,f.group_id,g.title AS group_title
              FROM items i JOIN feeds f ON f.id=i.feed_id JOIN groups g ON g.id=f.group_id
              WHERE i.id IN ({marks})""",
        selected_ids,
    ).fetchall()
    by_id = {int(row["item_id"]): row for row in rows}
    maximum_description = max(1, int(snapshot.get("maximum_description_characters", 1500)))
    return [
        {
            "item_id": identifier,
            "feed_id": int(by_id[identifier]["feed_id"]),
            "group_id": int(by_id[identifier]["group_id"]),
            "group_title": str(by_id[identifier]["group_title"]),
            "feed_title": str(by_id[identifier]["feed_title"]),
            "title": str(by_id[identifier]["title"]),
            "author": str(by_id[identifier]["author"] or ""),
            "published_at": (
                by_id[identifier]["published_at"] or by_id[identifier]["discovered_at"]
            ),
            "description": str(by_id[identifier]["description_text"] or "")[
                :maximum_description
            ],
        }
        for identifier in selected_ids if identifier in by_id
    ]


def provider_batches(
    rows: list[dict[str, Any]], snapshot: dict[str, Any],
) -> list[list[dict[str, Any]]]:
    """Apply every per-request constraint used by the provider execution path."""
    maximum = max(1, int(snapshot["maximum_items_per_request"]))
    per_feed = max(1, int(snapshot["maximum_items_per_feed"]))
    maximum_characters = max(1000, int(snapshot["maximum_input_characters"]))
    remaining = list(rows)
    result: list[list[dict[str, Any]]] = []
    while remaining:
        batch: list[dict[str, Any]] = []
        counts: defaultdict[int, int] = defaultdict(int)
        used = 0
        deferred: list[dict[str, Any]] = []
        for row in remaining:
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            if (
                len(batch) < maximum
                and counts[int(row["feed_id"])] < per_feed
                and (not batch or used + size <= maximum_characters)
            ):
                batch.append(row)
                counts[int(row["feed_id"])] += 1
                used += size
            else:
                deferred.append(row)
        if not batch:
            batch = [remaining[0]]
            deferred = remaining[1:]
        result.append(batch)
        remaining = deferred
    return result


def estimate_plan_cost(
    rows: list[dict[str, Any]], batches: list[list[dict[str, Any]]],
    snapshot: dict[str, Any], composition_requests: int,
) -> dict[str, Any]:
    """Return a conservative local estimate, never a provider balance claim."""
    evaluation_characters = sum(
        len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) for row in rows
    )
    evaluation_input = math.ceil(evaluation_characters / 4)
    composition_input = len(rows) * max(
        40, int(snapshot.get("estimated_output_tokens_per_item", 120))
    )
    input_tokens = evaluation_input + composition_input
    request_count = len(batches) + max(0, composition_requests)
    expected_output = (
        len(rows) * int(snapshot.get("estimated_output_tokens_per_item", 120))
        + max(0, composition_requests)
        * int(snapshot.get("estimated_output_tokens_per_group", 250))
    )
    output_tokens = min(
        request_count * int(snapshot.get("maximum_output_tokens", 16_000)),
        expected_output
        + request_count * int(snapshot.get("output_token_safety_margin", 1000)),
    ) if request_count else 0
    pricing = dict(snapshot.get("pricing", {}))
    estimated_cost = (
        input_tokens * float(pricing.get("input", 0))
        + output_tokens * float(pricing.get("output", 0))
    ) / 1_000_000
    return {
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": estimated_cost,
        "evaluation_requests": len(batches),
        "composition_requests": max(0, composition_requests),
        "planned_requests": request_count,
        "batch_sizes": [len(batch) for batch in batches],
    }


def _scope_group_ids(connection, group_id: int | None, feed_id: int | None) -> tuple[list[int], int | None]:
    if group_id is not None and feed_id is not None:
        raise ValueError("Choose either a group or a feed scope")
    if feed_id is not None:
        row = connection.execute("SELECT group_id FROM feeds WHERE id=?", (feed_id,)).fetchone()
        return ([int(row["group_id"])] if row else []), feed_id
    if group_id is not None:
        return group_descendant_ids(connection, group_id), None
    return [int(row["id"]) for row in connection.execute("SELECT id FROM groups")], None


def _ordinary_source_group_ids(connection, scoped_group_ids: list[int]) -> set[int]:
    """Return scoped groups belonging to ordinary RSS/Atom sources.

    Plugin-owned virtual feeds keep their own selection policy and lifecycle.
    Ancestors of regular feeds remain relevant because they define inherited
    source modes.
    """
    scoped = set(scoped_group_ids)
    parents = {
        int(row["id"]): int(row["parent_id"]) if row["parent_id"] is not None else None
        for row in connection.execute("SELECT id,parent_id FROM groups").fetchall()
    }
    relevant: set[int] = set()
    for row in connection.execute(
        "SELECT DISTINCT group_id FROM feeds WHERE xml_url NOT LIKE 'plugin://%'"
    ).fetchall():
        identifier = int(row["group_id"])
        while identifier in scoped:
            relevant.add(identifier)
            parent = parents.get(identifier)
            if parent is None:
                break
            identifier = parent
    return relevant


def build_plan(
    connection,
    config: Config,
    *,
    group_id: int | None = None,
    feed_id: int | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    """Build the exact, side-effect-free selection used by an AI update."""
    snapshot = policy_snapshot(config)
    group_ids, scoped_feed_id = _scope_group_ids(connection, group_id, feed_id)
    ordinary_group_ids = _ordinary_source_group_ids(connection, group_ids)
    group_modes = effective_group_modes(connection)
    source_groups = [
        {
            "id": int(row["id"]), "parent_id": row["parent_id"],
            "mode": group_modes.get(int(row["id"]), "off"),
            "priority": str(row["ai_priority"]),
            "interval_hours": int(row["summary_interval_hours"] or 0),
            "cycle_cap": int(row["summary_item_budget"] or 0),
        }
        for row in connection.execute(
            """SELECT id,parent_id,ai_priority,summary_interval_hours,summary_item_budget
                 FROM groups ORDER BY position,id"""
        ).fetchall()
        if int(row["id"]) in ordinary_group_ids
    ]
    source_feeds = [
        {
            "id": int(row["id"]), "group_id": int(row["group_id"]),
            "enabled": bool(row["enabled"]),
            "mode": effective_feed_mode(row, group_modes),
        }
        for row in connection.execute(
            """SELECT id,group_id,enabled,llm_enabled AS feed_llm_enabled,
                      ai_mode AS feed_ai_mode,xml_url FROM feeds ORDER BY position,id"""
        ).fetchall()
        if int(row["group_id"]) in set(group_ids)
        and not str(row["xml_url"]).startswith("plugin://")
        and (scoped_feed_id is None or int(row["id"]) == scoped_feed_id)
    ]
    # This is the immutable source policy used for both selection and
    # composition. A change made while a job is running applies to the next job.
    snapshot["sources"] = {"groups": source_groups, "feeds": source_feeds}
    policy_id = snapshot_hash(snapshot)
    if not group_ids:
        return {
            "created_at": utcnow(), "policy": snapshot, "policy_hash": policy_id,
            "scope_kind": "feed" if feed_id is not None else "group" if group_id is not None else "all",
            "scope_id": feed_id if feed_id is not None else group_id,
            "selected_ids": [], "selected_count": 0, "ready_count": 0,
            "inactive_count": 0, "excluded_count": 0, "retry_count": 0,
            "manual_count": 0, "off_count": 0, "expired_count": 0,
            "cadence_count": 0, "deferred_count": 0,
            "source_count": 0, "estimated_requests": 0, "allocations": [],
            "estimated_input_tokens": 0, "estimated_output_tokens": 0,
            "estimated_cost_usd": 0.0, "evaluation_requests": 0,
            "composition_requests": 0, "planned_requests": 0,
            "batch_sizes": [], "automatic": bool(automatic),
        }

    marks = ",".join("?" for _ in group_ids)
    feed_clause = " AND f.id=?" if scoped_feed_id is not None else ""
    parameters: list[Any] = [*group_ids, *([scoped_feed_id] if scoped_feed_id is not None else [])]
    rows = connection.execute(
        f"""SELECT i.id,i.feed_id,i.title,i.description_text,i.published_at,i.discovered_at,
                   q.state,q.available_at,f.group_id,f.title AS feed_title,
                   f.llm_enabled AS feed_llm_enabled,f.ai_mode AS feed_ai_mode,
                   g.title AS group_title,g.ai_priority,g.summary_item_budget,
                   COALESCE(pref.disposition,'default') AS disposition
              FROM items i JOIN feeds f ON f.id=i.feed_id JOIN groups g ON g.id=f.group_id
              JOIN ai_review_queue q ON q.item_id=i.id
              LEFT JOIN ai_item_preferences pref ON pref.item_id=i.id
              WHERE f.enabled=1 AND f.group_id IN ({marks}){feed_clause}
                AND q.state IN ('waiting','retry')
              ORDER BY q.queued_at ASC,i.id ASC""",
        parameters,
    ).fetchall()
    now = datetime.now(UTC)
    maximum_age = int(snapshot["maximum_age_days"])
    cutoff = now - timedelta(days=maximum_age) if maximum_age > 0 else None
    cadence_blocked: set[int] = set()
    if automatic and group_id is None and feed_id is None:
        for source in source_groups:
            interval = int(source["interval_hours"])
            if interval <= 0:
                continue
            latest = connection.execute(
                """SELECT lr.completed_at FROM summaries s JOIN llm_runs lr ON lr.id=s.llm_run_id
                   WHERE s.scope_kind='group' AND s.scope_id=? AND lr.status='success'
                   ORDER BY lr.id DESC LIMIT 1""",
                (source["id"],),
            ).fetchone()
            if latest and latest["completed_at"]:
                try:
                    if datetime.fromisoformat(latest["completed_at"]) > now - timedelta(hours=interval):
                        cadence_blocked.add(int(source["id"]))
                except ValueError:
                    pass
    ready: list[Any] = []
    inactive = manual = off = excluded = retry = expired = cadence = 0
    for row in rows:
        if str(row["disposition"]) == "excluded":
            excluded += 1
            continue
        mode = effective_feed_mode(row, group_modes)
        explicit_scope = group_id is not None or feed_id is not None
        if mode == "off" or (mode == "manual" and not explicit_scope):
            inactive += 1
            if mode == "off":
                off += 1
            else:
                manual += 1
            continue
        if int(row["group_id"]) in cadence_blocked:
            cadence += 1
            continue
        if row["state"] == "retry" and row["available_at"]:
            try:
                if datetime.fromisoformat(row["available_at"]) > now:
                    retry += 1
                    continue
            except ValueError:
                retry += 1
                continue
        timestamp = row["published_at"] or row["discovered_at"]
        if cutoff and timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                if parsed < cutoff:
                    expired += 1
                    continue
            except ValueError:
                pass
        ready.append(row)

    per_group_feed: defaultdict[int, defaultdict[int, deque[Any]]] = defaultdict(
        lambda: defaultdict(deque)
    )
    for row in ready:
        per_group_feed[int(row["group_id"])][int(row["feed_id"])].append(row)
    group_queues: dict[int, deque[Any]] = {}
    for identifier, feeds in per_group_feed.items():
        combined: deque[Any] = deque()
        feed_queues = [queue for _, queue in sorted(feeds.items())]
        while any(feed_queues):
            for queue in feed_queues:
                if queue:
                    combined.append(queue.popleft())
        group_queues[identifier] = combined

    group_rows = {
        int(row["id"]): row
        for row in connection.execute(
            "SELECT id,position,ai_priority,summary_item_budget FROM groups"
        ).fetchall()
    }
    priority_weight = {"high": 4.0, "normal": 2.0, "low": 1.0}
    selected: list[Any] = []
    selected_by_group: defaultdict[int, int] = defaultdict(int)
    scores = {identifier: 0.0 for identifier in group_queues}
    maximum = int(snapshot["maximum_items"])
    while len(selected) < maximum:
        active = []
        for identifier, queue in group_queues.items():
            cap = int(group_rows[identifier]["summary_item_budget"] or 0)
            if queue and (not cap or selected_by_group[identifier] < cap):
                active.append(identifier)
        if not active:
            break
        weights = {
            identifier: math.sqrt(len(group_queues[identifier]))
            * priority_weight.get(str(group_rows[identifier]["ai_priority"]), 2.0)
            for identifier in active
        }
        total = sum(weights.values())
        for identifier, weight in weights.items():
            scores[identifier] += weight
        chosen = max(
            active,
            key=lambda identifier: (
                scores[identifier], -int(group_rows[identifier]["position"] or 0), -identifier,
            ),
        )
        scores[chosen] -= total
        selected.append(group_queues[chosen].popleft())
        selected_by_group[chosen] += 1

    allocations = [
        {
            "group_id": identifier,
            "group_title": next(
                (str(row["group_title"]) for row in selected if int(row["group_id"]) == identifier),
                str(identifier),
            ),
            "items": amount,
            "priority": str(group_rows[identifier]["ai_priority"]),
        }
        for identifier, amount in sorted(selected_by_group.items())
    ]
    selected_ids = [int(row["id"]) for row in selected]
    payload_rows = selected_payload_rows(connection, selected_ids, snapshot)
    batches = provider_batches(payload_rows, snapshot)
    if selected_ids:
        composition_requests = (
            1 if feed_id is not None or group_id is not None else max(1, len(allocations))
        )
    else:
        composition_requests = 0
    estimate = estimate_plan_cost(payload_rows, batches, snapshot, composition_requests)
    return {
        "created_at": utcnow(), "policy": snapshot, "policy_hash": policy_id,
        "scope_kind": "feed" if feed_id is not None else "group" if group_id is not None else "all",
        "scope_id": feed_id if feed_id is not None else group_id,
        "selected_ids": selected_ids,
        "selected_count": len(selected), "ready_count": len(ready),
        "inactive_count": inactive, "excluded_count": excluded,
        "manual_count": manual, "off_count": off,
        "retry_count": retry, "expired_count": expired,
        "cadence_count": cadence,
        "source_count": len({int(row["feed_id"]) for row in selected}),
        # Kept as the evaluation-only count for the existing AI-page wording;
        # ``planned_requests`` below is the complete operation count.
        "estimated_requests": len(batches),
        "allocations": allocations,
        "deferred_count": max(0, len(ready) - len(selected)),
        "automatic": bool(automatic),
        **estimate,
    }
