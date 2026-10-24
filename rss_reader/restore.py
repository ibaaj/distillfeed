"""Offline restore entry point, including environments with proxy upload limits.

Usage: python -m rss_reader.restore --config config.toml --backup file.zip
Stop the application first. This preserves the local config and provider keys.
"""
from __future__ import annotations
import argparse
import secrets
from pathlib import Path
from .backup import restore_backup
from .config import load_config
from .db import connect, initialize, acquire_lock, release_lock


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--backup',required=True,type=Path)
    args=parser.parse_args()
    if not args.config.is_file() or not args.backup.is_file():
        parser.error('Both configuration and backup must be existing files')
    config=load_config(args.config)
    initialize(config.database_path)
    owner=secrets.token_urlsafe(24)
    with connect(config.database_path) as connection:
        if not acquire_lock(connection,'maintenance',owner,ttl_minutes=120,exclusive=True):
            parser.error('Stop refresh/summary jobs before restoring; the database is busy')
        try:
            safety=restore_backup(config,args.backup)
        finally:
            release_lock(connection,'maintenance',owner)
    print(f'Restore completed. Previous database backup: {safety}')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
