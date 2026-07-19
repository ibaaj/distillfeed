from __future__ import annotations

import os
import sqlite3
import tempfile
import tomllib
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Config
from .db import connect, connect_readonly, transaction
from .opml import write_database_opml


def resolve_source_database(source: str | Path) -> Path:
    """Resolve an older project directory, TOML config, or SQLite file."""
    path = Path(source).expanduser().resolve()
    if path.is_dir():
        path = path / "config.toml"
    if path.suffix.casefold() == ".toml":
        if not path.is_file():
            raise ValueError(f"Source configuration does not exist: {path}")
        with path.open("rb") as handle:
            values = tomllib.load(handle)
        raw_database = values.get("app", {}).get("database_path", "data/reader.sqlite3")
        database = Path(str(raw_database)).expanduser()
        path = database if database.is_absolute() else (path.parent / database).resolve()
    if not path.is_file():
        raise ValueError(f"Source database does not exist: {path}")
    return path


def _normal_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _group_paths(connection: sqlite3.Connection) -> dict[int, tuple[str, ...]]:
    rows = connection.execute("SELECT id,parent_id,title FROM groups").fetchall()
    by_id = {int(row["id"]): row for row in rows}
    cache: dict[int, tuple[str, ...]] = {}

    def path(identifier: int, trail: frozenset[int] = frozenset()) -> tuple[str, ...]:
        if identifier in cache:
            return cache[identifier]
        if identifier in trail:
            raise ValueError("A cycle exists in the source group hierarchy")
        row = by_id[identifier]
        parent = row["parent_id"]
        prefix = () if parent is None else path(int(parent), trail | {identifier})
        cache[identifier] = prefix + (str(row["title"]).strip().casefold(),)
        return cache[identifier]

    return {identifier: path(identifier) for identifier in by_id}


def _database_backup(config: Config) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = config.database_path.with_name(
        f"{config.database_path.name}.before-ai-settings-{timestamp}.bak"
    )
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    try:
        with connect(config.database_path) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


def migrate_ai_settings(config: Config, source: str | Path, *, apply: bool = False) -> dict[str, Any]:
    """Copy AI inclusion and group scheduling choices from an older DistillFeed database."""
    source_database = resolve_source_database(source)
    if source_database.samefile(config.database_path):
        raise ValueError("Source and destination databases are the same file")

    backup: Path | None = None
    with connect_readonly(source_database) as old, connect(config.database_path) as new:
        for table in ("groups", "feeds"):
            if not _columns(old, table):
                raise ValueError(f"Source database has no {table} table")
        if "llm_enabled" not in _columns(old, "groups") or "llm_enabled" not in _columns(old, "feeds"):
            raise ValueError("Source database predates per-group or per-feed AI inclusion settings")

        old_paths = _group_paths(old)
        new_paths = _group_paths(new)
        old_groups = {int(row["id"]): row for row in old.execute("SELECT * FROM groups")}
        new_groups = {int(row["id"]): row for row in new.execute("SELECT * FROM groups")}
        old_by_path: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for identifier, path in old_paths.items():
            old_by_path[path].append(identifier)

        group_updates: list[tuple[int, int, int, int]] = []
        unmatched_target_groups: list[str] = []
        matched_old_groups: set[int] = set()
        old_group_columns = _columns(old, "groups")
        for identifier, path in new_paths.items():
            candidates = old_by_path.get(path, [])
            if len(candidates) != 1:
                unmatched_target_groups.append(" / ".join(path))
                continue
            source_id = candidates[0]
            matched_old_groups.add(source_id)
            source_row = old_groups[source_id]
            group_updates.append((
                int(bool(source_row["llm_enabled"])),
                int(source_row["summary_interval_hours"]) if "summary_interval_hours" in old_group_columns else 0,
                int(source_row["summary_item_budget"]) if "summary_item_budget" in old_group_columns else 0,
                identifier,
            ))

        old_feed_by_url: dict[str, list[sqlite3.Row]] = defaultdict(list)
        old_feeds = old.execute("SELECT id,title,xml_url,llm_enabled FROM feeds").fetchall()
        for row in old_feeds:
            old_feed_by_url[_normal_url(str(row["xml_url"]))].append(row)
        feed_updates: list[tuple[int, int]] = []
        matched_old_feeds: set[int] = set()
        unmatched_target_feeds: list[dict[str, str]] = []
        ambiguous_urls: list[str] = []
        for row in new.execute("SELECT id,title,xml_url FROM feeds"):
            key = _normal_url(str(row["xml_url"]))
            candidates = old_feed_by_url.get(key, [])
            if len(candidates) == 1:
                source_row = candidates[0]
                matched_old_feeds.add(int(source_row["id"]))
                feed_updates.append((int(bool(source_row["llm_enabled"])), int(row["id"])))
            elif len(candidates) > 1:
                ambiguous_urls.append(str(row["xml_url"]))
            else:
                unmatched_target_feeds.append({"title": str(row["title"]), "url": str(row["xml_url"])})

        changed_groups = sum(
            1 for enabled, interval, budget, identifier in group_updates
            if (
                int(new_groups[identifier]["llm_enabled"]),
                int(new_groups[identifier]["summary_interval_hours"]),
                int(new_groups[identifier]["summary_item_budget"]),
            ) != (enabled, interval, budget)
        )
        new_feeds = {int(row["id"]): row for row in new.execute("SELECT id,llm_enabled FROM feeds")}
        changed_feeds = sum(
            1 for enabled, identifier in feed_updates
            if int(new_feeds[identifier]["llm_enabled"]) != enabled
        )

        if apply:
            backup = _database_backup(config)
            with transaction(new, immediate=True):
                new.executemany(
                    """UPDATE groups SET llm_enabled=?, summary_interval_hours=?,
                       summary_item_budget=? WHERE id=?""",
                    group_updates,
                )
                new.executemany("UPDATE feeds SET llm_enabled=? WHERE id=?", feed_updates)
                write_database_opml(new, config.working_opml_path)

        unmatched_source_feeds = [
            {"title": str(row["title"]), "url": str(row["xml_url"])}
            for row in old_feeds if int(row["id"]) not in matched_old_feeds
        ]
        unmatched_source_groups = [
            " / ".join(old_paths[identifier])
            for identifier in old_paths if identifier not in matched_old_groups
        ]

    return {
        "status": "applied" if apply else "dry-run",
        "source_database": str(source_database),
        "destination_database": str(config.database_path),
        "backup": str(backup) if backup else None,
        "matched_groups": len(group_updates),
        "changed_groups": changed_groups,
        "matched_feeds": len(feed_updates),
        "changed_feeds": changed_feeds,
        "ambiguous_feed_urls": ambiguous_urls,
        "unmatched_target_groups": unmatched_target_groups,
        "unmatched_target_feeds": unmatched_target_feeds,
        "unmatched_source_groups": unmatched_source_groups,
        "unmatched_source_feeds": unmatched_source_feeds,
    }
