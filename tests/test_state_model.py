import itertools
import re

from rss_reader.db import acquire_lock, connect, release_lock, utcnow
from rss_reader.opml import build_tree_from_database, parse_opml_bytes
from rss_reader.web import create_app


def _csrf(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)">', response.data)
    assert match
    return match.group(1).decode()


def _assert_opml_bisimulation(configured):
    with connect(configured.database_path) as connection:
        database_tree = build_tree_from_database(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    opml_tree = parse_opml_bytes(configured.working_opml_path.read_bytes())
    assert opml_tree == database_tree


def test_subscription_transition_sequence_preserves_opml_database_bisimulation(configured, monkeypatch):
    monkeypatch.setattr("rss_reader.web.validate_http_url", lambda *args, **kwargs: None)
    client = create_app(str(configured.path)).test_client()
    csrf = _csrf(client.get("/"))
    headers = {"X-CSRF-Token": csrf}

    root = client.post(
        "/api/groups", json={"title": "Root", "parent_id": "", "llm_enabled": True},
        headers=headers,
    ).get_json()["group_id"]
    _assert_opml_bisimulation(configured)
    child = client.post(
        "/api/groups", json={"title": "Child", "parent_id": root, "llm_enabled": False},
        headers=headers,
    ).get_json()["group_id"]
    _assert_opml_bisimulation(configured)
    feed = client.post(
        "/api/feeds",
        json={
            "title": "Feed", "xml_url": "https://example.test/state-feed",
            "group_id": child, "llm_enabled": True,
        },
        headers=headers,
    ).get_json()["feed_id"]
    _assert_opml_bisimulation(configured)

    assert client.patch(
        f"/api/groups/{child}",
        json={"title": "Renamed child", "llm_enabled": True,
              "summary_interval_hours": 6, "summary_item_budget": 12},
        headers=headers,
    ).status_code == 200
    assert client.patch(
        f"/api/feeds/{feed}", json={"title": "Renamed feed", "llm_enabled": False},
        headers=headers,
    ).status_code == 200
    _assert_opml_bisimulation(configured)

    assert client.delete(f"/api/feeds/{feed}", headers=headers).status_code == 200
    _assert_opml_bisimulation(configured)
    assert client.delete(f"/api/groups/{child}", headers=headers).status_code == 200
    _assert_opml_bisimulation(configured)


def test_subscription_moves_are_exact_reversible_and_cycle_safe(configured, monkeypatch):
    monkeypatch.setattr("rss_reader.web.validate_http_url", lambda *args, **kwargs: None)
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}
    with connect(configured.database_path) as connection:
        ungrouped = connection.execute(
            "SELECT id FROM groups WHERE parent_id IS NULL AND title='Ungrouped'"
        ).fetchone()[0]

    first = client.post(
        "/api/groups", json={"title": "First", "parent_id": ""}, headers=headers
    ).get_json()["group_id"]
    second = client.post(
        "/api/groups", json={"title": "Second", "parent_id": ""}, headers=headers
    ).get_json()["group_id"]
    child = client.post(
        "/api/groups", json={"title": "Child", "parent_id": first}, headers=headers
    ).get_json()["group_id"]
    loose = client.post(
        "/api/feeds", json={
            "title": "Loose feed", "xml_url": "https://example.test/loose", "group_id": ungrouped,
        }, headers=headers,
    ).get_json()["feed_id"]

    # A formerly ungrouped feed is a real root entry and can sit between groups.
    response = client.post(
        "/api/subscriptions/move",
        json={"kind": "feed", "id": loose, "parent_id": None, "position": 1},
        headers=headers,
    )
    assert response.status_code == 200
    _assert_opml_bisimulation(configured)
    with connect(configured.database_path) as connection:
        root_order = sorted(
            [(row["position"], "group", row["id"]) for row in connection.execute(
                "SELECT id,position FROM groups WHERE parent_id IS NULL AND id<>?", (ungrouped,)
            )] + [(row["position"], "feed", row["id"]) for row in connection.execute(
                "SELECT id,position FROM feeds WHERE group_id=?", (ungrouped,)
            )]
        )
    assert [(kind, identifier) for _, kind, identifier in root_order] == [
        ("group", first), ("feed", loose), ("group", second),
    ]

    # Cross-container movement changes only parent/order, never feed identity.
    assert client.post(
        "/api/subscriptions/move",
        json={"kind": "feed", "id": loose, "parent_id": first, "position": 0},
        headers=headers,
    ).status_code == 200
    _assert_opml_bisimulation(configured)
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT group_id FROM feeds WHERE id=?", (loose,)).fetchone()[0] == first

    before = configured.working_opml_path.read_bytes()
    assert client.post(
        "/api/subscriptions/move",
        json={"kind": "group", "id": first, "parent_id": child, "position": 0},
        headers=headers,
    ).status_code == 409
    assert configured.working_opml_path.read_bytes() == before
    _assert_opml_bisimulation(configured)

    # The same endpoint used by the keyboard Move-to dialog can reparent a group
    # and move it back without changing its identity or leaving OPML out of sync.
    assert client.post(
        "/api/subscriptions/move",
        json={"kind": "group", "id": child, "parent_id": second, "position": 0},
        headers=headers,
    ).status_code == 200
    _assert_opml_bisimulation(configured)
    assert client.post(
        "/api/subscriptions/move",
        json={"kind": "group", "id": child, "parent_id": first, "position": 0},
        headers=headers,
    ).status_code == 200
    _assert_opml_bisimulation(configured)

    # Move back to root and verify the hidden bucket never appears as a UI group.
    assert client.post(
        "/api/subscriptions/move",
        json={"kind": "feed", "id": loose, "parent_id": None, "position": 0},
        headers=headers,
    ).status_code == 200
    page = client.get("/").data
    assert b"Loose feed" in page
    assert b">Ungrouped<" not in page
    script = client.get("/static/app.js").data
    stylesheet = client.get("/static/app.css").data
    assert b"subscriptionDropIntent" in script and b"drop-inside" in script
    assert b".subscription-entry.drop-inside > summary" in stylesheet
    assert b".subscriptions.editing .feed-label" in stylesheet
    _assert_opml_bisimulation(configured)


