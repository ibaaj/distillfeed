from __future__ import annotations

import os

import pytest

from rss_reader.config import ensure_runtime_directories, save_config
from rss_reader.db import connect, utcnow
from rss_reader.feeds import refresh_feed
from rss_reader.generated_feeds import read_generated_feed, validate_generated_feed_url
from rss_reader.web import create_app


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Generated signals</title>
  <link href="https://example.test/"/>
  <id>urn:distillfeed:test</id>
  <updated>2026-07-17T08:00:00Z</updated>
  <entry>
    <title>Generated item</title>
    <id>urn:distillfeed:test:item-1</id>
    <link href="https://example.test/item-1"/>
    <updated>2026-07-17T08:00:00Z</updated>
    <summary>Created by an external collector.</summary>
  </entry>
</feed>
"""


def enable_generated_directory(configured, directory) -> None:
    configured.data["feeds"]["generated_feed_directory"] = str(directory)
    save_config(configured)
    ensure_runtime_directories(configured)


def test_generated_feed_file_is_read_without_executing_a_command(configured, tmp_path):
    drop = tmp_path / "generated"
    enable_generated_directory(configured, drop)
    (drop / "signals.xml").write_bytes(ATOM)
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Generated',0,?)",
            (utcnow(),),
        ).lastrowid
        feed_id = connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Signals", "generated://signals.xml", utcnow()),
        ).lastrowid
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        assert refresh_feed(connection, configured, feed, force=True) == 1
        item = connection.execute(
            "SELECT title,url,description_text FROM items WHERE feed_id=?", (feed_id,)
        ).fetchone()
        current = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    assert dict(item) == {
        "title": "Generated item",
        "url": "https://example.test/item-1",
        "description_text": "Created by an external collector.",
    }
    assert current["last_success_at"] and current["last_http_status"] is None
    assert current["etag"] is None and current["last_modified"] is None


@pytest.mark.parametrize(
    "value",
    [
        "generated://../secret.xml",
        "generated://nested/feed.xml",
        "generated://feed.xml?version=2",
        "generated://feed.py",
        "file:///etc/passwd",
    ],
)
def test_generated_feed_reference_rejects_paths_and_non_feed_names(configured, tmp_path, value):
    enable_generated_directory(configured, tmp_path / "drop")
    with pytest.raises(ValueError):
        validate_generated_feed_url(configured, value)


def test_generated_feed_reader_rejects_symlinks(configured, tmp_path):
    drop = tmp_path / "drop"
    enable_generated_directory(configured, drop)
    target = tmp_path / "outside.xml"
    target.write_bytes(ATOM)
    os.symlink(target, drop / "linked.xml")
    with pytest.raises(ValueError, match="safe regular file"):
        read_generated_feed(configured, "generated://linked.xml", 1024 * 1024)


def test_generated_feed_reader_enforces_the_configured_byte_limit(configured, tmp_path):
    drop = tmp_path / "drop"
    enable_generated_directory(configured, drop)
    (drop / "large.xml").write_bytes(ATOM)
    with pytest.raises(ValueError, match="exceeds"):
        read_generated_feed(configured, "generated://large.xml", len(ATOM) - 1)


def test_generated_feed_requires_server_admin_opt_in(configured):
    with pytest.raises(ValueError, match="server administrator"):
        validate_generated_feed_url(configured, "generated://signals.xml")


def test_web_accepts_only_safe_generated_references_after_server_opt_in(configured, tmp_path):
    drop = tmp_path / "drop"
    enable_generated_directory(configured, drop)
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Generated',0,?)",
            (utcnow(),),
        ).lastrowid
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    token = page.data.split(b'<meta name="csrf-token" content="', 1)[1].split(b'"', 1)[0].decode()
    accepted = client.post(
        "/api/feeds",
        json={"group_id": group, "title": "Local", "xml_url": "generated://local.xml"},
        headers={"X-CSRF-Token": token},
    )
    rejected = client.post(
        "/api/feeds",
        json={"group_id": group, "title": "Escape", "xml_url": "generated://../escape.xml"},
        headers={"X-CSRF-Token": token},
    )
    assert accepted.status_code == 201
    assert rejected.status_code == 400
    assert b"generated://name.xml" in page.data


def test_generated_directory_cannot_be_changed_through_runtime_settings(configured, tmp_path):
    client = create_app(str(configured.path)).test_client()
    page = client.get("/")
    token = page.data.split(b'<meta name="csrf-token" content="', 1)[1].split(b'"', 1)[0].decode()
    response = client.post(
        "/api/config",
        json={"values": {"feeds.generated_feed_directory": str(tmp_path / "unsafe")}},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert not (tmp_path / "unsafe").exists()
