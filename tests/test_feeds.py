import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from rss_reader.db import connect, utcnow
from rss_reader.feeds import (
    FeedSourceFailure, _content_kind, _entries_to_store, plain_text, refresh_all, refresh_feed,
)


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
    assert result == {
        "attempted": 2, "succeeded": 2, "failed": 0, "deferred": 0,
        "new_items": 0, "failure_kinds": {}, "feed_failures": [],
        "deferred_kinds": {}, "feed_deferred": [],
    }


class FakeResponse:
    def __init__(
        self, content: bytes = b"", status: int = 200, *,
        content_type: str | None = None, headers: dict | None = None,
    ):
        self.content = content
        self.status_code = status
        self.headers = dict(headers or {})
        if content_type is not None:
            self.headers["Content-Type"] = content_type
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


def test_forced_refresh_omits_cached_http_validators(configured, monkeypatch):
    feed_id = _orient_feed(configured)
    rss = b'<rss version="2.0"><channel><title>Feed</title></channel></rss>'
    captured = {}
    monkeypatch.setattr("rss_reader.feeds.validate_http_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "rss_reader.feeds.safe_get",
        lambda *args, **kwargs: captured.update(kwargs) or FakeResponse(rss),
    )
    with connect(configured.database_path) as connection:
        connection.execute(
            "UPDATE feeds SET etag='stale-etag',last_modified='yesterday' WHERE id=?",
            (feed_id,),
        )
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        refresh_feed(connection, configured, feed, force=True)
    assert "If-None-Match" not in captured["headers"]
    assert "If-Modified-Since" not in captured["headers"]



def test_provider_challenge_is_classified_without_parsing_or_bypass(configured, monkeypatch):
    feed_id = _orient_feed(configured)
    challenge = b"""<!doctype html><html><head><title>Making sure you're not a bot!</title></head>
    <body><script src=\"/.within.website/x/xess/main.mjs\"></script>Anubis</body></html>"""
    monkeypatch.setattr("rss_reader.feeds.validate_http_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "rss_reader.feeds.safe_get",
        lambda *args, **kwargs: FakeResponse(
            challenge, status=200, content_type="text/html; charset=utf-8",
        ),
    )
    parse_calls = []
    monkeypatch.setattr(
        "rss_reader.feeds.feedparser.parse",
        lambda *_args, **_kwargs: parse_calls.append(True),
    )
    before = datetime.now(UTC)
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        with pytest.raises(FeedSourceFailure, match="anti-bot HTML challenge") as captured:
            refresh_feed(connection, configured, feed, force=True)
        stored = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    assert captured.value.kind == "provider-challenge"
    assert parse_calls == []
    assert stored["last_http_status"] == 200
    assert stored["last_content_type"] == "text/html; charset=utf-8"
    assert stored["last_error_kind"] == "provider-challenge"
    assert "deferred" in stored["last_error"].lower()
    assert datetime.fromisoformat(stored["next_retry_at"]) >= before + timedelta(hours=23)


def test_mixed_group_refresh_keeps_successes_and_reports_source_deferral(configured, monkeypatch):
    with connect(configured.database_path) as connection:
        group_id = int(connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Mixed',0,?)", (utcnow(),)
        ).lastrowid)
        identifiers = []
        for index in range(3):
            identifiers.append(int(connection.execute(
                "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
                (group_id, f"Feed {index}", f"https://feed-{index}.example.test/feed.xml", utcnow()),
            ).lastrowid))

        def fake_refresh(_connection, _config, feed, force=False):
            if int(feed["id"]) == identifiers[1]:
                raise FeedSourceFailure(
                    "Source returned an anti-bot HTML challenge; RSS retrieval deferred",
                    kind="provider-challenge", status_code=200,
                    content_type="text/html; charset=utf-8", retry_hours=24,
                )
            return 2

        monkeypatch.setattr("rss_reader.feeds.refresh_feed", fake_refresh)
        result = refresh_all(connection, configured, group_id=group_id, force=True)
    assert result["attempted"] == 3
    assert result["succeeded"] == 2
    assert result["failed"] == 0
    assert result["deferred"] == 1
    assert result["new_items"] == 4
    assert result["failure_kinds"] == {}
    assert result["feed_failures"] == []
    assert result["deferred_kinds"] == {"provider-challenge": 1}
    assert result["feed_deferred"][0]["id"] == identifiers[1]
    assert result["feed_deferred"][0]["title"] == "Feed 1"
    assert result["feed_deferred"][0]["kind"] == "provider-challenge"