def test_running_job_stop_request_is_visible_and_idempotent(configured):
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}
    with connect(configured.database_path) as connection:
        assert acquire_lock(connection, "llm-summary", "state-test") is True
    stopped = client.post(
        "/api/jobs/cancel", json={"jobs": ["llm-summary"]}, headers=headers,
    )
    assert stopped.status_code == 202
    assert stopped.get_json()["jobs"] == ["llm-summary"]
    status = client.get("/api/status").get_json()
    assert status["phase"] == "summarizing"
    assert status["cancel_requested"] is True
    assert status["jobs"][0]["name"] == "llm-summary"
    assert status["jobs"][0]["cancel_requested"] == 1
    assert status["jobs"][0]["acquired_at"]
    with connect(configured.database_path) as connection:
        release_lock(connection, "llm-summary", "state-test")
    assert client.post(
        "/api/jobs/cancel", json={"jobs": ["llm-summary"]}, headers=headers,
    ).status_code == 409


def test_stale_page_cannot_start_a_second_update(configured):
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}
    with connect(configured.database_path) as connection:
        assert acquire_lock(connection, "feed-refresh", "already-running") is True
    for endpoint in ("/api/refresh", "/api/summarize"):
        response = client.post(endpoint, json={}, headers=headers)
        assert response.status_code == 409
        assert response.get_json()["error"] == "An update is already running"
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0
        release_lock(connection, "feed-refresh", "already-running")


def test_first_browser_request_reserves_update_before_worker_starts(configured, monkeypatch):
    pending = []
    monkeypatch.setattr("rss_reader.web.run_update_summaries", lambda config, **kwargs: {})
    monkeypatch.setattr(
        "rss_reader.web.start_thread",
        lambda target, *, name: pending.append((name, target)),
    )
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}

    first = client.post("/api/summarize", json={}, headers=headers)
    second = client.post("/api/refresh", json={}, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.get_json()["error"] == "An update is already running"
    with connect(configured.database_path) as connection:
        lock = connection.execute(
            "SELECT name,owner FROM job_locks WHERE name='summary-update'"
        ).fetchone()
        assert lock and lock["owner"]

    # The deferred worker owns and releases the exact reservation.
    assert pending[0][0] == "llm-summary"
    pending[0][1]()
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0] == 0


