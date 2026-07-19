from datetime import UTC, datetime, timedelta
from unittest.mock import ANY

import pytest

from rss_reader.db import connect, utcnow
from rss_reader.opml import build_tree_from_database, parse_opml_bytes, write_database_opml
from rss_reader.service import is_refresh_stale
from rss_reader.service import run_refresh, run_summary, run_update_summaries


def _add_feed(connection, group_id: int, stable: str, attempted_at: str | None) -> None:
    connection.execute(
        """INSERT INTO feeds(group_id,title,xml_url,last_attempt_at,created_at)
           VALUES(?,?,?,?,?)""",
        (group_id, stable, f"https://example.test/{stable}", attempted_at, utcnow()),
    )


def test_refresh_is_stale_when_any_enabled_feed_is_old(configured):
    now = datetime.now(UTC)
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('News',0,?)", (utcnow(),)
        ).lastrowid
        _add_feed(connection, group_id, "fresh", now.isoformat())
        _add_feed(connection, group_id, "old", (now - timedelta(hours=2)).isoformat())
        assert is_refresh_stale(connection, configured) is True


def test_refresh_is_fresh_only_after_every_feed_was_recently_attempted(configured):
    now = datetime.now(UTC).isoformat()
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('News',0,?)", (utcnow(),)
        ).lastrowid
        _add_feed(connection, group_id, "one", now)
        _add_feed(connection, group_id, "two", now)
        assert is_refresh_stale(connection, configured) is False


def test_refresh_is_stale_for_a_never_attempted_feed(configured):
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('News',0,?)", (utcnow(),)
        ).lastrowid
        _add_feed(connection, group_id, "new", None)
        assert is_refresh_stale(connection, configured) is True


def test_feed_check_legacy_summarize_flag_never_invokes_ai(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "rss_reader.service.refresh_all",
        lambda connection, config, **kwargs: calls.append(("refresh", kwargs)) or {
            "attempted": 2, "succeeded": 2, "failed": 0, "new_items": 0,
        },
    )
    result = run_refresh(configured, group_id=17, force=True, summarize_after=True)
    assert result["summary_deferred"] is True
    refresh_call = calls[0]
    cancel_check = refresh_call[1].pop("cancel_requested")
    assert callable(cancel_check)
    assert refresh_call == ("refresh", {"feed_id": None, "group_id": 17, "force": True})
    assert len(calls) == 1


