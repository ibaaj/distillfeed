from __future__ import annotations

import io
import json
import os
import sqlite3
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


def restore_backup(config: Config, content: bytes) -> Path:
    if len(content) > 200 * 1024 * 1024:
        raise ValueError("Backup is larger than 200 MiB")
    with tempfile.TemporaryDirectory(prefix="distillfeed-restore-") as directory:
        root = Path(directory)
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                if len(names) != len(archive.infolist()):
                    raise ValueError("Backup contains duplicate member names")
                if "manifest.json" not in names or "reader.sqlite3" not in names:
                    raise ValueError("This is not a DistillFeed backup")
                for name, maximum in {
                    "manifest.json": 1024 * 1024,
                    "reader.sqlite3": 200 * 1024 * 1024,
                    "subscriptions.opml": 20 * 1024 * 1024,
                    "config.toml.reference": 2 * 1024 * 1024,
                }.items():
                    if name in names and archive.getinfo(name).file_size > maximum:
                        raise ValueError(f"Backup member {name} is larger than the allowed limit")
                manifest = json.loads(archive.read("manifest.json"))
                if not isinstance(manifest, dict):
                    raise ValueError("Backup manifest must be a JSON object")
                if manifest.get("format") != "distillfeed-backup" or int(manifest.get("version", 0)) != MANIFEST_VERSION:
                    raise ValueError("Unsupported DistillFeed backup format")
                database = root / "reader.sqlite3"
                database.write_bytes(archive.read("reader.sqlite3"))
                try:
                    check = sqlite3.connect(database)
                    try:
                        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Backup database failed its integrity check")
                        if not check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone():
                            raise ValueError("Backup database does not contain DistillFeed tables")
                        if check.execute("PRAGMA foreign_key_check").fetchone() is not None:
                            raise ValueError("Backup database has broken foreign-key relationships")
                    finally:
                        check.close()
                except sqlite3.DatabaseError as exc:
                    raise ValueError("Backup database is invalid or damaged") from exc
                safety = save_safety_backup(config)
                source = sqlite3.connect(database)
                target = sqlite3.connect(config.database_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
        except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ValueError("Invalid or damaged backup archive") from exc
    initialize(config.database_path)
    with connect(config.database_path) as connection:
        connection.execute("DELETE FROM job_locks")
        write_database_opml(connection, config.working_opml_path)
    return safety
