"""Real Flask endpoint regressions; requires the installed application dependencies.

Core SQL, archive and browser/reducer tests have separate dependency-light suites.
This module is intentionally skipped when Flask is absent, rather than providing
fake Flask or provider modules and claiming a live-server pass.
"""
import re
import pytest
pytest.importorskip('flask')
pytest.importorskip('feedparser')
from rss_reader.web import create_app
from rss_reader.db import connect,utcnow
from rss_reader.backup import MAX_BACKUP_ARCHIVE_BYTES


def fixture_app(configured):
    with connect(configured.database_path) as c:
        g=int(c.execute("INSERT INTO groups(title,created_at) VALUES('Video lessons',?)",(utcnow(),)).lastrowid)
        f=int(c.execute("INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,'Logic channel','https://example.test/rss',?)",(g,utcnow())).lastrowid)
        i=int(c.execute('INSERT INTO items(feed_id,stable_id,title,url,published_at,discovered_at) VALUES(?,?,?,?,?,?)',
             (f,'web025','Lesson','https://youtube.com/watch?v=NmiD1YdbrAA',utcnow(),utcnow())).lastrowid)
    app=create_app(configured.path); app.config['TESTING']=True
    return app,g,f,i


def token(client,group):
    response=client.get(f'/?group={group}')
    assert response.status_code==200
    return re.search(rb'<meta name="csrf-token" content="([^"]+)"',response.data).group(1).decode()


def test_actual_reader_time_window_and_mark_read(configured):
    app,g,f,i=fixture_app(configured); client=app.test_client(); csrf=token(client,g)
    html=client.get(f'/?group={g}').data.decode()
    assert 'Current week' in html and 'Last 5 days' in html and 'Best read' in html
    assert 'Check feeds of Video lessons' in html and 'Update summaries of Video lessons' in html
    first=client.get(f'/api/review/days?group_id={g}&preset=current-week').get_json()
    assert first['counts']['matching']==1
    assert client.post(f'/api/items/{i}/read',headers={'X-CSRF-Token':csrf},json={'read':True}).status_code==200
    second=client.get(f'/api/review/days?group_id={g}&preset=current-week').get_json()
    assert second['counts']['matching']==1 and second['counts']['unread']==0


def test_actual_profile_csrf_scope_and_validation(configured):
    app,g,f,i=fixture_app(configured); client=app.test_client(); csrf=token(client,g)
    assert client.post('/api/review/interests',json={'group_id':g,'value':'Logic'}).status_code==403
    assert client.post('/api/review/interests',headers={'X-CSRF-Token':csrf},json={'group_id':g,'value':'Logic'}).status_code==200
    assert client.get(f'/api/review/interests?feed_id={f}').get_json()['effective']=='Logic'
    assert client.post('/api/review/interests',headers={'X-CSRF-Token':csrf},json={'group_id':g,'value':'x'*8001}).status_code==400
    with connect(configured.database_path) as c:
        assert c.execute('SELECT interest_profile FROM groups WHERE id=?',(g,)).fetchone()[0]=='Logic'


def test_actual_backup_limit_and_security_headers(configured):
    app,g,f,i=fixture_app(configured); client=app.test_client()
    assert app.config['MAX_CONTENT_LENGTH']>MAX_BACKUP_ARCHIVE_BYTES>=1024**3
    response=client.get(f'/?group={g}')
    policy=response.headers['Content-Security-Policy']
    assert 'frame-src https:' in policy and "object-src 'none'" in policy
    # Invalid input is rejected without entering restore or modifying state.
    csrf=token(client,g)
    assert client.post('/api/restore',headers={'X-CSRF-Token':csrf},data={}).status_code==400


def test_actual_rc2_preferences_config_validation_and_api_defaults(configured):
    import json
    from rss_reader.config import load_config
    app,g,f,i=fixture_app(configured);client=app.test_client();csrf=token(client,g)
    html=client.get(f'/?group={g}').data.decode()
    options=json.loads(re.search(r'<script id="reader-preferences" type="application/json">(.*?)</script>',html,re.S).group(1))
    assert options['review_default_view']=='adaptive'
    assert options['audio_controls_expanded'] is False
    assert 'Current month' in html and 'Audio reading for Video lessons' in html
    response=client.post('/api/config',headers={'X-CSRF-Token':csrf},json={'values':{
        'ui.review_default_view':'current-month','ui.review_page_size':50,
        'ui.tts_volume':0.4,'ui.audio_reading_enabled':False,
    }})
    assert response.status_code==200,response.get_data(as_text=True)
    response=client.get(f'/api/review/days?group_id={g}')
    assert response.status_code==200
    filters=response.get_json()['filters']
    assert filters['preset']=='current-month' and filters['page_size']==50
    assert client.get(f'/api/review/days?group_id={g}&preset=everything').get_json()['filters']['preset']=='everything'
    bad=client.post('/api/config',headers={'X-CSRF-Token':csrf},json={'values':{'ui.tts_volume':2}})
    assert bad.status_code==400
    restored=load_config(configured.path)
    assert restored.get('ui','tts_volume')==0.4
    assert restored.get('ui','audio_reading_enabled') is False


def test_actual_rc2_older_source_adapts_to_everything(configured):
    app,g,f,i=fixture_app(configured);client=app.test_client()
    with connect(configured.database_path) as c:
        c.execute("UPDATE items SET published_at='1999-01-01T00:00:00Z',discovered_at='1999-01-01T00:00:00Z' WHERE id=?",(i,))
    html=client.get(f'/?group={g}').data.decode()
    assert '<meta name="review-default-preset" content="everything">' in html
    response=client.get(f'/api/review/days?group_id={g}').get_json()
    assert response['filters']['preset']=='everything' and response['counts']['matching']==1
