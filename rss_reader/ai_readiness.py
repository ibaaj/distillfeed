from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import Any

from .ai_policy import build_plan
from .config import Config


def _month_start(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return current.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    ).isoformat(timespec="seconds")


def monthly_spend(connection, *, now: datetime | None = None) -> float:
    """Return locally estimated spend from immutable stored provider attempts."""
    row = connection.execute(
        """SELECT COALESCE(SUM(estimated_cost_usd),0)
           FROM llm_runs WHERE started_at>=?""",
        (_month_start(now),),
    ).fetchone()
    return float(row[0] if row else 0)


def budget_snapshot(
    connection, config: Config, *, estimated_cost_usd: float = 0,
) -> dict[str, Any]:
    budget = float(config.get("llm", "monthly_budget_usd", 0) or 0)
    spent = monthly_spend(connection)
    remaining = max(0.0, budget - spent) if budget > 0 else None
    projected = max(0.0, float(estimated_cost_usd))
    blocked = bool(budget > 0 and projected > max(0.0, budget - spent) + 1e-12)
    warning = bool(
        budget > 0 and not blocked
        and (budget - spent - projected) <= max(0.01, budget * 0.2)
    )
    return {
        "monthly_budget_usd": budget,
        "month_spend_usd": spent,
        "remaining_usd": remaining,
        "projected_update_usd": projected,
        "blocked": blocked,
        "warning": warning,
        "source": "local_estimate",
        "provider_balance_known": False,
    }


def _credential_blocker(environment_name: str, workflows: str) -> dict[str, Any]:
    return {
        "code": "API_KEY_MISSING",
        "message": (
            f"{environment_name} is not available to the DistillFeed server. "
            f"{workflows} cannot call OpenAI until the variable is set and the server is restarted."
        ),
        "action_url": "/ai#profile",
        "action_label": "Review AI setup",
        "environment": environment_name,
    }


