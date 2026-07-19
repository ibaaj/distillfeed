from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import copy
import threading
from io import BytesIO
from functools import wraps
from datetime import UTC, datetime, timedelta
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import BadRequest

from .ai_policy import build_plan, effective_feed_mode, effective_group_modes
from .ai_readiness import arxiv_readiness, blocked_result, ordinary_readiness
from .ai_queue import queue_dashboard, set_item_disposition
from .backup import build_backup, restore_backup
from .config import (
    Config,
    OPENAI_MODEL_PRICING,
    ensure_runtime_directories,
    flask_secret,
    load_config,
    save_config,
    validate_config,
)
from .db import (
    connect,
    acquire_lock,
    ensure_ungrouped,
    group_descendant_ids,
    initialize,
    llm_enabled_group_ids,
    release_lock,
    request_job_cancellation,
    transaction,
    utcnow,
)
from .generated_feeds import is_generated_feed_url, validate_generated_feed_url
from .net import validate_http_url
from .net import safe_external_url
from .notifications import send_ntfy_test
from .notice_service import acknowledge_issue, active_issues, synchronize_issues
from .ntfy_policy import ntfy_scope_settings, replace_ntfy_scope_policy
from .operations import (
    create_operation,
    fail_operation,
    finish_operation,
    operation_for_display,
    set_operation_phase,
    start_operation,
)
from .opml import build_tree_from_database, serialize_opml, write_database_opml
from .plugins import (
    available_plugin_names,
    decorate_page,
    enabled_plugin_names,
    initialize_plugins,
    plugin_settings_actions,
    plugin_settings_fields,
    run_plugin_settings_action,
    set_plugin_runtime_state,
    update_plugin_settings,
)
from .scheduler import BackgroundScheduler, defer_next_refresh
from .service import run_refresh, run_summary, run_update_summaries, start_thread
from .weather import get_weather

LOGGER = logging.getLogger(__name__)


def _strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError("Boolean values must be true or false")


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    if key not in payload:
        raise ValueError(f"{key} must be explicitly true or false")
    return _strict_bool(payload[key])


def _validate_feed_source(config: Config, value: str) -> None:
    if is_generated_feed_url(value):
        validate_generated_feed_url(config, value)
    else:
        validate_http_url(value, bool(config.get("feeds", "allow_private_urls")))


