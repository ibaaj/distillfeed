import time

import pytest
import requests

from rss_reader.db import connect, utcnow
from rss_reader.feeds import _entries_to_store, plain_text, refresh_all, refresh_feed


def test_plain_text_removes_feed_markup():
    assert plain_text(
        "<p>Hello &amp; <strong>world</strong></p><script>bad()</script><style>.bad{}</style>"
    ) == "Hello & world"


def test_initial_import_is_capped_by_age_and_count():
    now = time.gmtime()
    old = time.gmtime(time.time() - 90 * 86400)
    entries = [
        {"id": "new-1", "published_parsed": now},
        {"id": "new-2", "published_parsed": now},
        {"id": "new-3", "published_parsed": now},
        {"id": "old", "published_parsed": old},
    ]
    selected, initial = _entries_to_store(
        entries,
        {"last_success_at": None},
        {
            "max_entries_per_feed_update": 200,
            "initial_import_max_entries_per_feed": 2,
            "initial_import_max_age_days": 30,
        },
    )
    assert initial is True
    assert len(selected) == 2
    assert all(entry["id"].startswith("new") for entry in selected)


def test_initial_import_uses_bounded_fallback_for_old_only_bibliography_feed():
    old = time.gmtime(time.time() - 45 * 86400)
    older = time.gmtime(time.time() - 90 * 86400)
    selected, initial = _entries_to_store(
        [
            {"id": "older", "published_parsed": older},
            {"id": "newest-publication", "published_parsed": old},
            {"id": "also-old", "published_parsed": older},
        ],
        {"id": 7, "last_success_at": None},
        {
            "max_entries_per_feed_update": 200,
            "initial_import_max_entries_per_feed": 2,
            "initial_import_max_age_days": 30,
        },
    )
    assert initial is True
    assert len(selected) == 2
    assert selected[0]["id"] == "newest-publication"


def test_group_refresh_is_limited_to_group_and_descendants(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        root = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Refresh root',0,?)", (utcnow(),)
        ).lastrowid
        child = connection.execute(
            "INSERT INTO groups(parent_id,title,position,created_at) VALUES(?,'Refresh child',0,?)",
            (root, utcnow()),
        ).lastrowid
        other = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Refresh other',1,?)", (utcnow(),)
        ).lastrowid
        expected = {
            int(connection.execute(
                "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
                (group, f"Feed {index}", f"https://example.test/refresh-{index}", utcnow()),
            ).lastrowid)
            for index, group in enumerate((root, child))
        }
        connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (other, "Outside", "https://example.test/refresh-outside", utcnow()),
        )
        seen = []
        monkeypatch.setattr(
            "rss_reader.feeds.refresh_feed",
            lambda connection, config, feed, force=False: seen.append(int(feed["id"])) or 0,
        )
        result = refresh_all(connection, configured, group_id=int(root), force=True)
    assert set(seen) == expected
    assert result == {"attempted": 2, "succeeded": 2, "failed": 0, "new_items": 0}


class FakeResponse:
    def __init__(self, content: bytes = b"", status: int = 200):
        self.content = content
        self.status_code = status
        self.headers = {}
        self.url = "https://orientxxi.info/?page=backend&lang=fr"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)

    def iter_content(self, chunk_size=65536):
        yield self.content


def _orient_feed(configured):
    with connect(configured.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('News',0,?)", (utcnow(),)
        ).lastrowid
        feed = connection.execute(
            """INSERT INTO feeds(group_id,title,xml_url,created_at)
               VALUES(?, 'Orient XXI', 'https://orientxxi.info/?page=backend&lang=fr', ?)""",
            (group, utcnow()),
        ).lastrowid
    return int(feed)


def test_feed_with_query_string_and_missing_content_type_is_retrieved(configured, monkeypatch):
    feed_id = _orient_feed(configured)
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Orient XXI</title>
      <item><guid>one</guid><title>Article</title><link>https://orientxxi.info/article</link>
      <description><![CDATA[<p>Texte du flux.</p>]]></description></item></channel></rss>"""
    monkeypatch.setattr("rss_reader.feeds.validate_http_url", lambda *args, **kwargs: None)
    request_options = {}

    def fake_get(*args, **kwargs):
        request_options.update(kwargs)
        return FakeResponse(rss)

    monkeypatch.setattr("rss_reader.feeds.safe_get", fake_get)
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        assert refresh_feed(connection, configured, feed, force=True) == 1
        item = connection.execute("SELECT * FROM items WHERE feed_id=?", (feed_id,)).fetchone()
        updated = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    assert item["title"] == "Article"
    assert item["description_text"] == "Texte du flux."
    assert updated["last_error"] is None and updated["last_http_status"] == 200
    assert request_options["headers"]["User-Agent"] == configured.get("feeds", "user_agent")


def test_http_refusal_records_diagnostic_without_deleting_subscription(configured, monkeypatch):
    feed_id = _orient_feed(configured)
    monkeypatch.setattr("rss_reader.feeds.validate_http_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "rss_reader.feeds.safe_get", lambda *args, **kwargs: FakeResponse(status=403)
    )
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        with pytest.raises(requests.HTTPError):
            refresh_feed(connection, configured, feed, force=True)
        stored = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    assert stored is not None
    assert stored["last_http_status"] == 403
    assert "403 Client Error" in stored["last_error"]


def test_active_feed_links_are_not_stored(configured, monkeypatch):
    feed_id = _orient_feed(configured)
    rss = b"""<rss version="2.0"><channel><title>Safe feed</title>
      <link>javascript:alert('feed')</link><item><guid>bad-link</guid><title>Safe title</title>
      <link>javascript:alert('item')</link></item></channel></rss>"""
    monkeypatch.setattr("rss_reader.feeds.validate_http_url", lambda *args, **kwargs: None)
    monkeypatch.setattr("rss_reader.feeds.safe_get", lambda *args, **kwargs: FakeResponse(rss))
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        assert refresh_feed(connection, configured, feed, force=True) == 1
        item = connection.execute("SELECT * FROM items WHERE feed_id=?", (feed_id,)).fetchone()
        updated = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    assert item["url"] is None
    assert updated["html_url"] is None
