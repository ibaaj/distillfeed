from rss_reader.db import connect, utcnow
from rss_reader.notifications import deliver_ntfy_for_job, deliver_ntfy_for_run, send_ntfy_test
from rss_reader.ntfy_policy import load_ntfy_scope_policy, replace_ntfy_scope_policy


class SuccessfulResponse:
    def __init__(self, identifier: str = "ntfy-message"):
        self.identifier = identifier

    def raise_for_status(self):
        return None

    def json(self):
        return {"id": self.identifier}


class RejectedResponse:
    text = '{"code":40007,"error":"invalid priority"}'

    def raise_for_status(self):
        import requests
        raise requests.HTTPError("400 Client Error")


def seed_delivery_run(connection):
    group = connection.execute(
        "INSERT INTO groups(title,position,created_at) VALUES('Alerts',0,?)", (utcnow(),)
    ).lastrowid
    feed = connection.execute(
        "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
        (group, "Signal", "https://example.test/alerts", utcnow()),
    ).lastrowid
    run = connection.execute(
        """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
           VALUES('push-run',?,?,'success','model','prompt','{}')""",
        (utcnow(), utcnow()),
    ).lastrowid
    summary = connection.execute(
        "INSERT INTO summaries(llm_run_id,group_id,created_at) VALUES(?,?,?)",
        (run, group, utcnow()),
    ).lastrowid
    identifiers = []
    for index, (score, included, url) in enumerate((
        (90, 1, "https://example.test/high"),
        (85, 1, "https://example.test/equal"),
        (84, 1, "https://example.test/low"),
        (99, 0, "javascript:alert(1)"),
    )):
        item = connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,url,discovered_at)
               VALUES(?,?,?,?,?)""",
            (feed, f"push-{index}", f"Item {score}", url, utcnow()),
        ).lastrowid
        connection.execute(
            """INSERT INTO summary_items(summary_id,item_id,included,importance,description)
               VALUES(?,?,?,?,?)""",
            (summary, item, included, score, f"Description {score}"),
        )
        identifiers.append(int(item))
    return int(run), identifiers


def enable_ntfy(configured):
    configured.data["notifications"]["ntfy"].update({
        "enabled": True,
        "server_url": "https://ntfy.example.test",
        "topic": "private_mobile_topic",
        "minimum_relevance": 85,
        "max_items_per_summary": 5,
    })


def test_ntfy_threshold_payload_auth_and_duplicate_transitions(configured, monkeypatch):
    enable_ntfy(configured)
    monkeypatch.setenv("NTFY_TOKEN", "secret-token")
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return SuccessfulResponse(f"message-{len(calls)}")

    with connect(configured.database_path) as connection:
        run_id, _ = seed_delivery_run(connection)
        first = deliver_ntfy_for_run(connection, configured, run_id, post=post)
        second = deliver_ntfy_for_run(connection, configured, run_id, post=post)
        rows = connection.execute(
            "SELECT * FROM notification_deliveries ORDER BY relevance DESC"
        ).fetchall()

    assert first == {
        "status": "success", "eligible": 2, "claimed": 2,
        "duplicates": 0, "delivered": 2, "failed": 0,
    }
    assert second == {
        "status": "success", "eligible": 2, "claimed": 0,
        "duplicates": 2, "delivered": 0, "failed": 0,
    }
    assert len(calls) == 2
    assert all(call[0] == ("https://ntfy.example.test",) for call in calls)
    assert all(call[1]["headers"] == {"Authorization": "Bearer secret-token"} for call in calls)
    assert [call[1]["json"]["title"] for call in calls] == ["Item 90", "Item 85"]
    assert all(call[1]["json"]["priority"] == 4 for call in calls)
    assert calls[0][1]["json"]["click"] == "https://example.test/high"
    assert [row["status"] for row in rows] == ["delivered", "delivered"]
    assert [row["provider_message_id"] for row in rows] == ["message-1", "message-2"]


def test_ntfy_disabled_state_is_a_strict_no_op(configured):
    calls = []
    with connect(configured.database_path) as connection:
        run_id, _ = seed_delivery_run(connection)
        result = deliver_ntfy_for_run(
            connection, configured, run_id,
            post=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        delivery_count = connection.execute(
            "SELECT COUNT(*) FROM notification_deliveries"
        ).fetchone()[0]
    assert result == {"status": "disabled", "eligible": 0, "delivered": 0, "failed": 0}
    assert calls == []
    assert delivery_count == 0


def test_ordinary_ntfy_policy_excludes_disabled_and_plugin_owned_sources(configured):
    enable_ntfy(configured)
    calls = []
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Boundaries',0,?)",
            (utcnow(),),
        ).lastrowid
        disabled_feed = connection.execute(
            """INSERT INTO feeds(group_id,title,xml_url,enabled,created_at)
               VALUES(?,?,?,0,?)""",
            (group, "Disabled", "https://example.test/disabled", utcnow()),
        ).lastrowid
        plugin_feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Plugin", "plugin://private-source", utcnow()),
        ).lastrowid
        run = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('boundary-run',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        summary = connection.execute(
            "INSERT INTO summaries(llm_run_id,group_id,created_at) VALUES(?,?,?)",
            (run, group, utcnow()),
        ).lastrowid
        for feed_id, stable in ((disabled_feed, "disabled"), (plugin_feed, "plugin")):
            item = connection.execute(
                """INSERT INTO items(feed_id,stable_id,title,url,discovered_at)
                   VALUES(?,?,?,?,?)""",
                (feed_id, stable, stable, f"https://example.test/{stable}", utcnow()),
            ).lastrowid
            connection.execute(
                """INSERT INTO summary_items(summary_id,item_id,included,importance,description)
                   VALUES(?,?,1,100,?)""",
                (summary, item, stable),
            )
        result = deliver_ntfy_for_run(
            connection, configured, int(run),
            post=lambda *args, **kwargs: calls.append(kwargs),
        )
    assert result["eligible"] == result["claimed"] == result["delivered"] == 0
    assert calls == []


def test_ntfy_item_cap_claims_only_the_highest_ranked_candidate(configured):
    enable_ntfy(configured)
    configured.data["notifications"]["ntfy"]["max_items_per_summary"] = 1
    calls = []
    with connect(configured.database_path) as connection:
        run_id, _ = seed_delivery_run(connection)
        result = deliver_ntfy_for_run(
            connection, configured, run_id,
            post=lambda *args, **kwargs: calls.append(kwargs["json"]) or SuccessfulResponse(),
        )
        relevance = connection.execute(
            "SELECT relevance FROM notification_deliveries"
        ).fetchone()[0]
    assert result["eligible"] == result["claimed"] == result["delivered"] == 1
    assert relevance == 90
    assert [payload["title"] for payload in calls] == ["Item 90"]


def test_ntfy_failure_is_durable_and_never_retried_automatically(configured):
    enable_ntfy(configured)
    calls = 0

    def failing_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("mobile push timed out")

    with connect(configured.database_path) as connection:
        run_id, _ = seed_delivery_run(connection)
        first = deliver_ntfy_for_run(connection, configured, run_id, post=failing_post)
        second = deliver_ntfy_for_run(connection, configured, run_id, post=failing_post)
        deliveries = connection.execute(
            "SELECT status,error FROM notification_deliveries"
        ).fetchall()
        run_status = connection.execute(
            "SELECT status FROM llm_runs WHERE id=?", (run_id,)
        ).fetchone()[0]

    assert first["status"] == "partial" and first["failed"] == 2
    assert second["duplicates"] == 2 and second["failed"] == 0
    assert calls == 2
    assert run_status == "success"
    assert {row["status"] for row in deliveries} == {"failed"}
    assert all("timed out" in row["error"] for row in deliveries)


def test_ntfy_destination_change_is_a_new_explicit_delivery_state(configured):
    enable_ntfy(configured)
    calls = []
    post = lambda *args, **kwargs: calls.append(kwargs["json"]["topic"]) or SuccessfulResponse()
    with connect(configured.database_path) as connection:
        run_id, _ = seed_delivery_run(connection)
        deliver_ntfy_for_run(connection, configured, run_id, post=post)
        configured.data["notifications"]["ntfy"]["topic"] = "second_phone_topic"
        deliver_ntfy_for_run(connection, configured, run_id, post=post)
        count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
    assert calls == [
        "private_mobile_topic", "private_mobile_topic", "second_phone_topic", "second_phone_topic",
    ]
    assert count == 4


def test_ntfy_test_push_uses_saved_destination_without_delivery_claim(configured, monkeypatch):
    enable_ntfy(configured)
    monkeypatch.setenv("NTFY_TOKEN", "test-token")
    captured = {}

    def post(*args, **kwargs):
        captured.update({"args": args, **kwargs})
        return SuccessfulResponse("test-message")

    result = send_ntfy_test(configured, post=post)
    assert result == {"status": "delivered", "provider_message_id": "test-message"}
    assert captured["json"]["title"] == "DistillFeed test"
    assert captured["json"]["priority"] == 4
    assert captured["headers"] == {"Authorization": "Bearer test-token"}
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0


def test_ntfy_rejection_exposes_provider_detail(configured):
    enable_ntfy(configured)
    try:
        send_ntfy_test(configured, post=lambda *args, **kwargs: RejectedResponse())
    except RuntimeError as exc:
        assert "invalid priority" in str(exc)
    else:
        raise AssertionError("The ntfy rejection should have reached the caller")


def test_selected_ntfy_sources_use_feed_then_nearest_group_precedence(configured):
    enable_ntfy(configured)
    calls = []
    with connect(configured.database_path) as connection:
        parent = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('World',0,?)", (utcnow(),)
        ).lastrowid
        child = connection.execute(
            "INSERT INTO groups(parent_id,title,position,created_at) VALUES(?, 'Europe',0,?)",
            (parent, utcnow()),
        ).lastrowid
        unrelated = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Other',1,?)", (utcnow(),)
        ).lastrowid
        child_feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (child, "Europe feed", "https://example.test/europe", utcnow()),
        ).lastrowid
        override_feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (child, "Override feed", "https://example.test/override", utcnow()),
        ).lastrowid
        excluded_feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (unrelated, "Excluded feed", "https://example.test/excluded", utcnow()),
        ).lastrowid
        run = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('scoped-run',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        summaries = {
            group: connection.execute(
                "INSERT INTO summaries(llm_run_id,group_id,created_at) VALUES(?,?,?)",
                (run, group, utcnow()),
            ).lastrowid
            for group in (child, unrelated)
        }
        for stable, feed_id, group_id, score in (
            ("nearest-low", child_feed, child, 89),
            ("nearest-pass", child_feed, child, 92),
            ("feed-pass", override_feed, child, 85),
            ("unselected", excluded_feed, unrelated, 100),
        ):
            item = connection.execute(
                """INSERT INTO items(feed_id,stable_id,title,url,discovered_at)
                   VALUES(?,?,?,?,?)""",
                (feed_id, stable, stable, f"https://example.test/{stable}", utcnow()),
            ).lastrowid
            connection.execute(
                """INSERT INTO summary_items(summary_id,item_id,included,importance,description)
                   VALUES(?,?,1,?,?)""",
                (summaries[group_id], item, score, stable),
            )
        replace_ntfy_scope_policy(connection, "selected", [
            {"scope_kind": "group", "scope_id": parent, "minimum_relevance": 80},
            {"scope_kind": "group", "scope_id": child, "minimum_relevance": 90},
            {"scope_kind": "feed", "scope_id": override_feed, "minimum_relevance": 84},
        ])
        result = deliver_ntfy_for_run(
            connection, configured, int(run),
            post=lambda *args, **kwargs: calls.append(kwargs["json"]) or SuccessfulResponse(),
        )
        rows = connection.execute(
            """SELECT relevance,minimum_relevance,policy_scope_kind,policy_scope_id,
                      policy_label FROM notification_deliveries ORDER BY relevance DESC"""
        ).fetchall()

    assert result["eligible"] == result["delivered"] == 2
    assert {payload["title"] for payload in calls} == {"nearest-pass", "feed-pass"}
    assert [(row["relevance"], row["minimum_relevance"], row["policy_scope_kind"]) for row in rows] == [
        (92, 90, "group"), (85, 84, "feed"),
    ]
    assert rows[0]["policy_scope_id"] == child and rows[1]["policy_scope_id"] == override_feed
    assert rows[0]["policy_label"] == "World › Europe"


def test_selected_ntfy_mode_fails_closed_when_rules_disappear(configured):
    enable_ntfy(configured)
    calls = []
    with connect(configured.database_path) as connection:
        run_id, identifiers = seed_delivery_run(connection)
        feed_id = int(connection.execute(
            "SELECT feed_id FROM items WHERE id=?", (identifiers[0],)
        ).fetchone()[0])
        replace_ntfy_scope_policy(connection, "selected", [
            {"scope_kind": "feed", "scope_id": feed_id, "minimum_relevance": 85},
        ])
        connection.execute("DELETE FROM ntfy_scope_rules")
        policy = load_ntfy_scope_policy(connection, 85)
        result = deliver_ntfy_for_run(
            connection, configured, run_id,
            post=lambda *args, **kwargs: calls.append(kwargs),
        )
    assert policy.mode == "selected" and policy.rule_count == 0
    assert result["eligible"] == result["claimed"] == result["delivered"] == 0
    assert calls == []


def test_ntfy_rules_follow_moves_and_cascade_with_deleted_sources(configured):
    with connect(configured.database_path) as connection:
        first = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('First',0,?)", (utcnow(),)
        ).lastrowid
        second = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Second',1,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (first, "Movable", "https://example.test/movable", utcnow()),
        ).lastrowid
        connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (second, "Resident", "https://example.test/resident", utcnow()),
        )
        replace_ntfy_scope_policy(connection, "selected", [
            {"scope_kind": "group", "scope_id": first, "minimum_relevance": 70},
            {"scope_kind": "group", "scope_id": second, "minimum_relevance": 80},
            {"scope_kind": "feed", "scope_id": feed, "minimum_relevance": 90},
        ])
        assert load_ntfy_scope_policy(connection, 85).match(feed).minimum_relevance == 90
        connection.execute("DELETE FROM ntfy_scope_rules WHERE feed_id=?", (feed,))
        connection.execute("UPDATE feeds SET group_id=? WHERE id=?", (second, feed))
        match = load_ntfy_scope_policy(connection, 85).match(feed)
        assert match and match.scope_id == second and match.minimum_relevance == 80
        connection.execute("DELETE FROM feeds WHERE id=?", (feed,))
        assert connection.execute(
            "SELECT COUNT(*) FROM ntfy_scope_rules WHERE feed_id=?", (feed,)
        ).fetchone()[0] == 0
        connection.execute("DELETE FROM groups WHERE id=?", (second,))
        assert connection.execute(
            "SELECT COUNT(*) FROM ntfy_scope_rules WHERE group_id=?", (second,)
        ).fetchone()[0] == 0


def test_job_wide_ntfy_selection_uses_the_same_scoped_policy(configured):
    enable_ntfy(configured)
    calls = []
    with connect(configured.database_path) as connection:
        run_id, identifiers = seed_delivery_run(connection)
        job = connection.execute(
            """INSERT INTO ai_jobs(request_key,trigger_kind,scope_kind,policy_hash,policy_json,
                   status,stage,started_at,completed_at)
               VALUES('scope-job','automatic','all','hash','{}','success','complete',?,?)""",
            (utcnow(), utcnow()),
        ).lastrowid
        connection.execute("UPDATE summaries SET ai_job_id=? WHERE llm_run_id=?", (job, run_id))
        feed_id = int(connection.execute(
            "SELECT feed_id FROM items WHERE id=?", (identifiers[0],)
        ).fetchone()[0])
        replace_ntfy_scope_policy(connection, "selected", [
            {"scope_kind": "feed", "scope_id": feed_id, "minimum_relevance": 90},
        ])
        result = deliver_ntfy_for_job(
            connection, configured, int(job),
            post=lambda *args, **kwargs: calls.append(kwargs["json"]) or SuccessfulResponse(),
        )
    assert result["eligible"] == result["delivered"] == 1
    assert [payload["title"] for payload in calls] == ["Item 90"]
