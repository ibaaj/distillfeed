from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Config
from .ai_engine import execute_ai_update
from .ai_errors import classify_ai_error
from .ai_queue import sync_review_queue
from .db import acquire_lock, cancellation_requested, connect, release_lock, renew_lock, utcnow
from .feeds import refresh_all
from .net import read_limited_response, safe_get
from .opml import import_groups, is_remote_source, parse_opml_bytes, write_database_opml
from .notifications import deliver_ntfy_for_job
from .plugins import refresh_plugins, summarize_plugins

LOGGER = logging.getLogger(__name__)


def _lease_heartbeat(config: Config, name: str, owner: str, ttl_minutes: int):
    """Keep a legitimate long network/model operation from losing its lease."""
    stopped = threading.Event()

    def beat() -> None:
        while not stopped.wait(30):
            try:
                with connect(config.database_path) as heartbeat_connection:
                    if not renew_lock(
                        heartbeat_connection, name, owner, ttl_minutes,
                    ):
                        return
            except Exception:
                LOGGER.exception("Could not renew %s operation lease", name)

    thread = threading.Thread(
        target=beat, name=f"{name}-lease-heartbeat", daemon=True,
    )
    thread.start()
    return stopped, thread


def _stop_heartbeat(handle) -> None:
    stopped, thread = handle
    stopped.set()
    thread.join(timeout=2)


def import_opml_source(connection, config: Config, source: str) -> tuple[int, int]:
    if is_remote_source(source):
        options = config.section("feeds")
        with safe_get(
            source, timeout=int(options["timeout_seconds"]), allow_private=bool(options["allow_private_urls"]),
            headers={"User-Agent": str(options["user_agent"])},
        ) as response:
            response.raise_for_status()
            content = read_limited_response(response, int(options["max_response_bytes"]))
    else:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = (config.path.parent / path).resolve()
        content = path.read_bytes()
    result = import_groups(connection, parse_opml_bytes(content))
    write_database_opml(connection, config.working_opml_path)
    return result


def is_refresh_stale(connection, config: Config) -> bool:
    row = connection.execute(
        """SELECT COUNT(*) AS feed_count, MIN(last_attempt_at) AS oldest,
                  SUM(CASE WHEN last_attempt_at IS NULL THEN 1 ELSE 0 END) AS never_attempted
           FROM feeds WHERE enabled=1"""
    ).fetchone()
    if not row or not row["feed_count"] or row["never_attempted"] or not row["oldest"]:
        return True
    oldest = datetime.fromisoformat(row["oldest"])
    return oldest < datetime.now(UTC) - timedelta(minutes=int(config.get("app", "refresh_interval_minutes")))


