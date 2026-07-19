import json
from datetime import UTC, datetime, timedelta

from rss_reader.ai_policy import build_plan
from rss_reader.ai_queue import queue_dashboard, sync_review_queue
from rss_reader.db import connect, utcnow
from rss_reader.opml import parse_opml_bytes
from rss_reader.web import create_app


def seed_group(connection, title, priority="normal", item_count=1):
    mode = priority if priority in {"manual", "off"} else "automatic"
    group_id = connection.execute(
        """INSERT INTO groups(
               title,position,llm_enabled,ai_mode,ai_priority,created_at
           ) VALUES(?,?,?,?,?,?)""",
        (title, 0, int(mode != "off"), mode, priority, utcnow()),
    ).lastrowid
    feed_id = connection.execute(
        "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
        (group_id, f"{title} feed", f"https://example.test/{title}", utcnow()),
    ).lastrowid
    item_ids = [
        connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
            (feed_id, f"{title}-{number}", f"{title} item {number}", utcnow()),
        ).lastrowid
        for number in range(item_count)
    ]
    return int(group_id), int(feed_id), [int(value) for value in item_ids]


def test_stale_processing_claim_recovers_without_losing_item(configured):
    with connect(configured.database_path) as connection:
        _, _, item_ids = seed_group(connection, "Recovery")
        sync_review_queue(connection)
        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
        connection.execute(
            "UPDATE ai_review_queue SET state='processing',updated_at=? WHERE item_id=?",
            (stale, item_ids[0]),
        )
        sync_review_queue(connection)
        row = connection.execute(
            "SELECT * FROM ai_review_queue WHERE item_id=?", (item_ids[0],)
        ).fetchone()
    assert row["state"] == "retry"
    assert row["last_error"] == "Previous review was interrupted"


def test_priority_manual_pause_and_resume_state_model(configured):
    configured.data["llm"]["review_workload"] = "focused"
    with connect(configured.database_path) as connection:
        high_group, _, high_items = seed_group(connection, "High", "high", 3)
        low_group, _, low_items = seed_group(connection, "Low", "low", 3)
        manual_group, _, manual_items = seed_group(connection, "Manual", "manual", 1)
        sync_review_queue(connection)

        automatic = build_plan(connection, configured, automatic=True)
        automatic_ids = set(automatic["selected_ids"])
        assert automatic_ids == set(high_items + low_items)
        assert automatic_ids.isdisjoint(manual_items)
        assert automatic["manual_count"] == 1
        assert set(build_plan(
            connection, configured, group_id=manual_group,
        )["selected_ids"]) == set(manual_items)

        connection.execute(
            "UPDATE groups SET ai_mode='off',llm_enabled=0 WHERE id=?", (low_group,),
        )
        dashboard = queue_dashboard(connection, include_inactive=True)
        paused = [item for item in dashboard["items"] if item["group_id"] == low_group]
        assert paused and {item["display_state"] for item in paused} == {"disabled"}
        assert all(connection.execute(
            "SELECT state FROM ai_review_queue WHERE item_id=?", (item_id,)
        ).fetchone()[0] == "waiting" for item_id in low_items)

        connection.execute(
            "UPDATE groups SET ai_mode='automatic',llm_enabled=1 WHERE id=?", (low_group,),
        )
        resumed = queue_dashboard(connection)
        assert {item["display_state"] for item in resumed["items"] if item["group_id"] == low_group} == {"ready"}
        assert high_group != low_group


def test_ai_policy_page_and_source_priority_transitions(configured):
    with connect(configured.database_path) as connection:
        group_id, _, _ = seed_group(connection, "Policy UI", "normal", 2)
    client = create_app(str(configured.path)).test_client()
    page = client.get("/ai")
    assert page.status_code == 200
    assert b"Check feeds" in page.data and b"Update ordinary summaries" in page.data
    assert b"Digest \xc2\xb7 stored result" not in page.data
    assert b"Waiting entries" in page.data and b"Focused" in page.data and b"Wide" in page.data
    assert b'data-ai-content="sources"' in page.data
    token = client.get("/api/csrf").get_json()["csrf_token"]

    off = client.patch(
        f"/api/groups/{group_id}", json={"ai_priority": "off"},
        headers={"X-CSRF-Token": token},
    )
    assert off.status_code == 200
    with connect(configured.database_path) as connection:
        row = connection.execute("SELECT ai_priority,llm_enabled FROM groups WHERE id=?", (group_id,)).fetchone()
        assert (row["ai_priority"], row["llm_enabled"]) == ("off", 0)

    normal = client.patch(
        f"/api/groups/{group_id}", json={"ai_priority": "normal"},
        headers={"X-CSRF-Token": token},
    )
    assert normal.status_code == 200
    with connect(configured.database_path) as connection:
        row = connection.execute("SELECT ai_priority,llm_enabled FROM groups WHERE id=?", (group_id,)).fetchone()
        assert (row["ai_priority"], row["llm_enabled"]) == ("normal", 1)
    exported = parse_opml_bytes(client.get("/api/export-opml").data)
    policy_group = next(group for group in exported if group.title == "Policy UI")
    assert policy_group.ai_priority == "normal"
    assert policy_group.summary_interval_hours == 0
    assert policy_group.summary_item_budget == 0


def test_mobile_filter_actions_are_one_compact_layout_unit(configured):
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    css = client.get("/static/app.css").data
    assert b'class="item-filter-actions"' in page.data
    assert b"grid-column: 1 / -1; display: grid; grid-template-columns: auto auto auto minmax(70px,auto)" in css
    assert b"@container items-pane (max-width: 520px)" in css
    assert b"@media (max-width: 440px)" in css


def test_feed_summary_is_not_misrepresented_as_its_parent_group_summary(configured):
    with connect(configured.database_path) as connection:
        group_id, feed_id, item_ids = seed_group(connection, "Reusable review")
        run_id = connection.execute(
            """INSERT INTO llm_runs(
                   request_key,started_at,completed_at,status,model,prompt_version,pricing_json
               ) VALUES(?,?,?,'success','model','prompt','{}')""",
            ("feed-review-reuse", utcnow(), utcnow()),
        ).lastrowid
        summary_id = connection.execute(
            """INSERT INTO summaries(
                   llm_run_id,group_id,scope_feed_id,changes,sections_json,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                run_id, group_id, feed_id, "Feed-specific change",
                json.dumps([{"heading": "Feed finding", "body": "Reusable digest."}]), utcnow(),
            ),
        ).lastrowid
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description,justification,story_cluster
               ) VALUES(?,?,1,1,88,'Canonical description','Canonical reason','Reusable story')""",
            (summary_id, item_ids[0]),
        )
    page = create_app(str(configured.path)).test_client().get(f"/?group={group_id}")
    assert page.status_code == 200
    assert b"Feed finding" not in page.data
    assert b"No AI summary exists for this selection yet" in page.data


def test_queue_pagination_is_complete_and_clamps_invalid_pages(configured):
    with connect(configured.database_path) as connection:
        seed_group(connection, "Paged queue", item_count=105)
    client = create_app(str(configured.path)).test_client()
    second = client.get("/ai?queue_page=2")
    assert second.status_code == 200
    assert b"Page 2 of 2" in second.data
    assert second.data.count(b'data-queue-state="ready"') == 5
    clamped = client.get("/ai?queue_page=999")
    assert b"Page 2 of 2" in clamped.data
    assert clamped.data.count(b'data-queue-state="ready"') == 5
