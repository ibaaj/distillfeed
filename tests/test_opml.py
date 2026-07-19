from pathlib import Path

import pytest

from rss_reader.db import connect
from rss_reader.opml import (
    atomic_write,
    build_tree_from_database,
    import_groups,
    parse_opml_bytes,
    serialize_opml,
)


def test_starter_opml_is_valid_and_separate():
    source = Path("examples/starter-subscriptions.opml").read_bytes()
    assert source == Path("rss_reader/resources/starter-subscriptions.opml").read_bytes()
    groups = parse_opml_bytes(source)
    assert {group.title for group in groups} == {
        "Programming", "Artificial intelligence", "Science and technology",
    }
    urls = {feed.xml_url for group in groups for feed in group.feeds}
    assert "https://github.blog/feed/" in urls
    assert "https://simonwillison.net/atom/everything/" in urls
    assert "https://export.arxiv.org/rss/cs.AI" not in urls
    assert "https://deepmind.google/blog/rss.xml" in urls
    assert any(feed.title == "Anthropic News" for group in groups for feed in group.feeds)
    assert any(feed.title == "Google AI" for group in groups for feed in group.feeds)
    assert "https://news.google.com/rss/search?q=site%3Aanthropic.com%2Fnews&hl=en-US&gl=US&ceid=US%3Aen" in urls
    assert "https://deepmind.google/blog/rss.xml" in urls
    assert len(urls) == 11


def test_empty_starter_opml_is_valid():
    groups = parse_opml_bytes(Path("examples/empty-subscriptions.opml").read_bytes())
    assert groups == []


SAMPLE = b"""<?xml version="1.0"?>
<opml version="2.0"><head><title>Test</title></head><body>
  <outline text="Technology" llmEnabled="false">
    <outline text="Research">
      <outline type="atom" text="Lab" xmlUrl="https://example.org/atom.xml" htmlUrl="https://example.org/" llmEnabled="false" />
    </outline>
  </outline>
  <outline text="Loose" type="rss" xmlUrl="https://example.com/rss.xml" />
</body></opml>"""


def test_recursive_opml_import_and_export(configured):
    parsed = parse_opml_bytes(SAMPLE)
    assert [group.title for group in parsed] == ["Ungrouped", "Technology"]
    assert parsed[1].llm_enabled is False
    with connect(configured.database_path) as connection:
        groups, feeds = import_groups(connection, parsed)
        assert (groups, feeds) == (3, 2)
        output = serialize_opml(build_tree_from_database(connection))
    reparsed = parse_opml_bytes(output)
    assert sum(len(group.feeds) for group in reparsed) == 1
    assert reparsed[1].groups[0].feeds[0].title == "Lab"
    assert reparsed[1].groups[0].feeds[0].llm_enabled is False


def test_import_merges_without_deleting(configured):
    with connect(configured.database_path) as connection:
        import_groups(connection, parse_opml_bytes(SAMPLE))
        import_groups(connection, parse_opml_bytes(b'<opml version="2.0"><body/></opml>'))
        assert connection.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 2


def test_import_without_llm_attribute_preserves_local_exclusion(configured):
    source = b'<opml version="2.0"><body><outline text="G"><outline type="rss" text="F" xmlUrl="https://example/f" /></outline></body></opml>'
    with connect(configured.database_path) as connection:
        import_groups(connection, parse_opml_bytes(source))
        connection.execute("UPDATE groups SET llm_enabled=0 WHERE title='G'")
        connection.execute("UPDATE feeds SET llm_enabled=0 WHERE xml_url='https://example/f'")
        import_groups(connection, parse_opml_bytes(source))
        assert connection.execute("SELECT llm_enabled FROM groups WHERE title='G'").fetchone()[0] == 0
        assert connection.execute("SELECT llm_enabled FROM feeds WHERE xml_url='https://example/f'").fetchone()[0] == 0


def test_atomic_write_creates_backup(tmp_path):
    path = tmp_path / "feeds.opml"
    atomic_write(path, b"first")
    atomic_write(path, b"second")
    assert path.read_bytes() == b"second"
    assert (tmp_path / "feeds.opml.bak").read_bytes() == b"first"


def test_opml_rejects_unbounded_titles_and_urls():
    long_group = f'<opml><body><outline text="{"g" * 201}" /></body></opml>'.encode()
    with pytest.raises(ValueError, match="group title"):
        parse_opml_bytes(long_group)
    long_feed = f'<opml><body><outline type="rss" text="F" xmlUrl="https://example.test/{"x" * 4096}" /></body></opml>'.encode()
    with pytest.raises(ValueError, match="feed URL"):
        parse_opml_bytes(long_feed)


def test_opml_rejects_pathological_nesting():
    nested = "<outline text='G'>" * 51 + "</outline>" * 51
    with pytest.raises(ValueError, match="nesting"):
        parse_opml_bytes(f"<opml><body>{nested}</body></opml>".encode())