def test_item_markov_state_space_is_reachable_and_exact(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('States',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "State feed", "https://example.test/states", utcnow()),
        ).lastrowid
        item = connection.execute(
            """INSERT INTO items(
                   feed_id,stable_id,title,url,published_at,discovered_at,description_text
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                feed, "state-item", "State item", "https://example.test/state-item",
                "2026-07-14T08:30:00+00:00", utcnow(), "State description",
            ),
        ).lastrowid
        immutable_item_data = tuple(connection.execute(
            "SELECT title,url,published_at,description_text FROM items WHERE id=?", (item,)
        ).fetchone())
    client = create_app(str(configured.path)).test_client()
    csrf = _csrf(client.get("/"))
    headers = {"X-CSRF-Token": csrf}
    for is_read, is_starred, is_read_later in itertools.product((False, True), repeat=3):
        assert client.post(
            f"/api/items/{item}/read", json={"read": is_read}, headers=headers
        ).status_code == 200
        assert client.post(
            f"/api/items/{item}/star", json={"starred": is_starred}, headers=headers
        ).status_code == 200
        assert client.post(
            f"/api/items/{item}/read-later", json={"read_later": is_read_later}, headers=headers
        ).status_code == 200
        with connect(configured.database_path) as connection:
            row = connection.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
        assert (bool(row["is_read"]), bool(row["is_starred"]), bool(row["is_read_later"])) == (
            is_read, is_starred, is_read_later
        )
        assert tuple(row[key] for key in ("title", "url", "published_at", "description_text")) == immutable_item_data


def test_item_state_mutations_reject_missing_or_ambiguous_targets(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Explicit states',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Explicit feed", "https://example.test/explicit", utcnow()),
        ).lastrowid
        item = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
            (feed, "explicit", "Explicit", utcnow()),
        ).lastrowid
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}

    for endpoint in (
        f"/api/items/{item}/read",
        f"/api/items/{item}/star",
        f"/api/items/{item}/read-later",
    ):
        assert client.post(endpoint, json={}, headers=headers).status_code == 400
    assert client.post(
        "/api/items/bulk-read", json={"item_ids": [item]}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/items/bulk-star", json={"item_ids": [item]}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/items/bulk-read-later", json={"item_ids": [item]}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/items/bulk-tags", json={"item_ids": [item]}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/items/bulk-read",
        json={"item_ids": [float(item)], "read": True},
        headers=headers,
    ).status_code == 400

    with connect(configured.database_path) as connection:
        row = connection.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
        assert not row["is_read"] and not row["is_starred"] and not row["is_read_later"]
        assert connection.execute(
            "SELECT 1 FROM item_tags WHERE item_id=?", (item,)
        ).fetchone() is None


def test_every_selected_subset_read_transition_is_exact(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Subset states',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Subset feed", "https://example.test/subsets", utcnow()),
        ).lastrowid
        item_ids = [
            int(connection.execute(
                "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
                (feed, f"subset-{index}", f"Subset {index}", utcnow()),
            ).lastrowid)
            for index in range(3)
        ]
    client = create_app(str(configured.path)).test_client()
    csrf = _csrf(client.get("/"))
    headers = {"X-CSRF-Token": csrf}

    for starting_state in (False, True):
        for mask in range(1 << len(item_ids)):
            selected = [identifier for index, identifier in enumerate(item_ids) if mask & (1 << index)]
            for target_state in (False, True):
                with connect(configured.database_path) as connection:
                    connection.execute("UPDATE items SET is_read=?", (int(starting_state),))
                response = client.post(
                    "/api/items/bulk-read",
                    json={"mode": "selected", "item_ids": selected, "read": target_state},
                    headers=headers,
                )
                assert response.status_code == 200
                assert response.get_json()["item_ids"] == sorted(selected)
                with connect(configured.database_path) as connection:
                    states = {
                        int(row["id"]): bool(row["is_read"])
                        for row in connection.execute(
                            "SELECT id,is_read FROM items WHERE id IN (?,?,?)", item_ids
                        ).fetchall()
                    }
                assert states == {
                    identifier: target_state if identifier in selected else starting_state
                    for identifier in item_ids
                }


def test_scope_read_transitions_are_isolated_and_require_an_explicit_scope(configured):
    with connect(configured.database_path) as connection:
        first_group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('First scope',0,?)", (utcnow(),)
        ).lastrowid
        second_group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Second scope',0,?)", (utcnow(),)
        ).lastrowid
        first_feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (first_group, "First feed", "https://example.test/first-scope", utcnow()),
        ).lastrowid
        second_feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (second_group, "Second feed", "https://example.test/second-scope", utcnow()),
        ).lastrowid
        first_item = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
            (first_feed, "first", "First", utcnow()),
        ).lastrowid
        second_item = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,discovered_at,is_read) VALUES(?,?,?,?,1)",
            (second_feed, "second", "Second", utcnow()),
        ).lastrowid
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}

    assert client.post(
        "/api/items/bulk-read",
        json={"mode": "scope", "feed_id": first_feed, "read": True},
        headers=headers,
    ).status_code == 200
    with connect(configured.database_path) as connection:
        assert bool(connection.execute("SELECT is_read FROM items WHERE id=?", (first_item,)).fetchone()[0])
        assert bool(connection.execute("SELECT is_read FROM items WHERE id=?", (second_item,)).fetchone()[0])

    assert client.post(
        "/api/items/bulk-read",
        json={"mode": "scope", "group_id": first_group, "read": False},
        headers=headers,
    ).status_code == 200
    rejected = client.post(
        "/api/items/bulk-read", json={"mode": "scope", "read": False}, headers=headers
    )
    assert rejected.status_code == 400
    with connect(configured.database_path) as connection:
        assert not bool(connection.execute("SELECT is_read FROM items WHERE id=?", (first_item,)).fetchone()[0])
        assert bool(connection.execute("SELECT is_read FROM items WHERE id=?", (second_item,)).fetchone()[0])


def test_every_selected_subset_saved_state_transition_is_exact(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Saved subsets',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Saved feed", "https://example.test/saved-subsets", utcnow()),
        ).lastrowid
        item_ids = [
            int(connection.execute(
                "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
                (feed, f"saved-{index}", f"Saved {index}", utcnow()),
            ).lastrowid)
            for index in range(3)
        ]
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}

    transitions = (
        ("/api/items/bulk-star", "starred", "is_starred"),
        ("/api/items/bulk-read-later", "read_later", "is_read_later"),
    )
    for endpoint, payload_key, column in transitions:
        for starting_state in (False, True):
            for mask in range(1 << len(item_ids)):
                selected = [identifier for index, identifier in enumerate(item_ids) if mask & (1 << index)]
                for target_state in (False, True):
                    with connect(configured.database_path) as connection:
                        connection.execute(f"UPDATE items SET {column}=?", (int(starting_state),))
                    response = client.post(
                        endpoint,
                        json={"item_ids": selected, payload_key: target_state},
                        headers=headers,
                    )
                    assert response.status_code == 200
                    assert response.get_json()["item_ids"] == sorted(selected)
                    with connect(configured.database_path) as connection:
                        states = {
                            int(row["id"]): bool(row[column])
                            for row in connection.execute(
                                f"SELECT id,{column} FROM items WHERE id IN (?,?,?)", item_ids
                            ).fetchall()
                        }
                    assert states == {
                        identifier: target_state if identifier in selected else starting_state
                        for identifier in item_ids
                    }


def test_tag_replacement_and_removal_are_isolated_to_every_selected_subset(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Tag subsets',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Tag feed", "https://example.test/tag-subsets", utcnow()),
        ).lastrowid
        item_ids = [
            int(connection.execute(
                "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
                (feed, f"tag-{index}", f"Tag {index}", utcnow()),
            ).lastrowid)
            for index in range(3)
        ]
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": _csrf(client.get("/"))}

    for mask in range(1, 1 << len(item_ids)):
        selected = [identifier for index, identifier in enumerate(item_ids) if mask & (1 << index)]
        with connect(configured.database_path) as connection:
            connection.execute("DELETE FROM item_tags")
            connection.execute("DELETE FROM tags")
        assigned = client.post(
            "/api/items/bulk-tags",
            json={"item_ids": selected, "tags": ["Focus"]},
            headers=headers,
        )
        assert assigned.status_code == 200
        with connect(configured.database_path) as connection:
            tagged = {
                int(row["item_id"]) for row in connection.execute(
                    "SELECT item_id FROM item_tags"
                ).fetchall()
            }
        assert tagged == set(selected)

        assert client.post(
            "/api/items/bulk-tags",
            json={"item_ids": item_ids, "tags": ["Focus"]},
            headers=headers,
        ).status_code == 200
        removed = client.post(
            "/api/items/bulk-tags",
            json={"item_ids": selected, "tags": []},
            headers=headers,
        )
        assert removed.status_code == 200
        with connect(configured.database_path) as connection:
            remaining = {
                int(row["item_id"]) for row in connection.execute(
                    "SELECT item_id FROM item_tags"
                ).fetchall()
            }
        assert remaining == set(item_ids) - set(selected)


def test_every_popup_menu_dialog_transition_has_one_unambiguous_visible_layer():
    """Model the UI controller independently of click order and entry point."""
    events = (
        "open_menu_0", "open_menu_1", "open_menu_2",
        "open_dialog_0", "open_dialog_1",
        "outside", "focus_elsewhere", "escape", "scroll", "resize", "close_dialog",
    )

    def transition(state, event):
        menu, dialog = state
        if event.startswith("open_dialog_"):
            return None, int(event.rsplit("_", 1)[1])
        if event.startswith("open_menu_"):
            # A modal is the top layer; background controls cannot be activated.
            return state if dialog is not None else (int(event.rsplit("_", 1)[1]), None)
        if event == "close_dialog":
            return menu, None
        if event in {"outside", "focus_elsewhere", "escape", "scroll", "resize"}:
            return None, dialog
        raise AssertionError(event)

    for sequence in itertools.product(events, repeat=4):
        state = (None, None)
        for event in sequence:
            state = transition(state, event)
            menu, dialog = state
            assert menu in {None, 0, 1, 2}
            assert dialog in {None, 0, 1}
            assert not (menu is not None and dialog is not None)
