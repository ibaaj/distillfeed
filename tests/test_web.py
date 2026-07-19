import re
import base64

import pytest

from rss_reader.db import connect, utcnow
from rss_reader.config import load_config, save_config
from rss_reader.opml import parse_opml_bytes
from rss_reader.web import create_app


def csrf_from(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)">', response.data)
    assert match
    return match.group(1).decode()


def test_openai_model_presets_keep_cost_rates_in_sync(configured):
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": csrf_from(client.get("/"))}
    economy = client.post(
        "/api/config", json={"values": {"llm.model": "gpt-5.4-nano"}}, headers=headers,
    )
    assert economy.status_code == 200
    saved = load_config(configured.path)
    assert saved.get("llm", "model") == "gpt-5.4-nano"
    assert saved.section("llm")["pricing"] == {
        "input": 0.20, "cached_input": 0.02, "output": 1.25,
    }
    quality = client.post(
        "/api/config", json={"values": {"llm.model": "gpt-5.4-mini"}}, headers=headers,
    )
    assert quality.status_code == 200
    assert load_config(configured.path).section("llm")["pricing"] == {
        "input": 0.75, "cached_input": 0.075, "output": 4.50,
    }


def test_item_details_date_hierarchy_and_portable_exports(configured):
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Details',0,?)", (utcnow(),)
        ).lastrowid
        feed_id = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group_id, "Detail source", "https://example.test/details.xml", utcnow()),
        ).lastrowid
        item_id = connection.execute(
            """INSERT INTO items(
                   feed_id,stable_id,title,url,published_at,discovered_at,description_text
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                feed_id, "detail-item", "A complete title", "https://example.test/article",
                "2026-07-14T10:15:00+00:00", "2026-07-14T10:16:00+00:00",
                "A full <description> kept as text.",
            ),
        ).lastrowid

    client = create_app(str(configured.path)).test_client()
    page = client.get(f"/?group={group_id}")
    assert page.status_code == 200
    assert f'aria-controls="item-details-{item_id}"'.encode() in page.data
    assert f'id="item-details-{item_id}" hidden'.encode() in page.data
    assert b'aria-expanded="false"' in page.data
    assert b'data-date="2026-07-14T10:15:00+00:00"' in page.data
    assert b'<dt>Date</dt>' in page.data and b'<dt>URL</dt>' in page.data
    assert b'<dt>Title</dt>' in page.data and b'<dt>Description</dt>' in page.data
    assert b"A full &lt;description&gt; kept as text." in page.data

    script = client.get("/static/app.js").data
    assert b"function itemDay(row)" in script
    assert b"const dayOrder" in script
    assert b"item-day-group" in script
    assert b"updateDateGroupVisibility" in script

    opml = client.get("/api/export-opml")
    assert opml.status_code == 200
    assert "attachment" in opml.headers["Content-Disposition"]
    assert opml.headers["Content-Disposition"].rstrip('"').endswith(".opml")
    assert parse_opml_bytes(opml.data)[0].feeds[0].xml_url == "https://example.test/details.xml"

    summaries = client.get("/summaries")
    assert b"Print / PDF" in summaries.data
    assert b'aria-label="Print or save summaries as PDF"' in summaries.data
    assert b"summary.js?v=0.22.0" in summaries.data


def test_standalone_page_headers_keep_actions_compact(configured):
    client = create_app(str(configured.path)).test_client()
    for path in ("/notifications", "/costs", "/summaries", "/health", "/history", "/saved"):
        page = client.get(path)
        assert page.status_code == 200
        assert b'class="topbar page-header"' in page.data
        assert b'class="page-title"' in page.data
    stylesheet = client.get("/static/app.css").data
    assert b".page-header > .page-actions { justify-self: end; }" in stylesheet
    assert b".page-header .button-link { width: auto;" in stylesheet


def test_optional_basic_auth_rejects_missing_and_accepts_configured_password(configured, monkeypatch):
    configured.data["auth"].update({"enabled": True, "username": "reader", "password_env": "TEST_READER_PASSWORD"})
    save_config(configured)
    monkeypatch.setenv("TEST_READER_PASSWORD", "correct horse")
    client = create_app(str(configured.path)).test_client()
    assert client.get("/").status_code == 401
    token = base64.b64encode(b"reader:correct horse").decode()
    response = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 200


def test_local_development_and_production_modes_are_visible_and_enforced(configured):
    local_app = create_app(str(configured.path))
    local_page = local_app.test_client().get("/")
    assert b'class="mode-badge"' not in local_page.data
    assert local_page.headers["X-DistillFeed-Mode"] == "local"
    assert local_app.test_client().get(
        "/", headers={"Host": "attacker.example"}
    ).status_code == 400

    configured.data["app"]["mode"] = "development"
    save_config(configured)
    development_app = create_app(str(configured.path))
    development_page = development_app.test_client().get("/")
    assert b"Development" in development_page.data
    assert development_page.headers["X-DistillFeed-Mode"] == "development"

    configured.data["app"]["mode"] = "production"
    configured.data["app"]["debug"] = False
    save_config(configured)
    production_app = create_app(str(configured.path))
    production_page = production_app.test_client().get("/")
    assert b'class="mode-badge"' not in production_page.data
    assert production_page.headers["X-DistillFeed-Mode"] == "production"
    assert production_page.headers["Cache-Control"] == "no-store"
    assert production_page.headers["Strict-Transport-Security"].startswith("max-age=31536000")
    assert "Secure" in production_page.headers["Set-Cookie"]
    assert "HttpOnly" in production_page.headers["Set-Cookie"]
    assert "SameSite=Strict" in production_page.headers["Set-Cookie"]
    assert production_app.debug is False


def test_page_and_subscription_mutations(configured, monkeypatch):
    monkeypatch.setattr("rss_reader.web.validate_http_url", lambda *args, **kwargs: None)
    app = create_app(str(configured.path))
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert b"AI summaries" in page.data
    assert b"Check feeds" in page.data
    assert b"AI relevance" in page.data
    assert b"LLM relevance" not in page.data
    assert b'href="/ai"' in page.data
    assert b"Review and update summaries" not in page.data
    assert b"Update AI summaries" not in page.data
    assert b'id="scope-update-button"' not in page.data  # no group exists yet
    assert b"Latest summaries" in page.data
    assert b'aria-label="Open saved items"' in page.data
    assert b"Saved items" in page.data
    assert b'href="/saved?view=favorites"' in page.data
    assert b'href="/saved?view=read-later"' in page.data
    assert b'href="/saved?view=tags"' in page.data
    assert b"DistillFeed" in page.data
    assert b"Paris" in page.data
    assert b"Checkboxes affect LLM summaries only" not in page.data
    assert b"Selected" in page.data
    assert b"Add to Favorites" in page.data
    assert b"Mark every item" in page.data
    assert b"Mark as unread" in page.data
    assert b"Subscription properties" in page.data
    assert b"System notices" in page.data
    assert b"AI cost explorer" in page.data
    assert re.search(rb"\stitle=", page.data) is None
    assert b'aria-label="Check all RSS and Atom feeds"' in page.data
    assert b'aria-label="Resize items pane"' in page.data
    assert b'id="summary-font-size"' in page.data
    assert b'min="10" max="24"' in page.data
    assert b'id="settings-advanced"' in page.data
    assert b'class="settings-sticky-header"' in page.data
    assert b'class="settings-shell"' in page.data
    assert b'class="settings-sidebar" aria-label="Settings categories"' in page.data
    assert b'data-settings-target="settings-ai"' in page.data
    assert b'data-settings-target="settings-notifications"' in page.data
    assert b'id="settings-notifications" class="settings-panel"' in page.data
    assert b"AI notifications" not in page.data
    assert b'class="settings-switch"' in page.data
    assert b'role="switch"' in page.data
    assert b'id="settings-close-button"' in page.data
    assert b'Close</button><button id="save-settings-button"' in page.data
    assert page.data.index(b"Save settings") < page.data.index(b"Appearance")
    assert page.data.count(b"Save settings") == 1
    assert b"Save configuration" not in page.data
    assert b'id="settings-status"' in page.data
    assert b'id="job-progress"' in page.data
    assert b'href="/api/export-opml"' in page.data
    assert b'name="refresh-interval-minutes"' in page.data
    csrf = csrf_from(page)
    response = client.post(
        "/api/groups", json={"title": "Technology", "parent_id": "", "llm_enabled": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    assert isinstance(response.get_json()["group_id"], int)
    with connect(configured.database_path) as connection:
        group_id = connection.execute("SELECT id FROM groups WHERE title='Technology'").fetchone()[0]
    duplicate_group = client.post(
        "/api/groups", json={"title": "technology", "parent_id": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert duplicate_group.status_code == 409
    assert "already exists" in duplicate_group.get_json()["error"]
    response = client.post(
        "/api/feeds",
        json={"group_id": group_id, "title": "A long feed title", "xml_url": "https://example.com/rss", "llm_enabled": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    assert isinstance(response.get_json()["feed_id"], int)
    with connect(configured.database_path) as connection:
        item_feed = connection.execute("SELECT id FROM feeds WHERE xml_url='https://example.com/rss'").fetchone()[0]
        assert configured.working_opml_path.exists()
        assert "https://example.com/rss" in configured.working_opml_path.read_text(encoding="utf-8")
        item_id = connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,discovered_at)
               VALUES(?, 'entry', 'A title', ?)""",
            (item_feed, utcnow()),
        ).lastrowid
        second_item = connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,discovered_at)
               VALUES(?, 'entry-2', 'Another title', ?)""",
            (item_feed, utcnow()),
        ).lastrowid
    response = client.post(
        f"/api/items/{item_id}/read", json={"read": True}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    response = client.post(
        "/api/items/bulk-read", json={"mode": "selected", "item_ids": [second_item], "read": True}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200 and response.get_json()["changed"] == 1
    response = client.patch(
        f"/api/feeds/{item_feed}", json={"title": "Short", "llm_enabled": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    invalid_schedule = client.patch(
        f"/api/groups/{group_id}", json={"summary_interval_hours": 8761},
        headers={"X-CSRF-Token": csrf},
    )
    invalid_budget = client.patch(
        f"/api/groups/{group_id}", json={"summary_item_budget": -1},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid_schedule.status_code == 400
    assert invalid_budget.status_code == 400
    response = client.patch(
        f"/api/groups/{group_id}", json={
            "title": "Tech", "llm_enabled": False,
            "summary_interval_hours": 12, "summary_item_budget": 25,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    feed_page = client.get(f"/?feed={item_feed}")
    assert feed_page.status_code == 200
    assert b"A title" in feed_page.data and b"Another title" in feed_page.data
    assert b'id="scope-update-button"' in feed_page.data
    assert b'id="scope-refresh-button"' in feed_page.data and b"Check feeds" in feed_page.data
    assert b"Enable AI" in feed_page.data
    assert b'id="summary-ai-config"' in feed_page.data
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (item_feed,)).fetchone()
        group = connection.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        item = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    assert feed["title"] == "Short" and feed["title_locked"] == 1 and feed["llm_enabled"] == 0
    assert group["title"] == "Tech" and group["ai_mode"] == "off"
    assert group["summary_interval_hours"] == 12 and group["summary_item_budget"] == 25
    assert item["summary_eligible"] == 1
    duplicate = client.post(
        "/api/feeds",
        json={"group_id": group_id, "xml_url": "https://example.com/rss"},
        headers={"X-CSRF-Token": csrf},
    )
    assert duplicate.status_code == 409
    assert "already subscribed" in duplicate.get_json()["error"]
    assert client.get("/summaries").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert configured.get("feeds", "user_agent").encode() in health.data
    assert b"Settings" in health.data and b"User-Agent" in health.data
    assert client.patch(f"/api/feeds/{item_feed}", json={"title": "No CSRF"}).status_code == 403
    response = client.delete(f"/api/groups/{group_id}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM groups WHERE id=?", (group_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM feeds WHERE id=?", (item_feed,)).fetchone()[0] == 0


def test_mark_complete_feed_view_as_read(configured):
    app = create_app(str(configured.path))
    client = app.test_client()
    csrf = csrf_from(client.get("/"))
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('News',0,?)", (utcnow(),)
        ).lastrowid
        feed_id = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group_id, "Daily", "https://example.test/daily", utcnow()),
        ).lastrowid
        for index in range(3):
            connection.execute(
                "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
                (feed_id, f"entry-{index}", f"Entry {index}", utcnow()),
            )
    response = client.post(
        "/api/items/bulk-read", json={"mode": "scope", "feed_id": feed_id, "read": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.get_json()["changed"] == 3
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE feed_id=? AND is_read=0", (feed_id,)
        ).fetchone()[0] == 0


def test_feed_properties_can_move_rename_and_change_url_atomically(configured, monkeypatch):
    monkeypatch.setattr("rss_reader.web.validate_http_url", lambda *args, **kwargs: None)
    with connect(configured.database_path) as connection:
        first_group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Original group',0,?)", (utcnow(),)
        ).lastrowid
        second_group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('New group',1,?)", (utcnow(),)
        ).lastrowid
        feed_id = connection.execute(
            """INSERT INTO feeds(group_id,title,xml_url,llm_enabled,etag,last_modified,
                       consecutive_failures,last_http_status,last_error,created_at)
               VALUES(?,?,?,1,'old-etag','old-date',3,403,'refused',?)""",
            (first_group, "Original", "https://example.test/original", utcnow()),
        ).lastrowid
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": csrf_from(client.get("/"))}
    response = client.patch(
        f"/api/feeds/{feed_id}",
        json={
            "title": "Renamed", "xml_url": "https://example.test/new-feed",
            "group_id": second_group, "llm_enabled": False,
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["feed"] == {
        "id": feed_id, "title": "Renamed", "xml_url": "https://example.test/new-feed",
        "group_id": second_group, "llm_enabled": False, "ai_mode": "off",
    }
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    assert feed["etag"] is None and feed["last_modified"] is None
    assert feed["last_error"] is None and feed["last_http_status"] is None
    assert feed["consecutive_failures"] == 0
    assert "https://example.test/new-feed" in configured.working_opml_path.read_text(encoding="utf-8")


def test_notification_panel_separates_feed_health_from_ai_capacity(configured):
    with connect(configured.database_path) as connection:
        connection.execute(
            """INSERT INTO refresh_runs(started_at,completed_at,status,feeds_attempted,feeds_succeeded,new_items)
               VALUES(?,?,'partial',8,7,43)""",
            (utcnow(), utcnow()),
        )
        connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,
                   submitted_items,deferred_items,pricing_json)
               VALUES('distinct-status',?,?,'success','model','prompt',40,3,'{}')""",
            (utcnow(), utcnow()),
        )
    page = create_app(str(configured.path)).test_client().get("/notifications")
    assert page.status_code == 200
    assert b"The latest refresh updated 7 of 8 feeds" in page.data
    assert b"Recent AI activity" in page.data
    assert b">40</td>" in page.data
    assert b"left 3 eligible items queued" not in page.data
    assert b"Alerts sent to other devices with ntfy" in page.data
    assert b"Sending alerts to other devices is off" in page.data
    assert b"Background schedule" in page.data


