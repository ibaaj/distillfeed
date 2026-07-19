import json
import re
from datetime import UTC, datetime, timedelta

from rss_reader.db import connect, reconcile_interrupted_state, utcnow
from rss_reader.ai_queue import sync_review_queue
from rss_reader.ai_readiness import ordinary_readiness
from rss_reader.notice_service import synchronize_issues
from rss_reader.ntfy_policy import replace_ntfy_scope_policy
from rss_reader.operations import create_operation
from rss_reader.web import create_app


def _csrf(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
    assert match
    return match.group(1).decode()


def _ordinary_feed(connection) -> int:
    group = connection.execute(
        "INSERT INTO groups(title,position,created_at) VALUES('Operations',0,?)",
        (utcnow(),),
    ).lastrowid
    return int(connection.execute(
        "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
        (group, "Operation feed", "https://example.test/operations", utcnow()),
    ).lastrowid)


def test_browser_operation_reports_its_exact_terminal_result(configured, monkeypatch):
    pending = []
    with connect(configured.database_path) as connection:
        connection.execute(
            """INSERT INTO refresh_runs(started_at,completed_at,status,feeds_attempted,
                       feeds_succeeded,new_items)
               VALUES(?,?,'success',99,99,99)""",
            (utcnow(), utcnow()),
        )
    monkeypatch.setattr(
        "rss_reader.web.run_refresh",
        lambda config, **kwargs: {
            "status": "partial", "message": "One feed could not be checked",
            "attempted": 2, "succeeded": 1, "failed": 1, "new_items": 3,
        },
    )
    monkeypatch.setattr(
        "rss_reader.web.start_thread",
        lambda target, *, name: pending.append(target),
    )
    client = create_app(str(configured.path)).test_client()
    response = client.post(
        "/api/refresh", json={}, headers={"X-CSRF-Token": _csrf(client.get("/"))},
    )
    assert response.status_code == 202
    operation_id = response.get_json()["operation_id"]
    queued = client.get(f"/api/status?operation_id={operation_id}").get_json()
    assert queued["operation"]["state"] == "queued"
    assert queued["operation"]["active"] is True

    pending[0]()
    completed = client.get(f"/api/status?operation_id={operation_id}").get_json()
    assert completed["operation"]["state"] == "partial"
    assert completed["operation"]["message"] == "One feed could not be checked"
    assert completed["operation"]["result"]["new_items"] == 3
    assert completed["refresh"]["new_items"] == 99  # old global history is not mistaken for this click


def test_missing_api_key_blocks_before_refresh_and_becomes_notice(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        _ordinary_feed(connection)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    assert b"AI credentials are missing" in page.data
    response = client.post(
        "/api/summarize", json={}, headers={"X-CSRF-Token": _csrf(page)},
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "API_KEY_MISSING"
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM app_operations").fetchone()[0] == 0


def test_notice_lifecycle_resolves_when_readiness_recovers(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        _ordinary_feed(connection)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with connect(configured.database_path) as connection:
        active = synchronize_issues(connection, configured)
        assert any(issue["issue_key"] == "credential:OPENAI_API_KEY" for issue in active)
    monkeypatch.setenv("OPENAI_API_KEY", "restored-test-key")
    with connect(configured.database_path) as connection:
        assert not any(
            issue["issue_key"] == "credential:OPENAI_API_KEY"
            for issue in synchronize_issues(connection, configured)
        )
        stored = connection.execute(
            "SELECT active,resolved_at FROM app_issues WHERE issue_key='credential:OPENAI_API_KEY'"
        ).fetchone()
        assert stored["active"] == 0 and stored["resolved_at"]


def test_selected_ntfy_mode_without_a_source_becomes_a_system_notice(configured):
    configured.data["notifications"]["ntfy"].update({
        "enabled": True, "topic": "device_alerts",
    })
    with connect(configured.database_path) as connection:
        _ordinary_feed(connection)
        replace_ntfy_scope_policy(connection, "selected", [])
        active = synchronize_issues(connection, configured)
    issue = next(item for item in active if item["issue_key"] == "ntfy:empty-scope")
    assert issue["title"] == "Device alerts have no active source"
    assert "No article alert will be sent" in issue["message"]
    assert issue["action_url"] == "/?settings=notifications"


def test_local_budget_preflight_is_explicitly_not_provider_balance(configured):
    configured.data["llm"]["monthly_budget_usd"] = 0.000001
    with connect(configured.database_path) as connection:
        feed = _ordinary_feed(connection)
        connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,url,discovered_at,description_text)
               VALUES(?,?,?,?,?,?)""",
            (
                feed, "budget-item", "Budget evidence", "https://example.test/budget",
                utcnow(), "A sufficiently descriptive feed entry for a non-zero token estimate.",
            ),
        )
        sync_review_queue(connection)
        readiness = ordinary_readiness(connection, configured)
    assert readiness["status"] == "blocked"
    assert readiness["budget"]["provider_balance_known"] is False
    assert readiness["budget"]["projected_update_usd"] > 0
    assert any(
        blocker["code"] == "LOCAL_BUDGET_EXCEEDED"
        for blocker in readiness["blockers"]
    )


def test_startup_reconciles_orphaned_runtime_transitions(configured):
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
    with connect(configured.database_path) as connection:
        operation = create_operation(
            connection, kind="summary", trigger="browser",
            lock_name="summary-update", lock_owner="dead-worker",
        )
        connection.execute(
            """UPDATE app_operations SET state='running',phase='evaluating',
                       started_at=?,created_at=? WHERE operation_key=?""",
            (old, old, operation["operation_id"]),
        )
        connection.execute(
            """INSERT INTO ai_jobs(request_key,trigger_kind,scope_kind,policy_hash,
                       policy_json,status,stage,started_at)
               VALUES('orphan-job','manual','all','hash',?,'running','evaluation',?)""",
            (json.dumps({}), old),
        )
        connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,status,model,prompt_version,
                       pricing_json)
               VALUES('orphan-run',?,'running','test','distillfeed-evaluation-test',?)""",
            (old, json.dumps({})),
        )
        counts = reconcile_interrupted_state(connection)
        assert counts["operations"] == 1
        assert counts["ai_jobs"] == 1
        assert counts["llm_runs"] == 1
        assert connection.execute(
            "SELECT state FROM app_operations WHERE operation_key=?",
            (operation["operation_id"],),
        ).fetchone()["state"] == "failed"
        assert connection.execute(
            "SELECT stage FROM ai_jobs WHERE request_key='orphan-job'"
        ).fetchone()["stage"] == "interrupted"
        assert "server restart" in connection.execute(
            "SELECT error FROM llm_runs WHERE request_key='orphan-run'"
        ).fetchone()["error"]


def test_healthcheck_is_minimal_and_available_behind_application_auth(configured, monkeypatch):
    monkeypatch.setenv("DISTILLFEED_AUTH_ENABLED", "true")
    monkeypatch.setenv("RSSREADER_PASSWORD", "private-test-password")
    client = create_app(str(configured.path)).test_client()
    assert client.get("/healthz").get_json() == {"status": "ok"}
    assert client.get("/api/status").status_code == 401