def test_future_source_date_is_preserved_but_item_is_grouped_by_discovery(configured, monkeypatch):
    feed_id = _orient_feed(configured)
    future = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    rss = f"""<?xml version=\"1.0\"?><rss version=\"2.0\"><channel><title>Future feed</title>
      <item><guid>future-one</guid><title>Future item</title>
      <link>https://example.test/future</link><pubDate>{format_datetime(future)}</pubDate>
      <description>Future source date.</description></item></channel></rss>""".encode()
    monkeypatch.setattr("rss_reader.feeds.validate_http_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "rss_reader.feeds.safe_get",
        lambda *args, **kwargs: FakeResponse(rss, content_type="application/rss+xml"),
    )
    before = datetime.now(UTC) - timedelta(seconds=2)
    with connect(configured.database_path) as connection:
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        assert refresh_feed(connection, configured, feed, force=True) == 1
        item = connection.execute("SELECT * FROM items WHERE feed_id=?", (feed_id,)).fetchone()
    assert datetime.fromisoformat(item["source_published_at"]) == future
    display = datetime.fromisoformat(item["published_at"])
    discovered = datetime.fromisoformat(item["discovered_at"])
    assert display == discovered
    assert display >= before
    assert item["date_warning"] == "future-source-date"


def test_youtube_short_requires_explicit_shorts_url():
    assert _content_kind("https://www.youtube.com/shorts/NmiD1YdbrAA") == (
        "youtube-short", "explicit-url",
    )
    assert _content_kind("https://m.youtube.com/shorts/NmiD1YdbrAA?feature=share") == (
        "youtube-short", "explicit-url",
    )
    assert _content_kind("https://www.youtube.com/watch?v=NmiD1YdbrAA") == ("", "")
    assert _content_kind("https://example.test/shorts/NmiD1YdbrAA") == ("", "")


def test_existing_provider_challenge_defers_entire_host_without_requests(configured, monkeypatch):
    configured.data["feeds"]["max_workers_per_host"] = 1
    with connect(configured.database_path) as connection:
        group_id = int(connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('DBLP',0,?)", (utcnow(),)
        ).lastrowid)
        future = (datetime.now(UTC) + timedelta(hours=20)).isoformat()
        for index in range(3):
            connection.execute(
                """INSERT INTO feeds(group_id,title,xml_url,created_at,last_error_kind,next_retry_at)
                   VALUES(?,?,?,?,?,?)""",
                (group_id, f"DBLP {index}", f"https://dblp.org/pid/example-{index}.rss",
                 utcnow(), "provider-challenge" if index == 0 else None,
                 future if index == 0 else None),
            )
        monkeypatch.setattr(
            "rss_reader.feeds.refresh_feed",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("challenged host should not be requested")
            ),
        )
        result = refresh_all(connection, configured, group_id=group_id, force=False)
    assert result["attempted"] == 3
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    assert result["deferred"] == 3
    assert result["deferred_kinds"] == {"provider-challenge": 3}
    assert len(result["feed_deferred"]) == 3


def test_new_provider_challenge_stops_queued_siblings_on_same_host(configured, monkeypatch):
    configured.data["feeds"]["max_workers_per_host"] = 1
    with connect(configured.database_path) as connection:
        group_id = int(connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('DBLP queue',0,?)", (utcnow(),)
        ).lastrowid)
        for index in range(3):
            connection.execute(
                "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
                (group_id, f"DBLP {index}", f"https://dblp.org/pid/queued-{index}.rss", utcnow()),
            )
        calls = []

        def challenge(_connection, _config, feed, force=False):
            calls.append(int(feed["id"]))
            raise FeedSourceFailure(
                "Source returned an anti-bot HTML challenge; RSS retrieval deferred",
                kind="provider-challenge", status_code=200,
                content_type="text/html; charset=utf-8", retry_hours=24,
            )

        monkeypatch.setattr("rss_reader.feeds.refresh_feed", challenge)
        result = refresh_all(connection, configured, group_id=group_id, force=True)
    assert len(calls) == 1
    assert result["attempted"] == 3
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    assert result["deferred"] == 3
    assert result["failure_kinds"] == {}
    assert result["deferred_kinds"] == {"provider-challenge": 3}
