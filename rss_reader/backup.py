from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .config import Config
from .config import dump_toml
from .db import connect, initialize
from .opml import write_database_opml


MANIFEST_VERSION = 1
MAX_BACKUP_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_BACKUP_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
BACKUP_MEMBER_LIMITS = {
    "manifest.json": 1024 * 1024,
    "reader.sqlite3": MAX_BACKUP_DATABASE_BYTES,
    "subscriptions.opml": 32 * 1024 * 1024,
    "config.toml.reference": 2 * 1024 * 1024,
}



def build_backup(config: Config) -> BinaryIO:
    output = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    with tempfile.TemporaryDirectory(prefix="distillfeed-backup-") as directory:
        snapshot = Path(directory) / "reader.sqlite3"
        source = connect(config.database_path)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        manifest = {
            "format": "distillfeed-backup",
            "version": MANIFEST_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.write(snapshot, "reader.sqlite3")
            if config.working_opml_path.exists():
                archive.write(config.working_opml_path, "subscriptions.opml")
            archive.writestr("config.toml.reference", dump_toml(config.data))
    output.seek(0)
    return output


def save_safety_backup(config: Config) -> Path:
    directory = config.path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"before-restore-{timestamp}.zip"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            backup = build_backup(config)
            try:
                while chunk := backup.read(1024 * 1024):
                    handle.write(chunk)
            finally:
                backup.close()
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
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
    return path


def restore_backup(config: Config, content: bytes | BinaryIO | Path) -> Path:
    """Validate and stream a backup before replacing live data.

    Accept seekable upload streams and paths, not just bytes. The compressed
    archive and expanded database have separate bounds. Nothing is extracted
    by an archive-controlled path. Existing config and provider secrets stay local.
    """
    owned = isinstance(content, (bytes, Path))
    source_file = io.BytesIO(content) if isinstance(content, bytes) else (
        content.open("rb") if isinstance(content, Path) else content
    )
    try:
        source_file.seek(0, os.SEEK_END)
        size = source_file.tell()
        source_file.seek(0)
        if size > MAX_BACKUP_ARCHIVE_BYTES:
            raise ValueError("Backup ZIP exceeds the 1 GiB upload limit; use a smaller backup")
        with tempfile.TemporaryDirectory(prefix="distillfeed-restore-") as directory:
            root = Path(directory)
            database = root / "reader.sqlite3"
            try:
                with zipfile.ZipFile(source_file) as archive:
                    infos = archive.infolist()
                    names = {entry.filename for entry in infos}
                    if len(names) != len(infos):
                        raise ValueError("Backup contains duplicate member names")
                    if not {"manifest.json", "reader.sqlite3"} <= names:
                        raise ValueError("This is not a DistillFeed backup")
                    if names - BACKUP_MEMBER_LIMITS.keys():
                        raise ValueError("Backup contains unexpected members")
                    for entry in infos:
                        kind = stat.S_IFMT(entry.external_attr >> 16)
                        if entry.is_dir() or entry.flag_bits & 1 or kind not in {0, stat.S_IFREG}:
                            raise ValueError("Backup contains a link, directory or encrypted member")
                        if entry.file_size > BACKUP_MEMBER_LIMITS[entry.filename]:
                            raise ValueError(f"Expanded backup member {entry.filename} exceeds its limit")
                    manifest = json.loads(archive.read("manifest.json"))
                    if (not isinstance(manifest, dict)
                            or manifest.get("format") != "distillfeed-backup"
                            or manifest.get("version") != MANIFEST_VERSION):
                        raise ValueError("Unsupported DistillFeed backup format")
                    # CRC is checked while streaming. Do not allocate the entire
                    # expanded database in RAM (a 92 MB ZIP may expand beyond 200 MB).
                    with archive.open("reader.sqlite3") as incoming, database.open("xb") as outgoing:
                        copied = 0
                        while chunk := incoming.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > MAX_BACKUP_DATABASE_BYTES:
                                raise ValueError("Expanded database exceeds 4 GiB")
                            outgoing.write(chunk)
                    for name in names - {"reader.sqlite3", "manifest.json"}:
                        with archive.open(name) as member:
                            while member.read(1024 * 1024):
                                pass  # verify CRC, but never install reference configuration
            except (zipfile.BadZipFile, json.JSONDecodeError, EOFError, RuntimeError) as exc:
                raise ValueError("Invalid or damaged backup archive") from exc
            try:
                with sqlite3.connect(database) as check:
                    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Backup database failed its integrity check")
                    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"items", "feeds", "groups"} <= tables:
                        raise ValueError("Backup database does not contain DistillFeed tables")
                    if check.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise ValueError("Backup database has broken foreign-key relationships")
                # Run schema migration against the staged database BEFORE replacing data.
                initialize(database)
                with connect(database) as staged:
                    from .plugins import initialize_plugins
                    initialize_plugins(staged, config)
            except sqlite3.DatabaseError as exc:
                raise ValueError("Backup database is invalid or incompatible") from exc
            safety = save_safety_backup(config)
            rollback = root / "rollback.sqlite3"
            with sqlite3.connect(config.database_path) as live, sqlite3.connect(rollback) as old:
                live.backup(old)
            opml_paths = (config.working_opml_path, Path(str(config.working_opml_path) + ".bak"))
            opml_before = {path: path.read_bytes() if path.exists() else None for path in opml_paths}
            # Preserve the active maintenance lease in the staged DB. Restored
            # job locks belong to another process and must not survive a restore.
            with sqlite3.connect(config.database_path) as live:
                locks = live.execute("SELECT name,owner,acquired_at,expires_at,cancel_requested FROM job_locks WHERE name='maintenance'").fetchall()
            with sqlite3.connect(database) as staged:
                staged.execute('DELETE FROM job_locks')
                staged.executemany('INSERT INTO job_locks(name,owner,acquired_at,expires_at,cancel_requested) VALUES(?,?,?,?,?)',locks)
            try:
                with sqlite3.connect(database) as source, sqlite3.connect(config.database_path) as target:
                    source.backup(target)
                with connect(config.database_path) as connection:
                    write_database_opml(connection, config.working_opml_path)
            except Exception:
                with sqlite3.connect(rollback) as old, sqlite3.connect(config.database_path) as live:
                    old.backup(live)
                for path, data in opml_before.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(data)
                raise
            return safety
    finally:
        if owned:
            source_file.close()
