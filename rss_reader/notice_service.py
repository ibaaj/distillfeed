from __future__ import annotations

import hashlib
from typing import Any

from .ai_readiness import arxiv_readiness, ordinary_readiness
from .config import Config
from .db import transaction, utcnow
from .ntfy_policy import load_ntfy_scope_policy


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _candidate(
    key: str,
    severity: str,
    title: str,
    message: str,
    action_url: str | None = None,
    action_label: str | None = None,
) -> dict[str, Any]:
    return {
        "issue_key": key,
        "severity": severity,
        "title": title,
        "message": str(message)[:2000],
        "action_url": action_url,
        "action_label": action_label,
    }


def derive_issues(connection, config: Config) -> list[dict[str, Any]]:
    """Derive current actionable conditions; history is synchronized separately."""
    issues: list[dict[str, Any]] = []
    ordinary_feeds = int(connection.execute(
        "SELECT COUNT(*) FROM feeds WHERE enabled=1 AND xml_url NOT LIKE 'plugin://%'"
    ).fetchone()[0])
    ordinary = ordinary_readiness(connection, config)
    arxiv = arxiv_readiness(connection, config, require_enabled=False)

    missing_credentials: dict[str, list[str]] = {}
    for workflow, readiness, active in (
        ("ordinary summaries", ordinary, bool(ordinary_feeds)),
        ("arXiv digest", arxiv, bool(arxiv.get("enabled"))),
    ):
        if not active:
            continue
        for blocker in readiness.get("blockers", []):
            if blocker.get("code") == "API_KEY_MISSING":
                environment = str(blocker.get("environment", "OPENAI_API_KEY"))
                missing_credentials.setdefault(environment, []).append(workflow)
                continue
            code = str(blocker.get("code", "AI_BLOCKED"))
            if code in {"AI_DISABLED", "ARXIV_DISABLED"}:
                continue
            issues.append(_candidate(
                f"readiness:{workflow}:{code}", "error", f"{workflow.capitalize()} blocked",
                str(blocker.get("message", "The AI workflow is blocked.")),
                str(blocker.get("action_url") or "/ai"),
                str(blocker.get("action_label") or "Review AI settings"),
            ))
    for environment, workflows in missing_credentials.items():
        label = " and ".join(workflows)
        issues.append(_candidate(
            f"credential:{environment}", "error", "AI credentials are missing",
            f"{environment} is not available to the server; {label} cannot run.",
            "/ai#profile", "Review AI setup",
        ))

    plan = ordinary.get("plan", {})
    if ordinary_feeds and int(plan.get("deferred_count", 0)):
        selected = max(1, int(plan.get("selected_count", 0)))
        ready = int(plan.get("ready_count", 0))
        cycles = (ready + selected - 1) // selected
        issues.append(_candidate(
            "queue:deferred", "warning", "AI queue needs several updates",
            f"{ready} entries are ready; {int(plan['deferred_count'])} will remain after "
            f"the next update (about {cycles} cycles total).",
            "/ai#overview", "Review queue plan",
        ))
    retry_count = int(connection.execute(
        "SELECT COUNT(*) FROM ai_review_queue WHERE state='retry'"
    ).fetchone()[0])
    if retry_count:
        issues.append(_candidate(
            "queue:retry", "warning", "Some AI entries are waiting to retry",
            f"{retry_count} entr{'y is' if retry_count == 1 else 'ies are'} retained for a later retry.",
            "/ai?queue_view=retry#queue", "Review retries",
        ))

    feed_errors = int(connection.execute(
        "SELECT COUNT(*) FROM feeds WHERE enabled=1 AND last_error IS NOT NULL"
    ).fetchone()[0])
    if feed_errors:
        issues.append(_candidate(
            "feeds:errors", "warning", "Some feeds could not be refreshed",
            f"{feed_errors} enabled feed{' has' if feed_errors == 1 else 's have'} a current retrieval error; stored items remain available.",
            "/health", "Review feed health",
        ))

    ntfy_options = config.section("notifications")["ntfy"]
    if bool(ntfy_options["enabled"]):
        ntfy_policy = load_ntfy_scope_policy(
            connection, int(ntfy_options["minimum_relevance"]),
        )
        if ntfy_policy.mode == "selected":
            enabled_feed_ids = [
                int(row["id"])
                for row in connection.execute(
                    """SELECT id FROM feeds
                       WHERE enabled=1 AND xml_url NOT LIKE 'plugin://%'"""
                ).fetchall()
            ]
            if not ntfy_policy.rule_count:
                message = (
                    "Only selected sources may send ntfy alerts, but no group or feed "
                    "is selected. No article alert will be sent."
                )
            elif not any(ntfy_policy.match(feed_id) for feed_id in enabled_feed_ids):
                message = (
                    "The selected ntfy rules do not match an enabled ordinary feed. "
                    "No article alert will be sent."
                )
            else:
                message = ""
            if message:
                issues.append(_candidate(
                    "ntfy:empty-scope", "warning", "Device alerts have no active source",
                    message, "/?settings=notifications", "Review device alert sources",
                ))

    push_failures = int(connection.execute(
        "SELECT COUNT(*) FROM notification_deliveries WHERE status='failed'"
    ).fetchone()[0])
    try:
        push_failures += int(connection.execute(
            "SELECT COUNT(*) FROM distillfeed_arxiv_notifications WHERE status='failed'"
        ).fetchone()[0])
    except Exception:
        pass
    if push_failures:
        issues.append(_candidate(
            "notifications:delivery", "warning", "Device alert delivery failed",
            f"{push_failures} ntfy delivery attempt{' needs' if push_failures == 1 else 's need'} review.",
            "/notifications#device-alerts", "Review device delivery",
        ))

    latest_by_kind = connection.execute(
        """SELECT operation.* FROM app_operations operation
           WHERE operation.id=(
             SELECT latest.id FROM app_operations latest
             WHERE latest.kind=operation.kind ORDER BY latest.id DESC LIMIT 1
           ) AND operation.state IN ('failed','partial','blocked')
           ORDER BY operation.id DESC"""
    ).fetchall()
    for row in latest_by_kind:
        state = str(row["state"])
        if state == "blocked" and any(
            marker in str(row["message"]) for marker in ("API_KEY", "monthly budget", "disabled")
        ):
            continue
        kind = str(row["kind"])
        issues.append(_candidate(
            f"operation:{kind}:{row['operation_key']}",
            "error" if state == "failed" else "warning",
            f"Latest {kind} operation {state}", str(row["message"]),
            "/notifications#operations", "Review operation",
        ))
    return issues


