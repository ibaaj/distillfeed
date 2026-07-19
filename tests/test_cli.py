import json
from pathlib import Path

from rss_reader import cli
from rss_reader.config import load_config
from rss_reader.db import connect


def test_cli_init_offers_starter_and_explicit_empty_opml(tmp_path):
    examples = tmp_path / "examples"
    examples.mkdir()
    for name in ("starter-subscriptions.opml", "empty-subscriptions.opml"):
        (examples / name).write_bytes((Path("examples") / name).read_bytes())

    starter_config = tmp_path / "starter.toml"
    starter_config.write_text(
        '[app]\ndatabase_path="starter.sqlite3"\nworking_opml_path="starter.opml"\n',
        encoding="utf-8",
    )
    assert cli.main(["--config", str(starter_config), "init"]) == 0
    with connect(tmp_path / "starter.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM groups WHERE title IN ('Programming','Artificial intelligence')"
        ).fetchone()[0] == 2

    empty_config = tmp_path / "empty.toml"
    empty_config.write_text(
        '[app]\ndatabase_path="empty.sqlite3"\nworking_opml_path="empty.opml"\n'
        'opml_source="examples/empty-subscriptions.opml"\n',
        encoding="utf-8",
    )
    assert cli.main(["--config", str(empty_config), "init"]) == 0
    with connect(tmp_path / "empty.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 0


def test_cli_init_doctor_export_import_and_baseline(configured, tmp_path, capsys):
    assert cli.main(["--config", str(configured.path), "init"]) == 0
    assert configured.working_opml_path.is_file()
    capsys.readouterr()
    assert cli.main(["--config", str(configured.path), "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"] == str(configured.database_path)
    assert report["checks"] == {
        "sqlite": "ok", "foreign_key_violations": 0,
        "opml_matches_database": True, "opml_error": None,
    }

    exported = tmp_path / "exported.opml"
    assert cli.main(["--config", str(configured.path), "export-opml", str(exported)]) == 0
    assert exported.is_file()

    source = tmp_path / "source.opml"
    source.write_text(
        '<opml><body><outline text="News"><outline type="rss" text="Feed" '
        'xmlUrl="https://example.test/cli" /></outline></body></opml>',
        encoding="utf-8",
    )
    assert cli.main(["--config", str(configured.path), "import-opml", str(source)]) == 0
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM feeds WHERE xml_url='https://example.test/cli'"
        ).fetchone()[0] == 1
    assert cli.main([
        "--config", str(configured.path), "baseline", "--max-items", "1",
        "--max-per-feed", "1", "--max-age-days", "30", "--dry-run",
    ]) == 0


def test_cli_dispatches_refresh_summary_migration_and_server_options(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "run_refresh", lambda config, **kwargs: calls.append(("refresh", kwargs)) or {"status": "ok"}
    )
    monkeypatch.setattr(
        cli, "run_update_summaries",
        lambda config, **kwargs: calls.append(("summary", kwargs)) or {"status": "ok"},
    )
    monkeypatch.setattr(
        cli, "migrate_ai_settings",
        lambda config, source, apply=False: calls.append(("migration", {"source": source, "apply": apply})) or {"status": "ok"},
    )

    class App:
        def run(self, **kwargs):
            calls.append(("serve", kwargs))

    monkeypatch.setattr(cli, "create_app", lambda path: App())
    prefix = ["--config", str(configured.path)]
    assert cli.main([*prefix, "refresh", "--feed", "7", "--force", "--automatic"]) == 0
    assert cli.main([*prefix, "summarize", "--automatic"]) == 0
    assert cli.main([*prefix, "migrate-ai-settings", "/old/project", "--apply"]) == 0
    assert cli.main([*prefix, "serve", "--host", "127.0.0.2", "--port", "8099"]) == 0
    assert calls == [
        ("refresh", {"feed_id": 7, "force": True, "automatic": True}),
        ("summary", {"automatic": True}),
        ("migration", {"source": "/old/project", "apply": True}),
        ("serve", {"host": "127.0.0.2", "port": 8099, "debug": False, "use_reloader": False}),
    ]


def test_cli_doctor_fails_on_opml_database_drift(configured, capsys):
    assert cli.main(["--config", str(configured.path), "init"]) == 0
    capsys.readouterr()
    configured.working_opml_path.write_text("<opml><body/></opml>", encoding="utf-8")
    with connect(configured.database_path) as connection:
        connection.execute(
            "INSERT INTO groups(title,position,created_at) VALUES('Missing from OPML',0,'now')"
        )
    assert cli.main(["--config", str(configured.path), "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["opml_matches_database"] is False


def test_language_profile_updates_summary_weather_and_database_mirror(configured, capsys):
    assert cli.main(["--config", str(configured.path), "language-profile", "French"]) == 0
    assert "Language profile: French" in capsys.readouterr().out
    updated = load_config(configured.path)
    assert updated.get("app", "summary_language") == "French"
    assert updated.get("weather", "language") == "French"
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT value FROM settings WHERE key='summary_language'"
        ).fetchone()["value"] == "French"

    assert cli.main(["--config", str(configured.path), "language-profile", "English"]) == 0
    updated = load_config(configured.path)
    assert updated.get("app", "summary_language") == "English"
    assert updated.get("weather", "language") == "English"
