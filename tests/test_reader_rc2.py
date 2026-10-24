from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

from rss_reader.config import DEFAULTS, Config, load_config, save_config, validate_config
from rss_reader.db import connect, initialize
from rss_reader.reader_preferences import reader_options
from rss_reader.review import default_review_preset, parse_review_filters, resolve_review_scope
from distillfeed_arxiv.dates import ensure_date_schema

@pytest.fixture
def scoped(tmp_path):
    path = tmp_path / 'reader.sqlite3'
    initialize(path)
    connection = connect(path)
    connection.execute("INSERT INTO groups(id,title,created_at) VALUES(100,'Monthly source','2026-01-01')")
    connection.execute("INSERT INTO groups(id,title,parent_id,created_at) VALUES(101,'Child',100,'2026-01-01')")
    for identifier, group in [(100,100),(101,101)]:
        connection.execute("INSERT INTO feeds(id,group_id,title,xml_url,created_at) VALUES(?,?,?,?,'2026-01-01')",(identifier, group, str(identifier),f'https://example.test/{identifier}.xml'))
    yield connection
    connection.close()

def item(connection, stamp, *, feed=100, discovered=None, read=False):
    identifier = int(connection.execute('SELECT COALESCE(MAX(id),0)+1 FROM items').fetchone()[0])
    connection.execute('''INSERT INTO items(id,feed_id,stable_id,title,url,published_at,discovered_at,is_read)
        VALUES(?,?,?,?,?,?,?,?)''',(identifier,feed,str(identifier),'Title',f'https://example.test/{identifier}',stamp,discovered or stamp,read))
    return identifier

def choice(c, **kwargs):
    return default_review_preset(resolve_review_scope(c, group_id=100), c, today='2026-10-24', **kwargs)

def test_adaptive_week_month_everything_no_unread_dependency(scoped):
    c=scoped
    assert choice(c)=='everything'
    item(c,'2026-09-30T10:00:00Z')
    assert choice(c)=='everything'
    item(c,'2026-10-03T10:00:00Z',read=True)
    assert choice(c)=='current-month'
    item(c,'2026-10-20T10:00:00Z',read=True)
    assert choice(c)=='current-week'
    c.execute('UPDATE items SET is_read=1')
    assert choice(c)=='current-week'

def test_adaptive_source_scope_descendants_disabled_future(scoped):
    c=scoped
    item(c,'2026-10-20T10:00:00Z',feed=101)
    assert choice(c)=='current-week'
    scope=resolve_review_scope(c,feed_id=100)
    assert default_review_preset(scope,c,today='2026-10-24')=='everything'
    c.execute('UPDATE feeds SET enabled=0 WHERE id=101')
    assert choice(c)=='everything'
    item(c,'2099-01-01T00:00:00Z',discovered='2026-10-02T12:00:00Z')
    assert choice(c)=='current-month'

def test_adaptive_arxiv_uses_canonical_announcement_not_submission(scoped):
    c=scoped
    c.execute("UPDATE feeds SET xml_url='plugin://arxiv/cs.AI' WHERE id=100")
    c.execute('''CREATE TABLE distillfeed_arxiv_papers(item_id INTEGER PRIMARY KEY,source TEXT,evaluation_status TEXT)''')
    ensure_date_schema(c)
    identifier=item(c,'2026-10-24T00:00:00Z')
    c.execute("INSERT INTO distillfeed_arxiv_papers(item_id,source,announcement_source) VALUES(?,'api','unknown')",(identifier,))
    assert choice(c)=='everything'
    c.execute("UPDATE distillfeed_arxiv_papers SET announced_at='2026-10-02T00:00:00Z' WHERE item_id=?",(identifier,))
    assert choice(c)=='current-month'
    c.execute("UPDATE distillfeed_arxiv_papers SET announced_at='2026-10-23T00:00:00Z' WHERE item_id=?",(identifier,))
    assert choice(c)=='current-week'

@pytest.mark.parametrize('today,start',[('2026-01-01','2026-01-01'),('2024-02-29','2024-02-01'),('2026-10-24','2026-10-01'),('2026-12-31','2026-12-01')])
def test_month_calendar_filters(today,start):
    f=parse_review_filters({'preset':'current-month'},today=today)
    assert f['from']==start and f['to']==today and f['read']=='all'

def test_explicit_choice_and_url_never_overridden(scoped):
    c=scoped;item(c,'2026-10-24T00:00:00Z')
    assert choice(c,preference='everything')=='everything'
    for preset in ['current-week','current-month','last-5-days','best','best-read','best-unread','awaiting-ai','everything']:
        assert choice(c,preference=preset)==preset
        assert parse_review_filters({'preset':preset},default_preset=choice(c))['preset']==preset
    assert parse_review_filters({},default_preset='current-month', default_page_size=50)['page_size']==50
    assert parse_review_filters({'page_size':'10'},default_page_size=50)['page_size']==10

@pytest.mark.parametrize('key,value',[
    ('review_default_view','random'),('review_page_size',10000),('review_compact_cards','yes'),
    ('audio_reading_enabled',1),('tts_volume',float('nan')),('tts_volume',float('inf')),
    ('tts_volume',-1),('tts_volume',1.1),('tts_rate',5),('tts_chunk_chars',179),
    ('tts_chunk_chars',2401),('tts_pause_ms',-1),('review_rationale_lines',0),('tts_chunk_chars',200.2),
])
def test_preferences_invalid_rejected(key,value):
    data=copy.deepcopy(DEFAULTS);data['ui'][key]=value
    with pytest.raises(ValueError):validate_config(data)

def test_preferences_persist_and_no_secrets_in_public_options(tmp_path):
    config=load_config(tmp_path/'config.toml')
    assert config.get('ui','review_default_view')=='adaptive'
    assert not config.get('ui','audio_controls_expanded')
    config.data['ui'].update(tts_volume=0.25,review_default_view='current-month',review_page_size=50)
    save_config(config);restored=load_config(config.path)
    assert restored.get('ui','review_default_view')=='current-month'
    assert restored.get('ui','tts_volume')==0.25
    assert reader_options({'api_key':'secret','tts_volume':0.25})=={'tts_volume':0.25}

def test_shared_default_implementation_in_server():
    # The actual web module has optional runtime dependencies; AST verifies its
    # wiring without replacing Flask with a fake implementation.
    import ast
    source=Path(__file__).parents[1]/'rss_reader/web.py'
    tree=ast.parse(source.read_text())
    calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='default_review_preset']
    assert len(calls)==3
    assert all(len(node.args)==2 for node in calls)
    assert all(any(arg.arg=='preference' for arg in node.keywords) for node in calls)
