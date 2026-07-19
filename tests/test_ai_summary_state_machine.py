from __future__ import annotations

from collections.abc import Callable

import pytest

from rss_reader import ai_engine
from rss_reader.ai_engine import execute_ai_update
from rss_reader.ai_policy import build_plan
from rss_reader.ai_queue import set_item_disposition, sync_review_queue
from rss_reader.db import connect, utcnow
from rss_reader.service import run_refresh, run_update_summaries


def seed(connection, *, title: str = "State", items: int = 2) -> tuple[int, int, list[int]]:
    group_id = int(connection.execute(
        "INSERT INTO groups(title,position,created_at) VALUES(?,?,?)",
        (title, 0, utcnow()),
    ).lastrowid)
    feed_id = int(connection.execute(
        "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
        (group_id, f"{title} feed", f"https://example.test/{title}", utcnow()),
    ).lastrowid)
    item_ids = [int(connection.execute(
        """INSERT INTO items(
               feed_id,stable_id,title,url,description_text,discovered_at
           ) VALUES(?,?,?,?,?,?)""",
        (
            feed_id, f"{title}-{number}", f"{title} item {number}",
            f"https://example.test/{title}/{number}", "Feed-provided description", utcnow(),
        ),
    ).lastrowid) for number in range(items)]
    sync_review_queue(connection)
    return group_id, feed_id, item_ids


def successful_provider(calls: list[str] | None = None) -> Callable:
    def provider(snapshot, *, payload, schema_name, **kwargs):
        if calls is not None:
            calls.append(schema_name)
        if schema_name == "distillfeed_evaluations":
            return {
                "evaluations": {
                    str(entry["item_id"]): {
                        "relevance": 91,
                        "description": f"Summary of {entry['title']}",
                        "justification": "A strong match for the configured interests.",
                        "story_cluster": "State transition",
                    }
                    for entry in payload["entries"]
                }
            }, {}
        return {
            "changes": "The evaluated evidence changed.",
            "sections": [{"heading": "State transition", "body": "A concise digest."}],
        }, {}

    return provider


