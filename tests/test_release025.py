from __future__ import annotations
import io
import json
import os
import sqlite3
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest

from rss_reader.config import load_config
from rss_reader.db import connect, initialize, transaction, acquire_lock
from rss_reader.review import (parse_review_filters,resolve_review_scope,list_review_days,list_review_day_items,review_item_details)
from rss_reader.interests import interest_map, scope_interests, validate_interests
from rss_reader.ai_policy import build_plan, selected_payload_rows
from rss_reader.backup import build_backup, restore_backup
from rss_reader.media import youtube_metadata
from rss_reader.feed_safety import check_provider_response, ProviderChallenge
from rss_reader.opml import build_tree_from_database,serialize_opml,parse_opml_bytes,import_groups


def configured_at(path: Path):
    path.mkdir(parents=True,exist_ok=True)
    file=path/'config.toml'
    file.write_text('[app]\ndatabase_path="reader.sqlite3"\nworking_opml_path="subscriptions.opml"\nauto_refresh_on_load=false\n[plugins]\narxiv_digest_enabled=false\n')
    config=load_config(file); initialize(config.database_path); return config


def ordinary(connection):
    group=int(connection.execute("INSERT INTO groups(title,created_at) VALUES('News','2026-10-24')").lastrowid)
    feed=int(connection.execute("INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,'Feed','https://example.test/feed','2026-10-24')",(group,)).lastrowid)
    return group,feed


def item(connection,feed,identifier,day,*,read=False,abstract='source text'):
    return int(connection.execute('''INSERT INTO items(feed_id,stable_id,title,url,published_at,discovered_at,description_text,is_read)
        VALUES(?,?,?,?,?,?,?,?)''',(feed,str(identifier),f'Item {identifier}',f'https://example.test/{identifier}',day+'T12:00:00Z',day+'T12:00:00Z',abstract,int(read))).lastrowid)


def test_windows_keep_read_items_and_cross_month_year(tmp_path):
    config=configured_at(tmp_path)
    with connect(config.database_path) as c:
        group,feed=ordinary(c)
        for n,day in enumerate(['2025-12-28','2025-12-29','2025-12-31','2026-01-01','2026-01-02']): item(c,feed,n,day,read=n%2==0)
        scope=resolve_review_scope(c,group_id=group)
        for preset in ['current-week','last-5-days']:
            f=parse_review_filters({'preset':preset},today='2026-01-02')
            assert f['read']=='all' and f['from']=='2025-12-29' and f['to']=='2026-01-02'
            before=list_review_days(c,scope,f,minimum_relevance=70)
            c.execute('UPDATE items SET is_read=1')
            after=list_review_days(c,scope,f,minimum_relevance=70)
            assert before['counts']['matching']==after['counts']['matching']==4
        old=parse_review_filters({'preset':'catch-up','read':'unread','from':'1990-01-01'},today='2026-01-02')
        assert old['preset']=='last-5-days' and old['read']=='all' and old['from']=='2025-12-29'