def synchronize_issues(connection, config: Config) -> list[dict[str, Any]]:
    candidates = derive_issues(connection, config)
    now = utcnow()
    keys = {str(issue["issue_key"]) for issue in candidates}
    with transaction(connection, immediate=True):
        existing = {
            str(row["issue_key"]): row
            for row in connection.execute("SELECT * FROM app_issues").fetchall()
        }
        for issue in candidates:
            key = str(issue["issue_key"])
            fingerprint = hashlib.sha256(
                "\x1f".join(str(issue.get(field, "")) for field in (
                    "severity", "title", "message", "action_url", "action_label",
                )).encode("utf-8")
            ).hexdigest()
            row = existing.get(key)
            if row:
                changed = str(row["fingerprint"]) != fingerprint or not bool(row["active"])
                connection.execute(
                    """UPDATE app_issues SET severity=?,title=?,message=?,action_url=?,
                              action_label=?,fingerprint=?,active=1,last_seen_at=?,resolved_at=NULL,
                              occurrences=occurrences+?,
                              acknowledged_at=CASE WHEN ? THEN NULL ELSE acknowledged_at END
                       WHERE issue_key=?""",
                    (
                        issue["severity"], issue["title"], issue["message"],
                        issue.get("action_url"), issue.get("action_label"), fingerprint,
                        now, int(changed), int(changed), key,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO app_issues(
                           issue_key,severity,title,message,action_url,action_label,fingerprint,
                           active,occurrences,first_seen_at,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?,1,1,?,?)""",
                    (
                        key, issue["severity"], issue["title"], issue["message"],
                        issue.get("action_url"), issue.get("action_label"), fingerprint,
                        now, now,
                    ),
                )
        active_rows = connection.execute(
            "SELECT issue_key FROM app_issues WHERE active=1"
        ).fetchall()
        for row in active_rows:
            if str(row["issue_key"]) not in keys:
                connection.execute(
                    """UPDATE app_issues SET active=0,resolved_at=?,acknowledged_at=NULL
                       WHERE issue_key=?""",
                    (now, row["issue_key"]),
                )
    return active_issues(connection)


def active_issues(connection, *, include_acknowledged: bool = False) -> list[dict[str, Any]]:
    acknowledged = "" if include_acknowledged else "AND acknowledged_at IS NULL"
    rows = connection.execute(
        f"""SELECT * FROM app_issues WHERE active=1 {acknowledged}
            ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     last_seen_at DESC,id DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_issue(connection, issue_id: int) -> bool:
    cursor = connection.execute(
        """UPDATE app_issues SET acknowledged_at=?
           WHERE id=? AND active=1 AND acknowledged_at IS NULL""",
        (utcnow(), int(issue_id)),
    )
    return cursor.rowcount == 1
