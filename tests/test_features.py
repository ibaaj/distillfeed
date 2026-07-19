import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from rss_reader.backup import build_backup, restore_backup
from rss_reader.db import connect, utcnow
from rss_reader.web import create_app
from rss_reader.opml import build_tree_from_database, parse_opml_bytes


def csrf_from(page) -> str:
    text = page.get_data(as_text=True)
    return text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]


def seed_item(configured) -> tuple[int, int]:
    with connect(configured.database_path) as connection:
        group_id = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Research',0,?)", (utcnow(),)
        ).lastrowid
        feed_id = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group_id, "Journal", "https://example.test/research", utcnow()),
        ).lastrowid
        item_id = connection.execute(
            "INSERT INTO items(feed_id,stable_id,title,discovered_at) VALUES(?,?,?,?)",
            (feed_id, "paper", "A result", utcnow()),
        ).lastrowid
    return group_id, item_id


def test_read_later_tags_history_health_and_pwa(configured):
    _, item_id = seed_item(configured)
    app = create_app(str(configured.path))
    client = app.test_client()
    page = client.get("/")
    csrf = csrf_from(page)
    headers = {"X-CSRF-Token": csrf}
    response = client.post(
        "/api/items/bulk-read-later",
        json={"item_ids": [item_id], "read_later": True}, headers=headers,
    )
    assert response.status_code == 200
    response = client.post(
        "/api/items/bulk-tags",
        json={"item_ids": [item_id], "tags": ["Research", "Follow-up"]}, headers=headers,
    )
    assert response.status_code == 200
    response = client.post(
        "/api/items/bulk-star", json={"item_ids": [item_id], "starred": True}, headers=headers,
    )
    assert response.status_code == 200
    with connect(configured.database_path) as connection:
        item = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        tags = connection.execute(
            "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id=t.id WHERE it.item_id=?", (item_id,)
        ).fetchall()
    assert item["is_read_later"] == 1
    assert item["is_starred"] == 1
    assert {row["name"] for row in tags} == {"Research", "Follow-up"}
    assert b"A result" in client.get("/saved?view=read-later").data
    assert b"A result" in client.get("/saved?view=favorites").data
    assert b"A result" in client.get("/saved?view=tags").data
    assert b"A result" in client.get("/saved?tag=Research").data
    response = client.post(
        f"/api/items/{item_id}/read-later", json={"read_later": False}, headers=headers,
    )
    assert response.status_code == 200
    assert b"A result" not in client.get("/saved?view=read-later").data
    assert client.get("/history").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/service-worker.js").status_code == 200


def test_tags_are_casefolded_and_orphans_are_removed(configured):
    _, item_id = seed_item(configured)
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    headers = {"X-CSRF-Token": csrf}
    assigned = client.post(
        "/api/items/bulk-tags",
        json={"item_ids": [item_id], "tags": ["Research", "research", "  Research  "]},
        headers=headers,
    )
    assert assigned.status_code == 200
    assert assigned.get_json()["tags"] == ["Research"]
    removed = client.post(
        "/api/items/bulk-tags", json={"item_ids": [item_id], "tags": []}, headers=headers,
    )
    assert removed.status_code == 200
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
    missing = client.post(
        "/api/items/bulk-tags", json={"item_ids": [999999], "tags": ["Ghost"]}, headers=headers,
    )
    assert missing.status_code == 404
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0


def test_backup_round_trip_creates_safety_copy(configured):
    _, item_id = seed_item(configured)
    backup_file = build_backup(configured)
    backup = backup_file.read()
    backup_file.close()
    with zipfile.ZipFile(io.BytesIO(backup)) as archive:
        assert {"manifest.json", "reader.sqlite3", "config.toml.reference"} <= set(archive.namelist())
    with connect(configured.database_path) as connection:
        connection.execute("DELETE FROM items WHERE id=?", (item_id,))
    safety = restore_backup(configured, backup)
    assert safety.exists()
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items WHERE id=?", (item_id,)).fetchone()[0] == 1
        database_tree = build_tree_from_database(connection)
        assert connection.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0] == 0
    assert parse_opml_bytes(configured.working_opml_path.read_bytes()) == database_tree


def test_backup_download_and_restore_rejects_unknown_archive(configured):
    app = create_app(str(configured.path))
    client = app.test_client()
    page = client.get("/")
    csrf = csrf_from(page)
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    assert backup.mimetype == "application/zip"
    response = client.post(
        "/api/restore", data={"backup": (io.BytesIO(b"not a zip"), "bad.zip")},
        headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert not (configured.path.parent / "backups").exists()


@pytest.mark.parametrize(
    ("manifest", "database"),
    [([], b"not sqlite"), ({"format": "distillfeed-backup", "version": 1}, b"not sqlite")],
)
def test_restore_rejects_structurally_invalid_archives_without_safety_backup(
    configured, manifest, database
):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("reader.sqlite3", database)
    with pytest.raises(ValueError):
        restore_backup(configured, payload.getvalue())
    assert not (configured.path.parent / "backups").exists()


def test_restore_waits_for_active_jobs(configured):
    app = create_app(str(configured.path))
    client = app.test_client()
    csrf = csrf_from(client.get("/"))
    backup_file = build_backup(configured)
    backup = backup_file.read()
    backup_file.close()
    with connect(configured.database_path) as connection:
        connection.execute(
            """INSERT INTO job_locks(name,owner,acquired_at,expires_at)
               VALUES('feed-refresh','other',?,?)""",
            (utcnow(), (datetime.now(UTC) + timedelta(hours=1)).isoformat()),
        )
    response = client.post(
        "/api/restore", data={"backup": (io.BytesIO(backup), "backup.zip")},
        headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data",
    )
    assert response.status_code == 409
    assert "active refresh" in response.get_json()["error"]


def test_deleting_feed_invalidates_dependent_digest_and_orphan_tags(configured):
    group_id, item_id = seed_item(configured)
    with connect(configured.database_path) as connection:
        feed_id = connection.execute("SELECT feed_id FROM items WHERE id=?", (item_id,)).fetchone()[0]
        tag = connection.execute(
            "INSERT INTO tags(name,created_at) VALUES('Only here',?)", (utcnow(),)
        ).lastrowid
        connection.execute("INSERT INTO item_tags(item_id,tag_id) VALUES(?,?)", (item_id, tag))
        run = connection.execute(
            """INSERT INTO llm_runs(request_key,started_at,completed_at,status,model,prompt_version,pricing_json)
               VALUES('delete-feed',?,?,'success','model','prompt','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid
        summary = connection.execute(
            "INSERT INTO summaries(llm_run_id,group_id,sections_json,created_at) VALUES(?,?,?,?)",
            (run, group_id, '[{"heading":"Digest","body":"Uses deleted source"}]', utcnow()),
        ).lastrowid
        connection.execute(
            "INSERT INTO summary_items(summary_id,item_id,included) VALUES(?,?,1)",
            (summary, item_id),
        )
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    response = client.delete(f"/api/feeds/{feed_id}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    with connect(configured.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM summaries WHERE id=?", (summary,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