def test_check_feeds_never_calls_ai_and_update_has_two_durable_stages(configured, monkeypatch):
    group_id: int
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=2)
    monkeypatch.setattr(
        "rss_reader.service.refresh_all",
        lambda *args, **kwargs: {
            "attempted": 1, "succeeded": 1, "failed": 0, "new_items": 0,
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(ai_engine, "_provider_json", successful_provider(calls))

    checked = run_refresh(configured, group_id=group_id, force=True)
    assert checked["status"] == "success" and calls == []

    updated = run_update_summaries(
        configured, group_id=group_id, include_plugins=False,
    )
    assert updated["status"] == "success"
    assert calls == ["distillfeed_evaluations", "distillfeed_summary"]
    with connect(configured.database_path) as connection:
        assert {
            row["state"] for row in connection.execute(
                "SELECT state FROM ai_review_queue WHERE item_id IN (?,?)", item_ids,
            )
        } == {"reviewed"}
        summary = connection.execute(
            "SELECT * FROM summaries WHERE scope_kind='group' AND scope_id=?",
            (group_id,),
        ).fetchone()
        assert summary and summary["ai_job_id"] == updated["summary"]["job_id"]

    calls.clear()
    with connect(configured.database_path) as connection:
        repeated = execute_ai_update(connection, configured, group_id=group_id)
    assert repeated["status"] == "empty" and calls == []


def test_failed_evaluation_returns_claim_to_retry_then_succeeds(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=1)

        monkeypatch.setattr(
            ai_engine, "_provider_json",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
        )
        failed = execute_ai_update(connection, configured, group_id=group_id)
        assert failed["status"] == "failed"
        assert failed["failed_batches"][0]["code"] == "AI_REQUEST_FAILED"
        retry = connection.execute(
            "SELECT * FROM ai_review_queue WHERE item_id=?", (item_ids[0],),
        ).fetchone()
        assert retry["state"] == "retry" and retry["claimed_run_id"] is None
        assert retry["available_at"] and "provider unavailable" in retry["last_error"]

        monkeypatch.setattr(ai_engine, "_provider_json", successful_provider())
        result = execute_ai_update(connection, configured, group_id=group_id)
        reviewed = connection.execute(
            "SELECT * FROM ai_review_queue WHERE item_id=?", (item_ids[0],),
        ).fetchone()
        assert result["status"] == "success"
        assert reviewed["state"] == "reviewed" and reviewed["attempts"] == 2


def test_failed_batch_does_not_starve_later_independent_batch(configured, monkeypatch):
    configured.data["llm"]["max_entries_total"] = 1
    configured.data["llm"]["max_entries_per_feed"] = 1
    success = successful_provider()
    evaluation_calls = 0

    def provider(*args, **kwargs):
        nonlocal evaluation_calls
        if kwargs["schema_name"] == "distillfeed_evaluations":
            evaluation_calls += 1
            if evaluation_calls == 1:
                raise ValueError("invalid JSON in first batch")
        return success(*args, **kwargs)

    monkeypatch.setattr(ai_engine, "_provider_json", provider)
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=2)
        result = execute_ai_update(connection, configured, group_id=group_id)
        states = {
            int(row["item_id"]): str(row["state"])
            for row in connection.execute(
                "SELECT item_id,state FROM ai_review_queue WHERE item_id IN (?,?)",
                item_ids,
            ).fetchall()
        }
    assert result["status"] == "partial"
    assert result["failed_batches"][0]["batch"] == 1
    assert states[item_ids[0]] == "retry"
    assert states[item_ids[1]] == "reviewed"


def test_billed_usage_survives_invalid_provider_response(configured, monkeypatch):
    usage = {
        "input_tokens": 321, "cached_input_tokens": 21,
        "output_tokens": 45, "estimated_cost_usd": 0.0123,
        "provider_request_id": "response-with-invalid-json",
    }
    monkeypatch.setattr(
        ai_engine, "_provider_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ai_engine.ProviderResponseError("invalid structured response", usage)
        ),
    )
    with connect(configured.database_path) as connection:
        group_id, _, _ = seed(connection, items=1)
        result = execute_ai_update(connection, configured, group_id=group_id)
        run = connection.execute(
            "SELECT * FROM llm_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert result["status"] == "failed"
    assert run["status"] == "failed"
    assert run["input_tokens"] == 321 and run["output_tokens"] == 45
    assert run["estimated_cost_usd"] == 0.0123
    assert run["provider_request_id"] == "response-with-invalid-json"


def test_stop_keeps_completed_batch_and_leaves_unsent_entries_waiting(configured, monkeypatch):
    configured.data["llm"]["max_entries_total"] = 1
    configured.data["llm"]["max_entries_per_feed"] = 1
    completed_first = False

    def provider(*args, **kwargs):
        nonlocal completed_first
        result = successful_provider()(*args, **kwargs)
        if kwargs["schema_name"] == "distillfeed_evaluations":
            completed_first = True
        return result

    monkeypatch.setattr(ai_engine, "_provider_json", provider)
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=2)
        cancelled = execute_ai_update(
            connection, configured, group_id=group_id,
            cancel_requested=lambda: completed_first,
        )
        states = {
            int(row["item_id"]): str(row["state"])
            for row in connection.execute(
                "SELECT item_id,state FROM ai_review_queue WHERE item_id IN (?,?)", item_ids,
            ).fetchall()
        }
        assert cancelled["status"] == "cancelled"
        assert sorted(states.values()) == ["reviewed", "waiting"]
        assert connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0

        completed_first = False
        resumed = execute_ai_update(connection, configured, group_id=group_id)
        assert resumed["status"] == "success"
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_review_queue WHERE state='reviewed'"
        ).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1