def run_refresh(
    config: Config, feed_id: int | None = None, group_id: int | None = None,
    force: bool = False, automatic: bool = False, summarize_after: bool = False,
    _coordinated: bool = False, _reserved_owner: str | None = None,
) -> dict:
    if feed_id is not None and group_id is not None:
        raise ValueError("Choose either a group or a feed refresh scope")
    owner = _reserved_owner or str(uuid.uuid4())
    connection = connect(config.database_path)
    if _reserved_owner and not connection.execute(
        """SELECT 1 FROM job_locks
           WHERE name='feed-refresh' AND owner=? AND expires_at>=?""",
        (owner, utcnow()),
    ).fetchone():
        connection.close()
        return {"status": "running", "message": "The reserved feed check expired"}
    if not _reserved_owner and not _coordinated and connection.execute(
        """SELECT 1 FROM job_locks WHERE name IN ('summary-update','llm-summary')
           AND expires_at>=? LIMIT 1""", (utcnow(),),
    ).fetchone():
        connection.close()
        return {"status": "running", "message": "A summary update is already running"}
    if not _reserved_owner and not acquire_lock(
        connection, "feed-refresh", owner, ttl_minutes=120,
        exclusive=not _coordinated,
    ):
        connection.close()
        return {"status": "running", "message": "A feed refresh is already running"}
    heartbeat = _lease_heartbeat(config, "feed-refresh", owner, 120)
    run_id = None
    try:
        def stopped(active_connection=connection) -> bool:
            return cancellation_requested(active_connection, "feed-refresh", owner)

        if automatic and feed_id is None and group_id is None and not is_refresh_stale(connection, config):
            return {"status": "fresh", "message": "Feeds are not stale"}
        run_id = int(
            connection.execute(
                "INSERT INTO refresh_runs(started_at, status) VALUES (?, 'running')", (utcnow(),)
            ).lastrowid
        )
        source = str(config.get("app", "opml_source", "")).strip()
        if source and feed_id is None and group_id is None:
            groups, feeds = import_opml_source(connection, config, source)
            LOGGER.info("Merged subscription source before refresh: groups=%d feeds=%d", groups, feeds)
        if stopped():
            stats: dict = {
                "attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0,
                "cancelled": True,
            }
        else:
            stats = refresh_all(
                connection, config, feed_id=feed_id, group_id=group_id, force=force,
                cancel_requested=lambda worker_connection: cancellation_requested(
                    worker_connection, "feed-refresh", owner
                ),
            )
        if not stopped() and not stats.get("cancelled"):
            plugin_stats = refresh_plugins(
                connection, config, feed_id=feed_id, group_id=group_id,
                force=force, automatic=automatic, cancel_requested=stopped,
            )
            for key in ("attempted", "succeeded", "failed", "new_items"):
                stats[key] += int(plugin_stats[key])
            if plugin_stats["plugins"]:
                stats["plugins"] = plugin_stats["plugins"]
            if plugin_stats.get("cancelled"):
                stats["cancelled"] = True
        # Feed discovery may refine a subscription's canonical website URL.
        # Keep the portable working OPML synchronized with that database state
        # before doctor or another process compares the two representations.
        write_database_opml(connection, config.working_opml_path)
        sync_review_queue(connection)
        if stopped() or stats.get("cancelled"):
            stats["cancelled"] = True
            connection.execute(
                """UPDATE refresh_runs SET completed_at=?,status='cancelled',feeds_attempted=?,
                   feeds_succeeded=?,new_items=? WHERE id=?""",
                (utcnow(), stats["attempted"], stats["succeeded"], stats["new_items"], run_id),
            )
            return {
                "status": "cancelled", "message": "Refresh stopped safely",
                **stats, "run_id": run_id,
            }
        retention_days = int(config.get("app", "retention_days", 0))
        if retention_days > 0 and feed_id is None and group_id is None:
            cursor = connection.execute(
                """DELETE FROM items WHERE is_read=1 AND is_starred=0 AND is_read_later=0
                   AND COALESCE(published_at, discovered_at) < ?
                   AND NOT EXISTS (SELECT 1 FROM item_tags WHERE item_tags.item_id=items.id)
                   AND NOT EXISTS (SELECT 1 FROM summary_items WHERE summary_items.item_id=items.id)""",
                ((datetime.now(UTC) - timedelta(days=retention_days)).isoformat(),),
            )
            stats["deleted_old_items"] = max(cursor.rowcount, 0)
            if cursor.rowcount:
                connection.execute(
                    "DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM item_tags WHERE item_tags.tag_id=tags.id)"
                )
        status = "success" if stats["failed"] == 0 else "partial"
        connection.execute(
            """UPDATE refresh_runs SET completed_at=?, status=?, feeds_attempted=?,
               feeds_succeeded=?, new_items=? WHERE id=?""",
            (utcnow(), status, stats["attempted"], stats["succeeded"], stats["new_items"], run_id),
        )
        result = {"status": status, **stats, "run_id": run_id}
        # Feed checking has a strict public contract: it never invokes a model.
        # Scheduled and manual summary updates orchestrate their AI stage
        # explicitly through run_update_summaries(). Keep the former argument
        # accepted for one compatibility release, but never act on it.
        if summarize_after:
            result["summary_deferred"] = True
        if stopped():
            result["status"] = "cancelled"
            result["cancelled"] = True
            result["message"] = "Update stopped safely"
            connection.execute(
                "UPDATE refresh_runs SET status='cancelled' WHERE id=?", (run_id,)
            )
        return result
    except Exception as exc:
        if run_id is not None:
            connection.execute(
                "UPDATE refresh_runs SET completed_at=?, status='failed', error=? WHERE id=?",
                (utcnow(), str(exc)[:2000], run_id),
            )
        raise
    finally:
        _stop_heartbeat(heartbeat)
        release_lock(connection, "feed-refresh", owner)
        connection.close()


