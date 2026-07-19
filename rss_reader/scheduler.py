from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .db import connect, transaction
from .service import run_refresh, run_update_summaries

LOGGER = logging.getLogger(__name__)
NEXT_RUN_KEY = "background_scheduler_next_at"
LAST_RESULT_KEY = "background_scheduler_last_result"


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def claim_due_refresh(config: Config, *, now: datetime | None = None) -> bool:
    """Atomically claim one scheduled refresh across threads and processes."""
    current = now or datetime.now(UTC)
    interval = max(1, int(config.get("app", "refresh_interval_minutes", 30)))
    with connect(config.database_path) as connection, transaction(connection, immediate=True):
        row = connection.execute(
            "SELECT value FROM settings WHERE key=?", (NEXT_RUN_KEY,),
        ).fetchone()
        due = _parse_timestamp(str(row["value"]) if row else None)
        if due is not None and due > current:
            return False
        following = (current + timedelta(minutes=interval)).isoformat(timespec="seconds")
        connection.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (NEXT_RUN_KEY, following),
        )
    return True


def defer_next_refresh(
    config: Config, *, now: datetime | None = None, only_if_missing: bool = False,
) -> str:
    """Store the next due time without running a cycle."""
    current = now or datetime.now(UTC)
    interval = max(1, int(config.get("app", "refresh_interval_minutes", 30)))
    following = (current + timedelta(minutes=interval)).isoformat(timespec="seconds")
    with connect(config.database_path) as connection, transaction(connection, immediate=True):
        if only_if_missing and connection.execute(
            "SELECT 1 FROM settings WHERE key=?", (NEXT_RUN_KEY,),
        ).fetchone():
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?", (NEXT_RUN_KEY,),
            ).fetchone()
            return str(row["value"])
        connection.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (NEXT_RUN_KEY, following),
        )
    return following


def scheduled_refresh_once(
    config: Config, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Run one due automatic cycle; refresh itself owns the job lock."""
    if not bool(config.get("app", "background_scheduler_enabled", False)):
        return {"status": "disabled"}
    if not claim_due_refresh(config, now=now):
        return {"status": "waiting"}
    try:
        if bool(config.get("app", "auto_summarize_after_refresh", False)):
            result = dict(run_update_summaries(config, automatic=True))
            refresh = result.get("refresh", {})
            summary = result.get("summary")
        else:
            result = dict(run_refresh(config, automatic=True))
            refresh = result
            summary = None
        parts = [f"refresh {refresh.get('status', 'unknown')}"]
        if isinstance(summary, dict):
            parts.append(f"summary {summary.get('status', 'unknown')}")
            notification = summary.get("notifications")
            if isinstance(notification, dict):
                parts.append(f"ntfy {notification.get('status', 'unknown')}")
        stored = " · ".join(parts)
    except Exception as exc:
        LOGGER.exception("Background scheduled refresh failed")
        result = {"status": "failed", "message": str(exc)[:2000]}
        stored = "failed"
    with connect(config.database_path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (LAST_RESULT_KEY, f"{datetime.now(UTC).isoformat(timespec='seconds')}|{stored}"),
        )
    return result


class BackgroundScheduler:
    """Small in-process scheduler with a database claim for multi-worker safety."""

    def __init__(self, config: Config, *, poll_seconds: float = 5.0):
        self.config = config
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._guard:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="distillfeed-scheduler", daemon=True,
            )
            self._thread.start()

    def stop(self, *, wait: bool = False) -> None:
        self._stop.set()
        if wait and self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.poll_seconds * 2))

    def _loop(self) -> None:
        while not self._stop.is_set():
            if bool(self.config.get("app", "background_scheduler_enabled", False)):
                scheduled_refresh_once(self.config)
            self._stop.wait(self.poll_seconds)