def test_global_update_retries_composition_without_rescoring(configured, monkeypatch):
    evaluation_calls = 0
    composition_calls = 0

    def first_provider(*args, **kwargs):
        nonlocal evaluation_calls, composition_calls
        if kwargs["schema_name"] == "distillfeed_evaluations":
            evaluation_calls += 1
            return successful_provider()(*args, **kwargs)
        composition_calls += 1
        raise RuntimeError("composition unavailable")

    monkeypatch.setattr(ai_engine, "_provider_json", first_provider)
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=1)
        with pytest.raises(RuntimeError, match="composition unavailable"):
            execute_ai_update(connection, configured)
        assert connection.execute(
            "SELECT state FROM ai_review_queue WHERE item_id=?", (item_ids[0],),
        ).fetchone()[0] == "reviewed"
        assert connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0

        monkeypatch.setattr(ai_engine, "_provider_json", successful_provider())
        retried = execute_ai_update(connection, configured)
        assert retried["status"] == "success"
        assert retried["evaluated"] == 0 and len(retried["composition_run_ids"]) == 1
        assert evaluation_calls == 1 and composition_calls == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM summaries WHERE scope_kind='group' AND scope_id=?",
            (group_id,),
        ).fetchone()[0] == 1


def test_running_job_freezes_source_policy_and_next_plan_sees_change(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=1)

        def provider(*args, **kwargs):
            if kwargs["schema_name"] == "distillfeed_evaluations":
                connection.execute(
                    "UPDATE groups SET ai_mode='off',llm_enabled=0 WHERE id=?", (group_id,),
                )
            return successful_provider()(*args, **kwargs)

        monkeypatch.setattr(ai_engine, "_provider_json", provider)
        result = execute_ai_update(connection, configured, group_id=group_id)
        assert result["status"] == "success"
        assert connection.execute(
            "SELECT COUNT(*) FROM summary_items WHERE item_id=?", (item_ids[0],),
        ).fetchone()[0] == 1
        next_plan = build_plan(connection, configured, group_id=group_id)
        assert next_plan["selected_count"] == 0 and next_plan["off_count"] == 0
        assert next_plan["policy_hash"] != result["plan"]["policy_hash"]


def test_exclusion_wins_if_set_between_plan_and_claim(configured, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ai_engine, "_provider_json", successful_provider(calls))
    original = ai_engine.mark_processing
    with connect(configured.database_path) as connection:
        group_id, _, item_ids = seed(connection, items=1)

        def exclude_then_claim(connection, identifiers, run_id):
            set_item_disposition(connection, item_ids, "excluded")
            original(connection, identifiers, run_id)

        monkeypatch.setattr(ai_engine, "mark_processing", exclude_then_claim)
        result = execute_ai_update(connection, configured, group_id=group_id)
        row = connection.execute(
            "SELECT state FROM ai_review_queue WHERE item_id=?", (item_ids[0],),
        ).fetchone()
        assert result["status"] == "empty" and calls == []
        assert row["state"] == "waiting"


def test_plugin_virtual_sources_never_enter_ordinary_plan(configured):
    with connect(configured.database_path) as connection:
        group_id = int(connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('arXiv Digest',0,?)",
            (utcnow(),),
        ).lastrowid)
        feed_id = int(connection.execute(
            """INSERT INTO feeds(group_id,title,xml_url,created_at)
               VALUES(?,?,?,?)""",
            (group_id, "Machine Learning", "plugin://arxiv/cs.LG", utcnow()),
        ).lastrowid)
        connection.execute(
            """INSERT INTO items(
                   feed_id,stable_id,title,discovered_at,summary_eligible
               ) VALUES(?,?,?,?,0)""",
            (feed_id, "paper", "Paper", utcnow()),
        )
        sync_review_queue(connection)
        plan = build_plan(connection, configured)
        assert plan["policy"]["sources"] == {"groups": [], "feeds": []}
        assert plan["selected_count"] == 0
