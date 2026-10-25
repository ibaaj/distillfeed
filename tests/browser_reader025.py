from __future__ import annotations
import sys,json,re,traceback,os,shutil,tempfile
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from datetime import datetime,UTC
ROOT=Path(os.environ.get('DISTILLFEED_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from jinja2 import Environment,FileSystemLoader,ChainableUndefined,select_autoescape
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from test_review import _build_database
from rss_reader.db import connect,transaction
from rss_reader.review import *
from rss_reader.interests import scope_interests,validate_interests
from rss_reader.presentation import render_summary_markdown
from distillfeed_arxiv.dates import ensure_date_schema

LOG=Path(os.environ.get('DISTILLFEED_TEST_OUTPUT') or tempfile.mkdtemp(prefix='distillfeed-browser-'));LOG.mkdir(parents=True,exist_ok=True);DB=LOG/'browser.sqlite3'; DB.unlink(missing_ok=True);_build_database(DB)
with connect(DB) as c:
 ensure_date_schema(c)
 c.execute("INSERT INTO groups(id,title,created_at,review_display_mode) VALUES(50,'YouTube','2026-08-18','direct')")
 c.execute("INSERT INTO feeds(id,group_id,title,xml_url,created_at) VALUES(50,50,'Logic videos','https://example.test/feed','2026-08-18')")
 for i in range(10001,10011):
  c.execute('''INSERT INTO items(id,feed_id,stable_id,title,url,published_at,discovered_at,description_text)
   VALUES(?,50,?,?,?,'2026-08-18T10:00:00Z','2026-08-18T10:00:00Z',?)''',(i,str(i),'Logic video '+str(i),'https://www.youtube.com/shorts/NmiD1YdbrAA' if i==10001 else 'https://www.youtube.com/watch?v=NmiD1YdbrAA','An accessible discussion of logic and uncertainty.'))
 c.execute("INSERT INTO items(id,feed_id,stable_id,title,url,published_at,discovered_at) VALUES(999,10,'unknown','Announcement unknown','https://arxiv.org/abs/2608.00999','2026-08-19','2026-08-19')")
 c.execute("INSERT INTO distillfeed_arxiv_papers(item_id,arxiv_id,categories_json,source,announcement_source,evaluation_status) VALUES(999,'2608.00999','[]','api','unknown','pending')")

ENV=Environment(loader=FileSystemLoader(ROOT/'rss_reader/templates'),undefined=ChainableUndefined,autoescape=select_autoescape(['html']))
ENV.filters['summary_markup']=render_summary_markdown
ENV.globals.update(csrf_token=lambda:'test-token', url_for=lambda endpoint,**kw:'/static/'+kw['filename']+('?v='+kw['v'] if kw.get('v') else '') if endpoint=='static' else '/'+endpoint)
# Compile EVERY template; render actual index.html with the same review data shape.
for name in ENV.list_templates(): ENV.get_template(name)

def html(group):
 isarxiv=group==1
 data=dict(ui=dict(subscription_font_size=16,item_font_size=16,summary_font_size=16,offline_cache_enabled=False),selected_group_id=group,selected_feed_id=None,review_preference_group_id=group,review_display_mode='daily' if isarxiv else 'direct',review_default_preset='current-week',review_default_sort='ai' if isarxiv else 'date',scope_title='arXiv Digest' if isarxiv else 'YouTube',scope_ai_mode='arxiv' if isarxiv else 'automatic',scope_ai_own_mode='arxiv' if isarxiv else 'automatic',is_arxiv_scope=isarxiv,groups=[],feeds=[],tree=[],locks={},settings_sections=[],system_notices=[],settings_ai_groups=[],settings_ai_feeds=[],summary_minimum_relevance=70,auto_refresh=False,refresh_interval_minutes=30,app_mode='development',arxiv_available=True)
 page=BeautifulSoup(ENV.get_template('index.html').render(**data),'html.parser')
 for script in page.find_all('script'):
  if not any(v in script.get('src','') for v in ['review-state.js','review.js','reader-tools.js','layout-init.js']): script.decompose()
 return str(page)

requests=[];errors=[];checks=[]
def route_handler(route):
 request=route.request;parts=urlsplit(request.url);args={k:v[-1] for k,v in parse_qs(parts.query).items()}
 path=parts.path
 if parts.hostname!='127.0.0.1':
  route.fulfill(status=200,content_type='text/html',body='<p>External resource stub: no live network.</p>');return
 if path.startswith('/static/'):
  file=ROOT/'rss_reader/static'/Path(path).name
  mime='text/css' if file.suffix=='.css' else 'image/svg+xml' if file.suffix=='.svg' else 'application/javascript' if file.suffix=='.js' else 'application/json'
  route.fulfill(status=200,content_type=mime,body=file.read_bytes());return
 if path=='/':route.fulfill(status=200,content_type='text/html',body=html(int(args.get('group',1))));return
 if path.startswith('/api/'):
  requests.append((request.method,path,args))
  try:
   payload=request.post_data_json if request.method!='GET' else {}
   payload=payload or {}
   with connect(DB) as c:
    inputs=payload if request.method!='GET' else args
    if path.startswith('/api/review/'):
     scope=resolve_review_scope(c,group_id=int(inputs.get('group_id',0)) or None,feed_id=int(inputs.get('feed_id',0)) or None)
     filters=parse_review_filters(args,today='2026-08-18',default_preset='current-week',default_sort=default_review_sort(scope),default_min_ai=70)
    if path=='/api/review/days': result=list_review_days(c,scope,filters,minimum_relevance=70,before=args.get('before',''))
    elif re.fullmatch(r'/api/review/days/[^/]+/items',path):result=list_review_day_items(c,scope,path.split('/')[4],filters,minimum_relevance=70,cursor=args.get('cursor',''))
    elif re.fullmatch(r'/api/review/items/\d+',path):result=review_item_details(c,scope,int(path.split('/')[-1]),minimum_relevance=70)
    elif re.fullmatch(r'/api/groups/\d+',path) and request.method=='PATCH':
     group_id=int(path.rsplit('/',1)[-1]);mode=str(payload.get('review_display_mode','daily'))
     assert mode in {'daily','direct'}
     c.execute('UPDATE groups SET review_display_mode=? WHERE id=?',(mode,group_id))
     result={'id':group_id,'review_display_mode':mode}
    elif path=='/api/review/interests':
     if request.method=='POST':c.execute('UPDATE groups SET interest_profile=? WHERE id=?',(validate_interests(payload['value']),scope.scope_id))
     result=scope_interests(c,scope,'Global fallback interests')
    elif re.fullmatch(r'/api/items/\d+/(read|star|read-later)',path):
     identifier=int(path.split('/')[3]);action=path.split('/')[-1];field={'read':'is_read','star':'is_starred','read-later':'is_read_later'}[action];key={'read':'read','star':'starred','read-later':'read_later'}[action]
     c.execute(f'UPDATE items SET {field}=? WHERE id=?',(int(payload[key]),identifier));result={'status':'ok'}
    elif path.endswith('/finish'):
     result=finish_review_day(c,scope,path.split('/')[4])
    elif path=='/api/items/bulk-read':
     marks=','.join('?' for _ in payload['item_ids']);c.execute(f'UPDATE items SET is_read=1 WHERE id IN ({marks})',payload['item_ids']);result={'changed':len(payload['item_ids'])}
    else:result={'status':'ok'}
   route.fulfill(status=200,content_type='application/json',body=json.dumps(result));return
  except Exception as exc:
   errors.append(f'Fixture backend {path}: {exc}\n{traceback.format_exc()}')
   route.fulfill(status=500,content_type='application/json',body=json.dumps({'error':str(exc)}));return
 route.fulfill(status=404,body='not found')

def bridge(_source, raw_path, options):
    class Request:
        url='http://127.0.0.1:8769'+raw_path
        method=options.get('method','GET')
        post_data_json=json.loads(options.get('body','{}')) if options.get('body') else {}
    class Route:
        request=Request()
        result=None
        def fulfill(self, **kw): self.result=kw
    route=Route();route_handler(route)
    body=route.result.get('body','{}')
    return {'status':route.result.get('status',200),'body':body}

with sync_playwright() as play:
 browser=play.chromium.launch(executable_path=os.environ.get('DISTILLFEED_CHROMIUM') or shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
 context=browser.new_context(viewport={'width':1500,'height':1000})
 context.route('**/*',route_handler)  # All external frame/document resources are fixture stubs.
 def new_page(group):
  page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
  page.clock.install(time=datetime(2026,8,18,12,tzinfo=UTC))
  page.expose_binding('__fixture',bridge)
  doc=BeautifulSoup(html(group),'html.parser')
  for node in doc.find_all(['script','link']):node.decompose()
  page.set_content(str(doc))
  page.add_style_tag(content=(ROOT/'rss_reader/static/app.css').read_text())
  page.evaluate("""() => {
    window.__pending = 0; window.__requests = 0;
    window.fetch = async (url, options={}) => {
      window.__pending++; window.__requests++;
      try { const result = await window.__fixture(String(url), options); return new Response(result.body, {status: result.status, headers:{'Content-Type':'application/json'}}); }
      finally { window.__pending--; }
    };
    history.replaceState = (_state, _unused, url) => { window.__lastURL = String(url); };
  }""")
  for file in ['review-state.js','reader-tools.js','review.js']:
   page.add_script_tag(content=(ROOT/'rss_reader/static'/file).read_text())
  page.locator('.review-item').first.wait_for()
  return page
 def settled(page):
  page.wait_for_function('window.__pending === 0');page.wait_for_timeout(40)
 def select(page,control,value):
  page.locator('#'+control).select_option(value);settled(page)
 page=new_page(1)
 # RC5: one desktop action row with statistics underneath; mobile reflows.
 # The underlying native select still supports saved mode transitions.
 assert page.locator('.review-layout-control > span').inner_text() == 'Day view'
 assert page.locator('#review-display-mode').evaluate('(el)=>getComputedStyle(el).opacity') == '0'
 assert page.locator('#review-audio-disclosure > summary').get_attribute('aria-label') == 'Audio reading for arXiv Digest'
 assert page.locator('#review-audio-disclosure > summary svg').count() == 1
 assert page.locator('#arxiv-scope-config > summary').inner_text() == 'AI for arXiv Digest'
 layout_checks=[]
 for width in [2048, 1500, 1280, 1100, 1000, 900, 768, 620, 430, 375, 320]:
  page.set_viewport_size({'width':width,'height':900})
  page.wait_for_timeout(30)
  metrics=page.evaluate("""() => {
    const rect = s => document.querySelector(s)?.getBoundingClientRect();
    const compact = b => !b ? null : ({x:b.x,y:b.y,right:b.right,bottom:b.bottom,width:b.width,height:b.height,cy:b.y+b.height/2});
    const box = s => compact(rect(s));
    return {
      pane:box('#review-app'), header:box('.review-heading'), title:box('.review-heading-title'),
      controls:box('.review-heading-actions'), status:box('.review-scope-meta'),
      layout:box('.review-layout-control'), refresh:box('#scope-refresh-button'),
      update:box('#scope-update-button'), ai:box('#arxiv-scope-config > summary'),
      audio:box('#review-audio-disclosure > summary'), font:box('.review-reader-utilities .reader-text-size'),
      help:box('#summary-help-button'), cardTitle:box('.review-item .review-item-title'),
      caret:box('.review-item .review-content-toggle'), score:box('.review-item .review-score'),
      documentWidth:document.documentElement.scrollWidth, viewport:document.documentElement.clientWidth,
    };
  }""")
  assert metrics['documentWidth'] <= metrics['viewport'] + 2, (width, 'page overflow',metrics)
  assert metrics['cardTitle'] and metrics['caret'] and metrics['score'], width
  assert abs(metrics['cardTitle']['cy']-metrics['caret']['cy']) < 9, (width,'title/arrow misaligned',metrics)
  assert abs(metrics['cardTitle']['cy']-metrics['score']['cy']) < 9, (width,'title/score misaligned',metrics)
  # RC7: AI score should remain a compact, content-sized status pill.
  assert metrics['score']['height'] <= 23, (width,'oversized AI score badge',metrics)
  assert metrics['score']['width'] <= 52, (width,'overwide AI score badge',metrics)
  if metrics['pane']['width']>1070:
   row=[metrics[k] for k in ['layout','refresh','update','ai','audio','font','help']]
   assert max(x['cy'] for x in row)-min(x['cy'] for x in row)<8, (width,'controls wrapped',metrics)
   # RC6 keeps detailed statistics offscreen but accessible at wide widths.
   assert (metrics['status']['width'] <= 2 or
           metrics['status']['y'] >= max(x['bottom'] for x in row)-1), (width,'status overlay',metrics)
   assert abs(metrics['title']['cy']-metrics['controls']['cy']) < 10, (width,'heading title misaligned',metrics)
  for key in ['layout','refresh','update','ai','audio','font','help']:
   r=metrics[key]
   assert r['x'] >= metrics['header']['x']-2 and r['right'] <= metrics['header']['right']+2, (width,key,'control clipped',metrics)
  layout_checks.append((width,round(metrics['pane']['width'])))
 page.set_viewport_size({'width':1500,'height':1000})
 checks.append('RC5: responsive geometry, one-row desktop toolbar, statistics below, aligned title/arrow, no overflow: '+str(layout_checks))
 page.screenshot(path=str(LOG/'rc5-arxiv-clean-desktop.png'),full_page=False)
 select(page,'review-display-mode','direct')
 assert page.locator('#review-display-mode').input_value()=='direct'
 select(page,'review-display-mode','daily')
 assert page.locator('#review-display-mode').input_value()=='daily'
 checks.append('RC5: compact Day view menu preserves saved mode transitions')
 assert page.locator('.review-item').count()==25
 assert page.locator('#review-preset').input_value()=='current-week'
 assert page.locator('.review-rationale-preview').count()>0
 card=page.locator('.review-item').first;identifier=card.get_attribute('data-item-id')
 card.locator('[data-action="toggle-item"]').click();settled(page)
 page.locator(f'#review-item-details-{identifier} .review-detail-section').first.wait_for()
 page.locator(f'article[data-item-id="{identifier}"] [data-action="read"]').click();settled(page)
 assert page.locator(f'article[data-item-id="{identifier}"]').count()==1
 assert page.locator(f'#review-item-details-{identifier}').is_visible()
 checks.append('opening and marking read retains card and expanded content')
 page.locator('details.review-daily-brief summary').first.click()
 page.locator('[data-action="load-more"]').first.click();page.wait_for_function('document.querySelectorAll(".review-item").length === 50')
 assert len(set(page.locator('.review-item').evaluate_all('(els)=>els.map(el=>el.dataset.itemId)')))==50
 assert page.locator('details.review-daily-brief').first.get_attribute('open') is not None
 checks.append('pagination is unique and preserves opened brief')
 select(page,'review-preset','everything')
 assert page.locator('details.review-day').last.get_attribute('data-day')=='undated'
 assert not page.locator('details.review-day[data-day="undated"]').evaluate('(el)=>el.open')
 checks.append('unknown announcement date is last and initially collapsed')
 for preset in ['current-week','last-5-days','best','best-unread','best-read','today','awaiting-ai','starred','everything']:
  select(page,'review-preset',preset)
  assert page.locator('#review-preset').input_value()==preset
  assert not page.locator('.review-error-page').count(),preset
  if preset == 'awaiting-ai':
   pending = page.locator('.review-score-pending').first
   pending.wait_for()
   bounds = pending.bounding_box()
   assert bounds and bounds['height'] <= 23 and bounds['width'] <= 75, (preset,bounds)
 checks.append('all nine presets executed with SQL-backed transport fixture')
 select(page,'review-preset','current-week')
 page.screenshot(path=str(LOG/'arxiv-review-wide.png'),full_page=False)
 select(page,'review-preset','everything')
 page.locator('#review-advanced summary').click()
 for control,value in [('review-read','read'),('review-saved','starred'),('review-ai-state','pending'),('review-decision','keep'),('review-min-ai','80'),('review-source','10'),('review-sort','date'),('review-page-size','10')]:
  select(page,control,value);assert not page.locator('.review-error-page').count(),control
 for control,value in [('review-from','2026-08-17'),('review-to','2026-08-18')]:
  page.locator('#'+control).fill(value);page.locator('#'+control).dispatch_event('change');settled(page)
 page.locator('#review-search').fill('Causal');page.wait_for_timeout(350);settled(page)
 checks.append('all advanced controls, dates and search executed')
 page.close();page=new_page(50)
 page.screenshot(path=str(LOG/'rc5-ordinary-desktop.png'),full_page=False)
 page.set_viewport_size({'width':430,'height':900})
 page.screenshot(path=str(LOG/'rc5-ordinary-mobile.png'),full_page=False)
 page.set_viewport_size({'width':1500,'height':1000})
 assert 'Check feeds of YouTube' in page.locator('#scope-refresh-button').inner_text()
 assert 'Update summaries of YouTube' in page.locator('#scope-update-button').inner_text()
 assert page.locator('.review-item').count()==10
 page.locator('#summary-ai-config > summary').click()
 page.locator('#review-interests summary').click();settled(page)
 page.wait_for_function('document.querySelector("#review-interests-text").placeholder.includes("Global")')
 page.locator('#review-interests-text').fill('Prefer formal logic tutorials.')
 page.locator('#review-interests-save').click();settled(page)
 assert 'Saved.' in page.locator('#review-interests-status').inner_text()
 checks.append('source interest editor persisted actual SQLite preference')
 page.locator('article[data-item-id="10001"] [data-action="play-video"]').click()
 assert page.locator('#reader-theatre').evaluate('(el)=>el.open')
 assert page.locator('#reader-frame-container iframe').get_attribute('src').startswith('https://www.youtube-nocookie.com/embed/')
 page.locator('#reader-wide').click();assert 'wide' in page.locator('#reader-theatre').get_attribute('class')
 page.keyboard.press('Escape');page.wait_for_timeout(40);assert page.locator('#reader-frame-container iframe').count()==0
 checks.append('YouTube embed construction, theatre toggle, Escape cleanup; live playback not requested')
 page.locator('article[data-item-id="10001"] [data-action="toggle-item"]').click();settled(page)
 page.locator('#review-item-details-10001 .review-detail-section').first.wait_for()
 page.locator('article[data-item-id="10001"] [data-action="article-theatre"]').click()
 assert 'block embedding' in page.locator('#reader-frame-notice').inner_text()
 page.locator('#reader-close').click();page.wait_for_timeout(40);assert page.locator('#reader-frame-container iframe').count()==0
 page.screenshot(path=str(LOG/'review-wide.png'),full_page=False)
 page.set_viewport_size({'width':430,'height':900});page.screenshot(path=str(LOG/'review-narrow.png'),full_page=False)
 checks.append('wide and narrow layouts captured')
 assert not errors, errors
 browser.close()
(LOG/'browser-result.json').write_text(json.dumps({'passed':checks,'errors':errors,'api_requests':len(requests),'transport':'local binding to actual SQLite handlers; Flask HTTP not exercised'},indent=2))
print(json.dumps({'passed':checks,'errors':errors,'api_requests':len(requests)},indent=2))
