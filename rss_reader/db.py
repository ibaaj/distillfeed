from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    llm_enabled INTEGER NOT NULL DEFAULT 1,
    summary_interval_hours INTEGER NOT NULL DEFAULT 0,
    summary_item_budget INTEGER NOT NULL DEFAULT 0,
    ai_mode TEXT NOT NULL DEFAULT 'automatic',
    ai_priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL,
    UNIQUE(parent_id, title)
);
CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    title_locked INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    xml_url TEXT NOT NULL UNIQUE,
    html_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    llm_enabled INTEGER NOT NULL DEFAULT 1,
    ai_mode TEXT NOT NULL DEFAULT 'inherit',
    etag TEXT,
    last_modified TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    next_retry_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_http_status INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feeds_group ON feeds(group_id);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    stable_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    author TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    description_text TEXT NOT NULL DEFAULT '',
    summary_eligible INTEGER NOT NULL DEFAULT 1,
    is_read INTEGER NOT NULL DEFAULT 0,
    is_starred INTEGER NOT NULL DEFAULT 0,
    is_read_later INTEGER NOT NULL DEFAULT 0,
    UNIQUE(feed_id, stable_id)
);
CREATE INDEX IF NOT EXISTS idx_items_feed_date ON items(feed_id, published_at DESC, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_read ON items(is_read);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    feeds_attempted INTEGER NOT NULL DEFAULT 0,
    feeds_succeeded INTEGER NOT NULL DEFAULT 0,
    new_items INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS llm_runs (
    id INTEGER PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    submitted_items INTEGER NOT NULL DEFAULT 0,
    deferred_items INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    pricing_json TEXT NOT NULL,
    provider_request_id TEXT,
    ai_job_id INTEGER,
    stage TEXT NOT NULL DEFAULT 'evaluation',
    batch_number INTEGER NOT NULL DEFAULT 1,
    error TEXT
);

CREATE TABLE IF NOT EXISTS ai_review_queue (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'waiting',
    queued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    available_at TEXT,
    claimed_run_id INTEGER REFERENCES llm_runs(id) ON DELETE SET NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_review_queue_state
ON ai_review_queue(state, available_at, queued_at);

CREATE TABLE IF NOT EXISTS ai_item_preferences (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    disposition TEXT NOT NULL DEFAULT 'default',
    updated_at TEXT NOT NULL,
    CHECK(disposition IN ('default', 'excluded'))
);

CREATE TABLE IF NOT EXISTS ai_jobs (
    id INTEGER PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    trigger_kind TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_id INTEGER,
    policy_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    planned_items INTEGER NOT NULL DEFAULT 0,
    completed_items INTEGER NOT NULL DEFAULT 0,
    planned_requests INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_started ON ai_jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS ai_evaluations (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    llm_run_id INTEGER NOT NULL REFERENCES llm_runs(id) ON DELETE CASCADE,
    ai_job_id INTEGER REFERENCES ai_jobs(id) ON DELETE SET NULL,
    policy_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    language TEXT NOT NULL,
    relevance INTEGER NOT NULL,
    description TEXT NOT NULL,
    justification TEXT NOT NULL,
    story_cluster TEXT NOT NULL DEFAULT '',
    current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK(relevance BETWEEN 0 AND 100)
);
CREATE INDEX IF NOT EXISTS idx_ai_evaluations_item
ON ai_evaluations(item_id, current, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_evaluations_current
ON ai_evaluations(item_id) WHERE current=1;

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY,
    llm_run_id INTEGER NOT NULL REFERENCES llm_runs(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    scope_feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
    ai_job_id INTEGER REFERENCES ai_jobs(id) ON DELETE SET NULL,
    scope_kind TEXT NOT NULL DEFAULT 'group',
    scope_id INTEGER,
    policy_hash TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    changes TEXT NOT NULL DEFAULT '',
    sections_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(llm_run_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_summaries_group_run ON summaries(group_id, llm_run_id DESC);

CREATE TABLE IF NOT EXISTS summary_items (
    summary_id INTEGER NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    included INTEGER NOT NULL DEFAULT 0,
    rank INTEGER,
    importance INTEGER,
    description TEXT,
    justification TEXT,
    story_cluster TEXT,
    evaluation_id INTEGER REFERENCES ai_evaluations(id) ON DELETE SET NULL,
    PRIMARY KEY(summary_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_summary_items_item ON summary_items(item_id);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    destination_key TEXT NOT NULL,
    llm_run_id INTEGER NOT NULL REFERENCES llm_runs(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    relevance INTEGER NOT NULL,
    minimum_relevance INTEGER,
    policy_scope_kind TEXT NOT NULL DEFAULT 'global',
    policy_scope_id INTEGER,
    policy_label TEXT NOT NULL DEFAULT 'All feeds',
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    delivered_at TEXT,
    provider_message_id TEXT,
    error TEXT,
    UNIQUE(channel, destination_key, item_id)
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
ON notification_deliveries(status, attempted_at DESC);

CREATE TABLE IF NOT EXISTS ntfy_scope_rules (
    id INTEGER PRIMARY KEY,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
    minimum_relevance INTEGER NOT NULL CHECK(minimum_relevance BETWEEN 0 AND 100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (group_id IS NOT NULL AND feed_id IS NULL)
        OR (group_id IS NULL AND feed_id IS NOT NULL)
    ),
    UNIQUE(group_id),
    UNIQUE(feed_id)
);
CREATE INDEX IF NOT EXISTS idx_ntfy_scope_rules_group ON ntfy_scope_rules(group_id);
CREATE INDEX IF NOT EXISTS idx_ntfy_scope_rules_feed ON ntfy_scope_rules(feed_id);

CREATE TABLE IF NOT EXISTS app_operations (
    id INTEGER PRIMARY KEY,
    operation_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    trigger TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_id INTEGER,
    lock_name TEXT NOT NULL,
    lock_owner TEXT NOT NULL,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_operations_state
ON app_operations(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_operations_kind
ON app_operations(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS app_issues (
    id INTEGER PRIMARY KEY,
    issue_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    action_url TEXT,
    action_label TEXT,
    fingerprint TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_issues_active
ON app_issues(active, acknowledged_at, severity, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS job_locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ManagedConnection(sqlite3.Connection):
    """A SQLite connection whose context manager also releases the file handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), timeout=30, isolation_level=None, factory=ManagedConnection
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite database without changing it or its journal mode."""
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(
        uri, uri=True, timeout=30, isolation_level=None, factory=ManagedConnection
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize(path: str | Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(summary_items)").fetchall()}
        if "justification" not in columns:
            connection.execute("ALTER TABLE summary_items ADD COLUMN justification TEXT")
        if "story_cluster" not in columns:
            connection.execute("ALTER TABLE summary_items ADD COLUMN story_cluster TEXT")
        item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)").fetchall()}
        if "summary_eligible" not in item_columns:
            connection.execute("ALTER TABLE items ADD COLUMN summary_eligible INTEGER NOT NULL DEFAULT 1")
        if "is_read_later" not in item_columns:
            connection.execute("ALTER TABLE items ADD COLUMN is_read_later INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_summary_eligible ON items(summary_eligible)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_items_read_later ON items(is_read_later)")
        group_columns = {row["name"] for row in connection.execute("PRAGMA table_info(groups)").fetchall()}
        if "llm_enabled" not in group_columns:
            connection.execute("ALTER TABLE groups ADD COLUMN llm_enabled INTEGER NOT NULL DEFAULT 1")
        if "summary_interval_hours" not in group_columns:
            connection.execute("ALTER TABLE groups ADD COLUMN summary_interval_hours INTEGER NOT NULL DEFAULT 0")
        if "summary_item_budget" not in group_columns:
            connection.execute("ALTER TABLE groups ADD COLUMN summary_item_budget INTEGER NOT NULL DEFAULT 0")
        if "ai_priority" not in group_columns:
            connection.execute("ALTER TABLE groups ADD COLUMN ai_priority TEXT NOT NULL DEFAULT 'normal'")
        if "ai_mode" not in group_columns:
            connection.execute("ALTER TABLE groups ADD COLUMN ai_mode TEXT NOT NULL DEFAULT 'automatic'")
            connection.execute(
                """UPDATE groups SET ai_mode=CASE
                       WHEN llm_enabled=0 OR ai_priority='off' THEN 'off'
                       WHEN ai_priority='manual' THEN 'manual'
                       ELSE 'automatic' END"""
            )
        feed_columns = {row["name"] for row in connection.execute("PRAGMA table_info(feeds)").fetchall()}
        if "llm_enabled" not in feed_columns:
            connection.execute("ALTER TABLE feeds ADD COLUMN llm_enabled INTEGER NOT NULL DEFAULT 1")
        if "ai_mode" not in feed_columns:
            connection.execute("ALTER TABLE feeds ADD COLUMN ai_mode TEXT NOT NULL DEFAULT 'inherit'")
            connection.execute("UPDATE feeds SET ai_mode='off' WHERE llm_enabled=0")
        if "title_locked" not in feed_columns:
            connection.execute("ALTER TABLE feeds ADD COLUMN title_locked INTEGER NOT NULL DEFAULT 0")
        if "position" not in feed_columns:
            connection.execute("ALTER TABLE feeds ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
            # Existing readers displayed child groups before loose feeds.  Seed a
            # deterministic order without changing any hierarchy; the first drag
            # operation will compact positions to consecutive integers.
            connection.execute(
                """UPDATE feeds SET position=100000 + (
                       SELECT COUNT(*) FROM feeds earlier
                       WHERE earlier.group_id=feeds.group_id
                         AND (earlier.title COLLATE NOCASE < feeds.title COLLATE NOCASE
                              OR (earlier.title=feeds.title AND earlier.id<feeds.id)))"""
            )
        lock_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(job_locks)").fetchall()
        }
        if "cancel_requested" not in lock_columns:
            connection.execute(
                "ALTER TABLE job_locks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            )
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(llm_runs)").fetchall()
        }
        if "ai_job_id" not in run_columns:
            connection.execute("ALTER TABLE llm_runs ADD COLUMN ai_job_id INTEGER")
        if "stage" not in run_columns:
            connection.execute("ALTER TABLE llm_runs ADD COLUMN stage TEXT NOT NULL DEFAULT 'evaluation'")
        if "batch_number" not in run_columns:
            connection.execute("ALTER TABLE llm_runs ADD COLUMN batch_number INTEGER NOT NULL DEFAULT 1")
        summary_columns = {row["name"] for row in connection.execute("PRAGMA table_info(summaries)").fetchall()}
        if "changes" not in summary_columns:
            connection.execute("ALTER TABLE summaries ADD COLUMN changes TEXT NOT NULL DEFAULT ''")
        if "sections_json" not in summary_columns:
            connection.execute("ALTER TABLE summaries ADD COLUMN sections_json TEXT NOT NULL DEFAULT '[]'")
        if "scope_feed_id" not in summary_columns:
            connection.execute(
                "ALTER TABLE summaries ADD COLUMN scope_feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE"
            )
        if "ai_job_id" not in summary_columns:
            connection.execute(
                "ALTER TABLE summaries ADD COLUMN ai_job_id INTEGER REFERENCES ai_jobs(id) ON DELETE SET NULL"
            )
        if "scope_kind" not in summary_columns:
            connection.execute("ALTER TABLE summaries ADD COLUMN scope_kind TEXT NOT NULL DEFAULT 'group'")
        if "scope_id" not in summary_columns:
            connection.execute("ALTER TABLE summaries ADD COLUMN scope_id INTEGER")
        if "policy_hash" not in summary_columns:
            connection.execute("ALTER TABLE summaries ADD COLUMN policy_hash TEXT NOT NULL DEFAULT ''")
        connection.execute(
            """UPDATE summaries SET
                   scope_kind=CASE WHEN scope_feed_id IS NULL THEN 'group' ELSE 'feed' END,
                   scope_id=COALESCE(scope_feed_id, group_id)
               WHERE scope_id IS NULL"""
        )
        evaluation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(summary_items)").fetchall()
        }
        if "evaluation_id" not in evaluation_columns:
            connection.execute(
                "ALTER TABLE summary_items ADD COLUMN evaluation_id INTEGER REFERENCES ai_evaluations(id) ON DELETE SET NULL"
            )
        notification_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(notification_deliveries)").fetchall()
        }
        if "minimum_relevance" not in notification_columns:
            connection.execute(
                "ALTER TABLE notification_deliveries ADD COLUMN minimum_relevance INTEGER"
            )
        if "policy_scope_kind" not in notification_columns:
            connection.execute(
                """ALTER TABLE notification_deliveries ADD COLUMN
                   policy_scope_kind TEXT NOT NULL DEFAULT 'global'"""
            )
        if "policy_scope_id" not in notification_columns:
            connection.execute(
                "ALTER TABLE notification_deliveries ADD COLUMN policy_scope_id INTEGER"
            )
        if "policy_label" not in notification_columns:
            connection.execute(
                """ALTER TABLE notification_deliveries ADD COLUMN
                   policy_label TEXT NOT NULL DEFAULT 'All feeds'"""
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_feed_run ON summaries(scope_feed_id, llm_run_id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_exact_scope ON summaries(scope_kind, scope_id, llm_run_id DESC)"
        )
        # The review queue is deliberately stored rather than inferred. This
        # makes processing/retry transitions observable and recoverable after a
        # process restart while preserving the former summary_eligible flag for
        # backwards-compatible backlog controls.
        connection.execute(
            """INSERT OR IGNORE INTO ai_review_queue(
                   item_id,state,queued_at,updated_at,available_at
               )
               SELECT i.id,
                      CASE
                        WHEN EXISTS (
                          SELECT 1 FROM summary_items si
                          JOIN summaries s ON s.id=si.summary_id
                          JOIN llm_runs lr ON lr.id=s.llm_run_id
                          WHERE si.item_id=i.id AND lr.status='success'
                        ) THEN 'reviewed'
                        WHEN i.summary_eligible=1 THEN 'waiting'
                        ELSE 'archived'
                      END,
                      i.discovered_at, ?, NULL
               FROM items i""",
            (utcnow(),),
        )
        # Preserve successful historical scoring as canonical evaluations. A
        # summary revision may be removed or become stale without erasing the
        # fact that its feed entry was already evaluated.
        historical = connection.execute(
            """SELECT si.item_id,si.summary_id,si.importance,si.description,si.justification,
                      si.story_cluster,s.llm_run_id,lr.model,lr.prompt_version,lr.completed_at
                 FROM summary_items si JOIN summaries s ON s.id=si.summary_id
                 JOIN llm_runs lr ON lr.id=s.llm_run_id
                 WHERE lr.status='success' AND si.importance IS NOT NULL
                 ORDER BY lr.id,si.summary_id"""
        ).fetchall()
        for row in historical:
            if connection.execute(
                "SELECT 1 FROM ai_evaluations WHERE item_id=? AND current=1",
                (row["item_id"],),
            ).fetchone():
                continue
            connection.execute(
                """INSERT INTO ai_evaluations(
                       item_id,llm_run_id,policy_hash,model,language,relevance,
                       description,justification,story_cluster,current,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
                (
                    row["item_id"], row["llm_run_id"], row["prompt_version"], row["model"],
                    "Unknown", max(0, min(100, int(row["importance"] or 0))),
                    str(row["description"] or ""), str(row["justification"] or ""),
                    str(row["story_cluster"] or ""), row["completed_at"] or utcnow(),
                ),
            )
        reconcile_interrupted_state(connection)


def reconcile_interrupted_state(connection: sqlite3.Connection) -> dict[str, int]:
    """Recover persisted transitions whose owning process is no longer alive.

    Active leases win.  Rows are changed only after a grace period, which keeps
    a second web worker from mistaking legitimate startup work for a crash.
    """
    now = datetime.now(UTC)
    now_text = now.isoformat(timespec="seconds")
    grace = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    queue_grace = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    delivery_grace = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    with transaction(connection, immediate=True):
        connection.execute("DELETE FROM job_locks WHERE expires_at<?", (now_text,))
        cursor = connection.execute(
            """UPDATE app_operations
               SET state='failed',phase='complete',completed_at=?,
                   message='The server restarted before this operation completed',
                   error='Interrupted: no live operation lease'
               WHERE state IN ('queued','running') AND created_at<?
                 AND NOT EXISTS (
                   SELECT 1 FROM job_locks lock
                   WHERE lock.name=app_operations.lock_name
                     AND lock.owner=app_operations.lock_owner AND lock.expires_at>=?
                 )""",
            (now_text, grace, now_text),
        )
        counts["operations"] = max(0, cursor.rowcount)
        cursor = connection.execute(
            """UPDATE refresh_runs SET status='failed',completed_at=?,
                   error='Interrupted by a server restart; checking feeds is safe to retry'
               WHERE status='running' AND started_at<? AND NOT EXISTS (
                 SELECT 1 FROM job_locks WHERE name='feed-refresh' AND expires_at>=?
               )""",
            (now_text, grace, now_text),
        )
        counts["refresh_runs"] = max(0, cursor.rowcount)
        cursor = connection.execute(
            """UPDATE ai_jobs SET status='failed',stage='interrupted',completed_at=?,
                   error='Interrupted by a server restart; completed evaluations were retained'
               WHERE status='running' AND started_at<? AND NOT EXISTS (
                 SELECT 1 FROM job_locks
                 WHERE name IN ('llm-summary','summary-update') AND expires_at>=?
               )""",
            (now_text, grace, now_text),
        )
        counts["ai_jobs"] = max(0, cursor.rowcount)
        cursor = connection.execute(
            """UPDATE llm_runs SET status='failed',completed_at=?,
                   error='Interrupted by a server restart; this provider attempt may be retried'
               WHERE status='running' AND started_at<? AND NOT EXISTS (
                 SELECT 1 FROM job_locks
                 WHERE name IN ('llm-summary','summary-update') AND expires_at>=?
               )""",
            (now_text, grace, now_text),
        )
        counts["llm_runs"] = max(0, cursor.rowcount)
        cursor = connection.execute(
            """UPDATE ai_review_queue
               SET state='retry',updated_at=?,available_at=?,claimed_run_id=NULL,
                   last_error='Interrupted while processing; retained for retry'
               WHERE state='processing' AND updated_at<?""",
            (now_text, now_text, queue_grace),
        )
        counts["queue_claims"] = max(0, cursor.rowcount)
        cursor = connection.execute(
            """UPDATE notification_deliveries
               SET status='failed',error='Interrupted during delivery; review before retrying'
               WHERE status='sending' AND attempted_at<?""",
            (delivery_grace,),
        )
        counts["notifications"] = max(0, cursor.rowcount)
        if connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type='table'
               AND name='distillfeed_arxiv_notifications'"""
        ).fetchone():
            cursor = connection.execute(
                """UPDATE distillfeed_arxiv_notifications
                   SET status='failed',error='Interrupted during delivery; review before retrying'
                   WHERE status='sending' AND attempted_at<?""",
                (delivery_grace,),
            )
            counts["arxiv_notifications"] = max(0, cursor.rowcount)
    return counts


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def acquire_lock(
    connection: sqlite3.Connection,
    name: str,
    owner: str,
    ttl_minutes: int = 60,
    *,
    exclusive: bool = False,
) -> bool:
    now = datetime.now(UTC)
    with transaction(connection, immediate=True):
        connection.execute("DELETE FROM job_locks WHERE expires_at < ?", (now.isoformat(),))
        if exclusive and connection.execute("SELECT 1 FROM job_locks LIMIT 1").fetchone():
            return False
        if not exclusive and connection.execute(
            "SELECT 1 FROM job_locks WHERE name='maintenance' LIMIT 1"
        ).fetchone():
            return False
        try:
            connection.execute(
                "INSERT INTO job_locks(name, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (name, owner, now.isoformat(), (now + timedelta(minutes=ttl_minutes)).isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def release_lock(connection: sqlite3.Connection, name: str, owner: str) -> None:
    connection.execute("DELETE FROM job_locks WHERE name = ? AND owner = ?", (name, owner))


def renew_lock(
    connection: sqlite3.Connection, name: str, owner: str, ttl_minutes: int,
) -> bool:
    """Extend a live lease without ever resurrecting an expired lock."""
    now = datetime.now(UTC)
    cursor = connection.execute(
        """UPDATE job_locks SET expires_at=?
           WHERE name=? AND owner=? AND expires_at>=?""",
        ((now + timedelta(minutes=ttl_minutes)).isoformat(), name, owner, now.isoformat()),
    )
    return cursor.rowcount == 1


def cancellation_requested(
    connection: sqlite3.Connection, name: str, owner: str,
) -> bool:
    """Return whether the current owner has been asked to stop at a safe boundary."""
    row = connection.execute(
        "SELECT cancel_requested FROM job_locks WHERE name=? AND owner=?",
        (name, owner),
    ).fetchone()
    return bool(row and row["cancel_requested"])


def request_job_cancellation(
    connection: sqlite3.Connection, names: tuple[str, ...],
) -> list[str]:
    """Request cancellation for existing jobs and return the affected lock names."""
    if not names:
        return []
    marks = ",".join("?" for _ in names)
    now = utcnow()
    with transaction(connection, immediate=True):
        rows = connection.execute(
            f"SELECT name FROM job_locks WHERE name IN ({marks}) AND expires_at>=?",
            (*names, now),
        ).fetchall()
        affected = [str(row["name"]) for row in rows]
        if affected:
            affected_marks = ",".join("?" for _ in affected)
            connection.execute(
                f"UPDATE job_locks SET cancel_requested=1 WHERE name IN ({affected_marks})",
                affected,
            )
    return affected


def ensure_ungrouped(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT id FROM groups WHERE parent_id IS NULL AND title = 'Ungrouped'").fetchone()
    if row:
        return int(row["id"])
    cursor = connection.execute(
        "INSERT INTO groups(parent_id, title, position, created_at) VALUES(NULL, 'Ungrouped', 0, ?)",
        (utcnow(),),
    )
    return int(cursor.lastrowid)


def group_descendant_ids(connection: sqlite3.Connection, group_id: int) -> list[int]:
    rows = connection.execute(
        """WITH RECURSIVE descendants(id) AS (
               SELECT id FROM groups WHERE id = ?
               UNION ALL
               SELECT g.id FROM groups g JOIN descendants d ON g.parent_id = d.id
           ) SELECT id FROM descendants""",
        (group_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def llm_enabled_group_ids(connection: sqlite3.Connection) -> list[int]:
    """Return groups enabled themselves and through every ancestor."""
    rows = connection.execute("SELECT id, parent_id, llm_enabled, ai_mode FROM groups").fetchall()
    by_id = {int(row["id"]): row for row in rows}
    cache: dict[int, bool] = {}

    def enabled(group_id: int, trail: set[int] | None = None) -> bool:
        if group_id in cache:
            return cache[group_id]
        trail = set() if trail is None else trail
        if group_id in trail:
            cache[group_id] = False
            return False
        row = by_id.get(group_id)
        if row is None or not bool(row["llm_enabled"]) or str(row["ai_mode"]) == "off":
            cache[group_id] = False
            return False
        parent_id = row["parent_id"]
        result = parent_id is None or enabled(int(parent_id), trail | {group_id})
        cache[group_id] = result
        return result

    return [group_id for group_id in by_id if enabled(group_id)]