def run_update_summaries(
    config: Config, *, automatic: bool = False,
    group_id: int | None = None, feed_id: int | None = None,
    include_plugins: bool = True, include_generic: bool = True,
    _reserved_owner: str | None = None,
) -> dict:
    """Check one scope, then update its summaries using the configured model."""
    owner = _reserved_owner or str(uuid.uuid4())
    coordinator = connect(config.database_path)
    if _reserved_owner and not coordinator.execute(
        """SELECT 1 FROM job_locks
           WHERE name='summary-update' AND owner=? AND expires_at>=?""",
        (owner, utcnow()),
    ).fetchone():
        coordinator.close()
        return {"status": "running", "message": "The reserved summary update expired"}
    if not _reserved_owner and coordinator.execute(
        """SELECT 1 FROM job_locks WHERE name IN ('feed-refresh','llm-summary')
           AND expires_at>=? LIMIT 1""", (utcnow(),),
    ).fetchone():
        coordinator.close()
        return {"status": "running", "message": "Another update is already running"}
    if not _reserved_owner and not acquire_lock(
        coordinator, "summary-update", owner, ttl_minutes=180, exclusive=True,
    ):
        coordinator.close()
        return {"status": "running", "message": "A summary update is already running"}
    heartbeat = _lease_heartbeat(config, "summary-update", owner, 180)
    try:
        refresh = run_refresh(
            config, feed_id=feed_id, group_id=group_id,
            force=not automatic, automatic=automatic, summarize_after=False,
            _coordinated=True,
        )
        if refresh.get("status") in {"running", "cancelled"}:
            return {"status": refresh["status"], "refresh": refresh}
        if cancellation_requested(coordinator, "summary-update", owner):
            return {"status": "cancelled", "refresh": refresh, "message": "Summary update stopped safely"}
        try:
            summary = run_summary(
                config, automatic=automatic, group_id=group_id, feed_id=feed_id,
                _coordinated=True, include_plugins=include_plugins,
                include_generic=include_generic,
            )
        except Exception as exc:
            summary = {"status": "failed", "message": str(exc)[:2000]}
        status = summary.get("status", "failed")
        if status in {"empty", "disabled", "cooldown"} and refresh.get("status") in {"success", "partial"}:
            status = refresh["status"]
        # Retrieval and AI are independent axes.  Never let a successful AI
        # stage hide feeds that failed during the same user operation.
        if refresh.get("status") == "partial" and status in {"success", "empty"}:
            status = "partial"
            message = (
                f"{summary.get('message', 'Summaries were updated')}. "
                "Some feeds could not be checked; their previous content was kept."
            )
        else:
            message = summary.get("message", "Summary update finished")
        return {
            "status": status,
            "message": message,
            "refresh": refresh,
            "summary": summary,
        }
    finally:
        _stop_heartbeat(heartbeat)
        release_lock(coordinator, "summary-update", owner)
        coordinator.close()


