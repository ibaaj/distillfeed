from pathlib import Path

from rss_reader.config import load_config
from rss_reader.db import connect, initialize, utcnow
from rss_reader.migration import migrate_ai_settings, resolve_source_database


def _project(tmp_path: Path, name: str):
    root = tmp_path / name
    root.mkdir()
    config_path = root / "config.toml"
    config_path.write_text(
        """[app]
database_path = "data/reader.sqlite3"
working_opml_path = "data/subscriptions.opml"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    config.database_path.parent.mkdir(parents=True)
    initialize(config.database_path)
    return root, config


def _seed(config, *, enabled: int, interval: int, budget: int, query: str) -> tuple[int, int]:
    with connect(config.database_path) as connection:
        parent = connection.execute(
            "INSERT INTO groups(title,position,llm_enabled,created_at) VALUES('News',0,1,?)",
            (utcnow(),),
        ).lastrowid
        group = connection.execute(
            """INSERT INTO groups(parent_id,title,position,llm_enabled,summary_interval_hours,
               summary_item_budget,created_at) VALUES(?,'International',0,?,?,?,?)""",
            (parent, enabled, interval, budget, utcnow()),
        ).lastrowid
        feed = connection.execute(
            """INSERT INTO feeds(group_id,title,xml_url,llm_enabled,created_at)
               VALUES(?, 'Orient XXI', ?, ?, ?)""",
            (group, f"https://orientxxi.info/?{query}", enabled, utcnow()),
        ).lastrowid
    return int(group), int(feed)


def test_migrate_ai_settings_is_report_only_then_atomic_apply(tmp_path):
    old_root, old = _project(tmp_path, "opml-llm-reader")
    _, new = _project(tmp_path, "distillfeed-perso")
    _seed(old, enabled=0, interval=12, budget=7, query="page=backend&lang=fr")
    new_group, new_feed = _seed(
        new, enabled=1, interval=0, budget=0, query="lang=fr&page=backend"
    )

    assert resolve_source_database(old_root) == old.database_path
    report = migrate_ai_settings(new, old_root)
    assert report["status"] == "dry-run"
    assert report["matched_groups"] == 2
    assert report["matched_feeds"] == 1
    assert report["changed_groups"] == 1
    assert report["changed_feeds"] == 1
    assert report["backup"] is None
    with connect(new.database_path) as connection:
        assert connection.execute("SELECT llm_enabled FROM feeds WHERE id=?", (new_feed,)).fetchone()[0] == 1

    applied = migrate_ai_settings(new, old.path, apply=True)
    assert applied["status"] == "applied"
    assert Path(applied["backup"]).is_file()
    with connect(new.database_path) as connection:
        group = connection.execute("SELECT * FROM groups WHERE id=?", (new_group,)).fetchone()
        feed = connection.execute("SELECT * FROM feeds WHERE id=?", (new_feed,)).fetchone()
    assert (group["llm_enabled"], group["summary_interval_hours"], group["summary_item_budget"]) == (0, 12, 7)
    assert feed["llm_enabled"] == 0
    assert 'llmEnabled="false"' in new.working_opml_path.read_text(encoding="utf-8")


def test_migration_reports_unmatched_sources_and_targets(tmp_path):
    _, old = _project(tmp_path, "old")
    _, new = _project(tmp_path, "new")
    _seed(old, enabled=0, interval=0, budget=0, query="page=backend&lang=fr")
    with connect(new.database_path) as connection:
        group = connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Different',0,?)", (utcnow(),)
        ).lastrowid
        connection.execute(
            "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
            (group, "Another", "https://example.test/feed", utcnow()),
        )
    report = migrate_ai_settings(new, old.database_path)
    assert report["matched_groups"] == 0
    assert report["matched_feeds"] == 0
    assert report["unmatched_source_feeds"][0]["title"] == "Orient XXI"
    assert report["unmatched_target_feeds"][0]["title"] == "Another"
