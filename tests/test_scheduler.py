from datetime import UTC, datetime, timedelta

from rss_reader.config import load_config
from rss_reader.db import connect, utcnow
from rss_reader.notifications import deliver_ntfy_for_job
from rss_reader.scheduler import (
    LAST_RESULT_KEY,
    NEXT_RUN_KEY,
    claim_due_refresh,
    scheduled_refresh_once,
)
from rss_reader.web import create_app


def _csrf(client) -> str:
    return client.get("/api/csrf").get_json()["csrf_token"]


def test_schedule_claim_is_durable_and_shared_by_independent_instances(configured):
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    first = load_config(configured.path)
    second = load_config(configured.path)

    assert claim_due_refresh(first, now=now) is True
    assert claim_due_refresh(second, now=now) is False
    assert claim_due_refresh(second, now=now + timedelta(minutes=29)) is False
    assert claim_due_refresh(second, now=now + timedelta(minutes=30)) is True

    with connect(configured.database_path) as connection:
        stored = connection.execute(
            "SELECT value FROM settings WHERE key=?", (NEXT_RUN_KEY,)
        ).fetchone()[0]
    assert stored == "2026-07-14T09:00:00+00:00"


def test_due_server_cycle_runs_real_refresh_summary_and_push_chain(configured, monkeypatch):
    configured.data["app"].update({
        "background_scheduler_enabled": True,
        "auto_summarize_after_refresh": True,
    })
    calls = []
    monkeypatch.setattr(
        "rss_reader.scheduler.run_update_summaries",
        lambda config, **kwargs: calls.append(kwargs) or {
            "status": "success",
            "refresh": {"status": "success", "new_items": 3},
            "summary": {"status": "success", "notifications": {
                "status": "success", "delivered": 2,
            }},
        },
    )

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    result = scheduled_refresh_once(configured, now=now)
    repeated = scheduled_refresh_once(configured, now=now)

    assert result["status"] == "success"
    assert result["summary"]["notifications"] == {
        "status": "success", "delivered": 2,
    }
    assert repeated == {"status": "waiting"}
    assert calls == [{"automatic": True}]
    with connect(configured.database_path) as connection:
        last = connection.execute(
            "SELECT value FROM settings WHERE key=?", (LAST_RESULT_KEY,)
        ).fetchone()[0]
    assert last.endswith("|refresh success · summary success · ntfy success")


def test_due_server_cycle_with_no_new_items_checks_backlog_without_push(configured, monkeypatch):
    configured.data["app"].update({
        "background_scheduler_enabled": True,
        "auto_summarize_after_refresh": True,
    })
    calls = []
    monkeypatch.setattr(
        "rss_reader.scheduler.run_update_summaries",
        lambda config, **kwargs: calls.append(kwargs) or {
            "status": "success", "refresh": {"status": "success", "new_items": 0},
            "summary": {"status": "empty", "message": "No entries are ready"},
        },
    )

    result = scheduled_refresh_once(
        configured, now=datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    )

    assert result["status"] == "success"
    assert result["refresh"]["new_items"] == 0
    assert result["summary"]["status"] == "empty"
    assert calls == [{"automatic": True}]


def test_deferred_automatic_summary_is_retried_after_cooldown_without_new_feed_items(
    configured, monkeypatch,
):
    configured.data["app"].update({
        "background_scheduler_enabled": True,
        "auto_summarize_after_refresh": True,
    })
    cycles = iter((
        {"status": "success", "refresh": {"status": "success", "new_items": 4},
         "summary": {"status": "empty"}},
        {"status": "success", "refresh": {"status": "success", "new_items": 0},
         "summary": {"status": "success", "notifications": {"status": "success", "delivered": 1}}},
    ))
    monkeypatch.setattr(
        "rss_reader.scheduler.run_update_summaries", lambda *args, **kwargs: next(cycles)
    )

    first = scheduled_refresh_once(
        configured, now=datetime(2026, 7, 14, 11, 0, tzinfo=UTC)
    )
    second = scheduled_refresh_once(
        configured, now=datetime(2026, 7, 14, 11, 30, tzinfo=UTC)
    )

    assert first["summary"]["status"] == "empty"
    assert second["refresh"]["new_items"] == 0
    assert second["summary"]["status"] == "success"
    assert second["summary"]["notifications"]["delivered"] == 1


