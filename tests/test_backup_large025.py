"""Large fixtures are generated temporarily, never shipped in release archives."""
from pathlib import Path
import zipfile
import time
from rss_reader.db import connect
from rss_reader.backup import build_backup,restore_backup
from test_release025 import configured_at,ordinary,item


def test_restore_over_100mb_zip_and_over_200mb_database(tmp_path: Path):
    source=configured_at(tmp_path/'source');target=configured_at(tmp_path/'target')
    with connect(source.database_path) as c:
        group,feed=ordinary(c);item(c,feed,'large-backup','2026-10-24')
        c.execute('CREATE TABLE bulk_fixture(id INTEGER PRIMARY KEY,data BLOB)')
        # >100 MiB incompressible content plus 130 MiB compressible content.
        # This exercises both the upload/archive and expanded-database limits.
        c.executemany('INSERT INTO bulk_fixture(data) VALUES(randomblob(1048576))',[() for _ in range(101)])
        c.execute('INSERT INTO bulk_fixture(data) VALUES(zeroblob(136314880))')
    start=time.monotonic()
    path=tmp_path/'large.zip'
    with build_backup(source) as incoming,path.open('wb') as outgoing:
        while chunk:=incoming.read(1024*1024):outgoing.write(chunk)
    assert path.stat().st_size>100*1024*1024
    with zipfile.ZipFile(path) as z:expanded=z.getinfo('reader.sqlite3').file_size
    assert expanded>200*1024*1024
    safety=restore_backup(target,path)
    with connect(target.database_path) as c:
        assert c.execute('SELECT COUNT(*) FROM bulk_fixture').fetchone()[0]==102
        assert c.execute('SELECT stable_id FROM items').fetchone()[0]=='large-backup'
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert safety.is_file()
    print(f'large restore: zip={path.stat().st_size} bytes; expanded={expanded} bytes; elapsed={time.monotonic()-start:.2f}s')
