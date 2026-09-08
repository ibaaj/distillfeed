import sqlite3

import pytest

from rss_reader.db import (
    acquire_lock,
    cancellation_requested,
    connect,
    connect_readonly,
    initialize,
    release_lock,
    request_job_cancellation,
)


def test_connection_context_releases_database_handle(configured):
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_maintenance_lock_is_mutually_exclusive_with_jobs(configured):
    with connect(configured.database_path) as connection:
        assert acquire_lock(connection, "feed-refresh", "refresh") is True
        assert acquire_lock(connection, "maintenance", "restore", exclusive=True) is False
        release_lock(connection, "feed-refresh", "refresh")
        assert acquire_lock(connection, "maintenance", "restore", exclusive=True) is True
        assert acquire_lock(connection, "llm-summary", "summary") is False
        release_lock(connection, "maintenance", "restore")
        assert acquire_lock(connection, "llm-summary", "summary") is True


def test_job_cancellation_is_scoped_to_the_current_owner(configured):
    with connect(configured.database_path) as connection:
        assert acquire_lock(connection, "feed-refresh", "refresh-owner") is True
        assert cancellation_requested(connection, "feed-refresh", "refresh-owner") is False
        assert request_job_cancellation(
            connection, ("feed-refresh", "llm-summary")
        ) == ["feed-refresh"]
        assert cancellation_requested(connection, "feed-refresh", "refresh-owner") is True
        assert cancellation_requested(connection, "feed-refresh", "another-owner") is False
        release_lock(connection, "feed-refresh", "refresh-owner")
        assert acquire_lock(connection, "feed-refresh", "next-owner") is True
        assert cancellation_requested(connection, "feed-refresh", "next-owner") is False


def test_readonly_connection_cannot_mutate_migration_source(configured):
    with connect_readonly(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "INSERT INTO groups(title,position,created_at) VALUES('No',0,'now')"
            )


def test_initialize_migrates_legacy_columns_before_creating_their_indexes(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                stable_id TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                author TEXT,
                published_at TEXT,
                discovered_at TEXT NOT NULL,
                description_text TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                is_starred INTEGER NOT NULL DEFAULT 0,
                UNIQUE(feed_id, stable_id)
            );
            CREATE TABLE summaries (
                id INTEGER PRIMARY KEY,
                llm_run_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                overview TEXT NOT NULL DEFAULT '',
                changes TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(llm_run_id, group_id)
            );
            """
        )

    initialize(database)

    with sqlite3.connect(database) as connection:
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
        item_indexes = {row[1] for row in connection.execute("PRAGMA index_list(items)")}
        summary_columns = {row[1] for row in connection.execute("PRAGMA table_info(summaries)")}
        summary_indexes = {row[1] for row in connection.execute("PRAGMA index_list(summaries)")}

    assert {"summary_eligible", "is_read_later"} <= item_columns
    assert {"idx_items_summary_eligible", "idx_items_read_later"} <= item_indexes
    assert "scope_feed_id" in summary_columns
    assert "idx_summaries_feed_run" in summary_indexes


def test_initialize_adds_device_alert_policy_audit_columns_to_legacy_deliveries(tmp_path):
    database = tmp_path / "legacy-notifications.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE notification_deliveries (
                id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL,
                destination_key TEXT NOT NULL,
                llm_run_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                relevance INTEGER NOT NULL,
                status TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                delivered_at TEXT,
                provider_message_id TEXT,
                error TEXT,
                UNIQUE(channel, destination_key, item_id)
            );
            """
        )

    initialize(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(notification_deliveries)"
        )}
        policy_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ntfy_scope_rules'"
        ).fetchone()
    assert {
        "minimum_relevance", "policy_scope_kind", "policy_scope_id", "policy_label",
    } <= columns
    assert policy_table is not None


def test_initialize_adds_group_review_display_mode_with_safe_default(tmp_path):
    database = tmp_path / "legacy-group-layout.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE groups (
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
            INSERT INTO groups(id,parent_id,title,position,created_at)
            VALUES(1,NULL,'Legacy group',0,'2026-08-18T00:00:00+00:00');
            """
        )

    initialize(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)")}
        mode = connection.execute(
            "SELECT review_display_mode FROM groups WHERE id=1"
        ).fetchone()[0]
    assert "review_display_mode" in columns
    assert mode == "daily"



def test_initialize_preserves_future_source_date_and_backfills_explicit_shorts(tmp_path):
    database = tmp_path / "legacy-item-metadata.sqlite3"
    initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO groups(id,title,position,created_at) VALUES(1,'Videos',0,'2026-09-08T00:00:00+00:00')"
        )
        connection.execute(
            """INSERT INTO feeds(id,group_id,title,xml_url,created_at)
               VALUES(1,1,'Channel','https://example.test/feed.xml','2026-09-08T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO items(
                   id,feed_id,stable_id,title,url,published_at,source_published_at,
                   discovered_at,date_warning,content_kind,content_kind_source
               ) VALUES(1,1,'future','Future item','https://example.test/article',
                        '2026-09-12T12:00:00+00:00',NULL,'2026-09-08T12:00:00+00:00',NULL,'','')"""
        )
        connection.execute(
            """INSERT INTO items(
                   id,feed_id,stable_id,title,url,published_at,discovered_at,
                   content_kind,content_kind_source
               ) VALUES(2,1,'short','Short','https://www.youtube.com/shorts/NmiD1YdbrAA',
                        '2026-09-08T10:00:00+00:00','2026-09-08T10:01:00+00:00','','')"""
        )
        connection.commit()

    initialize(database)
    initialize(database)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        future = connection.execute("SELECT * FROM items WHERE id=1").fetchone()
        short = connection.execute("SELECT * FROM items WHERE id=2").fetchone()
        feed_columns = {row[1] for row in connection.execute("PRAGMA table_info(feeds)")}
    assert future["source_published_at"] == "2026-09-12T12:00:00+00:00"
    assert future["published_at"] == "2026-09-08T12:00:00+00:00"
    assert future["date_warning"] == "future-source-date"
    assert short["content_kind"] == "youtube-short"
    assert short["content_kind_source"] == "explicit-url"
    assert {"last_content_type", "last_error_kind"} <= feed_columns