def _display_timestamp(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def _ai_task_label(prompt_version: Any) -> str:
    value = str(prompt_version or "")
    if value.startswith("distillfeed-evaluation-"):
        return "Entry evaluation"
    if value.startswith("distillfeed-summary-"):
        return "Summary writing"
    if value.startswith("distillfeed-arxiv-"):
        return "arXiv daily digest"
    return f"Other · {value[:80]}" if value else "Legacy summary"


def _display_ai_error(value: Any) -> str:
    """Return an actionable provider error without exposing credential-shaped text."""
    text = str(value or "").strip()
    lowered = text.casefold()
    if "invalid_api_key" in lowered or "incorrect api key" in lowered:
        return (
            "OpenAI rejected the API key (HTTP 401). Set a valid OPENAI_API_KEY "
            "for the DistillFeed server process, then restart it."
        )
    text = re.sub(r"\bsk-[A-Za-z0-9_.*-]{8,}", "[redacted OpenAI key]", text)
    return text[:500] if text else "The provider request failed."


def _run_for_display(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    result = dict(row)
    if result.get("error"):
        result["error"] = _display_ai_error(result["error"])
    return result


def _finish_reserved_operation(
    config: Config, lock_name: str, owner: str, operation_id: str,
    kind: str, target,
) -> None:
    """Persist one exact terminal result and never strand its startup lock."""
    try:
        with connect(config.database_path) as connection:
            start_operation(connection, operation_id, "The worker started")
            set_operation_phase(
                connection, operation_id,
                "refreshing" if kind == "refresh" else "updating",
                "Checking feeds" if kind == "refresh" else "Checking feeds before the AI stage",
            )
        result = target()
        if not isinstance(result, dict):
            result = {"status": "failed", "message": "The worker returned no operation result"}
        with connect(config.database_path) as connection:
            finish_operation(connection, operation_id, kind, result)
            synchronize_issues(connection, config)
    except Exception as exc:
        LOGGER.exception("Browser %s operation %s failed", kind, operation_id)
        with connect(config.database_path) as connection:
            fail_operation(connection, operation_id, exc)
            try:
                synchronize_issues(connection, config)
            except Exception:
                LOGGER.exception("Issue synchronization failed after operation failure")
    finally:
        with connect(config.database_path) as connection:
            release_lock(connection, lock_name, owner)


def _requested_item_ids(payload: dict[str, Any], *, limit: int = 1000) -> list[int]:
    raw_identifiers = payload.get("item_ids", [])
    if not isinstance(raw_identifiers, list):
        raise ValueError("item_ids must be a list")
    identifiers: set[int] = set()
    for raw_identifier in raw_identifiers:
        if isinstance(raw_identifier, bool) or isinstance(raw_identifier, float):
            raise ValueError("item_ids must contain positive integer IDs")
        if not isinstance(raw_identifier, (int, str)):
            raise ValueError("item_ids must contain positive integer IDs")
        text = str(raw_identifier).strip()
        if not text.isdecimal():
            raise ValueError("item_ids must contain positive integer IDs")
        identifier = int(text)
        if identifier <= 0:
            raise ValueError("item_ids must contain positive integer IDs")
        identifiers.add(identifier)
    if len(identifiers) > limit:
        raise ValueError(f"A selected action is limited to {limit} items")
    return sorted(identifiers)


def _existing_item_ids(connection, identifiers: list[int]) -> list[int]:
    if not identifiers:
        return []
    marks = ",".join("?" for _ in identifiers)
    return [
        int(row["id"]) for row in connection.execute(
            f"SELECT id FROM items WHERE id IN ({marks}) ORDER BY id", identifiers
        ).fetchall()
    ]


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _group_tree(connection) -> list[dict[str, Any]]:
    groups = connection.execute(
        """SELECT g.* FROM groups g
           WHERE NOT EXISTS (
               SELECT 1 FROM feeds plugin_feed
               WHERE plugin_feed.group_id=g.id AND plugin_feed.xml_url LIKE 'plugin://%'
           ) OR EXISTS (
               SELECT 1 FROM feeds visible_feed
               WHERE visible_feed.group_id=g.id AND visible_feed.enabled=1
           )
           ORDER BY g.position, g.title COLLATE NOCASE"""
    ).fetchall()
    ungrouped = next(
        (row for row in groups if row["parent_id"] is None and row["title"] == "Ungrouped"), None
    )
    ungrouped_id = int(ungrouped["id"]) if ungrouped else None
    nodes = {
        int(row["id"]): {
            "id": int(row["id"]), "title": row["title"], "parent_id": row["parent_id"],
            "position": int(row["position"]), "kind": "group",
            "llm_enabled": bool(row["llm_enabled"]), "effective_llm": True,
            "summary_interval_hours": int(row["summary_interval_hours"]),
            "summary_item_budget": int(row["summary_item_budget"]),
            "ai_priority": str(row["ai_priority"]),
            "children": [], "feeds": [], "entries": [], "unread": 0, "errors": 0,
            "arxiv_feeds": 0, "ordinary_feeds": 0, "is_arxiv": False,
        }
        for row in groups
    }
    feed_rows = connection.execute(
        """SELECT f.*, COUNT(CASE WHEN i.is_read=0 THEN 1 END) AS unread
           FROM feeds f LEFT JOIN items i ON i.feed_id=f.id
           WHERE f.enabled=1 GROUP BY f.id ORDER BY f.position, f.title COLLATE NOCASE"""
    ).fetchall()
    for feed in feed_rows:
        node = nodes.get(int(feed["group_id"]))
        if node:
            item = dict(feed)
            item["kind"] = "feed"
            item["position"] = int(feed["position"])
            item["parent_id"] = None if int(feed["group_id"]) == ungrouped_id else int(feed["group_id"])
            item["unread"] = int(feed["unread"] or 0)
            item["llm_enabled"] = bool(feed["llm_enabled"])
            item["is_arxiv"] = str(feed["xml_url"]).startswith("plugin://arxiv/")
            node["feeds"].append(item)
            node["unread"] += item["unread"]
            node["errors"] += int(bool(feed["last_error"]))
            node["arxiv_feeds" if item["is_arxiv"] else "ordinary_feeds"] += 1
    roots: list[dict[str, Any]] = []
    for row in groups:
        node = nodes[int(row["id"])]
        if row["parent_id"] is None and int(row["id"]) != ungrouped_id:
            roots.append(node)
        elif row["parent_id"] is not None and int(row["parent_id"]) in nodes:
            nodes[int(row["parent_id"])]["children"].append(node)

    def totals(node: dict[str, Any], parent_llm: bool = True) -> None:
        node["effective_llm"] = parent_llm and node["llm_enabled"]
        for feed in node["feeds"]:
            feed["effective_llm"] = node["effective_llm"] and feed["llm_enabled"]
        for child in node["children"]:
            totals(child, node["effective_llm"])
            node["unread"] += child["unread"]
            node["errors"] += child["errors"]
            node["arxiv_feeds"] += child["arxiv_feeds"]
            node["ordinary_feeds"] += child["ordinary_feeds"]
        node["is_arxiv"] = bool(node["arxiv_feeds"] and not node["ordinary_feeds"])
        node["entries"] = sorted(
            [*node["children"], *node["feeds"]],
            key=lambda entry: (entry["position"], entry["kind"], entry["title"].casefold()),
        )

    for root in roots:
        totals(root)
    loose_feeds: list[dict[str, Any]] = []
    if ungrouped_id is not None:
        hidden = nodes[ungrouped_id]
        hidden["effective_llm"] = hidden["llm_enabled"]
        for feed in hidden["feeds"]:
            feed["effective_llm"] = hidden["effective_llm"] and feed["llm_enabled"]
            loose_feeds.append(feed)
    return sorted(
        [*roots, *loose_feeds],
        key=lambda entry: (entry["position"], entry["kind"], entry["title"].casefold()),
    )


def _visible_subscription_entries(connection, parent_id: int | None) -> list[tuple[str, int]]:
    """Return the exact mixed order displayed in one subscription container."""
    if parent_id is None:
        ungrouped_id = ensure_ungrouped(connection)
        groups = connection.execute(
            "SELECT id,position,title FROM groups WHERE parent_id IS NULL AND id<>?",
            (ungrouped_id,),
        ).fetchall()
        feeds = connection.execute(
            "SELECT id,position,title FROM feeds WHERE group_id=?", (ungrouped_id,)
        ).fetchall()
    else:
        groups = connection.execute(
            "SELECT id,position,title FROM groups WHERE parent_id=?", (parent_id,)
        ).fetchall()
        feeds = connection.execute(
            "SELECT id,position,title FROM feeds WHERE group_id=?", (parent_id,)
        ).fetchall()
    entries = [
        (int(row["position"]), "group", str(row["title"]).casefold(), int(row["id"]))
        for row in groups
    ]
    entries.extend(
        (int(row["position"]), "feed", str(row["title"]).casefold(), int(row["id"]))
        for row in feeds
    )
    return [(kind, identifier) for _, kind, _, identifier in sorted(entries)]


def _store_subscription_order(connection, entries: list[tuple[str, int]]) -> None:
    for position, (kind, identifier) in enumerate(entries):
        table = "groups" if kind == "group" else "feeds"
        connection.execute(f"UPDATE {table} SET position=? WHERE id=?", (position, identifier))


def _summary_for_scope(connection, group_id: int, feed_id: int | None):
    scope_kind = "feed" if feed_id is not None else "group"
    scope_id = feed_id if feed_id is not None else group_id
    return connection.execute(
        """SELECT lr.* FROM llm_runs lr JOIN summaries s ON s.llm_run_id=lr.id
           WHERE lr.status='success'
             AND CASE WHEN s.scope_id IS NOT NULL THEN s.scope_kind
                      WHEN s.scope_feed_id IS NOT NULL THEN 'feed' ELSE 'group' END=?
             AND COALESCE(s.scope_id,s.scope_feed_id,s.group_id)=?
           ORDER BY lr.id DESC LIMIT 1""",
        (scope_kind, scope_id),
    ).fetchone()


def _active_summary_for_scope(connection, group_id: int, feed_id: int | None):
    """Historical output remains visible; source policy governs future sending."""
    return _summary_for_scope(connection, group_id, feed_id)


def _cluster_summary_items(rows) -> list[dict[str, Any]]:
    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        label = str(item.get("story_cluster") or item.get("title") or "Other").strip()
        key = label.casefold()
        cluster = clusters.setdefault(key, {"label": label, "importance": 0, "items": []})
        cluster["importance"] = max(cluster["importance"], int(item.get("importance") or 0))
        cluster["items"].append(item)
    return sorted(clusters.values(), key=lambda cluster: cluster["importance"], reverse=True)


def _page_data(
    connection, config: Config, group_id: int | None, feed_id: int | None,
    rolling_hours: int = 24,
) -> dict[str, Any]:
    tree = _group_tree(connection)
    all_groups = connection.execute(
        """SELECT g.id, g.title, g.parent_id, g.llm_enabled, g.ai_mode, g.ai_priority,
                  g.summary_interval_hours, g.summary_item_budget
           FROM groups g
           WHERE NOT EXISTS (
               SELECT 1 FROM feeds plugin_feed
               WHERE plugin_feed.group_id=g.id AND plugin_feed.xml_url LIKE 'plugin://%'
           ) OR EXISTS (
               SELECT 1 FROM feeds visible_feed
               WHERE visible_feed.group_id=g.id AND visible_feed.enabled=1
           )
           ORDER BY g.title COLLATE NOCASE"""
    ).fetchall()
    ungrouped = next(
        (row for row in all_groups if row["parent_id"] is None and row["title"] == "Ungrouped"), None
    )
    visible_groups = [row for row in all_groups if row is not ungrouped]
    all_feeds = connection.execute(
        """SELECT id, group_id, title, xml_url, html_url, llm_enabled, ai_mode,
                  last_attempt_at, last_success_at, last_http_status, last_error
           FROM feeds WHERE enabled=1 ORDER BY title COLLATE NOCASE"""
    ).fetchall()
    feed_lookup = {int(row["id"]): row for row in all_feeds}
    group_lookup = {int(row["id"]): row for row in all_groups}
    valid_group_ids = {int(row["id"]) for row in all_groups}
    if feed_id not in feed_lookup:
        feed_id = None
    if feed_id is not None:
        group_id = int(feed_lookup[feed_id]["group_id"])
    elif group_id not in valid_group_ids:
        group_id = int(visible_groups[0]["id"]) if visible_groups else (
            int(ungrouped["id"])
            if ungrouped and any(int(feed["group_id"]) == int(ungrouped["id"]) for feed in all_feeds)
            else None
        )

    is_arxiv_scope = False
    scope_ai_mode = "automatic"
    scope_ai_own_mode = "automatic"
    scope_ai_priority = "normal"
    scope_summary_interval_hours = 0
    scope_summary_item_budget = 0
    if group_id is not None:
        if feed_id is not None:
            is_arxiv_scope = str(feed_lookup[feed_id]["xml_url"]).startswith(
                "plugin://arxiv/"
            )
        else:
            source_kinds = connection.execute(
                """SELECT
                       SUM(CASE WHEN xml_url LIKE 'plugin://arxiv/%' THEN 1 ELSE 0 END) AS arxiv,
                       SUM(CASE WHEN xml_url NOT LIKE 'plugin://%' THEN 1 ELSE 0 END) AS ordinary
                   FROM feeds WHERE enabled=1 AND group_id=?""",
                (group_id,),
            ).fetchone()
            is_arxiv_scope = bool(
                source_kinds and int(source_kinds["arxiv"] or 0)
                and not int(source_kinds["ordinary"] or 0)
            )
        if not is_arxiv_scope:
            group_modes = effective_group_modes(connection)
            scope_group = group_lookup[group_id]
            scope_ai_priority = str(scope_group["ai_priority"] or "normal")
            if scope_ai_priority not in {"high", "normal", "low"}:
                scope_ai_priority = "normal"
            scope_summary_interval_hours = int(scope_group["summary_interval_hours"] or 0)
            scope_summary_item_budget = int(scope_group["summary_item_budget"] or 0)
            if feed_id is None:
                scope_ai_own_mode = str(scope_group["ai_mode"] or "automatic")
                scope_ai_mode = group_modes.get(group_id, "off")
            else:
                mode_row = connection.execute(
                    """SELECT group_id,llm_enabled AS feed_llm_enabled,
                              ai_mode AS feed_ai_mode FROM feeds WHERE id=?""",
                    (feed_id,),
                ).fetchone()
                scope_ai_mode = (
                    effective_feed_mode(mode_row, group_modes) if mode_row else "off"
                )
                scope_ai_own_mode = str(feed_lookup[feed_id]["ai_mode"] or "inherit")
        else:
            scope_ai_mode = "arxiv"
            scope_ai_own_mode = "arxiv"

    items = []
    summary = None
    summary_overviews = []
    summary_sections: list[dict[str, Any]] = []
    summary_items = []
    summary_stale = False
    scope_pending_items = 0
    scope_arxiv_error = None
    scope_title = (
        feed_lookup[feed_id]["title"] if feed_id is not None
        else group_lookup[group_id]["title"] if group_id is not None else None
    )
    if group_id is not None:
        group_ids = [group_id] if feed_id is not None else group_descendant_ids(connection, group_id)
        marks = ",".join("?" for _ in group_ids)
        enabled_group_ids = set(llm_enabled_group_ids(connection))
        active_marks = ",".join(str(identifier) for identifier in sorted(enabled_group_ids)) or "NULL"
        where = "i.feed_id=?" if feed_id is not None else f"f.group_id IN ({marks})"
        parameters = [feed_id] if feed_id is not None else group_ids
        items = connection.execute(
            f"""SELECT i.*, f.title AS feed_title, eval.relevance AS relevance,
                       eval.justification AS relevance_justification
                       ,CASE WHEN f.llm_enabled=1 AND f.group_id IN ({active_marks}) THEN 1 ELSE 0 END AS ai_active
                       ,COALESCE((SELECT GROUP_CONCAT(t.name, ' · ') FROM item_tags it
                         JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id), '') AS tags
                FROM items i JOIN feeds f ON f.id=i.feed_id
                LEFT JOIN ai_evaluations eval ON eval.item_id=i.id AND eval.current=1
                WHERE f.enabled=1 AND {where}
                ORDER BY i.is_read, COALESCE(i.published_at, i.discovered_at) DESC LIMIT 1000""",
            parameters,
        ).fetchall()
        summary_feed_id = None if is_arxiv_scope else feed_id
        scope_plan = build_plan(
            connection, config,
            group_id=None if summary_feed_id is not None else group_id,
            feed_id=summary_feed_id,
        )
        scope_pending_items = int(scope_plan["ready_count"])
        if is_arxiv_scope:
            try:
                scope_pending_items = int(connection.execute(
                    """SELECT COUNT(*) FROM distillfeed_arxiv_papers ap
                       JOIN items i ON i.id=ap.item_id JOIN feeds f ON f.id=i.feed_id
                       WHERE ap.evaluation_status='pending' AND f.group_id=?""",
                    (group_id,),
                ).fetchone()[0])
                arxiv_state = {
                    str(row["key"]): str(row["value"])
                    for row in connection.execute(
                        "SELECT key,value FROM distillfeed_arxiv_state"
                    ).fetchall()
                }
                # Pending evidence is actionable even when it arrived later on
                # the same publication date as the current digest.
                summary_stale = scope_pending_items > 0
                last_arxiv_run = connection.execute(
                    """SELECT status,error FROM llm_runs
                       WHERE prompt_version LIKE 'distillfeed-arxiv-%'
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                if last_arxiv_run and last_arxiv_run["status"] == "failed":
                    scope_arxiv_error = _display_ai_error(last_arxiv_run["error"])
            except Exception:
                scope_pending_items = 0
        elif scope_ai_mode == "off":
            summary_stale = False
        summary = _active_summary_for_scope(connection, group_id, summary_feed_id)
        archived_summary_exists = False
        if summary:
            scope_kind = "feed" if summary_feed_id is not None else "group"
            scope_id = summary_feed_id if summary_feed_id is not None else group_id
            latest = connection.execute(
                """SELECT s.*,g.title AS group_title,lr.completed_at
                   FROM summaries s JOIN groups g ON g.id=s.group_id
                   JOIN llm_runs lr ON lr.id=s.llm_run_id
                   WHERE lr.status='success'
                     AND CASE WHEN s.scope_id IS NOT NULL THEN s.scope_kind
                              WHEN s.scope_feed_id IS NOT NULL THEN 'feed' ELSE 'group' END=?
                     AND COALESCE(s.scope_id,s.scope_feed_id,s.group_id)=?
                   ORDER BY lr.id DESC LIMIT 1""",
                (scope_kind, scope_id),
            ).fetchone()
            summary_ids = [int(latest["id"])] if latest else []
            if latest:
                policy_changed = bool(
                    latest["ai_job_id"] is not None
                    and str(latest["policy_hash"] or "") != str(scope_plan["policy_hash"])
                )
                if not is_arxiv_scope and scope_ai_mode != "off":
                    summary_stale = bool(scope_pending_items or policy_changed)
                summary_overviews.append(latest)
                summary_sections.extend(
                    dict(section) | {"group_title": scope_title}
                    for section in _json_list(latest["sections_json"])
                    if isinstance(section, dict)
                )
            summary_items = connection.execute(
                """SELECT si.*,i.title,i.url,i.feed_id,s.group_id,
                          f.title AS feed_title,g.title AS group_title
                    FROM summary_items si JOIN summaries s ON s.id=si.summary_id
                    JOIN items i ON i.id=si.item_id JOIN feeds f ON f.id=i.feed_id
                    JOIN groups g ON g.id=s.group_id
                    WHERE s.id=? AND si.included=1
                    ORDER BY si.importance DESC,si.rank""",
                (summary_ids[0],),
            ).fetchall()
    latest_refresh = connection.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
    latest_llm = connection.execute("SELECT * FROM llm_runs ORDER BY id DESC LIMIT 1").fetchone()
    system_notices = synchronize_issues(connection, config)
    locks = {
        row["name"]
        for row in connection.execute("SELECT name FROM job_locks WHERE expires_at >= ?", (utcnow(),)).fetchall()
    }
    return {
        "tree": tree, "groups": visible_groups, "feeds": all_feeds,
        "ungrouped_id": int(ungrouped["id"]) if ungrouped else None,
        "selected_group_id": group_id, "selected_feed_id": feed_id, "scope_title": scope_title,
        "items": items, "summary": summary, "summary_overviews": summary_overviews,
        "summary_items": summary_items, "summary_clusters": _cluster_summary_items(summary_items),
        "summary_sections": summary_sections, "merged_summary_count": len(summary_ids) if summary else 0,
        "archived_summary_exists": archived_summary_exists if group_id is not None else False,
        "is_arxiv_scope": is_arxiv_scope,
        "scope_ai_mode": scope_ai_mode,
        "scope_ai_own_mode": scope_ai_own_mode,
        "scope_ai_priority": scope_ai_priority,
        "scope_summary_interval_hours": scope_summary_interval_hours,
        "scope_summary_item_budget": scope_summary_item_budget,
        "summary_minimum_relevance": int(config.get("llm", "minimum_relevance", 70)),
        "summary_evidence_hours": int(config.get("llm", "rolling_digest_hours", 24)),
        "scope_pending_items": scope_pending_items,
        "scope_arxiv_error": scope_arxiv_error,
        "summary_stale": summary_stale,
        "system_notices": system_notices,
        "notification_count": len(system_notices) + int(
            archived_summary_exists if group_id is not None else False
        ),
        "latest_refresh": latest_refresh,
        "latest_llm": latest_llm, "locks": locks,
    }


def _flatten_config(data: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    metadata = {
        "app.mode": ("Advanced", "Application mode"),
        "app.database_path": ("Advanced", "Database file"),
        "app.working_opml_path": ("Advanced", "Working subscriptions file"),
        "app.opml_source": ("Advanced", "Subscription source imported before checks"),
        "app.host": ("Advanced", "Listen address"),
        "app.port": ("Advanced", "Listen port"),
        "app.trusted_hosts": ("Advanced", "Allowed request hostnames"),
        "app.debug": ("Advanced", "Debug logging and errors"),
        "app.log_level": ("Advanced", "Log level"),
        "app.retention_days": ("Advanced", "Entry retention (days; 0 keeps all)"),
        "ui.dark_mode": ("Appearance", "Dark mode"),
        "ui.groups_expanded_by_default": ("Appearance", "Open subscription groups by default"),
        "ui.offline_cache_enabled": ("Appearance", "Keep the latest reader pages available offline"),
        "ui.completion_notifications": ("Updates", "Browser alert when an update finishes"),
        "ui.subscription_font_size": ("Appearance", "Subscription text size"),
        "ui.item_font_size": ("Appearance", "Item text size"),
        "ui.summary_font_size": ("Appearance", "Summary text size"),
        "llm.enabled": ("AI summaries", "Enable ordinary AI summaries"),
        "app.summary_language": ("AI summaries", "Summary language"),
        "app.interest_profile": ("AI summaries", "Topics and interests"),
        "llm.provider": ("AI summaries", "AI provider"),
        "llm.api_key_env": ("AI summaries", "API key environment variable"),
        "llm.base_url": ("AI summaries", "Ollama API URL"),
        "llm.model": ("AI summaries", "AI model"),
        "llm.review_workload": ("AI summaries", "Workload per update"),
        "llm.candidate_max_age_days": ("AI summaries", "Unscored candidate age limit (days)"),
        "llm.minimum_relevance": ("AI summaries", "Include articles scoring at least (0–100)"),
        "llm.maximum_summary_items": ("AI summaries", "Maximum entries in one summary"),
        "llm.max_entries_total": ("Advanced", "Maximum items per AI request"),
        "llm.monthly_budget_usd": ("Advanced", "Local monthly AI budget (USD)"),
        "llm.rolling_digest_hours": ("AI summaries", "Ordinary summary evidence window (hours)"),
        "app.auto_summarize_after_refresh": ("AI summaries", "Update summaries after checking feeds"),
        "weather.enabled": ("Weather", "Show weather"),
        "weather.language": ("Weather", "Weather language"),
        "weather.location_name": ("Weather", "Location name"),
        "weather.latitude": ("Weather", "Latitude"),
        "weather.longitude": ("Weather", "Longitude"),
        "weather.timezone": ("Weather", "Timezone"),
        "weather.refresh_minutes": ("Weather", "Refresh interval (minutes)"),
        "app.auto_refresh_on_load": ("Updates", "Refresh on schedule while this reader is open"),
        "app.background_scheduler_enabled": ("Updates", "Run feed checks while the browser is closed"),
        "app.refresh_interval_minutes": ("Updates", "Update interval (minutes)"),
        "feeds.user_agent": ("Updates", "Feed request identity (User-Agent)"),
        "feeds.timeout_seconds": ("Advanced", "Feed request timeout (seconds)"),
        "feeds.max_response_bytes": ("Advanced", "Maximum feed download (bytes)"),
        "feeds.max_entries_per_feed_update": ("Advanced", "Maximum entries per feed check"),
        "feeds.initial_import_max_entries_per_feed": ("Advanced", "Initial entries kept per feed"),
        "feeds.initial_import_max_age_days": ("Advanced", "Initial entry age limit (days)"),
        "feeds.max_workers": ("Advanced", "Concurrent feed requests"),
        "feeds.max_workers_per_host": ("Advanced", "Concurrent requests per website"),
        "feeds.allow_private_urls": ("Advanced", "Allow feeds on private network addresses"),
        "feeds.retry_base_minutes": ("Advanced", "First retry delay (minutes)"),
        "feeds.retry_max_hours": ("Advanced", "Maximum retry delay (hours)"),
        "auth.enabled": ("Advanced", "Require application password"),
        "auth.username": ("Advanced", "Application username"),
        "auth.password_env": ("Advanced", "Password environment variable"),
        "notifications.ntfy.enabled": ("Device alerts", "Send article alerts with ntfy"),
        "notifications.ntfy.server_url": ("Device alerts", "ntfy server URL"),
        "notifications.ntfy.topic": ("Device alerts", "ntfy topic"),
        "notifications.ntfy.token_env": ("Device alerts", "Access-token environment variable"),
        "notifications.ntfy.minimum_relevance": ("Device alerts", "Default relevance threshold"),
        "notifications.ntfy.max_items_per_summary": ("Device alerts", "Maximum alerts per summary"),
        "notifications.ntfy.priority": ("Device alerts", "Device alert priority"),
        "notifications.ntfy.timeout_seconds": ("Device alerts", "Delivery timeout (seconds)"),
        "plugins.arxiv_digest_enabled": ("arXiv digest", "Enable focused arXiv digests"),
    }

    def visit(prefix: str, values: dict[str, Any]) -> None:
        for key, value in values.items():
            path = f"{prefix}.{key}" if prefix else key
            if path == "plugins.enabled":
                continue
            if path == "app.auto_baseline_initial_refresh":
                continue
            if path == "app.starter_subscriptions":
                # This affects only creation of a new database. Showing it in
                # the live Settings screen would imply it changes subscriptions.
                continue
            if path == "feeds.generated_feed_directory":
                # This is a server-administrator trust boundary. It is edited
                # in TOML, never through the remotely accessible Settings UI.
                continue
            if isinstance(value, dict):
                visit(path, value)
            else:
                if (path.startswith("llm.") or path.startswith("notifications.ntfy.")) and path not in metadata:
                    continue
                category, label = metadata.get(path, ("Advanced", path))
                fields.append(
                    {
                        "path": path, "section": prefix, "key": key, "value": value,
                        "type": type(value).__name__, "category": category, "label": label,
                        "common": path in metadata,
                    }
                )

    visit("", data)
    return fields


def _all_summaries(connection, rolling_hours: int = 24) -> list[dict[str, Any]]:
    rows = connection.execute(
        """WITH normalized AS (
                SELECT s.*,
                       CASE WHEN s.scope_id IS NOT NULL THEN s.scope_kind
                            WHEN s.scope_feed_id IS NOT NULL THEN 'feed' ELSE 'group' END AS effective_kind,
                       COALESCE(s.scope_id,s.scope_feed_id,s.group_id) AS effective_id
                FROM summaries s
            ), ranked AS (
                SELECT s.*,g.title AS group_title,f.title AS feed_scope_title,
                       CASE WHEN s.effective_kind='feed' THEN f.title ELSE g.title END AS scope_title,
                       lr.completed_at,lr.model,
                       ROW_NUMBER() OVER (
                         PARTITION BY s.effective_kind,s.effective_id ORDER BY lr.id DESC
                       ) AS position
                FROM normalized s JOIN groups g ON g.id=s.group_id
                LEFT JOIN feeds f ON f.id=s.effective_id AND s.effective_kind='feed'
                JOIN llm_runs lr ON lr.id=s.llm_run_id
                WHERE lr.status='success'
            ) SELECT * FROM ranked WHERE position=1 ORDER BY scope_title COLLATE NOCASE"""
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        items = connection.execute(
            """SELECT si.*,i.title,i.url,i.feed_id,f.title AS feed_title
                 FROM summary_items si JOIN items i ON i.id=si.item_id
                 JOIN feeds f ON f.id=i.feed_id
                 WHERE si.summary_id=? AND si.included=1
                 ORDER BY si.importance DESC,si.rank""",
            (int(row["id"]),),
        ).fetchall()
        result.append({
            "summary": dict(row), "items": items,
            "clusters": _cluster_summary_items(items),
            "sections": _json_list(row["sections_json"]),
            "merged_summary_count": 1,
        })
    return result


def _notification_data(connection, config: Config) -> dict[str, Any]:
    """Build a durable activity view from stored refresh and model-run records."""
    issues = synchronize_issues(connection, config)
    issue_history = connection.execute(
        "SELECT * FROM app_issues ORDER BY last_seen_at DESC,id DESC LIMIT 100"
    ).fetchall()
    queue = queue_dashboard(
        connection, limit=1, view="ready",
        maximum_age_days=int(config.get("llm", "candidate_max_age_days", 0)),
    )
    backlog = [
        {"group_id": row["id"], "group_title": row["title"], "item_count": row["ready"]}
        for row in queue["groups"] if int(row["ready"])
    ]
    paused_groups = connection.execute(
        """SELECT id,title,parent_id,ai_mode FROM groups WHERE ai_mode<>'automatic'
           ORDER BY title COLLATE NOCASE"""
    ).fetchall()
    paused_feeds = connection.execute(
        """SELECT f.id,f.title,f.group_id,f.ai_mode,g.title AS group_title FROM feeds f
           JOIN groups g ON g.id=f.group_id WHERE f.ai_mode IN ('manual','off')
           ORDER BY g.title COLLATE NOCASE,f.title COLLATE NOCASE"""
    ).fetchall()
    inherited_paused_feeds = []
    feed_errors = connection.execute(
        """SELECT f.id,f.title,f.xml_url,f.last_http_status,f.last_attempt_at,f.last_error,
                  g.title AS group_title
           FROM feeds f JOIN groups g ON g.id=f.group_id
           WHERE f.last_error IS NOT NULL ORDER BY f.last_attempt_at DESC,f.title COLLATE NOCASE"""
    ).fetchall()
    archived_count = 0
    refresh_runs = connection.execute(
        "SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 25"
    ).fetchall()
    llm_runs = connection.execute(
        "SELECT * FROM llm_runs ORDER BY id DESC LIMIT 25"
    ).fetchall()
    operations = connection.execute(
        "SELECT * FROM app_operations ORDER BY id DESC LIMIT 50"
    ).fetchall()
    push_deliveries = [dict(row) for row in connection.execute(
        """SELECT nd.*,i.title,i.url,f.title AS feed_title,
                  'ordinary' AS delivery_channel
           FROM notification_deliveries nd
           LEFT JOIN items i ON i.id=nd.item_id LEFT JOIN feeds f ON f.id=i.feed_id
           ORDER BY nd.id DESC LIMIT 25"""
    ).fetchall()]
    push_failure_count = int(connection.execute(
        "SELECT COUNT(*) FROM notification_deliveries WHERE status='failed'"
    ).fetchone()[0])
    if connection.execute(
        """SELECT 1 FROM sqlite_master WHERE type='table'
           AND name='distillfeed_arxiv_notifications'"""
    ).fetchone():
        arxiv_deliveries = [dict(row) for row in connection.execute(
            """SELECT delivery.*,delivery.llm_score AS relevance,
                      NULL AS minimum_relevance,'arXiv digest' AS policy_label,
                      'arxiv' AS delivery_channel,i.title,i.url,f.title AS feed_title
               FROM distillfeed_arxiv_notifications delivery
               LEFT JOIN items i ON i.id=delivery.item_id
               LEFT JOIN feeds f ON f.id=i.feed_id
               ORDER BY delivery.id DESC LIMIT 25"""
        ).fetchall()]
        push_deliveries.extend(arxiv_deliveries)
        push_deliveries.sort(
            key=lambda row: str(row.get("delivered_at") or row.get("attempted_at") or ""),
            reverse=True,
        )
        push_deliveries = push_deliveries[:25]
        push_failure_count += int(connection.execute(
            """SELECT COUNT(*) FROM distillfeed_arxiv_notifications
               WHERE status='failed'"""
        ).fetchone()[0])
    scheduler_values = {
        str(row["key"]): str(row["value"])
        for row in connection.execute(
            "SELECT key,value FROM settings WHERE key IN (?,?)",
            ("background_scheduler_next_at", "background_scheduler_last_result"),
        ).fetchall()
    }
    scheduler_last = scheduler_values.get("background_scheduler_last_result", "")
    scheduler_last_at, separator, scheduler_last_status = scheduler_last.partition("|")
    ntfy_options = config.section("notifications")["ntfy"]
    return {
        "backlog": backlog,
        "issues": issues,
        "issue_history": issue_history,
        "operations": operations,
        "backlog_count": int(queue["counts"]["ready"]),
        "inactive_count": int(queue["counts"]["inactive"]),
        "retry_count": int(queue["counts"]["retry"]),
        "paused_groups": paused_groups,
        "paused_feeds": paused_feeds,
        "inherited_paused_feeds": inherited_paused_feeds,
        "feed_errors": feed_errors,
        "archived_summary_count": archived_count,
        "refresh_runs": refresh_runs,
        "llm_runs": llm_runs,
        "push_deliveries": push_deliveries,
        "push_failure_count": push_failure_count,
        "ntfy_scope": ntfy_scope_settings(
            connection, int(ntfy_options["minimum_relevance"]),
        ),
        "scheduler_next_at": scheduler_values.get("background_scheduler_next_at"),
        "scheduler_last_at": scheduler_last_at or None,
        "scheduler_last_status": scheduler_last_status if separator else None,
        "latest_refresh": refresh_runs[0] if refresh_runs else None,
        "latest_llm": llm_runs[0] if llm_runs else None,
    }


def _cost_data(connection, days: int) -> dict[str, Any]:
    cutoff = (
        (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        if days else None
    )
    where = "WHERE started_at>=?" if cutoff else ""
    parameters: list[Any] = [cutoff] if cutoff else []
    totals = connection.execute(
        f"""SELECT COUNT(*) AS request_count,
                   COALESCE(SUM(estimated_cost_usd),0) AS total_cost,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(cached_input_tokens),0) AS cached_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(submitted_items),0) AS submitted_items
            FROM llm_runs {where}""",
        parameters,
    ).fetchone()
    daily = connection.execute(
        f"""SELECT date(COALESCE(completed_at,started_at)) AS day,
                   COUNT(*) AS request_count,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(cached_input_tokens),0) AS cached_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens
            FROM llm_runs {where}
            GROUP BY date(COALESCE(completed_at,started_at)) ORDER BY day DESC""",
        parameters,
    ).fetchall()
    models = connection.execute(
        f"""SELECT model,prompt_version,COUNT(*) AS request_count,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(cached_input_tokens),0) AS cached_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens
            FROM llm_runs {where}
            GROUP BY model,prompt_version ORDER BY cost DESC,request_count DESC""",
        parameters,
    ).fetchall()
    runs = connection.execute(
        f"SELECT * FROM llm_runs {where} ORDER BY id DESC LIMIT 500",
        parameters,
    ).fetchall()
    return {
        "cost_totals": totals,
        "cost_daily": daily,
        "cost_models": models,
        "cost_runs": runs,
        "maximum_daily_cost": max((float(row["cost"]) for row in daily), default=0.0),
        "cost_days": days,
    }


def create_app(config_path: str | None = None) -> Flask:
    config = load_config(config_path)
    ensure_runtime_directories(config)
    initialize(config.database_path)
    with connect(config.database_path) as connection:
        ensure_ungrouped(connection)
        initialize_plugins(connection, config)
    app = Flask(__name__)
    app.secret_key = flask_secret()
    app.config["RSS_CONFIG"] = config
    app.config["DISTILLFEED_MODE"] = str(config.get("app", "mode"))
    app.config["DEBUG"] = bool(config.get("app", "debug")) if config.get("app", "mode") == "development" else False
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=str(config.get("app", "mode")) == "production",
        MAX_CONTENT_LENGTH=210 * 1024 * 1024,
    )
    config_write_lock = threading.Lock()
    background_scheduler = BackgroundScheduler(config)
    app.extensions["distillfeed_scheduler"] = background_scheduler

    @app.after_request
    def security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; worker-src 'self'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if str(config.get("app", "mode")) == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        response.headers.setdefault("X-DistillFeed-Mode", str(config.get("app", "mode")))
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return response

    @app.before_request
    def validate_request_host():
        # Loopback applications are otherwise vulnerable to DNS rebinding: a
        # hostile public page can point its hostname at 127.0.0.1. Production
        # deployments retain their existing reverse-proxy hostname behavior.
        if str(config.get("app", "mode")) not in {"local", "development"}:
            return None
        supplied = request.host.split(":", 1)[0].rstrip(".").casefold()
        trusted = {
            value.strip().rstrip(".").casefold()
            for value in str(config.get("app", "trusted_hosts", "127.0.0.1,localhost")).split(",")
            if value.strip()
        }
        if supplied not in trusted:
            return ("Untrusted request host", 400)
        return None

    @app.before_request
    def authenticate():
        # Hosting health checks must not require user credentials.  This route
        # exposes no application state beyond process/database readiness.
        if request.path == "/healthz":
            return None
        auth = config.section("auth")
        if not auth.get("enabled"):
            return None
        password = config.application_password
        if not password:
            return ("Application authentication is enabled but its password environment variable is unset", 503)
        supplied = request.authorization
        valid = supplied and hmac.compare_digest(supplied.username or "", str(auth["username"])) and hmac.compare_digest(supplied.password or "", password)
        if not valid:
            return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="DistillFeed"'})
        return None

    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["safe_external_url"] = safe_external_url
    app.jinja_env.globals["display_timestamp"] = _display_timestamp
    app.jinja_env.globals["ai_task_label"] = _ai_task_label
    app.jinja_env.globals["display_ai_error"] = _display_ai_error

    @app.get("/healthz")
    def healthcheck():
        try:
            with connect(config.database_path) as connection:
                connection.execute("SELECT 1").fetchone()
            return jsonify({"status": "ok"})
        except Exception:
            LOGGER.exception("Health check failed")
            return jsonify({"status": "unavailable"}), 503

    def mutation(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, session.get("csrf_token", "")):
                abort(403, "Invalid CSRF token")
            try:
                return function(*args, **kwargs)
            except (BadRequest, TypeError, ValueError) as exc:
                return jsonify({"error": str(exc) or "Invalid request"}), 400
            except OSError:
                LOGGER.exception("A state change could not be persisted")
                return jsonify({"error": "The change could not be persisted; no data was changed"}), 500
        return wrapped

    def serialized_config_write(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with config_write_lock:
                return function(*args, **kwargs)
        return wrapped

    @app.get("/")
    def index():
        with connect(config.database_path) as connection:
            data = _page_data(
                connection, config, request.args.get("group", type=int), request.args.get("feed", type=int),
                int(config.get("llm", "rolling_digest_hours")),
            )
            # SQLite rows are immutable; installed plugins receive ordinary
            # dictionaries and can add presentation metadata without changing
            # the common item state model.
            data["items"] = [dict(item) for item in data["items"]]
            decorate_page(connection, config, data)
            data["ntfy_scope"] = ntfy_scope_settings(
                connection, int(config.get("notifications", "ntfy", {}).get(
                    "minimum_relevance", 85
                )),
            )
        core_fields = _flatten_config(config.data)
        return render_template(
            "index.html", **data, auto_refresh=config.get("app", "auto_refresh_on_load"),
            refresh_interval_minutes=config.get("app", "refresh_interval_minutes"),
            summary_language=config.get("app", "summary_language"),
            interest_profile=str(config.get("app", "interest_profile"))[:2000],
            config_fields=core_fields, ui=config.section("ui"),
            app_mode=config.get("app", "mode"),
            arxiv_available="arxiv_digest" in available_plugin_names(),
            generated_feeds_enabled=config.generated_feed_directory is not None,
        )

    @app.get("/api/csrf")
    def fresh_csrf_token():
        response = jsonify({"csrf_token": csrf_token()})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/notifications/ntfy/test")
    @mutation
    def test_ntfy_notification():
        try:
            return jsonify(send_ntfy_test(config))
        except Exception as exc:
            LOGGER.warning("ntfy test device alert failed: %s", exc)
            return jsonify({"error": str(exc)[:2000]}), 502

    @app.post("/api/plugins/<plugin_name>/actions/<action>")
    @mutation
    def plugin_settings_action(plugin_name: str, action: str):
        try:
            return jsonify(run_plugin_settings_action(config, plugin_name, action))
        except Exception as exc:
            LOGGER.warning("Plugin settings action %s/%s failed: %s", plugin_name, action, exc)
            return jsonify({"error": str(exc)[:2000]}), 502

    @app.get("/summaries")
    def summaries_page():
        with connect(config.database_path) as connection:
            summaries = _all_summaries(connection, int(config.get("llm", "rolling_digest_hours")))
        return render_template("summaries.html", summaries=summaries, ui=config.section("ui"))

    @app.get("/ai")
    def ai_policy_page():
        queue_page = max(1, request.args.get("queue_page", default=1, type=int) or 1)
        queue_page_size = 100
        queue_view = str(request.args.get("queue_view", "ready"))
        queue_query = str(request.args.get("q", ""))[:200]
        include_inactive = request.args.get("include_inactive", "0") == "1"
        selected_feed_id = request.args.get("feed_id", type=int)
        selected_group_id = request.args.get("group_id", type=int)
        if selected_feed_id is not None and selected_group_id is not None:
            abort(400, "Choose either a group or a feed scope")
        with connect(config.database_path) as connection:
            queue = queue_dashboard(
                connection, limit=queue_page_size,
                offset=(queue_page - 1) * queue_page_size,
                view=queue_view, include_inactive=include_inactive, query=queue_query,
                maximum_age_days=int(config.get("llm", "candidate_max_age_days", 0)),
                group_id=selected_group_id, feed_id=selected_feed_id,
            )
            queue_page_count = max(
                1, (int(queue["total"]) + queue_page_size - 1) // queue_page_size
            )
            if queue_page > queue_page_count:
                queue_page = queue_page_count
                queue = queue_dashboard(
                    connection, limit=queue_page_size,
                    offset=(queue_page - 1) * queue_page_size,
                    view=queue_view, include_inactive=include_inactive, query=queue_query,
                    maximum_age_days=int(config.get("llm", "candidate_max_age_days", 0)),
                    group_id=selected_group_id, feed_id=selected_feed_id,
                )
            plan = build_plan(
                connection, config, group_id=selected_group_id, feed_id=selected_feed_id,
            )
            readiness = ordinary_readiness(
                connection, config, group_id=selected_group_id,
                feed_id=selected_feed_id, plan=plan,
            )
            arxiv_ready = arxiv_readiness(connection, config, require_enabled=False)
            enabled_groups = set(llm_enabled_group_ids(connection))
            group_modes = effective_group_modes(connection)
            group_rows = connection.execute(
                "SELECT id,parent_id FROM groups"
            ).fetchall()
            parents = {
                int(row["id"]): int(row["parent_id"]) if row["parent_id"] is not None else None
                for row in group_rows
            }
            ordinary_group_ids: set[int] = set()
            for row in connection.execute(
                "SELECT DISTINCT group_id FROM feeds WHERE xml_url NOT LIKE 'plugin://%'"
            ).fetchall():
                identifier = int(row["group_id"])
                while True:
                    ordinary_group_ids.add(identifier)
                    parent = parents.get(identifier)
                    if parent is None:
                        break
                    identifier = parent
            groups = [
                dict(row) | {
                    "effective_llm": int(row["id"]) in enabled_groups,
                    "effective_mode": group_modes.get(int(row["id"]), "off"),
                }
                for row in connection.execute(
                    """SELECT g.id,g.title,g.parent_id,g.position,g.llm_enabled,g.ai_mode,g.ai_priority,
                              g.summary_interval_hours,g.summary_item_budget,
                              COUNT(q.item_id) AS pending_count
                       FROM groups g LEFT JOIN feeds f ON f.group_id=g.id
                       LEFT JOIN items i ON i.feed_id=f.id
                       LEFT JOIN ai_review_queue q ON q.item_id=i.id
                            AND q.state IN ('waiting','retry','processing')
                       WHERE NOT (g.parent_id IS NULL AND g.title='Ungrouped')
                       GROUP BY g.id ORDER BY
                         CASE g.ai_priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1
                         WHEN 'low' THEN 2 WHEN 'manual' THEN 3 ELSE 4 END,
                         g.position,g.title COLLATE NOCASE"""
                ).fetchall()
                if int(row["id"]) in ordinary_group_ids
            ]
            ai_feeds = [dict(row) for row in connection.execute(
                """SELECT f.id,f.group_id,f.title,f.ai_mode,f.llm_enabled,g.title AS group_title,
                          COUNT(q.item_id) AS pending_count
                     FROM feeds f JOIN groups g ON g.id=f.group_id LEFT JOIN items i ON i.feed_id=f.id
                     LEFT JOIN ai_review_queue q ON q.item_id=i.id
                          AND q.state IN ('waiting','retry','processing')
                     WHERE f.enabled=1 AND f.xml_url NOT LIKE 'plugin://%'
                     GROUP BY f.id ORDER BY f.group_id,f.position,f.title COLLATE NOCASE"""
            ).fetchall()]
            for feed in ai_feeds:
                parent_mode = group_modes.get(int(feed["group_id"]), "off")
                own_mode = str(feed["ai_mode"])
                feed["effective_mode"] = (
                    "off" if not feed["llm_enabled"] or own_mode == "off" or parent_mode == "off"
                    else "manual" if own_mode == "manual" or parent_mode == "manual"
                    else parent_mode if own_mode == "inherit" else "automatic"
                )
            selected_scope_title = None
            if selected_feed_id is not None:
                selected_scope_title = next(
                    (str(row["title"]) for row in ai_feeds if int(row["id"]) == selected_feed_id), None
                )
            elif selected_group_id is not None:
                selected_scope_title = next(
                    (str(row["title"]) for row in groups if int(row["id"]) == selected_group_id), None
                )
            recent_runs = connection.execute(
                "SELECT * FROM llm_runs ORDER BY id DESC LIMIT 8"
            ).fetchall()
            locks = {
                str(row["name"]) for row in connection.execute(
                    "SELECT name FROM job_locks WHERE expires_at>=?", (utcnow(),)
                ).fetchall()
            }
            arxiv_state: dict[str, Any] = {}
            arxiv_last_run = None
            if bool(config.get("plugins", "arxiv_digest_enabled", False)):
                try:
                    arxiv_state = {
                        str(row["key"]): str(row["value"])
                        for row in connection.execute(
                            "SELECT key,value FROM distillfeed_arxiv_state"
                        ).fetchall()
                    }
                    arxiv_last_run = _run_for_display(connection.execute(
                        """SELECT * FROM llm_runs
                           WHERE prompt_version LIKE 'distillfeed-arxiv-%'
                           ORDER BY id DESC LIMIT 1"""
                    ).fetchone())
                except Exception:
                    arxiv_state = {}
        workload = str(config.get("llm", "review_workload", "balanced"))
        return render_template(
            "ai.html", queue=queue, groups=groups, ai_feeds=ai_feeds,
            recent_runs=recent_runs, locks=locks,
            ui=config.section("ui"), app=config.section("app"), llm=config.section("llm"),
            workload=workload,
            workload_limit={"focused": 80, "balanced": 200, "wide": 500}.get(workload, 200),
            queue_page=queue_page,
            queue_page_count=queue_page_count,
            queue_view=queue_view, queue_query=queue_query,
            include_inactive=include_inactive, plan=plan,
            readiness=readiness, arxiv_readiness=arxiv_ready,
            selected_feed_id=selected_feed_id, selected_group_id=selected_group_id,
            selected_scope_title=selected_scope_title,
            arxiv_available="arxiv_digest" in available_plugin_names(),
            arxiv_enabled=bool(config.get("plugins", "arxiv_digest_enabled", False)),
            arxiv_state=arxiv_state,
            arxiv_last_run=arxiv_last_run,
            arxiv_fields=[
                field for field in plugin_settings_fields(config)
                if str(field.get("path", "")).startswith("plugin.arxiv_digest.")
            ],
        )

    @app.get("/api/ai/plan")
    def ai_plan_api():
        feed_id = request.args.get("feed_id", type=int)
        group_id = request.args.get("group_id", type=int)
        if feed_id is not None and group_id is not None:
            return jsonify({"error": "Choose either a group or a feed scope"}), 400
        with connect(config.database_path) as connection:
            return jsonify(build_plan(connection, config, group_id=group_id, feed_id=feed_id))

    @app.get("/api/ai/readiness")
    def ai_readiness_api():
        feed_id = request.args.get("feed_id", type=int)
        group_id = request.args.get("group_id", type=int)
        if feed_id is not None and group_id is not None:
            return jsonify({"error": "Choose either a group or a feed scope"}), 400
        with connect(config.database_path) as connection:
            return jsonify({
                "ordinary": ordinary_readiness(
                    connection, config, group_id=group_id, feed_id=feed_id,
                ),
                "arxiv": arxiv_readiness(
                    connection, config, require_enabled=False,
                ),
            })

    @app.get("/api/ai/queue")
    def ai_queue_api():
        page = max(1, request.args.get("page", default=1, type=int) or 1)
        page_size = max(1, min(200, request.args.get("page_size", default=100, type=int) or 100))
        try:
            with connect(config.database_path) as connection:
                return jsonify(queue_dashboard(
                    connection, limit=page_size, offset=(page - 1) * page_size,
                    view=str(request.args.get("view", "ready")),
                    include_inactive=request.args.get("include_inactive", "0") == "1",
                    query=str(request.args.get("q", ""))[:200],
                    maximum_age_days=int(config.get("llm", "candidate_max_age_days", 0)),
                    group_id=request.args.get("group_id", type=int),
                    feed_id=request.args.get("feed_id", type=int),
                ))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/items/ai-disposition")
    @mutation
    def update_ai_item_disposition():
        payload = request.get_json(force=True)
        raw_ids = payload.get("item_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "Choose at least one item"}), 400
        disposition = str(payload.get("disposition", ""))
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            changed = set_item_disposition(connection, (int(value) for value in raw_ids), disposition)
        return jsonify({"status": "ok", "changed": changed, "disposition": disposition})

    @app.get("/history")
    def history_page():
        with connect(config.database_path) as connection:
            rows = connection.execute(
                """SELECT s.*, g.title AS group_title, f.title AS scope_feed_title,
                          lr.completed_at, lr.model,
                          lr.estimated_cost_usd, lr.submitted_items
                   FROM summaries s JOIN groups g ON g.id=s.group_id
                   LEFT JOIN feeds f ON f.id=s.scope_feed_id
                   JOIN llm_runs lr ON lr.id=s.llm_run_id WHERE lr.status='success'
                   ORDER BY lr.id DESC, g.title COLLATE NOCASE LIMIT 500"""
            ).fetchall()
        summaries = [dict(row) | {"sections": _json_list(row["sections_json"])} for row in rows]
        return render_template("history.html", summaries=summaries, ui=config.section("ui"))

    @app.get("/costs")
    def costs_page():
        days = request.args.get("days", default=30, type=int)
        if days not in {0, 7, 30, 90, 365}:
            days = 30
        with connect(config.database_path) as connection:
            data = _cost_data(connection, days)
        return render_template("costs.html", **data, ui=config.section("ui"))

    @app.get("/health")
    def health_page():
        with connect(config.database_path) as connection:
            feeds = connection.execute(
                """SELECT f.*, g.title AS group_title, COUNT(i.id) AS item_count,
                          SUM(CASE WHEN i.is_read=0 THEN 1 ELSE 0 END) AS unread_count
                   FROM feeds f JOIN groups g ON g.id=f.group_id LEFT JOIN items i ON i.feed_id=f.id
                   GROUP BY f.id ORDER BY (f.last_error IS NOT NULL) DESC, f.title COLLATE NOCASE"""
            ).fetchall()
        return render_template(
            "health.html", feeds=feeds, ui=config.section("ui"),
            feed_user_agent=config.get("feeds", "user_agent"),
        )

    @app.get("/notifications")
    def notifications_page():
        with connect(config.database_path) as connection:
            data = _notification_data(connection, config)
        return render_template(
            "notifications.html", **data, ui=config.section("ui"),
            ntfy=config.section("notifications")["ntfy"],
            app_mode=config.get("app", "mode"),
            background_scheduler_enabled=config.get("app", "background_scheduler_enabled"),
            auto_summarize_after_refresh=config.get("app", "auto_summarize_after_refresh"),
            refresh_interval_minutes=config.get("app", "refresh_interval_minutes"),
        )

    @app.get("/saved")
    def saved_page():
        view = request.args.get("view", "read-later")
        tag = request.args.get("tag", "").strip()
        with connect(config.database_path) as connection:
            tags = connection.execute(
                """SELECT t.id, t.name, COUNT(it.item_id) AS item_count FROM tags t
                   LEFT JOIN item_tags it ON it.tag_id=t.id GROUP BY t.id
                   ORDER BY t.name COLLATE NOCASE"""
            ).fetchall()
            conditions: list[str] = []
            parameters: list[Any] = []
            if tag:
                conditions.append("EXISTS (SELECT 1 FROM item_tags chosen JOIN tags t ON t.id=chosen.tag_id WHERE chosen.item_id=i.id AND t.name=?)")
                parameters.append(tag)
                title = f"Tag · {tag}"
            elif view == "favorites":
                conditions.append("i.is_starred=1")
                title = "Favorites"
            elif view == "tags":
                conditions.append("EXISTS (SELECT 1 FROM item_tags tagged WHERE tagged.item_id=i.id)")
                title = "Tagged items"
            else:
                conditions.append("i.is_read_later=1")
                view = "read-later"
                title = "Read later"
            where = " AND ".join(conditions)
            items = connection.execute(
                f"""SELECT i.*, f.title AS feed_title, g.title AS group_title,
                           COALESCE((SELECT GROUP_CONCAT(t.name, ' · ') FROM item_tags it
                             JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id), '') AS tags
                    FROM items i JOIN feeds f ON f.id=i.feed_id JOIN groups g ON g.id=f.group_id
                    WHERE {where} ORDER BY COALESCE(i.published_at,i.discovered_at) DESC LIMIT 2000""",
                parameters,
            ).fetchall()
        return render_template(
            "saved.html", items=items, tags=tags, selected_view=view,
            selected_tag=tag, title=title, ui=config.section("ui"),
        )

    @app.get("/api/backup")
    def download_backup():
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return send_file(
            build_backup(config), mimetype="application/zip", as_attachment=True,
            download_name=f"distillfeed-backup-{timestamp}.zip",
        )

    @app.get("/api/export-opml")
    def download_opml():
        with connect(config.database_path) as connection:
            content = serialize_opml(build_tree_from_database(connection))
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return send_file(
            BytesIO(content), mimetype="text/x-opml", as_attachment=True,
            download_name=f"distillfeed-subscriptions-{timestamp}.opml",
        )

    @app.post("/api/restore")
    @mutation
    def restore_route():
        uploaded = request.files.get("backup")
        if not uploaded:
            return jsonify({"error": "Choose a DistillFeed backup file"}), 400
        owner = secrets.token_urlsafe(24)
        lock_connection = connect(config.database_path)
        if not acquire_lock(
            lock_connection, "maintenance", owner, ttl_minutes=30, exclusive=True
        ):
            lock_connection.close()
            return jsonify({"error": "Wait for active refresh, summary, or restore work to finish"}), 409
        try:
            safety = restore_backup(config, uploaded.read())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            release_lock(lock_connection, "maintenance", owner)
            lock_connection.close()
        return jsonify({"status": "ok", "safety_backup": str(safety)})

    @app.get("/api/weather")
    def weather():
        return jsonify(get_weather(config))

    @app.get("/api/status")
    def status():
        requested_operation = str(request.args.get("operation_id", ""))[:200] or None
        with connect(config.database_path) as connection:
            operation = operation_for_display(connection, requested_operation)
            if requested_operation and operation is None:
                return jsonify({"error": "Operation not found"}), 404
            refresh = connection.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
            llm = connection.execute("SELECT * FROM llm_runs ORDER BY id DESC LIMIT 1").fetchone()
            arxiv_run = connection.execute(
                """SELECT * FROM llm_runs WHERE prompt_version LIKE 'distillfeed-arxiv-%'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            ai_job = connection.execute(
                "SELECT * FROM ai_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            ai_result = None
            if ai_job:
                ai_result_row = connection.execute(
                    """SELECT COUNT(DISTINCT s.id) AS summaries,
                              COUNT(si.item_id) AS included
                       FROM summaries s
                       LEFT JOIN summary_items si
                         ON si.summary_id=s.id AND si.included=1
                       WHERE s.ai_job_id=?""",
                    (int(ai_job["id"]),),
                ).fetchone()
                ai_result = dict(ai_result_row)
            jobs = [
                dict(row) for row in connection.execute(
                    """SELECT name,cancel_requested,acquired_at FROM job_locks
                       WHERE expires_at>=? ORDER BY acquired_at""",
                    (utcnow(),),
                ).fetchall()
            ]
            locks = [str(job["name"]) for job in jobs]
            arxiv_state = None
            if connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='distillfeed_arxiv_state'"""
            ).fetchone():
                state = {
                    str(row["key"]): str(row["value"])
                    for row in connection.execute(
                        "SELECT key,value FROM distillfeed_arxiv_state"
                    ).fetchall()
                }
                pending_items = int(connection.execute(
                    """SELECT COUNT(*) FROM distillfeed_arxiv_papers
                       WHERE evaluation_status='pending'"""
                ).fetchone()[0])
                arxiv_state = {
                    "pending_announcement": state.get("pending_announcement", ""),
                    "last_digest_announcement": state.get("last_digest_announcement", ""),
                    "last_api_error": state.get("last_api_error", ""),
                    "pending_items": pending_items,
                }
            issues = synchronize_issues(connection, config)
        if "llm-summary" in locks:
            llm_lock_started = next(
                (str(job["acquired_at"]) for job in jobs if job["name"] == "llm-summary"),
                "",
            )
            if (
                ai_job and ai_job["status"] == "running"
                and str(ai_job["started_at"]) >= llm_lock_started
            ):
                phase = "composing" if ai_job["stage"] == "composition" else "evaluating"
            elif llm and str(llm["prompt_version"] or "").startswith("distillfeed-arxiv-"):
                phase = "arxiv-digest"
            else:
                phase = "summarizing"
        elif "feed-refresh" in locks:
            phase = (
                "summarizing" if refresh and refresh["status"] != "running"
                and config.get("app", "auto_summarize_after_refresh") and refresh["new_items"]
                else "refreshing"
            )
        else:
            phase = "idle"
        return jsonify({
            "refresh": dict(refresh) if refresh else None,
            "llm": _run_for_display(llm),
            "arxiv_run": _run_for_display(arxiv_run),
            "arxiv": arxiv_state,
            "ai_job": dict(ai_job) if ai_job else None,
            "ai_result": ai_result,
            "locks": locks,
            "jobs": jobs,
            "cancel_requested": any(bool(job["cancel_requested"]) for job in jobs),
            "phase": str(operation["phase"]) if operation and operation["active"] else phase,
            "operation": operation,
            "issues": issues,
        })

    @app.post("/api/issues/<int:issue_id>/acknowledge")
    @mutation
    def acknowledge_issue_route(issue_id: int):
        with connect(config.database_path) as connection:
            changed = acknowledge_issue(connection, issue_id)
        if not changed:
            return jsonify({"error": "Active notice not found"}), 404
        return jsonify({"status": "acknowledged", "issue_id": issue_id})

    @app.post("/api/jobs/cancel")
    @mutation
    def cancel_jobs():
        payload = request.get_json(silent=True) or {}
        requested = payload.get("jobs", ["feed-refresh", "llm-summary", "summary-update"])
        if not isinstance(requested, list) or not requested:
            raise ValueError("jobs must be a non-empty list")
        allowed = {"feed-refresh", "llm-summary", "summary-update"}
        names = tuple(dict.fromkeys(str(name) for name in requested))
        if any(name not in allowed for name in names):
            raise ValueError("Only feed checks and AI summary updates can be stopped")
        with connect(config.database_path) as connection:
            affected = request_job_cancellation(connection, names)
        if not affected:
            return jsonify({"status": "idle", "message": "No cancellable job is running"}), 409
        return jsonify({
            "status": "stopping", "jobs": affected,
            "message": "Stopping safely after the current network or AI request",
        }), 202

    @app.post("/api/refresh")
    @mutation
    def refresh():
        payload = request.get_json(silent=True) or {}
        feed_id = int(payload["feed_id"]) if payload.get("feed_id") is not None else None
        group_id = int(payload["group_id"]) if payload.get("group_id") is not None else None
        if feed_id is not None and group_id is not None:
            raise ValueError("Choose either a group or a feed refresh scope")
        force = _strict_bool(payload.get("force", False))
        automatic = _strict_bool(payload.get("automatic", False))
        if _strict_bool(payload.get("summarize_after", False)):
            return jsonify({
                "error": "Feed checking never uses AI. Use Update summaries instead."
            }), 400
        if feed_id is not None or group_id is not None:
            with connect(config.database_path) as connection:
                if feed_id is not None and not connection.execute(
                    "SELECT 1 FROM feeds WHERE id=?", (feed_id,)
                ).fetchone():
                    return jsonify({"error": "Feed not found"}), 404
                if group_id is not None and not connection.execute(
                    "SELECT 1 FROM groups WHERE id=?", (group_id,)
                ).fetchone():
                    return jsonify({"error": "Group not found"}), 404
        owner = secrets.token_hex(16)
        with connect(config.database_path) as connection:
            reserved = acquire_lock(
                connection, "feed-refresh", owner, ttl_minutes=120, exclusive=True,
            )
            operation = create_operation(
                connection, kind="refresh",
                trigger="automatic" if automatic else "browser",
                lock_name="feed-refresh", lock_owner=owner,
                scope_kind="feed" if feed_id is not None else "group" if group_id is not None else "all",
                scope_id=feed_id if feed_id is not None else group_id,
            ) if reserved else None
        if not reserved:
            return jsonify({"error": "An update is already running"}), 409
        try:
            start_thread(
                lambda: _finish_reserved_operation(
                    config, "feed-refresh", owner, str(operation["operation_id"]), "refresh",
                    lambda: run_refresh(
                        config, feed_id=feed_id, group_id=group_id, force=force,
                        automatic=automatic, summarize_after=False,
                        _reserved_owner=owner,
                    ),
                ),
                name="feed-refresh",
            )
        except Exception:
            with connect(config.database_path) as connection:
                fail_operation(connection, str(operation["operation_id"]), "The refresh worker could not start")
                release_lock(connection, "feed-refresh", owner)
            raise
        return jsonify({"status": "started", **operation}), 202

    @app.post("/api/summarize")
    @mutation
    def summarize_route():
        payload = request.get_json(silent=True) or {}
        feed_id = int(payload["feed_id"]) if payload.get("feed_id") is not None else None
        group_id = int(payload["group_id"]) if payload.get("group_id") is not None else None
        if feed_id is not None and group_id is not None:
            raise ValueError("Choose either a group or a feed summary scope")
        if feed_id is not None or group_id is not None:
            with connect(config.database_path) as connection:
                if feed_id is not None and not connection.execute(
                    "SELECT 1 FROM feeds WHERE id=?", (feed_id,)
                ).fetchone():
                    return jsonify({"error": "Feed not found"}), 404
                if group_id is not None and not connection.execute(
                    "SELECT 1 FROM groups WHERE id=?", (group_id,)
                ).fetchone():
                    return jsonify({"error": "Group not found"}), 404
        with connect(config.database_path) as connection:
            ready = ordinary_readiness(
                connection, config, group_id=group_id, feed_id=feed_id,
            )
        if not ready["can_start"]:
            result = blocked_result(ready)
            return jsonify({"error": result["message"], **result}), 409
        owner = secrets.token_hex(16)
        with connect(config.database_path) as connection:
            reserved = acquire_lock(
                connection, "summary-update", owner, ttl_minutes=180, exclusive=True,
            )
            operation = create_operation(
                connection, kind="summary", trigger="browser",
                lock_name="summary-update", lock_owner=owner,
                scope_kind="feed" if feed_id is not None else "group" if group_id is not None else "all",
                scope_id=feed_id if feed_id is not None else group_id,
            ) if reserved else None
        if not reserved:
            return jsonify({"error": "An update is already running"}), 409
        try:
            start_thread(
                lambda: _finish_reserved_operation(
                    config, "summary-update", owner, str(operation["operation_id"]), "summary",
                    lambda: run_update_summaries(
                        config, automatic=False, group_id=group_id, feed_id=feed_id,
                        include_plugins=False, include_generic=True,
                        _reserved_owner=owner,
                    ),
                ),
                name="llm-summary",
            )
        except Exception:
            with connect(config.database_path) as connection:
                fail_operation(connection, str(operation["operation_id"]), "The summary worker could not start")
                release_lock(connection, "summary-update", owner)
            raise
        return jsonify({"status": "started", **operation}), 202

    @app.post("/api/arxiv/update")
    @mutation
    def update_arxiv_daily_digest():
        if not bool(config.get("plugins", "arxiv_digest_enabled", False)):
            return jsonify({"error": "The arXiv daily digest is disabled"}), 409
        with connect(config.database_path) as connection:
            group = connection.execute(
                """SELECT g.id FROM groups g JOIN feeds f ON f.group_id=g.id
                   WHERE f.enabled=1 AND f.xml_url LIKE 'plugin://arxiv/%'
                   ORDER BY g.id LIMIT 1"""
            ).fetchone()
        if not group:
            return jsonify({"error": "The arXiv daily digest is not initialized"}), 409
        group_id = int(group["id"])
        with connect(config.database_path) as connection:
            ready = arxiv_readiness(connection, config, require_enabled=True)
        if not ready["can_start"]:
            result = blocked_result(ready)
            return jsonify({"error": result["message"], **result}), 409
        owner = secrets.token_hex(16)
        with connect(config.database_path) as connection:
            reserved = acquire_lock(
                connection, "summary-update", owner, ttl_minutes=180, exclusive=True,
            )
            operation = create_operation(
                connection, kind="arxiv", trigger="browser",
                lock_name="summary-update", lock_owner=owner,
                scope_kind="group", scope_id=group_id,
            ) if reserved else None
        if not reserved:
            return jsonify({"error": "An update is already running"}), 409
        try:
            start_thread(
                lambda: _finish_reserved_operation(
                    config, "summary-update", owner, str(operation["operation_id"]), "arxiv",
                    lambda: run_update_summaries(
                        config, automatic=False, group_id=group_id,
                        include_plugins=True, include_generic=False,
                        _reserved_owner=owner,
                    ),
                ),
                name="arxiv-daily-digest",
            )
        except Exception:
            with connect(config.database_path) as connection:
                fail_operation(connection, str(operation["operation_id"]), "The arXiv worker could not start")
                release_lock(connection, "summary-update", owner)
            raise
        return jsonify({"status": "started", "group_id": group_id, **operation}), 202

    @app.post("/api/items/<int:item_id>/read")
    @mutation
    def mark_read(item_id: int):
        payload = request.get_json(silent=True) or {}
        with connect(config.database_path) as connection:
            cursor = connection.execute(
                "UPDATE items SET is_read=? WHERE id=?",
                (int(_required_bool(payload, "read")), item_id),
            )
        if not cursor.rowcount:
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"status": "ok", "changed": cursor.rowcount})

    @app.post("/api/items/<int:item_id>/star")
    @mutation
    def star(item_id: int):
        payload = request.get_json(silent=True) or {}
        with connect(config.database_path) as connection:
            cursor = connection.execute(
                "UPDATE items SET is_starred=? WHERE id=?",
                (int(_required_bool(payload, "starred")), item_id),
            )
        if not cursor.rowcount:
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"status": "ok", "changed": cursor.rowcount})

    @app.post("/api/items/<int:item_id>/read-later")
    @mutation
    def read_later(item_id: int):
        payload = request.get_json(silent=True) or {}
        value = int(_required_bool(payload, "read_later"))
        with connect(config.database_path) as connection:
            cursor = connection.execute("UPDATE items SET is_read_later=? WHERE id=?", (value, item_id))
        if not cursor.rowcount:
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"status": "ok", "changed": cursor.rowcount})

    @app.post("/api/items/bulk-star")
    @mutation
    def bulk_star():
        payload = request.get_json(force=True)
        identifiers = _requested_item_ids(payload)
        value = int(_required_bool(payload, "starred"))
        if not identifiers:
            return jsonify({"status": "ok", "changed": 0, "matched": 0, "item_ids": []})
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            matched_ids = _existing_item_ids(connection, identifiers)
            marks = ",".join("?" for _ in matched_ids)
            cursor = connection.execute(
                f"UPDATE items SET is_starred=? WHERE is_starred<>? AND id IN ({marks})",
                [value, value, *matched_ids],
            ) if matched_ids else None
        return jsonify({
            "status": "ok", "changed": max(cursor.rowcount, 0) if cursor else 0,
            "matched": len(matched_ids), "item_ids": matched_ids,
        })

    @app.post("/api/items/bulk-read")
    @mutation
    def bulk_mark_read():
        payload = request.get_json(force=True)
        mode = str(payload.get("mode", "selected"))
        value = int(_required_bool(payload, "read"))
        if mode == "scope":
            has_feed = payload.get("feed_id") is not None
            has_group = payload.get("group_id") is not None
            if has_feed == has_group:
                raise ValueError("A scope action requires exactly one feed_id or group_id")
            with connect(config.database_path) as connection, transaction(connection, immediate=True):
                if has_feed:
                    if not connection.execute(
                        "SELECT 1 FROM feeds WHERE id=?", (int(payload["feed_id"]),)
                    ).fetchone():
                        return jsonify({"error": "Feed not found"}), 404
                    cursor = connection.execute(
                        "UPDATE items SET is_read=? WHERE feed_id=? AND is_read<>?",
                        (value, int(payload["feed_id"]), value),
                    )
                    matched = int(connection.execute(
                        "SELECT COUNT(*) FROM items WHERE feed_id=?", (int(payload["feed_id"]),)
                    ).fetchone()[0])
                else:
                    group_ids = group_descendant_ids(connection, int(payload["group_id"]))
                    if not group_ids:
                        return jsonify({"error": "Group not found"}), 404
                    marks = ",".join("?" for _ in group_ids)
                    cursor = connection.execute(
                        f"""UPDATE items SET is_read=? WHERE is_read<>? AND feed_id IN (
                            SELECT id FROM feeds WHERE group_id IN ({marks})
                        )""",
                        [value, value, *group_ids],
                    )
                    matched = int(connection.execute(
                        f"""SELECT COUNT(*) FROM items WHERE feed_id IN (
                            SELECT id FROM feeds WHERE group_id IN ({marks})
                        )""",
                        group_ids,
                    ).fetchone()[0])
            return jsonify({
                "status": "ok", "read": bool(value),
                "changed": max(cursor.rowcount, 0), "matched": matched,
            })
        if mode != "selected":
            return jsonify({"error": "mode must be selected or scope"}), 400
        identifiers = _requested_item_ids(payload)
        if not identifiers:
            return jsonify({
                "status": "ok", "read": bool(value), "changed": 0,
                "matched": 0, "item_ids": [],
            })
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            matched_ids = _existing_item_ids(connection, identifiers)
            if matched_ids:
                matched_marks = ",".join("?" for _ in matched_ids)
                cursor = connection.execute(
                    f"UPDATE items SET is_read=? WHERE is_read<>? AND id IN ({matched_marks})",
                    [value, value, *matched_ids],
                )
                changed = max(cursor.rowcount, 0)
            else:
                changed = 0
        return jsonify({
            "status": "ok", "read": bool(value), "changed": changed,
            "matched": len(matched_ids), "item_ids": matched_ids,
        })

    @app.post("/api/items/bulk-read-later")
    @mutation
    def bulk_read_later():
        payload = request.get_json(force=True)
        identifiers = _requested_item_ids(payload)
        value = int(_required_bool(payload, "read_later"))
        if not identifiers:
            return jsonify({"status": "ok", "changed": 0, "matched": 0, "item_ids": []})
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            matched_ids = _existing_item_ids(connection, identifiers)
            marks = ",".join("?" for _ in matched_ids)
            cursor = connection.execute(
                f"UPDATE items SET is_read_later=? WHERE is_read_later<>? AND id IN ({marks})",
                [value, value, *matched_ids],
            ) if matched_ids else None
        return jsonify({
            "status": "ok", "changed": max(cursor.rowcount, 0) if cursor else 0,
            "matched": len(matched_ids), "item_ids": matched_ids,
        })

    @app.post("/api/items/bulk-tags")
    @mutation
    def bulk_tags():
        payload = request.get_json(force=True)
        identifiers = _requested_item_ids(payload)
        if "tags" not in payload:
            raise ValueError("tags must be explicitly provided as a list")
        raw_names = payload["tags"]
        if not isinstance(raw_names, list):
            raise ValueError("tags must be a list")
        names_by_key: dict[str, str] = {}
        for raw_name in raw_names:
            name = str(raw_name).strip()[:80]
            if name:
                names_by_key.setdefault(name.casefold(), name)
        names = sorted(names_by_key.values(), key=str.casefold)[:20]
        if not identifiers:
            return jsonify({"error": "Select at least one item"}), 400
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            identifiers = _existing_item_ids(connection, identifiers)
            if not identifiers:
                return jsonify({"error": "The selected items no longer exist"}), 404
            for name in names:
                connection.execute("INSERT OR IGNORE INTO tags(name,created_at) VALUES(?,?)", (name, utcnow()))
            marks = ",".join("?" for _ in identifiers)
            connection.execute(f"DELETE FROM item_tags WHERE item_id IN ({marks})", identifiers)
            if names:
                tag_marks = ",".join("?" for _ in names)
                tag_ids = [row["id"] for row in connection.execute(
                    f"SELECT id FROM tags WHERE name IN ({tag_marks})", names
                ).fetchall()]
                connection.executemany(
                    "INSERT OR IGNORE INTO item_tags(item_id,tag_id) VALUES(?,?)",
                    [(item_id, tag_id) for item_id in identifiers for tag_id in tag_ids],
                )
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM item_tags WHERE item_tags.tag_id=tags.id)"
            )
        return jsonify({
            "status": "ok", "changed": len(identifiers), "matched": len(identifiers),
            "item_ids": identifiers, "tags": names,
        })

    @app.post("/api/groups")
    @mutation
    def add_group():
        payload = request.get_json(force=True)
        title = str(payload.get("title", "")).strip()
        if not title or len(title) > 200:
            return jsonify({"error": "A group title of at most 200 characters is required"}), 400
        try:
            parent_id = int(payload["parent_id"]) if payload.get("parent_id") else None
            with connect(config.database_path) as connection, transaction(connection, immediate=True):
                if parent_id is not None and not connection.execute(
                    "SELECT 1 FROM groups WHERE id=?", (parent_id,)
                ).fetchone():
                    return jsonify({"error": "Choose an existing parent group"}), 400
                duplicate = connection.execute(
                    """SELECT id FROM groups WHERE title=? COLLATE NOCASE
                       AND ((parent_id IS NULL AND ? IS NULL) OR parent_id=?)""",
                    (title, parent_id, parent_id),
                ).fetchone()
                if duplicate:
                    return jsonify({"error": "A group with this name already exists here"}), 409
                group_enabled = _strict_bool(payload.get("llm_enabled", True))
                group_id = int(connection.execute(
                    """INSERT INTO groups(
                           parent_id,title,position,llm_enabled,ai_mode,ai_priority,created_at
                       ) VALUES(?,?,9999,?,?,?,?)""",
                    (
                        parent_id, title, int(group_enabled),
                        "automatic" if group_enabled else "off",
                        "normal" if group_enabled else "off", utcnow(),
                    ),
                ).lastrowid)
                write_database_opml(connection, config.working_opml_path)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "ok", "group_id": group_id}), 201

    @app.post("/api/subscriptions/move")
    @mutation
    def move_subscription():
        payload = request.get_json(force=True)
        kind = str(payload.get("kind", ""))
        if kind not in {"group", "feed"}:
            return jsonify({"error": "Subscription kind must be group or feed"}), 400
        identifier = int(payload.get("id", 0))
        if identifier <= 0:
            return jsonify({"error": "Choose an existing subscription"}), 400
        raw_parent = payload.get("parent_id")
        target_parent = None if raw_parent in {None, ""} else int(raw_parent)
        requested_position = int(payload.get("position", 0))
        if requested_position < 0:
            return jsonify({"error": "Subscription position cannot be negative"}), 400

        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            ungrouped_id = ensure_ungrouped(connection)
            if target_parent is not None:
                target = connection.execute(
                    "SELECT id FROM groups WHERE id=? AND id<>?", (target_parent, ungrouped_id)
                ).fetchone()
                if not target:
                    return jsonify({"error": "Choose an existing visible parent group"}), 400
            if kind == "group":
                row = connection.execute(
                    "SELECT id,parent_id,title FROM groups WHERE id=? AND id<>?",
                    (identifier, ungrouped_id),
                ).fetchone()
                if not row:
                    return jsonify({"error": "Group not found"}), 404
                source_parent = int(row["parent_id"]) if row["parent_id"] is not None else None
                if target_parent == identifier:
                    return jsonify({"error": "A group cannot contain itself"}), 409
                if target_parent is not None and target_parent in group_descendant_ids(connection, identifier):
                    return jsonify({"error": "A group cannot move inside one of its descendants"}), 409
                duplicate = connection.execute(
                    """SELECT id FROM groups WHERE id<>? AND title=? COLLATE NOCASE
                       AND ((parent_id IS NULL AND ? IS NULL) OR parent_id=?)""",
                    (identifier, row["title"], target_parent, target_parent),
                ).fetchone()
                if duplicate:
                    return jsonify({"error": "A group with this name already exists there"}), 409
            else:
                row = connection.execute(
                    "SELECT id,group_id FROM feeds WHERE id=?", (identifier,)
                ).fetchone()
                if not row:
                    return jsonify({"error": "Feed not found"}), 404
                source_parent = (
                    None if int(row["group_id"]) == ungrouped_id else int(row["group_id"])
                )

            source_entries = _visible_subscription_entries(connection, source_parent)
            moving = (kind, identifier)
            if moving not in source_entries:
                return jsonify({"error": "Subscription hierarchy changed; reload and try again"}), 409
            source_entries.remove(moving)
            if source_parent == target_parent:
                destination_entries = source_entries
            else:
                destination_entries = _visible_subscription_entries(connection, target_parent)
            position = min(requested_position, len(destination_entries))
            destination_entries.insert(position, moving)

            if kind == "group":
                connection.execute(
                    "UPDATE groups SET parent_id=? WHERE id=?", (target_parent, identifier)
                )
            else:
                connection.execute(
                    "UPDATE feeds SET group_id=? WHERE id=?",
                    (target_parent if target_parent is not None else ungrouped_id, identifier),
                )
            if source_parent != target_parent:
                _store_subscription_order(connection, source_entries)
            _store_subscription_order(connection, destination_entries)
            write_database_opml(connection, config.working_opml_path)
        return jsonify({
            "status": "ok", "kind": kind, "id": identifier,
            "parent_id": target_parent, "position": position,
        })

    @app.patch("/api/groups/<int:group_id>")
    @mutation
    def update_group(group_id: int):
        payload = request.get_json(force=True)
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            row = connection.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
            if not row:
                return jsonify({"error": "Group not found"}), 404
            if row["parent_id"] is None and row["title"] == "Ungrouped":
                return jsonify({"error": "The internal top-level feed container cannot be edited"}), 409
            title = str(payload.get("title", row["title"])).strip()
            if not title or len(title) > 200:
                return jsonify({"error": "Invalid group title"}), 400
            duplicate = connection.execute(
                """SELECT id FROM groups WHERE id<>? AND title=? COLLATE NOCASE
                   AND ((parent_id IS NULL AND ? IS NULL) OR parent_id=?)""",
                (group_id, title, row["parent_id"], row["parent_id"]),
            ).fetchone()
            if duplicate:
                return jsonify({"error": "A group with this name already exists here"}), 409
            llm_enabled = int(_strict_bool(payload.get("llm_enabled", bool(row["llm_enabled"]))))
            interval = int(payload.get("summary_interval_hours", row["summary_interval_hours"]))
            budget = int(payload.get("summary_item_budget", row["summary_item_budget"]))
            priority = str(payload.get("ai_priority", row["ai_priority"])).strip().casefold()
            mode = str(payload.get("ai_mode", row["ai_mode"])).strip().casefold()
            if not 0 <= interval <= 8760:
                return jsonify({"error": "Summary interval must be between 0 and 8760 hours"}), 400
            if not 0 <= budget <= 1000:
                return jsonify({"error": "Summary item budget must be between 0 and 1000"}), 400
            if mode not in {"automatic", "manual", "off"}:
                return jsonify({"error": "Summary mode must be Automatic, Only on request, or Excluded"}), 400
            if priority not in {"high", "normal", "low", "manual", "off"}:
                return jsonify({"error": "AI priority must be High, Normal, or Low"}), 400
            # Translate legacy writes while keeping one canonical mode.
            if "ai_mode" not in payload and "llm_enabled" in payload:
                mode = "automatic" if llm_enabled else "off"
            if "ai_mode" not in payload and "ai_priority" in payload:
                mode = priority if priority in {"manual", "off"} else "automatic"
            if mode == "off":
                llm_enabled = 0
                legacy_priority = "off"
            elif mode == "manual":
                llm_enabled = 1
                legacy_priority = "manual"
            else:
                llm_enabled = 1
                legacy_priority = priority if priority in {"high", "normal", "low"} else "normal"
            connection.execute(
                """UPDATE groups SET title=?,llm_enabled=?,summary_interval_hours=?,summary_item_budget=?,
                       ai_mode=?,ai_priority=?
                   WHERE id=?""",
                (title, llm_enabled, interval, budget, mode, legacy_priority, group_id),
            )
            write_database_opml(connection, config.working_opml_path)
        return jsonify({"status": "ok"})

    @app.delete("/api/groups/<int:group_id>")
    @mutation
    def delete_group(group_id: int):
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            if group_id == ensure_ungrouped(connection):
                return jsonify({"error": "The internal top-level feed container cannot be deleted"}), 409
            cursor = connection.execute("DELETE FROM groups WHERE id=?", (group_id,))
            if not cursor.rowcount:
                return jsonify({"error": "Group not found"}), 404
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM item_tags WHERE item_tags.tag_id=tags.id)"
            )
            write_database_opml(connection, config.working_opml_path)
        return jsonify({"status": "ok", "changed": cursor.rowcount})

    @app.post("/api/feeds")
    @mutation
    def add_feed():
        payload = request.get_json(force=True)
        title = str(payload.get("title", "")).strip() or "New feed"
        xml_url = str(payload.get("xml_url", "")).strip()
        try:
            if len(title) > 300:
                raise ValueError("A feed title cannot exceed 300 characters")
            group_id = int(payload.get("group_id", 0))
            _validate_feed_source(config, xml_url)
            with connect(config.database_path) as connection, transaction(connection, immediate=True):
                if not connection.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
                    return jsonify({"error": "Choose an existing subscription group"}), 400
                existing = connection.execute(
                    "SELECT id,title FROM feeds WHERE xml_url=?", (xml_url,)
                ).fetchone()
                if existing:
                    return jsonify({
                        "error": f"This feed is already subscribed as {existing['title']}",
                        "feed_id": int(existing["id"]),
                    }), 409
                feed_id = int(connection.execute(
                    """INSERT INTO feeds(group_id,title,title_locked,position,xml_url,llm_enabled,created_at)
                       VALUES(?,?,?,9999,?,?,?)""",
                    (group_id, title, int(bool(payload.get("title"))), xml_url, int(_strict_bool(payload.get("llm_enabled", True))), utcnow()),
                ).lastrowid)
                write_database_opml(connection, config.working_opml_path)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "ok", "feed_id": feed_id}), 201

    @app.patch("/api/feeds/<int:feed_id>")
    @mutation
    def update_feed(feed_id: int):
        payload = request.get_json(force=True)
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            row = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
            if not row:
                return jsonify({"error": "Feed not found"}), 404
            title = str(payload.get("title", row["title"])).strip()
            if not title or len(title) > 300:
                return jsonify({"error": "Invalid feed title"}), 400
            xml_url = str(payload.get("xml_url", row["xml_url"])).strip()
            if xml_url != row["xml_url"]:
                _validate_feed_source(config, xml_url)
                duplicate = connection.execute(
                    "SELECT id,title FROM feeds WHERE xml_url=? AND id<>?", (xml_url, feed_id)
                ).fetchone()
                if duplicate:
                    return jsonify({
                        "error": f"This feed URL is already subscribed as {duplicate['title']}"
                    }), 409
            group_id = int(payload.get("group_id", row["group_id"]))
            if not connection.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
                return jsonify({"error": "Choose an existing subscription group"}), 400
            position = int(row["position"])
            if group_id != int(row["group_id"]):
                ungrouped_id = ensure_ungrouped(connection)
                visible_parent = None if group_id == ungrouped_id else group_id
                position = len(_visible_subscription_entries(connection, visible_parent))
            llm_enabled = int(_strict_bool(payload.get("llm_enabled", bool(row["llm_enabled"]))))
            ai_mode = str(payload.get("ai_mode", row["ai_mode"])).strip().casefold()
            if ai_mode not in {"inherit", "automatic", "manual", "off"}:
                return jsonify({"error": "Feed summary mode must be Inherit, Automatic, Only on request, or Excluded"}), 400
            if "ai_mode" not in payload and "llm_enabled" in payload:
                ai_mode = "inherit" if llm_enabled else "off"
            llm_enabled = int(ai_mode != "off")
            title_locked = 1 if "title" in payload else int(row["title_locked"])
            connection.execute(
                """UPDATE feeds SET title=?,title_locked=?,xml_url=?,group_id=?,position=?,llm_enabled=?,ai_mode=?,
                   etag=CASE WHEN xml_url<>? THEN NULL ELSE etag END,
                   last_modified=CASE WHEN xml_url<>? THEN NULL ELSE last_modified END,
                   next_retry_at=CASE WHEN xml_url<>? THEN NULL ELSE next_retry_at END,
                   consecutive_failures=CASE WHEN xml_url<>? THEN 0 ELSE consecutive_failures END,
                   last_http_status=CASE WHEN xml_url<>? THEN NULL ELSE last_http_status END,
                   last_error=CASE WHEN xml_url<>? THEN NULL ELSE last_error END
                   WHERE id=?""",
                (
                    title, title_locked, xml_url, group_id, position, llm_enabled, ai_mode,
                    xml_url, xml_url, xml_url, xml_url, xml_url, xml_url, feed_id,
                ),
            )
            if group_id != int(row["group_id"]):
                connection.execute(
                    "UPDATE summaries SET group_id=? WHERE scope_kind='feed' AND scope_id=?",
                    (group_id, feed_id),
                )
            write_database_opml(connection, config.working_opml_path)
        return jsonify({
            "status": "ok", "feed": {
                "id": feed_id, "title": title, "xml_url": xml_url,
                "group_id": group_id, "llm_enabled": bool(llm_enabled), "ai_mode": ai_mode,
            },
        })

    @app.delete("/api/feeds/<int:feed_id>")
    @mutation
    def delete_feed(feed_id: int):
        with connect(config.database_path) as connection, transaction(connection, immediate=True):
            connection.execute(
                """DELETE FROM summaries WHERE (scope_kind='feed' AND scope_id=?)
                   OR id IN (
                     SELECT DISTINCT si.summary_id FROM summary_items si
                     JOIN items i ON i.id=si.item_id WHERE i.feed_id=?
                   )""",
                (feed_id, feed_id),
            )
            cursor = connection.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
            if not cursor.rowcount:
                return jsonify({"error": "Feed not found"}), 404
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM item_tags WHERE item_tags.tag_id=tags.id)"
            )
            write_database_opml(connection, config.working_opml_path)
        return jsonify({"status": "ok", "changed": cursor.rowcount})

    @app.post("/api/config")
    @mutation
    @serialized_config_write
    def update_config():
        payload = request.get_json(force=True)
        values = payload.get("values", {})
        if not isinstance(values, dict):
            return jsonify({"error": "values must be an object"}), 400
        ntfy_scope_supplied = "ntfy_scope_policy" in payload
        ntfy_scope_payload = payload.get("ntfy_scope_policy")
        if ntfy_scope_supplied and not isinstance(ntfy_scope_payload, dict):
            return jsonify({"error": "ntfy_scope_policy must be an object"}), 400
        plugin_values = {
            str(path): value for path, value in values.items()
            if str(path).startswith("plugin.")
        }
        core_values = {
            str(path): value for path, value in values.items()
            if not str(path).startswith("plugin.")
        }
        try:
            candidate_data = copy.deepcopy(config.data)
            changed_paths: set[str] = set()
            for path, raw_value in core_values.items():
                parts = str(path).split(".")
                if path in {
                    "app.database_path", "app.working_opml_path",
                    "feeds.generated_feed_directory",
                } and raw_value != config.get(parts[0], parts[-1]):
                    raise ValueError(f"{path} cannot be changed while the application is running")
                target: dict[str, Any] = candidate_data
                for part in parts[:-1]:
                    target = target[part]
                key = parts[-1]
                current = target[key]
                if isinstance(current, bool):
                    value = _strict_bool(raw_value)
                elif isinstance(current, int):
                    value = int(raw_value)
                elif isinstance(current, float):
                    value = float(raw_value)
                else:
                    value = str(raw_value)
                target[key] = value
                if value != current:
                    changed_paths.add(str(path))
            if changed_paths & {"llm.model", "llm.provider"}:
                model = str(candidate_data["llm"]["model"])
                prices = OPENAI_MODEL_PRICING.get(model)
                pricing_paths = {
                    "llm.pricing.input", "llm.pricing.cached_input", "llm.pricing.output",
                }
                if (
                    candidate_data["llm"]["provider"] == "openai" and prices
                    and not changed_paths.intersection(pricing_paths)
                ):
                    candidate_data["llm"]["pricing"] = dict(prices)
                    changed_paths.update(pricing_paths)
            candidate = Config(config.path, candidate_data)
            validate_config(candidate_data)
            language = str(candidate.get("app", "summary_language"))
            if language not in {"English", "French"}:
                raise ValueError("app.summary_language must be English or French")
            if not 10 <= int(candidate.get("ui", "subscription_font_size")) <= 24:
                raise ValueError("ui.subscription_font_size must be between 10 and 24")
            if not 10 <= int(candidate.get("ui", "item_font_size")) <= 24:
                raise ValueError("ui.item_font_size must be between 10 and 24")
            if not 10 <= int(candidate.get("ui", "summary_font_size")) <= 24:
                raise ValueError("ui.summary_font_size must be between 10 and 24")
            if int(candidate.get("app", "refresh_interval_minutes")) < 1:
                raise ValueError("app.refresh_interval_minutes must be positive")
            if int(candidate.get("weather", "refresh_minutes")) < 1:
                raise ValueError("weather.refresh_minutes must be positive")
            if str(candidate.get("weather", "language")) not in {"English", "French"}:
                raise ValueError("weather.language must be English or French")
            if candidate.get("auth", "enabled") and not os.environ.get(str(candidate.get("auth", "password_env"))):
                raise ValueError("Set the configured authentication password environment variable before enabling auth")
            # Each plugin owns and validates its separate file. It is persisted
            # before the public configuration so invalid plugin input cannot
            # partially change config.toml.
            update_plugin_settings(config, plugin_values)
            with connect(config.database_path) as connection, transaction(connection, immediate=True):
                if ntfy_scope_supplied:
                    assert isinstance(ntfy_scope_payload, dict)
                    replace_ntfy_scope_policy(
                        connection,
                        str(ntfy_scope_payload.get("mode", "")),
                        ntfy_scope_payload.get("rules", []),
                    )
                previous_arxiv = "arxiv_digest" in enabled_plugin_names(config)
                requested_arxiv = bool(candidate.get("plugins", "arxiv_digest_enabled", False))
                legacy_enabled = [
                    name.strip()
                    for name in str(candidate.get("plugins", "enabled", "")).split(",")
                    if name.strip() and name.strip() != "arxiv_digest"
                ]
                candidate_data["plugins"]["enabled"] = ",".join(dict.fromkeys(legacy_enabled))
                if previous_arxiv != requested_arxiv or (requested_arxiv and plugin_values):
                    set_plugin_runtime_state(
                        connection, candidate, "arxiv_digest", requested_arxiv
                    )
                if changed_paths & {
                    "app.background_scheduler_enabled", "app.refresh_interval_minutes",
                }:
                    next_run = (
                        datetime.now(UTC) + timedelta(
                            minutes=max(1, int(candidate.get("app", "refresh_interval_minutes")))
                        )
                    ).isoformat(timespec="seconds")
                    connection.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                        ("background_scheduler_next_at", next_run),
                    )
                connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('summary_language',?)", (language,))
                connection.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('interest_profile',?)",
                    (str(candidate.get("app", "interest_profile"))[:2000],),
                )
                # Keep the database transaction open until the atomic TOML write
                # succeeds, so a file failure rolls back the mirrored settings.
                save_config(candidate)
            config.data.clear()
            config.data.update(candidate_data)
            if bool(config.get("app", "background_scheduler_enabled", False)):
                background_scheduler.start()
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        restart_paths = {"app.mode", "app.host", "app.port", "app.trusted_hosts", "app.debug", "app.log_level", "auth.enabled", "auth.username", "auth.password_env"}
        return jsonify({
            "status": "ok",
            "restart_recommended": bool(changed_paths & restart_paths),
            "plugin_state_changed": "plugins.arxiv_digest_enabled" in changed_paths,
            "ntfy_policy_changed": ntfy_scope_supplied,
        })

    if bool(config.get("app", "background_scheduler_enabled", False)):
        defer_next_refresh(config, only_if_missing=True)
        background_scheduler.start()
    return app