def test_repeated_due_cycles_use_the_real_durable_ntfy_duplicate_ledger(configured, monkeypatch):
    configured.data["app"].update({
        "background_scheduler_enabled": True,
        "auto_summarize_after_refresh": True,
    })
    configured.data["notifications"]["ntfy"].update({
        "enabled": True,
        "server_url": "https://ntfy.example.test",
        "topic": "scheduled_phone",
        "minimum_relevance": 90,
        "max_items_per_summary": 3,
    })
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Scheduled',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Scheduled feed", "https://example.test/scheduled", utcnow()),
        ).lastrowid
        item = connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,url,discovered_at)
               VALUES(?,?,?,?,?)""",
            (feed, "scheduled-item", "High signal", "https://example.test/high", utcnow()),
        ).lastrowid
    request_number = 0

    def scheduled_update(config, **kwargs):
        nonlocal request_number
        request_number += 1
        with connect(config.database_path) as connection:
            job = connection.execute(
                """INSERT INTO ai_jobs(
                       request_key,trigger_kind,scope_kind,scope_id,policy_hash,
                       policy_json,status,stage,planned_items,completed_items,
                       planned_requests,started_at,completed_at
                   ) VALUES(?, 'automatic', 'global', NULL, 'policy', '{}',
                            'success', 'completed', 1, 1, 2, ?, ?)""",
                (f"scheduled-job-{request_number}", utcnow(), utcnow()),
            ).lastrowid
            run = connection.execute(
                """INSERT INTO llm_runs(
                       request_key,started_at,completed_at,status,model,prompt_version,
                       pricing_json,ai_job_id,stage
                   ) VALUES(?,?,?,'success','model','prompt','{}',?,'composition')""",
                (f"scheduled-run-{request_number}", utcnow(), utcnow(), job),
            ).lastrowid
            summary = connection.execute(
                """INSERT INTO summaries(
                       llm_run_id,group_id,ai_job_id,scope_kind,scope_id,
                       policy_hash,created_at
                   ) VALUES(?,?,?,'group',?,'policy',?)""",
                (run, group, job, group, utcnow()),
            ).lastrowid
            connection.execute(
                """INSERT INTO summary_items(
                       summary_id,item_id,included,rank,importance,description,justification
                   ) VALUES(?,?,1,1,96,'Description','Strong match')""",
                (summary, item),
            )
            notification = deliver_ntfy_for_job(
                connection, config, int(job), post=post,
            )
        return {
            "status": "success",
            "refresh": {"status": "success", "new_items": 1},
            "summary": {
                "status": "success", "job_id": int(job),
                "notifications": notification,
            },
        }

    pushes = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": f"push-{len(pushes)}"}

    def post(*args, **kwargs):
        pushes.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("rss_reader.scheduler.run_update_summaries", scheduled_update)
    first = scheduled_refresh_once(
        configured, now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    )
    second = scheduled_refresh_once(
        configured, now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    )

    assert first["summary"]["notifications"]["delivered"] == 1
    assert second["summary"]["notifications"]["delivered"] == 0
    assert second["summary"]["notifications"]["duplicates"] == 1
    assert [payload["title"] for payload in pushes] == ["High signal"]
    with connect(configured.database_path) as connection:
        deliveries = connection.execute(
            "SELECT status,destination_key,item_id FROM notification_deliveries"
        ).fetchall()
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "delivered"


def test_disabled_or_failed_server_cycle_has_explicit_durable_state(configured, monkeypatch):
    assert scheduled_refresh_once(configured) == {"status": "disabled"}
    configured.data["app"]["background_scheduler_enabled"] = True
    monkeypatch.setattr(
        "rss_reader.scheduler.run_refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network offline")),
    )

    result = scheduled_refresh_once(
        configured, now=datetime(2026, 7, 14, 11, 0, tzinfo=UTC)
    )

    assert result == {"status": "failed", "message": "network offline"}
    with connect(configured.database_path) as connection:
        last = connection.execute(
            "SELECT value FROM settings WHERE key=?", (LAST_RESULT_KEY,)
        ).fetchone()[0]
        next_run = connection.execute(
            "SELECT value FROM settings WHERE key=?", (NEXT_RUN_KEY,)
        ).fetchone()[0]
    assert last.endswith("|failed")
    assert next_run == "2026-07-14T11:30:00+00:00"


def test_settings_enable_server_schedule_and_defer_first_cycle(configured):
    app = create_app(str(configured.path))
    client = app.test_client()
    scheduler = app.extensions["distillfeed_scheduler"]
    assert scheduler.running is False

    response = client.post(
        "/api/config",
        json={"values": {
            "app.background_scheduler_enabled": True,
            "app.refresh_interval_minutes": 7,
            "app.auto_summarize_after_refresh": True,
        }},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 200
    assert scheduler.running is True
    with connect(configured.database_path) as connection:
        next_run = connection.execute(
            "SELECT value FROM settings WHERE key=?", (NEXT_RUN_KEY,)
        ).fetchone()[0]
    assert datetime.fromisoformat(next_run) > datetime.now(UTC)
    reloaded = load_config(configured.path)
    assert reloaded.get("app", "background_scheduler_enabled") is True
    assert reloaded.get("app", "auto_summarize_after_refresh") is True
    assert reloaded.get("app", "refresh_interval_minutes") == 7
    scheduler.stop(wait=True)