def test_refresh_keeps_working_opml_equal_to_discovered_feed_metadata(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Metadata',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            """INSERT INTO feeds(group_id,title,xml_url,html_url,created_at)
               VALUES(?,?,?,?,?)""",
            (group, "Metadata feed", "https://example.test/feed", None, utcnow()),
        ).lastrowid
        write_database_opml(connection, configured.working_opml_path)

    def refresh_with_discovered_url(connection, config, **kwargs):
        connection.execute(
            "UPDATE feeds SET html_url=? WHERE id=?",
            ("https://example.test/site", feed),
        )
        return {"attempted": 1, "succeeded": 1, "failed": 0, "new_items": 0}

    monkeypatch.setattr("rss_reader.service.refresh_all", refresh_with_discovered_url)
    assert run_refresh(configured, feed_id=feed, force=True)["status"] == "success"
    with connect(configured.database_path) as connection:
        database_tree = build_tree_from_database(connection)
    assert parse_opml_bytes(configured.working_opml_path.read_bytes()) == database_tree


def test_refresh_stop_before_fetch_preserves_a_clean_cancelled_run(configured, monkeypatch):
    monkeypatch.setattr("rss_reader.service.cancellation_requested", lambda *args: True)
    monkeypatch.setattr(
        "rss_reader.service.refresh_all",
        lambda *args, **kwargs: pytest.fail("cancelled refresh must not start feed workers"),
    )
    result = run_refresh(configured)
    assert result["status"] == "cancelled"
    assert result["attempted"] == 0 and result["new_items"] == 0
    with connect(configured.database_path) as connection:
        run = connection.execute("SELECT * FROM refresh_runs ORDER BY id DESC").fetchone()
        assert run["status"] == "cancelled" and run["completed_at"]


def test_ai_stop_before_plugin_or_provider_call_leaves_queue_untouched(configured, monkeypatch):
    monkeypatch.setattr("rss_reader.service.cancellation_requested", lambda *args: True)
    monkeypatch.setattr(
        "rss_reader.service.summarize_plugins",
        lambda *args, **kwargs: pytest.fail("cancelled AI review must not start plugins"),
    )
    result = run_summary(configured)
    assert result == {
        "status": "cancelled", "message": "Summary update stopped safely", "cancelled": True,
    }
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0

def test_only_successful_automatic_summaries_trigger_mobile_push(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "rss_reader.service.execute_ai_update",
        lambda *args, **kwargs: {"status": "success", "job_id": 42},
    )
    monkeypatch.setattr(
        "rss_reader.service.deliver_ntfy_for_job",
        lambda connection, config, run_id: calls.append(run_id) or {"status": "success"},
    )
    automatic = run_summary(configured, automatic=True)
    manual = run_summary(configured, automatic=False)
    assert automatic["notifications"] == {"status": "success"}
    assert "notifications" not in manual
    assert calls == [42]


def test_plugin_summary_success_is_visible_when_base_summary_has_no_items(configured, monkeypatch):
    monkeypatch.setattr(
        "rss_reader.service.summarize_plugins",
        lambda *args, **kwargs: {
            "succeeded": 1,
            "failed": 0,
            "plugins": [{"name": "arxiv_digest", "status": "success", "evaluated_items": 100}],
        },
    )
    monkeypatch.setattr(
        "rss_reader.service.execute_ai_update",
        lambda *args, **kwargs: {"status": "empty", "message": "No new unsummarized items"},
    )

    result = run_summary(configured, automatic=False)

    assert result["status"] == "success"
    assert result["message"] == "Plugin summary completed"
    assert result["plugin_summaries_succeeded"] == 1
    assert result["plugins"][0]["name"] == "arxiv_digest"


def test_mobile_push_processing_cannot_turn_a_digest_into_a_failed_run(configured, monkeypatch):
    monkeypatch.setattr(
        "rss_reader.service.execute_ai_update",
        lambda *args, **kwargs: {"status": "success", "job_id": 43},
    )
    monkeypatch.setattr(
        "rss_reader.service.deliver_ntfy_for_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("push database unavailable")),
    )
    result = run_summary(configured, automatic=True)
    assert result["status"] == "success" and result["job_id"] == 43
    assert result["notifications"] == {
        "status": "failed", "message": "push database unavailable",
    }


def test_periodic_refresh_to_summary_to_mobile_push_transition(configured, monkeypatch):
    configured.data["app"]["auto_summarize_after_refresh"] = True
    calls = []
    monkeypatch.setattr(
        "rss_reader.service.refresh_all",
        lambda connection, config, **kwargs: {
            "attempted": 1, "succeeded": 1, "failed": 0, "new_items": 3,
        },
    )
    monkeypatch.setattr(
        "rss_reader.service.execute_ai_update",
        lambda connection, config, **kwargs: calls.append(("summary", kwargs))
        or {"status": "success", "job_id": 88},
    )
    monkeypatch.setattr(
        "rss_reader.service.deliver_ntfy_for_job",
        lambda connection, config, run_id: calls.append(("push", run_id))
        or {"status": "success", "delivered": 1},
    )

    result = run_update_summaries(configured, automatic=True)

    assert result["status"] == "success"
    assert result["summary"]["notifications"] == {"status": "success", "delivered": 1}
    assert calls == [
        ("summary", {
            "automatic": True, "group_id": None, "feed_id": None,
            "cancel_requested": ANY,
        }),
        ("push", 88),
    ]


def test_service_preserves_engine_job_aggregation(configured, monkeypatch):
    configured.data["llm"]["review_workload"] = "balanced"
    calls = []
    monkeypatch.setattr(
        "rss_reader.service.execute_ai_update",
        lambda connection, config, **kwargs: calls.append(kwargs) or {
            "status": "success", "job_id": 101, "submitted": 200,
            "evaluated": 200, "deferred": 40, "requests": 3,
            "run_ids": [101, 102, 103],
        },
    )
    pushes = []
    monkeypatch.setattr(
        "rss_reader.service.deliver_ntfy_for_job",
        lambda connection, config, job_id: pushes.append(job_id) or {"status": "success", "job_id": job_id},
    )

    result = run_summary(configured, automatic=True)

    assert result["submitted"] == 200 and result["requests"] == 3
    assert result["run_ids"] == [101, 102, 103]
    assert calls[0]["automatic"] is True
    assert pushes == [101]
    assert result["notifications"]["job_id"] == 101


def test_retention_preserves_every_explicitly_saved_state(configured, monkeypatch):
    configured.data["app"]["retention_days"] = 1
    configured.data["app"]["auto_baseline_initial_refresh"] = False
    monkeypatch.setattr(
        "rss_reader.service.refresh_all",
        lambda *args, **kwargs: {"attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0},
    )
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Saved',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Source", "https://example.test/saved", utcnow()),
        ).lastrowid
        states = {
            "plain": (0, 0),
            "favorite": (1, 0),
            "later": (0, 1),
            "tagged": (0, 0),
            "unread": (0, 0),
            "summarized": (0, 0),
        }
        ids = {}
        for name, (starred, later) in states.items():
            ids[name] = connection.execute(
                """INSERT INTO items(feed_id,stable_id,title,discovered_at,is_read,is_starred,is_read_later)
                   VALUES(?,?,?,?,?,?,?)""",
                (feed, name, name, old, 0 if name == "unread" else 1, starred, later),
            ).lastrowid
        tag = connection.execute(
            "INSERT INTO tags(name,created_at) VALUES('Keep',?)", (utcnow(),)
        ).lastrowid
        connection.execute(
            "INSERT INTO item_tags(item_id,tag_id) VALUES(?,?)", (ids["tagged"], tag)
        )
        run = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('retention',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        summary = connection.execute(
            "INSERT INTO summaries(llm_run_id,group_id,created_at) VALUES(?,?,?)",
            (run, group, utcnow()),
        ).lastrowid
        connection.execute(
            "INSERT INTO summary_items(summary_id,item_id,included) VALUES(?,?,1)",
            (summary, ids["summarized"]),
        )
    result = run_refresh(configured)
    assert result["deleted_old_items"] == 1
    with connect(configured.database_path) as connection:
        remaining = {
            row["stable_id"] for row in connection.execute("SELECT stable_id FROM items")
        }
    assert remaining == {"favorite", "later", "tagged", "unread", "summarized"}