def test_status_exposes_compact_completed_operation_counts(configured):
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Results',0,?)", (utcnow(),)
        ).lastrowid
        feed_id = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group_id, "Result feed", "https://example.test/results", utcnow()),
        ).lastrowid
        item_id = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
            (feed_id, "result-item", "Included result", utcnow()),
        ).lastrowid
        job_id = connection.execute(
            """INSERT INTO ai_jobs(
                   request_key,trigger_kind,scope_kind,scope_id,policy_hash,policy_json,
                   status,stage,planned_items,completed_items,planned_requests,started_at,completed_at
               ) VALUES('result-job','manual','group',?,'hash','{}','success','complete',2,2,2,?,?)""",
            (group_id, utcnow(), utcnow()),
        ).lastrowid
        run_id = connection.execute(
            """INSERT INTO llm_runs(
                   request_key,started_at,completed_at,status,model,prompt_version,
                   pricing_json,ai_job_id,stage
               ) VALUES('result-run',?,?,'success','model','prompt','{}',?,'composition')""",
            (utcnow(), utcnow(), job_id),
        ).lastrowid
        summary_id = connection.execute(
            """INSERT INTO summaries(llm_run_id,group_id,ai_job_id,scope_kind,scope_id,created_at)
               VALUES(?,?,?,'group',?,?)""",
            (run_id, group_id, job_id, group_id, utcnow()),
        ).lastrowid
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description,justification
               ) VALUES(?,?,1,1,90,'Description','Reason')""",
            (summary_id, item_id),
        )
    status = create_app(str(configured.path)).test_client().get("/api/status").get_json()
    assert status["ai_job"]["completed_items"] == 2
    assert status["ai_result"] == {"included": 1, "summaries": 1}


def test_operation_completion_uses_existing_self_dismissing_toast(configured):
    client = create_app(str(configured.path)).test_client()
    script = client.get("/static/app.js").data
    css = client.get("/static/app.css").data
    assert b"distillfeedCompletedOperation" in script
    assert b"new entries" in script and b"evaluated" in script and b"included" in script
    assert b"right: max(18px, env(safe-area-inset-right))" in css
    assert b"setTimeout(() => toast.classList.remove('visible'), 4000)" in script


def test_ai_cost_explorer_filters_aggregates_and_escapes_history(configured):
    with connect(configured.database_path) as connection:
        connection.execute(
            """INSERT INTO llm_runs(
                   request_key,started_at,completed_at,status,model,prompt_version,
                   submitted_items,input_tokens,cached_input_tokens,output_tokens,
                   estimated_cost_usd,pricing_json,error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "recent-cost", utcnow(), utcnow(), "success", "gpt-5.4-mini", "digest-v4",
                12, 1000, 400, 200, 0.012345, "{}", None,
            ),
        )
        connection.execute(
            """INSERT INTO llm_runs(
                   request_key,started_at,completed_at,status,model,prompt_version,
                   submitted_items,input_tokens,cached_input_tokens,output_tokens,
                   estimated_cost_usd,pricing_json,error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "old-cost", "2025-01-01T00:00:00+00:00", "2025-01-01T00:01:00+00:00",
                "failed", "ollama-local", "arxiv<private>", 2, 50, 0, 10, 2.0,
                "{}", "failure <detail>",
            ),
        )

    client = create_app(str(configured.path)).test_client()
    recent = client.get("/costs?days=30")
    assert recent.status_code == 200
    assert b"AI cost explorer" in recent.data
    assert b"$0.0123" in recent.data
    assert b"gpt-5.4-mini" in recent.data and b"digest-v4" in recent.data
    assert b"old-cost" not in recent.data and b"ollama-local" not in recent.data
    all_time = client.get("/costs?days=0")
    assert b"$2.0123" in all_time.data
    assert b"ollama-local" in all_time.data
    assert b"arxiv&lt;private&gt;" in all_time.data
    assert b"failure &lt;detail&gt;" in all_time.data
    invalid = client.get("/costs?days=13")
    assert b'href="/costs?days=30" aria-current="page"' in invalid.data


def test_popup_and_dialog_script_enforces_one_visible_layer(configured):
    client = create_app(str(configured.path)).test_client()
    script = client.get("/static/app.js").data
    assert script.count(b"dialog.showModal()") == 1
    assert b"function closePopupMenus(except = null)" in script
    assert b"if (menu.open) { closePopupMenus(menu); setSubscriptionsOpen(false); }" in script
    assert b"document.addEventListener('pointerdown'" in script
    assert b"document.addEventListener('focusin'" in script
    assert b"window.addEventListener('resize'" in script
    assert b"document.addEventListener('scroll'" in script
    assert b"dialog.getBoundingClientRect()" in script
    assert b"if (dialog === settingsDialog) closeSettings()" in script
    assert b"function selectSettingsPanel(panelId" in script
    assert b"settingsDesktop.addEventListener('change', syncSettingsLayout)" in script
    assert b"['ArrowDown', 'ArrowUp', 'Home', 'End']" in script
    assert b"panel.classList.toggle('dirty', panelDirty)" in script
    assert b"let settingsSaving = false" in script
    assert b"settingsControls.forEach(input => { input.disabled = saving; })" in script
    assert b"if (settingsSaving) { updateSettingsActions('Saving\xe2\x80\xa6'); return false; }" in script
    assert b"if (!settingsDirty || settingsSaving) return" in script
    assert b"function setSubscriptionsOpen(open" in script
    assert b"popupBackdrop?.addEventListener('click', () => closePopupMenus())" in script
    assert b"subscriptionBackdrop?.addEventListener('click'" in script
    assert b"document.getElementById('settings-menu-button')?.addEventListener('click', showSettings)" in script


def test_subscription_manage_mode_has_aligned_accessible_and_bounded_controls(configured):
    app = create_app(str(configured.path))
    client = app.test_client()
    first_page = client.get("/")
    headers = {"X-CSRF-Token": csrf_from(first_page)}
    first = client.post(
        "/api/groups", json={"title": "First source group", "parent_id": ""}, headers=headers,
    ).get_json()["group_id"]
    client.post(
        "/api/groups", json={"title": "Second source group", "parent_id": ""}, headers=headers,
    )
    client.post(
        "/api/feeds",
        json={
            "group_id": first,
            "title": "Example source",
            "xml_url": "https://example.com/feed.xml",
        },
        headers=headers,
    )

    page = client.get("/").data
    assert b'id="subscription-edit-toggle" class="edit-subscriptions" type="button" aria-pressed="false"' in page
    assert b'role="group" aria-label="Manage First source group"' in page
    assert b'class="subscription-move-actions" role="group" aria-label="Move Example source"' in page
    assert b'class="subscription-action move-parent-subscription" aria-label="Move Example source to another group"' in page
    assert b'type="button" class="subscription-action move-subscription" data-direction="up"' in page
    assert b'type="button" class="subscription-action move-subscription" data-direction="down"' in page
    assert 'Use the arrow buttons to reorder a source, or “Move to…” to change its group.'.encode() in page
    assert b'id="move-subscription-dialog" aria-labelledby="move-subscription-heading"' in page
    assert b'name="parent_id" aria-describedby="move-subscription-help move-subscription-status"' in page

    css = client.get("/static/app.css").data
    assert b".subscriptions { container: subscription-pane / inline-size; }" in css
    assert b".subscriptions.editing .feed-row { display: grid; grid-template-columns: 18px minmax(0, 1fr) auto auto" in css
    assert b".subscriptions.editing .group-summary-row { display: grid; grid-template-columns: 18px minmax(0, 1fr) auto" in css
    assert b".subscriptions.editing .subscription-actions { grid-column: 1 / -1; width: 100%; display: grid !important" in css
    assert b"@container subscription-pane (max-width: 410px)" in css
    assert b'.subscriptions[aria-busy="true"] .subscription-actions' in css
    assert b".subscription-action:disabled" in css
    assert b".subscription-action:focus-visible" in css

    script = client.get("/static/app.js").data
    assert b"subscriptionEditButton.setAttribute('aria-pressed', editing ? 'true' : 'false')" in script
    assert b"[subscriptionTree, ...subscriptionTree.querySelectorAll('.subscription-container')]" in script
    assert b"up.disabled = subscriptionMovePending || index === 0" in script
    assert b"down.disabled = subscriptionMovePending || index === entries.length - 1" in script
    assert b"setSubscriptionMovePending(true, entry)" in script
    assert b"setSubscriptionMovePending(false); button.focus(); notify(error.message)" in script
    assert b"function openMoveSubscriptionDialog(entry)" in script
    assert b"entry.dataset.kind === 'group' && entry.contains(group)" in script
    assert b"position: 2147483647" in script
    assert b"if (moveDialogRequestPending) event.preventDefault()" in script


def test_mobile_layers_narrow_pane_controls_and_favicon_are_bounded(configured):
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    assert b'id="popup-backdrop"' in page.data
    assert b'id="subscription-backdrop"' in page.data
    assert b'id="settings-menu-button"' in page.data
    assert b'class="nav-menu main-menu"' in page.data
    assert b'<span class="toolbar-label">Menu</span>' in page.data
    assert b'class="action-menu scope-actions"' in page.data
    favicon = b'<link rel="icon" type="image/svg+xml" href="/static/distillfeed-icon.svg?v=0.22.0">'
    for path in ("/", "/summaries", "/history", "/health", "/notifications", "/costs", "/saved?view=favorites"):
        response = client.get(path)
        assert response.status_code == 200
        assert favicon in response.data, path
    css = client.get("/static/app.css").data
    assert b"container: items-pane / inline-size" in css
    assert b"@container items-pane (max-width: 680px)" in css
    assert b".mobile-menu-glyph { display: none; }" in css
    assert b".top-actions .mobile-menu-glyph { display: inline; }" in css
    assert b"text-decoration: none" in css
    assert b".subscriptions { display: none; position: fixed; z-index: 10040" in css
    assert b".popup-backdrop { z-index: 10045" in css
    assert b"position: fixed; z-index: 10050" in css
    assert b".top-actions .notification-link, .top-actions #settings-button" in css


def test_settings_are_one_atomic_form_with_contained_responsive_sections(configured):
    client = create_app(str(configured.path)).test_client()
    page = client.get("/").data
    settings = page[page.index(b'<dialog id="settings-dialog"'):page.index(b'</form></dialog>', page.index(b'<dialog id="settings-dialog"'))]
    assert settings.count(b'<form id="settings-form"') == 1
    assert settings.count(b'<form') == 1
    assert settings.count(b'id="save-settings-button"') == 1
    assert settings.count(b'id="settings-close-button"') == 1
    panel_count = settings.count(b'data-settings-panel')
    assert panel_count == 6
    assert settings.count(b'data-settings-target=') == panel_count
    assert settings.count(b'class="settings-nav-button settings-nav-link"') == 0
    assert b'id="settings-ai"' in settings and b'href="/ai#queue"' in settings
    assert b'id="ntfy-test-button"' in settings
    assert settings.index(b'id="settings-advanced"') < settings.index(b'id="data-tools-button"')
    assert settings.count(b'data-config-path=') == len(set(re.findall(rb'data-config-path="([^"]+)"', settings)))
    assert b'data-config-path="llm.model"' in settings
    assert b'data-config-path="notifications.ntfy.topic"' in settings


def test_ntfy_test_push_requires_csrf_and_returns_delivery_status(configured, monkeypatch):
    observed = {}

    def send_test(config):
        observed.update(config.section("notifications")["ntfy"])
        return {"status": "delivered", "provider_message_id": "test-id"}

    monkeypatch.setattr(
        "rss_reader.web.send_ntfy_test",
        send_test,
    )
    client = create_app(str(configured.path)).test_client()
    assert client.post("/api/notifications/ntfy/test", json={}).status_code == 403
    page = client.get("/")
    headers = {"X-CSRF-Token": csrf_from(page)}
    saved = client.post(
        "/api/config",
        json={"values": {
            "notifications.ntfy.enabled": True,
            "notifications.ntfy.topic": "live_settings_topic",
        }},
        headers=headers,
    )
    assert saved.status_code == 200
    response = client.post("/api/notifications/ntfy/test", json={}, headers=headers)
    assert response.status_code == 200
    assert response.get_json() == {"status": "delivered", "provider_message_id": "test-id"}
    assert observed["enabled"] is True and observed["topic"] == "live_settings_topic"


def test_ntfy_settings_render_once_and_persist_as_one_atomic_configuration(configured):
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    assert page.data.count(b"Send test alert") == 1
    assert page.data.count(b'data-config-path="notifications.ntfy.enabled"') == 1
    assert page.data.count(b'data-config-path="notifications.ntfy.minimum_relevance"') == 1
    assert page.data.count(b"Save settings") == 1
    response = client.post(
        "/api/config",
        json={"values": {
            "notifications.ntfy.enabled": True,
            "notifications.ntfy.server_url": "https://ntfy.example.test/",
            "notifications.ntfy.topic": "private_phone",
            "notifications.ntfy.minimum_relevance": 91,
            "notifications.ntfy.max_items_per_summary": 3,
            "notifications.ntfy.priority": "max",
            "notifications.ntfy.timeout_seconds": 7,
            "notifications.ntfy.token_env": "PRIVATE_NTFY_TOKEN",
        }},
        headers={"X-CSRF-Token": csrf_from(page)},
    )
    assert response.status_code == 200
    ntfy = load_config(configured.path).section("notifications")["ntfy"]
    assert ntfy == {
        "enabled": True,
        "server_url": "https://ntfy.example.test/",
        "topic": "private_phone",
        "token_env": "PRIVATE_NTFY_TOKEN",
        "minimum_relevance": 91,
        "max_items_per_summary": 3,
        "priority": "max",
        "timeout_seconds": 7,
    }


def test_ntfy_group_and_feed_thresholds_save_with_the_settings_transaction(configured):
    with connect(configured.database_path) as connection:
        parent = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Topics',0,?)", (utcnow(),)
        ).lastrowid
        child = connection.execute(
            "INSERT INTO groups(parent_id,title,position,created_at) VALUES(?, 'Security',0,?)",
            (parent, utcnow()),
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (child, "Advisories", "https://example.test/advisories", utcnow()),
        ).lastrowid
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    assert b"Send alerts to other devices with ntfy" in page.data
    assert f'notifications.ntfy.scopes.group.{parent}.enabled'.encode() in page.data
    assert f'notifications.ntfy.scopes.feed.{feed}.threshold'.encode() in page.data
    response = client.post(
        "/api/config",
        json={
            "values": {
                "notifications.ntfy.enabled": True,
                "notifications.ntfy.topic": "scoped_device_alerts",
            },
            "ntfy_scope_policy": {
                "mode": "selected",
                "rules": [
                    {"scope_kind": "group", "scope_id": parent, "minimum_relevance": 93},
                    {"scope_kind": "feed", "scope_id": feed, "minimum_relevance": 81},
                ],
            },
        },
        headers={"X-CSRF-Token": csrf_from(page)},
    )
    assert response.status_code == 200
    with connect(configured.database_path) as connection:
        mode = connection.execute(
            "SELECT value FROM settings WHERE key='ntfy_scope_mode'"
        ).fetchone()[0]
        rules = connection.execute(
            """SELECT group_id,feed_id,minimum_relevance FROM ntfy_scope_rules
               ORDER BY feed_id IS NOT NULL, id"""
        ).fetchall()
    assert mode == "selected"
    assert [tuple(row) for row in rules] == [(parent, None, 93), (None, feed, 81)]
    rendered = client.get("/").data
    assert b'<option value="selected" selected>Only selected groups and feeds</option>' in rendered
    assert f'notifications.ntfy.scopes.feed.{feed}.enabled" data-type="bool" checked'.encode() in rendered


def test_invalid_ntfy_scope_rule_rolls_back_config_and_rule_changes(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Alerts',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Alerts", "https://example.test/alerts", utcnow()),
        ).lastrowid
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    response = client.post(
        "/api/config",
        json={
            "values": {"notifications.ntfy.topic": "must_not_persist"},
            "ntfy_scope_policy": {
                "mode": "selected",
                "rules": [
                    {"scope_kind": "feed", "scope_id": feed, "minimum_relevance": 101},
                ],
            },
        },
        headers={"X-CSRF-Token": csrf_from(page)},
    )
    assert response.status_code == 400
    assert load_config(configured.path).section("notifications")["ntfy"]["topic"] == ""
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ntfy_scope_rules").fetchone()[0] == 0
        assert connection.execute(
            "SELECT value FROM settings WHERE key='ntfy_scope_mode'"
        ).fetchone() is None


def test_legacy_active_urls_render_as_plain_text(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Unsafe',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Source", "https://example.test/feed", utcnow()),
        ).lastrowid
        connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,url,discovered_at) VALUES(?,?,?,?,?)",
            (feed, "unsafe", "Unsafe legacy URL", "javascript:alert(1)", utcnow()),
        )
    page = create_app(str(configured.path)).test_client().get(f"/?feed={feed}")
    assert b"Unsafe legacy URL" in page.data
    assert b"javascript:alert" not in page.data


def test_corrupt_legacy_sections_do_not_break_reader_or_history(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Legacy',0,?)", (utcnow(),)
        ).lastrowid
        run = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('legacy-json',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        connection.execute(
            "INSERT INTO summaries(llm_run_id,group_id,sections_json,created_at) VALUES(?,?,?,?)",
            (run, group, "not-json", utcnow()),
        )
    client = create_app(str(configured.path)).test_client()
    assert client.get(f"/?group={group}").status_code == 200
    assert client.get("/summaries").status_code == 200
    assert client.get("/history").status_code == 200


def test_configuration_is_saved(configured):
    app = create_app(str(configured.path))
    client = app.test_client()
    csrf = csrf_from(client.get("/"))
    response = client.post(
        "/api/config",
        json={"values": {
            "app.summary_language": "French", "app.interest_profile": "Science",
            "ui.dark_mode": True, "ui.summary_font_size": 10,
            "feeds.user_agent": "DistillFeed-Personal/1.0",
        }},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.get_json()["restart_recommended"] is False
    saved = configured.path.read_text(encoding="utf-8")
    assert 'summary_language = "French"' in saved
    assert "dark_mode = true" in saved
    assert "summary_font_size = 10" in saved
    assert 'user_agent = "DistillFeed-Personal/1.0"' in saved

    invalid = client.post(
        "/api/config", json={"values": {"llm.rolling_digest_hours": 0}},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid.status_code == 400
    invalid_agent = client.post(
        "/api/config", json={"values": {"feeds.user_agent": "bad\nheader"}},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid_agent.status_code == 400


def test_every_effective_configuration_leaf_is_rendered_and_accepted(configured):
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    ai_page = client.get("/ai")
    csrf = csrf_from(page)
    leaves = {}

    def visit(prefix, values):
        for key, value in values.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                visit(path, value)
            else:
                leaves[path] = value

    visit("", configured.data)
    leaves.pop("plugins.enabled")  # internal entry-point list; bundled plugins have named controls
    leaves.pop("app.auto_baseline_initial_refresh", None)  # one-time initialization policy
    user_facing = {
        "ui.dark_mode", "ui.groups_expanded_by_default", "ui.offline_cache_enabled",
        "ui.completion_notifications", "ui.subscription_font_size", "ui.item_font_size",
        "ui.summary_font_size", "app.summary_language", "app.interest_profile",
        "app.auto_refresh_on_load", "app.background_scheduler_enabled",
        "app.auto_summarize_after_refresh", "app.refresh_interval_minutes",
        "feeds.user_agent", "weather.enabled", "weather.language", "weather.location_name",
        "weather.latitude", "weather.longitude", "weather.refresh_minutes", "llm.enabled",
        "llm.provider", "llm.base_url", "llm.model", "llm.review_workload",
        "llm.minimum_relevance", "llm.maximum_summary_items", "llm.max_entries_total",
        "llm.max_entries_per_feed", "llm.candidate_max_age_days", "llm.rolling_digest_hours",
        "llm.max_input_chars", "llm.max_output_tokens", "llm.reasoning_effort",
        "notifications.ntfy.enabled", "notifications.ntfy.server_url",
        "notifications.ntfy.topic", "notifications.ntfy.token_env",
        "notifications.ntfy.minimum_relevance", "notifications.ntfy.max_items_per_summary",
        "notifications.ntfy.priority", "plugins.arxiv_digest_enabled",
    }
    combined = page.data + ai_page.data
    for path in user_facing:
        assert f'data-config-path="{path}"'.encode() in combined
    response = client.post(
        "/api/config", json={"values": leaves}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert response.get_json()["restart_recommended"] is False


def test_fresh_csrf_endpoint_recovers_a_changed_browser_session(configured):
    client = create_app(str(configured.path)).test_client()
    stale = csrf_from(client.get("/"))
    with client.session_transaction() as session:
        session["csrf_token"] = "replacement-token"
    rejected = client.post(
        "/api/config", json={"values": {}}, headers={"X-CSRF-Token": stale}
    )
    assert rejected.status_code == 403
    fresh = client.get("/api/csrf")
    assert fresh.status_code == 200
    assert fresh.headers["Cache-Control"] == "no-store"
    token = fresh.get_json()["csrf_token"]
    assert token == "replacement-token"
    accepted = client.post(
        "/api/config", json={"values": {}}, headers={"X-CSRF-Token": token}
    )
    assert accepted.status_code == 200


def test_refresh_and_summary_actions_forward_explicit_options(configured, monkeypatch):
    calls = []
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Jobs',0,?)", (utcnow(),)
        ).lastrowid
        connection.execute(
            "INSERT INTO feeds(id,group_id,title,xml_url,created_at) VALUES(42,?,?,?,?)",
            (group, "Job feed", "https://example.test/jobs", utcnow()),
        )
    monkeypatch.setattr(
        "rss_reader.web.run_refresh",
        lambda config, **kwargs: calls.append(("refresh", kwargs)),
    )
    monkeypatch.setattr(
        "rss_reader.web.run_update_summaries",
        lambda config, **kwargs: calls.append(("summary", kwargs)),
    )
    monkeypatch.setattr(
        "rss_reader.web.start_thread",
        lambda target, *, name: target(),
    )
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    headers = {"X-CSRF-Token": csrf}
    refresh = client.post(
        "/api/refresh", json={"feed_id": 42, "force": True, "automatic": False},
        headers=headers,
    )
    summary = client.post("/api/summarize", json={}, headers=headers)
    assert refresh.status_code == 202 and summary.status_code == 202
    assert [kind for kind, _ in calls] == ["refresh", "summary"]
    refresh_call, summary_call = calls[0][1], calls[1][1]
    assert refresh_call.pop("_reserved_owner")
    assert summary_call.pop("_reserved_owner")
    assert refresh_call == {
        "feed_id": 42, "group_id": None, "force": True,
        "automatic": False, "summarize_after": False,
    }
    assert summary_call == {
        "automatic": False, "group_id": None, "feed_id": None,
        "include_plugins": False, "include_generic": True,
    }


def test_refresh_and_summary_actions_can_target_exactly_one_scope(configured, monkeypatch):
    calls = []
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Scoped jobs',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Scoped feed", "https://example.test/scoped-job", utcnow()),
        ).lastrowid
    monkeypatch.setattr(
        "rss_reader.web.run_refresh",
        lambda config, **kwargs: calls.append(("refresh", kwargs)),
    )
    monkeypatch.setattr(
        "rss_reader.web.run_update_summaries",
        lambda config, **kwargs: calls.append(("summary", kwargs)),
    )
    monkeypatch.setattr("rss_reader.web.start_thread", lambda target, *, name: target())
    client = create_app(str(configured.path)).test_client()
    headers = {"X-CSRF-Token": csrf_from(client.get("/"))}

    legacy_combined = client.post(
        "/api/refresh",
        json={"group_id": group, "force": True, "summarize_after": True},
        headers=headers,
    )
    refreshed = client.post(
        "/api/refresh", json={"group_id": group, "force": True}, headers=headers,
    )
    summarized = client.post(
        "/api/summarize", json={"feed_id": feed}, headers=headers,
    )
    rejected = client.post(
        "/api/refresh", json={"group_id": group, "feed_id": feed}, headers=headers,
    )
    assert legacy_combined.status_code == 400
    assert refreshed.status_code == 202 and summarized.status_code == 202
    assert rejected.status_code == 400
    assert [kind for kind, _ in calls] == ["refresh", "summary"]
    refresh_call, summary_call = calls[0][1], calls[1][1]
    assert refresh_call.pop("_reserved_owner")
    assert summary_call.pop("_reserved_owner")
    assert refresh_call == {
        "feed_id": None, "group_id": group, "force": True,
        "automatic": False, "summarize_after": False,
    }
    assert summary_call == {
        "automatic": False, "group_id": None, "feed_id": feed,
        "include_plugins": False, "include_generic": True,
    }


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/items/999/read", {"read": "perhaps"}),
        ("post", "/api/items/bulk-star", {"item_ids": ["not-an-id"], "starred": True}),
        ("post", "/api/groups", {"title": "Bad parent", "parent_id": "nan"}),
        ("post", "/api/feeds", {"xml_url": "https://example.test/feed", "group_id": "nan"}),
        ("post", "/api/config", {"values": {"ui.dark_mode": "perhaps"}}),
    ],
)
def test_malformed_mutations_return_json_400_not_server_errors(configured, method, path, payload):
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    response = getattr(client, method)(path, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert response.is_json and response.get_json()["error"]


def test_missing_entities_return_404(configured):
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/items/999/read", json={"read": True}, headers=headers).status_code == 404
    assert client.delete("/api/groups/999", headers=headers).status_code == 404
    assert client.delete("/api/feeds/999", headers=headers).status_code == 404
    assert client.post(
        "/api/items/bulk-read", json={"mode": "scope", "feed_id": 999, "read": True}, headers=headers
    ).status_code == 404
    assert client.post(
        "/api/items/bulk-read", json={"mode": "scope", "group_id": 999, "read": True}, headers=headers
    ).status_code == 404
    assert client.post(
        "/api/refresh", json={"feed_id": 999, "force": True}, headers=headers
    ).status_code == 404


def test_opml_write_failure_rolls_back_database_mutation(configured, monkeypatch):
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    monkeypatch.setattr(
        "rss_reader.web.write_database_opml",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    response = client.post(
        "/api/groups", json={"title": "Must roll back", "llm_enabled": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.is_json
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM groups WHERE title='Must roll back'"
        ).fetchone()[0] == 0


def test_config_file_failure_rolls_back_mirrored_database_settings(configured, monkeypatch):
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    original = configured.path.read_bytes()
    monkeypatch.setattr(
        "rss_reader.web.save_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    response = client.post(
        "/api/config", json={"values": {"app.summary_language": "French"}},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 500
    assert configured.path.read_bytes() == original
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT value FROM settings WHERE key='summary_language'"
        ).fetchone() is None


def test_subscription_groups_can_start_collapsed_or_expanded(configured):
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Reading',0,?)", (utcnow(),)
        ).lastrowid
    app = create_app(str(configured.path))
    collapsed = app.test_client().get("/").data
    marker = f'<details class="group subscription-entry" data-kind="group" data-id="{group_id}"'.encode()
    assert marker in collapsed
    assert marker + b" open" not in collapsed

    configured.path.write_text(
        configured.path.read_text(encoding="utf-8") + "\n[ui]\ngroups_expanded_by_default = true\n",
        encoding="utf-8",
    )
    expanded = create_app(str(configured.path)).test_client().get("/").data
    assert marker + b" open" in expanded


def test_all_summaries_page_contains_active_feed_summary(configured):
    app = create_app(str(configured.path))
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Research',0,?)", (utcnow(),)
        ).lastrowid
        feed_id = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group_id, "Journal", "https://example.test/feed", utcnow()),
        ).lastrowid
        item_id = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,url,discovered_at) VALUES(?,?,?,?,?)",
            (feed_id, "one", "Important result", "https://example.test/one", utcnow()),
        ).lastrowid
        run_id = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('page-test',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        summary_id = connection.execute(
            "INSERT INTO summaries(llm_run_id,group_id,overview,created_at) VALUES(?,?,?,?)",
            (run_id, group_id, "A clear overview.", utcnow()),
        ).lastrowid
        connection.execute(
            """INSERT INTO summary_items(summary_id,item_id,included,rank,importance,description,justification)
               VALUES(?,?,1,1,90,'Description','Reason')""",
            (summary_id, item_id),
        )
        second_item_id = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,url,discovered_at) VALUES(?,?,?,?,?)",
            (feed_id, "two", "New follow-up", "https://example.test/two", utcnow()),
        ).lastrowid
        second_run = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('page-test-2',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        second_summary = connection.execute(
            """INSERT INTO summaries(llm_run_id,group_id,changes,sections_json,created_at)
               VALUES(?,?,?,'[]',?)""",
            (second_run, group_id, "A follow-up was published.", utcnow()),
        ).lastrowid
        connection.execute(
            """INSERT INTO summary_items(summary_id,item_id,included,rank,importance,description,justification)
               VALUES(?,?,1,1,80,'New description','New reason')""",
            (second_summary, second_item_id),
        )
    page = app.test_client().get("/summaries")
    assert page.status_code == 200
    assert b"Important result" not in page.data
    assert b"New follow-up" in page.data
    assert b"Rolling view combining" not in page.data
    assert b"A clear overview" not in page.data
    reader_page = app.test_client().get(f"/?group={group_id}")
    assert reader_page.status_code == 200
    assert b"Description</" not in reader_page.data
    assert b"New description" in reader_page.data
    assert b"rolling view combines" not in reader_page.data
    with connect(configured.database_path) as connection:
        connection.execute("UPDATE feeds SET llm_enabled=0 WHERE id=?", (feed_id,))
    active_page = app.test_client().get("/summaries")
    assert b"New follow-up" in active_page.data  # Stored results remain readable.
    paused_reader = app.test_client().get(f"/?group={group_id}")
    assert b"New description" in paused_reader.data
    assert b"New reason" in paused_reader.data
