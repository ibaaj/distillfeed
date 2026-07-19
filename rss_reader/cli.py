from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .config import (
    DEFAULTS,
    Config,
    dump_toml,
    ensure_runtime_directories,
    load_config,
    save_config,
    validate_config,
)
from .baseline import baseline_backlog
from .db import connect, initialize, transaction
from .migration import migrate_ai_settings
from .opml import build_tree_from_database, parse_opml_bytes, write_database_opml
from .plugins import (
    available_plugin_names,
    enabled_plugin_names,
    initialize_plugins,
    set_plugin_runtime_state,
)
from .service import import_opml_source, run_refresh, run_update_summaries
from .web import create_app


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="DistillFeed — your feeds, distilled",
    )
    result.add_argument("--config", default=os.environ.get("RSSREADER_CONFIG", "config.toml"))
    result.add_argument("--verbose", action="store_true", help="Enable debug logging")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize the database and working OPML")
    imp = sub.add_parser("import-opml", help="Merge a local or remote OPML source")
    imp.add_argument("source", nargs="?")
    ref = sub.add_parser("refresh", help="Refresh RSS and Atom feeds")
    ref.add_argument("--feed", type=int)
    ref.add_argument("--force", action="store_true")
    ref.add_argument(
        "--automatic", action="store_true",
        help="Apply schedule/cooldown behavior and deliver automatic ntfy device alerts",
    )
    summ = sub.add_parser("summarize", help="Summarize previously unsummarized items")
    summ.add_argument("--automatic", action="store_true", help="Respect the automatic cooldown")
    baseline = sub.add_parser("baseline", help="Exclude historical backlog from LLM processing without deleting it")
    baseline.add_argument("--max-items", type=int)
    baseline.add_argument("--max-per-feed", type=int)
    baseline.add_argument("--max-age-days", type=int)
    baseline.add_argument("--dry-run", action="store_true")
    baseline.add_argument("--restore", action="store_true", help="Make unsummarized baseline items eligible again")
    exp = sub.add_parser("export-opml", help="Write an OPML export")
    exp.add_argument("path", nargs="?")
    migration = sub.add_parser(
        "migrate-ai-settings",
        help="Copy feed/group AI choices from an older DistillFeed project",
    )
    migration.add_argument("source", help="Older project directory, config.toml, or reader.sqlite3")
    migration.add_argument(
        "--apply", action="store_true",
        help="Apply the migration after creating a database backup (default: report only)",
    )
    language_profile = sub.add_parser(
        "language-profile", help="Set both AI summary and weather language"
    )
    language_profile.add_argument("language", choices=("English", "French"))
    plugins = sub.add_parser("plugin", help="List, enable, or disable installed source plugins")
    plugin_commands = plugins.add_subparsers(dest="plugin_command", required=True)
    plugin_commands.add_parser("list", help="List installed and enabled plugins")
    plugin_enable = plugin_commands.add_parser("enable", help="Enable an installed plugin")
    plugin_enable.add_argument("name")
    plugin_disable = plugin_commands.add_parser("disable", help="Disable a plugin")
    plugin_disable.add_argument("name")
    sub.add_parser("doctor", help="Inspect configuration and current state")
    serve = sub.add_parser("serve", help="Run the development web server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        configure_logging("DEBUG" if args.verbose else str(config.get("app", "log_level")))
        ensure_runtime_directories(config)
        initialize(config.database_path)
        # Plugin management must remain available even when an enabled plugin
        # was uninstalled or its private configuration is temporarily broken.
        if args.command != "plugin":
            with connect(config.database_path) as connection:
                initialize_plugins(connection, config)
        if args.command == "init":
            if not Path(args.config).exists():
                example = Path(__file__).resolve().parent.parent / "config.example.toml"
                if example.exists():
                    shutil.copy2(example, args.config)
                    print(f"Created {Path(args.config).resolve()}")
                else:
                    Path(args.config).write_text(dump_toml(copy.deepcopy(DEFAULTS)), encoding="utf-8")
                    print(f"Created {Path(args.config).resolve()}")
                config = load_config(args.config)
                ensure_runtime_directories(config)
                initialize(config.database_path)
                with connect(config.database_path) as connection:
                    initialize_plugins(connection, config)
            with connect(config.database_path) as connection:
                source = str(config.get("app", "opml_source", "")).strip()
                if source:
                    groups, feeds = import_opml_source(connection, config, source)
                    print(f"Imported {groups} groups and {feeds} feeds")
                else:
                    starter = config.path.parent / "examples" / "starter-subscriptions.opml"
                    if not starter.is_file():
                        starter = Path(__file__).resolve().parent / "resources" / "starter-subscriptions.opml"
                    has_subscriptions = connection.execute(
                        "SELECT 1 FROM groups LIMIT 1"
                    ).fetchone()
                    if (
                        not has_subscriptions
                        and bool(config.get("app", "starter_subscriptions", True))
                        and starter.is_file()
                    ):
                        groups, feeds = import_opml_source(connection, config, str(starter))
                        print(f"Imported {groups} starter groups and {feeds} feeds")
                    else:
                        write_database_opml(connection, config.working_opml_path)
            print(f"Database: {config.database_path}")
            print(f"Working OPML: {config.working_opml_path}")
        elif args.command == "import-opml":
            source = args.source or str(config.get("app", "opml_source", "")).strip()
            if not source:
                raise ValueError("Provide SOURCE or set app.opml_source")
            with connect(config.database_path) as connection:
                groups, feeds = import_opml_source(connection, config, source)
            print(f"Merged {groups} groups and {feeds} feeds; no missing subscriptions were deleted")
        elif args.command == "refresh":
            print(json.dumps(run_refresh(
                config, feed_id=args.feed, force=args.force, automatic=args.automatic,
            ), indent=2))
        elif args.command == "summarize":
            print(json.dumps(run_update_summaries(config, automatic=args.automatic), indent=2))
        elif args.command == "baseline":
            with connect(config.database_path) as connection:
                if args.restore:
                    count = connection.execute(
                        """SELECT COUNT(*) FROM items i WHERE i.summary_eligible=0 AND NOT EXISTS (
                           SELECT 1 FROM summary_items si JOIN summaries s ON s.id=si.summary_id
                           JOIN llm_runs lr ON lr.id=s.llm_run_id
                           WHERE si.item_id=i.id AND lr.status='success')"""
                    ).fetchone()[0]
                    if not args.dry_run:
                        connection.execute(
                            """UPDATE items SET summary_eligible=1 WHERE summary_eligible=0 AND NOT EXISTS (
                               SELECT 1 FROM summary_items si JOIN summaries s ON s.id=si.summary_id
                               JOIN llm_runs lr ON lr.id=s.llm_run_id
                               WHERE si.item_id=items.id AND lr.status='success')"""
                        )
                    result = {"restored": count, "dry_run": args.dry_run}
                else:
                    result = baseline_backlog(
                        connection, config, max_items=args.max_items, max_per_feed=args.max_per_feed,
                        max_age_days=args.max_age_days, dry_run=args.dry_run,
                    )
            print(json.dumps(result, indent=2))
        elif args.command == "export-opml":
            path = Path(args.path).resolve() if args.path else config.working_opml_path
            with connect(config.database_path) as connection:
                write_database_opml(connection, path)
            print(path)
        elif args.command == "migrate-ai-settings":
            print(json.dumps(migrate_ai_settings(config, args.source, apply=args.apply), indent=2))
        elif args.command == "language-profile":
            candidate_data = copy.deepcopy(config.data)
            candidate_data["app"]["summary_language"] = args.language
            candidate_data["weather"]["language"] = args.language
            validate_config(candidate_data)
            candidate = Config(config.path, candidate_data)
            with connect(config.database_path) as connection, transaction(connection, immediate=True):
                connection.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('summary_language',?)",
                    (args.language,),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('interest_profile',?)",
                    (str(candidate.get("app", "interest_profile"))[:2000],),
                )
                save_config(candidate)
            print(f"Language profile: {args.language}")
        elif args.command == "plugin":
            installed = available_plugin_names()
            enabled = list(enabled_plugin_names(config))
            if args.plugin_command == "list":
                print(json.dumps({"installed": installed, "enabled": enabled}, indent=2))
            else:
                name = str(args.name).strip()
                if not name or any(character in name for character in "\r\n,"):
                    raise ValueError("Plugin name is invalid")
                if args.plugin_command == "enable":
                    if name not in installed:
                        raise ValueError(f"Plugin is not installed: {name}")
                    if name not in enabled:
                        enabled.append(name)
                elif name in enabled:
                    enabled.remove(name)
                candidate_data = copy.deepcopy(config.data)
                if name == "arxiv_digest":
                    candidate_data["plugins"]["arxiv_digest_enabled"] = (
                        args.plugin_command == "enable"
                    )
                    enabled = [value for value in enabled if value != name]
                candidate_data["plugins"]["enabled"] = ",".join(enabled)
                validate_config(candidate_data)
                candidate = Config(config.path, candidate_data)
                with connect(config.database_path) as connection, transaction(connection, immediate=True):
                    set_plugin_runtime_state(
                        connection, candidate, name, args.plugin_command == "enable"
                    )
                    save_config(candidate)
                state = "enabled" if args.plugin_command == "enable" else "disabled"
                print(f"Plugin {name} {state}")
        elif args.command == "doctor":
            with connect(config.database_path) as connection:
                sqlite_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
                try:
                    opml_matches = (
                        config.working_opml_path.is_file()
                        and parse_opml_bytes(config.working_opml_path.read_bytes())
                        == build_tree_from_database(connection)
                    )
                    opml_error = None
                except Exception as exc:
                    opml_matches = False
                    opml_error = str(exc)
                counts = {
                    "groups": connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0],
                    "feeds": connection.execute("SELECT COUNT(*) FROM feeds").fetchone()[0],
                    "items": connection.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                    "unread": connection.execute("SELECT COUNT(*) FROM items WHERE is_read=0").fetchone()[0],
                    "feed_errors": connection.execute("SELECT COUNT(*) FROM feeds WHERE last_error IS NOT NULL").fetchone()[0],
                    "unsummarized": connection.execute(
                        """SELECT COUNT(*) FROM items i WHERE NOT EXISTS (
                           SELECT 1 FROM summary_items si JOIN summaries s ON s.id=si.summary_id
                           JOIN llm_runs lr ON lr.id=s.llm_run_id WHERE si.item_id=i.id AND lr.status='success')"""
                    ).fetchone()[0],
                    "llm_eligible": connection.execute(
                        """SELECT COUNT(*) FROM items i WHERE i.summary_eligible=1 AND NOT EXISTS (
                           SELECT 1 FROM summary_items si JOIN summaries s ON s.id=si.summary_id
                           JOIN llm_runs lr ON lr.id=s.llm_run_id WHERE si.item_id=i.id AND lr.status='success')"""
                    ).fetchone()[0],
                    "historical_baseline": connection.execute(
                        "SELECT COUNT(*) FROM items WHERE summary_eligible=0"
                    ).fetchone()[0],
                }
            report = {
                "config": str(config.path), "database": str(config.database_path),
                "working_opml": str(config.working_opml_path), "opml_backup": str(config.working_opml_path) + ".bak",
                "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")), "counts": counts,
                "checks": {
                    "sqlite": sqlite_check,
                    "foreign_key_violations": foreign_key_violations,
                    "opml_matches_database": opml_matches,
                    "opml_error": opml_error,
                },
            }
            print(json.dumps(report, indent=2))
            if sqlite_check != "ok" or foreign_key_violations or not opml_matches:
                return 1
        elif args.command == "serve":
            app = create_app(str(config.path))
            app.run(
                host=args.host or str(config.get("app", "host")), port=args.port or int(config.get("app", "port")),
                debug=bool(config.get("app", "debug")), use_reloader=False,
            )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error("Command failed: %s", exc)
        logging.getLogger(__name__).debug("Command traceback", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