def test_32000_items_bounded_counts_days_cards_and_detail(tmp_path):
    config=configured_at(tmp_path)
    with connect(config.database_path) as c, transaction(c,immediate=True):
        group,feed=ordinary(c)
        base=datetime(2026,10,24,tzinfo=UTC)
        rows=[]
        for n in range(32000):
            stamp=(base-timedelta(days=n//200)).isoformat()
            rows.append((feed,str(n),f'Paper {n}','https://example.test/article',stamp,stamp,'Long source '+('x'*2000),n%2))
        c.executemany('INSERT INTO items(feed_id,stable_id,title,url,published_at,discovered_at,description_text,is_read) VALUES(?,?,?,?,?,?,?,?)',rows)
    with connect(config.database_path) as c:
        scope=resolve_review_scope(c,group_id=group);f=parse_review_filters({'preset':'everything','sort':'date'})
        start=time.monotonic(); first=list_review_days(c,scope,f,minimum_relevance=70)
        assert first['counts']['total']==32000 and len(first['days'])==60 and first['has_more_days']
        page=list_review_day_items(c,scope,'2026-10-24',f,minimum_relevance=70)
        assert len(page['items'])==25 and 'description_text' not in page['items'][0]
        assert len(json.dumps(page))<40000
        found=[]; before=''
        while True:
            result=list_review_days(c,scope,f,minimum_relevance=70,before=before)
            found.extend(d['day'] for d in result['days'])
            if not result['has_more_days']: break
            before=result['next_day_cursor']
        assert len(found)==len(set(found))==160
        assert sum(d['total'] for d in first['days'])==12000
        detail=review_item_details(c,scope,page['items'][0]['id'],minimum_relevance=70)
        assert len(detail['source_html'])>2000
        print(f'32000-item day/count/page traversal seconds={time.monotonic()-start:.3f}; initial_cards=25; day_page=60')


def test_future_source_date_is_preserved_but_not_first(tmp_path):
    config=configured_at(tmp_path)
    with connect(config.database_path) as c:
        group,feed=ordinary(c); bad=item(c,feed,1,'2026-10-24')
        c.execute("UPDATE items SET published_at='2099-01-01T00:00:00Z' WHERE id=?",(bad,))
        scope=resolve_review_scope(c,group_id=group);f=parse_review_filters({'preset':'everything'})
        days=list_review_days(c,scope,f,minimum_relevance=70)
        assert days['days'][0]['day']=='2026-10-24'
        card=list_review_day_items(c,scope,'2026-10-24',f,minimum_relevance=70)['items'][0]
        assert card['future_date_warning'] and card['source_published_at'].startswith('2099')
        assert c.execute('SELECT published_at FROM items WHERE id=?',(bad,)).fetchone()[0].startswith('2099')


def test_interests_inherit_snapshot_and_portable_opml(tmp_path):
    config=configured_at(tmp_path)
    with connect(config.database_path) as c:
        group,feed=ordinary(c)
        c.execute('UPDATE groups SET interest_profile=? WHERE id=?',('Robotics & causal models',group))
        assert interest_map(c,'global')[feed]=='Robotics & causal models'
        c.execute('UPDATE feeds SET interest_profile=? WHERE id=?',('Videos about logic',feed))
        assert interest_map(c,'global')[feed]=='Videos about logic'
        identifier=item(c,feed,'new',datetime.now(UTC).date().isoformat())
        plan=build_plan(c,config,group_id=group)
        snapshot=plan['policy']
        assert snapshot['feed_interests'][str(feed)]=='Videos about logic'
        c.execute("UPDATE feeds SET interest_profile='Changed later' WHERE id=?",(feed,))
        rows=selected_payload_rows(c,[identifier],snapshot)
        assert rows[0]['interests']=='Videos about logic'
        xml=serialize_opml(build_tree_from_database(c))
        assert b'distillfeedInterests' in xml and b'Robotics &amp; causal' in xml
        imported=parse_opml_bytes(xml)
        c.execute("UPDATE feeds SET interest_profile='' WHERE id=?",(feed,))
        import_groups(c,imported)
        assert interest_map(c,'global')[feed]=='Changed later'
        with pytest.raises(ValueError): validate_interests('x'*8001)
        with pytest.raises(ValueError): validate_interests(None)


@pytest.mark.parametrize('url,short',[
    ('https://www.youtube.com/shorts/NmiD1YdbrAA',True),
    ('https://youtu.be/NmiD1YdbrAA',False),
    ('https://www.youtube.com/watch?v=NmiD1YdbrAA',False),
])
def test_media_explicit_urls(url,short):
    assert youtube_metadata(url)=={'youtube_id':'NmiD1YdbrAA','is_short':short}


@pytest.mark.parametrize('url',['javascript:alert(1)','https://evil.test/shorts/NmiD1YdbrAA','https://www.youtube.com.evil.test/shorts/NmiD1YdbrAA','https://youtube.com/shorts/bad','https://user@youtube.com/shorts/NmiD1YdbrAA'])
def test_media_rejects_noncanonical(url): assert youtube_metadata(url)=={}


def test_provider_challenge_is_passive_and_long_backoff():
    with pytest.raises(ProviderChallenge) as caught:
        check_provider_response(b'<!doctype html><title>Making sure you are not a bot</title>Anubis',200,{'Content-Type':'text/html','Retry-After':'86400'})
    assert caught.value.status_code==200
    assert caught.value.retry_at>datetime.now(UTC)+timedelta(hours=23)
    check_provider_response(b'<?xml version="1.0"?><rss><channel/></rss>',200,{'Content-Type':'application/rss+xml'})


def test_restore_stream_preserves_config_and_maintenance_and_rolls_back(tmp_path,monkeypatch):
    source=configured_at(tmp_path/'source'); target=configured_at(tmp_path/'target')
    with connect(source.database_path) as c: group,feed=ordinary(c);item(c,feed,'restored','2026-10-24')
    with connect(target.database_path) as c:
        group,feed=ordinary(c); item(c,feed,'old','2026-10-23')
        assert acquire_lock(c,'maintenance','test-owner',ttl_minutes=120,exclusive=True)
    original_config=target.path.read_bytes()
    with build_backup(source) as archive:
        safety=restore_backup(target,archive)
    assert safety.is_file() and target.path.read_bytes()==original_config
    with connect(target.database_path) as c:
        assert c.execute('SELECT stable_id FROM items').fetchone()[0]=='restored'
        assert c.execute("SELECT owner FROM job_locks WHERE name='maintenance'").fetchone()[0]=='test-owner'
        c.execute("UPDATE items SET stable_id='keep-me'")
    def fail(*args): raise OSError('simulated OPML disk write failure')
    monkeypatch.setattr('rss_reader.backup.write_database_opml',fail)
    with build_backup(source) as archive, pytest.raises(OSError): restore_backup(target,archive)
    with connect(target.database_path) as c:
        assert c.execute('SELECT stable_id FROM items').fetchone()[0]=='keep-me'
        assert c.execute("SELECT owner FROM job_locks WHERE name='maintenance'").fetchone()[0]=='test-owner'


def test_corrupt_backup_does_not_touch_live_data(tmp_path):
    target=configured_at(tmp_path)
    with connect(target.database_path) as c: ordinary(c)
    before=target.database_path.read_bytes()
    with pytest.raises(ValueError): restore_backup(target,b'not a zip')
    assert target.database_path.read_bytes()==before
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('manifest.json','{}');z.writestr('reader.sqlite3',b'bad');z.writestr('../escape',b'bad')
    archive.seek(0)
    with pytest.raises(ValueError): restore_backup(target,archive)
    assert target.database_path.read_bytes()==before