def ordinary_readiness(
    connection,
    config: Config,
    *,
    group_id: int | None = None,
    feed_id: int | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan or build_plan(connection, config, group_id=group_id, feed_id=feed_id)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    llm = config.section("llm")
    provider = str(llm.get("provider", "openai")).casefold()
    if not bool(llm.get("enabled", True)):
        blockers.append({
            "code": "AI_DISABLED",
            "message": "Ordinary AI summaries are disabled in Model & writing.",
            "action_url": "/ai#profile",
            "action_label": "Enable AI summaries",
        })
    elif provider == "openai":
        environment_name = str(llm.get("api_key_env", "OPENAI_API_KEY"))
        if not os.environ.get(environment_name, "").strip():
            blockers.append(_credential_blocker(environment_name, "Ordinary summaries"))
    estimated = float(plan.get("estimated_cost_usd", 0) or 0)
    budget = budget_snapshot(connection, config, estimated_cost_usd=estimated)
    if budget["blocked"]:
        blockers.append({
            "code": "LOCAL_BUDGET_EXCEEDED",
            "message": (
                f"This update is estimated at ${estimated:.4f}, but only "
                f"${float(budget['remaining_usd'] or 0):.4f} remains in the local monthly budget."
            ),
            "action_url": "/ai#profile",
            "action_label": "Review budget",
        })
    elif budget["warning"]:
        warnings.append({
            "code": "LOCAL_BUDGET_LOW",
            "message": (
                f"About ${float(budget['remaining_usd'] or 0):.4f} remains before this "
                "estimated update; the provider's actual account balance is not available here."
            ),
            "action_url": "/costs",
            "action_label": "Review costs",
        })
    if int(plan.get("deferred_count", 0)):
        cycles = math.ceil(
            int(plan.get("ready_count", 0)) / max(1, int(plan.get("selected_count", 0)))
        ) if int(plan.get("selected_count", 0)) else 0
        warnings.append({
            "code": "QUEUE_DEFERRED",
            "message": (
                f"{int(plan['deferred_count'])} ready entries will remain queued after the "
                f"next update{f'; about {cycles} cycles are needed' if cycles > 1 else ''}."
            ),
            "action_url": "/ai#overview",
            "action_label": "Review next update",
        })
    return {
        "status": "blocked" if blockers else "warning" if warnings else "ready",
        "provider": provider,
        "model": str(llm.get("model", "")),
        "blockers": blockers,
        "warnings": warnings,
        "budget": budget,
        "plan": plan,
        "can_start": not blockers,
    }


def _arxiv_estimate(connection, cfg: dict[str, Any]) -> dict[str, Any]:
    broad = int(cfg["filters"].get("broad_candidate_threshold", 0))
    try:
        rows = connection.execute(
            """SELECT ap.evaluation_status,ap.local_score,ap.llm_score,
                      LENGTH(i.title)+LENGTH(i.description_text) AS characters,
                      substr(COALESCE(i.published_at,i.discovered_at),1,10) AS digest_day
                 FROM distillfeed_arxiv_papers ap
                 JOIN items i ON i.id=ap.item_id
                 JOIN feeds f ON f.id=i.feed_id
                WHERE f.enabled=1 AND f.xml_url LIKE 'plugin://arxiv/%'
                ORDER BY COALESCE(i.published_at,i.discovered_at),ap.item_id"""
        ).fetchall()
        dedicated_rows = connection.execute(
            """SELECT MIN(substr(COALESCE(i.published_at,i.discovered_at),1,10)) AS first_day,
                      MAX(substr(COALESCE(i.published_at,i.discovered_at),1,10)) AS last_day
                 FROM summaries s
                 JOIN llm_runs lr ON lr.id=s.llm_run_id
                 JOIN summary_items si ON si.summary_id=s.id
                 JOIN items i ON i.id=si.item_id
                 JOIN feeds f ON f.id=i.feed_id
                WHERE lr.status='success'
                  AND lr.prompt_version LIKE 'distillfeed-arxiv-%'
                  AND f.enabled=1 AND f.xml_url LIKE 'plugin://arxiv/%'
                GROUP BY s.id
               HAVING first_day=last_day"""
        ).fetchall()
    except Exception:
        rows = []
        dedicated_rows = []

    dedicated_days = {
        str(row["first_day"]) for row in dedicated_rows if row["first_day"]
    }
    ranking_rows = []
    scored_by_day: dict[str, list[Any]] = {}
    for row in rows:
        day = str(row["digest_day"] or "undated")
        local = row["local_score"]
        status = str(row["evaluation_status"] or "")
        llm_score = row["llm_score"]
        needs_ai = (
            status == "pending" and (local is None or int(local) >= broad)
        ) or (
            status in {"screened_out", "complete"}
            and llm_score is None and local is not None and int(local) >= broad
        )
        if needs_ai:
            ranking_rows.append(row)
        if (
            status == "complete" and llm_score is not None
            and local is not None and int(local) >= broad
        ):
            scored_by_day.setdefault(day, []).append(row)

    batch_size = max(1, int(cfg["llm"].get("ranking_batch_size", 20)))
    ranking_day_counts: dict[str, int] = {}
    for row in ranking_rows:
        day = str(row["digest_day"] or "undated")
        ranking_day_counts[day] = ranking_day_counts.get(day, 0) + 1
    missing_digest_days = set(scored_by_day).difference(dedicated_days)
    composition_days = set(ranking_day_counts).union(missing_digest_days)
    ranking_requests = sum(
        math.ceil(value / batch_size) for value in ranking_day_counts.values()
    )
    composition_requests = len(composition_days)

    ranking_chars = sum(int(row["characters"] or 0) for row in ranking_rows)
    composition_chars = 0
    for day in composition_days:
        composition_chars += sum(
            int(row["characters"] or 0) for row in scored_by_day.get(day, [])
        )
    # Pending papers will also be part of their day's digest after ranking.
    composition_chars += ranking_chars
    input_tokens = math.ceil((ranking_chars + composition_chars) / 4)
    count = len(ranking_rows)
    output_tokens = (
        count * max(80, int(cfg["llm"].get("estimated_output_tokens_per_paper", 40)))
        + ranking_requests * 1000 + composition_requests * 5000
    )
    cost = (
        input_tokens * float(cfg["llm"].get("input_price_per_million", 0))
        + output_tokens * float(cfg["llm"].get("output_price_per_million", 0))
    ) / 1_000_000
    return {
        "pending_items": count,
        "evaluation_requests": ranking_requests,
        "composition_requests": composition_requests,
        "planned_requests": ranking_requests + composition_requests,
        "pending_days": len(composition_days),
        "missing_daily_digests": len(missing_digest_days),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": cost,
    }


def arxiv_readiness(connection, config: Config, *, require_enabled: bool = True) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    enabled = bool(config.get("plugins", "arxiv_digest_enabled", False))
    if require_enabled and not enabled:
        blockers.append({
            "code": "ARXIV_DISABLED",
            "message": "The arXiv daily digest is disabled.",
            "action_url": "/?settings=arxiv",
            "action_label": "Enable arXiv digest",
        })
    try:
        from distillfeed_arxiv.config import load_plugin_config

        cfg = load_plugin_config(config)
    except Exception as exc:
        return {
            "status": "blocked", "can_start": False, "enabled": enabled,
            "blockers": [{
                "code": "ARXIV_CONFIG_INVALID", "message": str(exc)[:1000],
                "action_url": "/?settings=arxiv", "action_label": "Review ArXiv settings",
            }],
            "warnings": [], "plan": {},
            "budget": budget_snapshot(connection, config),
        }
    if enabled and not bool(cfg["llm"].get("enabled", True)):
        blockers.append({
            "code": "ARXIV_AI_DISABLED",
            "message": "arXiv papers are waiting, but AI ranking and digest writing are disabled.",
            "action_url": "/?settings=arxiv",
            "action_label": "Enable arXiv AI",
        })
    if enabled:
        environment_name = str(cfg["llm"].get("api_key_env", "OPENAI_API_KEY"))
        if not os.environ.get(environment_name, "").strip():
            blockers.append(_credential_blocker(environment_name, "The arXiv digest"))
    plan = _arxiv_estimate(connection, cfg)
    budget = budget_snapshot(
        connection, config, estimated_cost_usd=float(plan.get("estimated_cost_usd", 0)),
    )
    if enabled and budget["blocked"]:
        blockers.append({
            "code": "LOCAL_BUDGET_EXCEEDED",
            "message": (
                f"The pending arXiv digest is estimated at "
                f"${float(plan.get('estimated_cost_usd', 0)):.4f}, above the local budget remainder."
            ),
            "action_url": "/ai#profile", "action_label": "Review budget",
        })
    return {
        "status": "blocked" if blockers else "warning" if warnings else "ready",
        "can_start": not blockers,
        "enabled": enabled,
        "blockers": blockers,
        "warnings": warnings,
        "plan": plan,
        "budget": budget,
    }


def blocked_result(readiness: dict[str, Any]) -> dict[str, Any]:
    blockers = list(readiness.get("blockers", []))
    first = blockers[0] if blockers else {
        "code": "AI_BLOCKED", "message": "The AI update is not ready to start."
    }
    return {
        "status": "blocked",
        "code": str(first.get("code", "AI_BLOCKED")),
        "message": str(first.get("message", "The AI update is blocked")),
        "readiness": readiness,
    }