def run_summary(
    config: Config, automatic: bool = False,
    group_id: int | None = None, feed_id: int | None = None,
    _coordinated: bool = False,
    include_plugins: bool = True, include_generic: bool = True,
) -> dict:
    if feed_id is not None and group_id is not None:
        raise ValueError("Choose either a group or a feed summary scope")
    owner = str(uuid.uuid4())
    connection = connect(config.database_path)
    if not _coordinated and connection.execute(
        """SELECT 1 FROM job_locks WHERE name IN ('summary-update','feed-refresh')
           AND expires_at>=? LIMIT 1""", (utcnow(),),
    ).fetchone():
        connection.close()
        return {"status": "running", "message": "A feed check or summary update is already running"}
    if not acquire_lock(
        connection, "llm-summary", owner, ttl_minutes=120,
        exclusive=not _coordinated,
    ):
        connection.close()
        return {"status": "running", "message": "A summary request is already running"}
    heartbeat = _lease_heartbeat(config, "llm-summary", owner, 120)
    try:
        def stopped() -> bool:
            return cancellation_requested(connection, "llm-summary", owner)

        if stopped() or not include_plugins:
            plugin_result = {"succeeded": 0, "failed": 0, "plugins": [], "cancelled": True}
            if not stopped():
                plugin_result.pop("cancelled")
        else:
            plugin_result = summarize_plugins(
                connection, config, automatic=automatic, group_id=group_id, feed_id=feed_id,
                cancel_requested=stopped,
            )
        cancelled = bool(plugin_result.get("cancelled") or stopped())
        if cancelled:
            result = {
                "status": "cancelled", "message": "Summary update stopped safely",
                "cancelled": True,
            }
        elif include_generic:
            try:
                result = execute_ai_update(
                    connection, config, automatic=automatic,
                    group_id=group_id, feed_id=feed_id,
                    cancel_requested=stopped,
                )
            except Exception as exc:
                failure = classify_ai_error(exc)
                result = {
                    "status": "failed",
                    "code": failure.code,
                    "retryable": failure.retryable,
                    "message": f"Ordinary feed summaries failed: {failure.message}",
                }
        else:
            result = {
                "status": "success" if plugin_result["succeeded"] else (
                    "failed" if plugin_result["failed"] else "empty"
                ),
                "message": (
                    "Daily plugin digest updated" if plugin_result["succeeded"]
                    else "Plugin digest failed; its announcement remains waiting"
                    if plugin_result["failed"] else "No plugin digest is waiting"
                ),
            }
            if plugin_result.get("blocked") and not plugin_result["succeeded"]:
                blocked_plugin = next(
                    (item for item in plugin_result["plugins"] if str(item.get("status"))
                     in {"blocked", "pending-llm-disabled", "missing-credential", "budget-blocked", "disabled"}),
                    {},
                )
                result["status"] = "blocked"
                result["message"] = str(
                    blocked_plugin.get("message") or blocked_plugin.get("error")
                    or "A plugin digest is waiting, but its AI configuration is not ready"
                )
        if plugin_result["plugins"]:
            result["plugins"] = plugin_result["plugins"]
            result["plugin_summaries_succeeded"] = plugin_result["succeeded"]
            result["plugin_summaries_failed"] = plugin_result["failed"]
            result["plugin_summaries_blocked"] = plugin_result.get("blocked", 0)
            if result.get("status") in {"empty", "disabled", "cooldown"}:
                if plugin_result["succeeded"]:
                    result["status"] = "success"
                    result["message"] = "Plugin summary completed"
                elif plugin_result["failed"]:
                    result["status"] = "failed"
                    result["message"] = "Plugin summary failed; its pending items can be retried"
                elif plugin_result.get("blocked"):
                    blocked_plugin = next(
                        (item for item in plugin_result["plugins"] if str(item.get("status"))
                         in {"blocked", "pending-llm-disabled", "missing-credential", "budget-blocked", "disabled"}),
                        {},
                    )
                    result["status"] = "blocked"
                    result["message"] = str(
                        blocked_plugin.get("message") or blocked_plugin.get("error")
                        or "A plugin digest is waiting for AI configuration"
                    )
            elif plugin_result["failed"] and result.get("status") == "success":
                result["status"] = "partial"
                result["message"] = (
                    "Ordinary summaries were updated; a plugin digest remains waiting after an error"
                )
            elif plugin_result.get("blocked") and result.get("status") == "success":
                result["status"] = "partial"
                result["message"] = (
                    "Ordinary summaries were updated; a plugin digest remains blocked by its AI configuration"
                )
            elif plugin_result["succeeded"] and result.get("status") == "failed":
                result["status"] = "partial"
                result["message"] = (
                    "A plugin digest was updated; ordinary feed summaries failed and can be retried"
                )
        # A specialist plugin can fail independently after an ordinary-feed job
        # has published successfully.  That makes the combined operation
        # ``partial``, but must not suppress notifications for the completed
        # ordinary job.
        if automatic and result.get("status") in {"success", "partial"} and result.get("job_id"):
            try:
                result["notifications"] = deliver_ntfy_for_job(
                    connection, config, int(result["job_id"]),
                )
            except Exception as exc:
                LOGGER.exception("ntfy device-alert processing failed after a successful summary")
                result["notifications"] = {"status": "failed", "message": str(exc)[:2000]}
        return result
    finally:
        _stop_heartbeat(heartbeat)
        release_lock(connection, "llm-summary", owner)
        connection.close()


def start_thread(target, *, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread
