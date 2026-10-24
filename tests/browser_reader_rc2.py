from __future__ import annotations
import sys,json,re,traceback,os,shutil,tempfile,ast,copy
from typing import Any
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
from rss_reader.config import DEFAULTS, Config, save_config, validate_config
from rss_reader.reader_preferences import reader_options, VIEW_OPTIONS, RANGES
# Extract the actual pure settings field builder; this is not a Flask substitute.
web_ast=ast.parse((ROOT/'rss_reader/web.py').read_text())
flatten=next(n for n in web_ast.body if isinstance(n,ast.FunctionDef) and n.name=='_flatten_config')
namespace={'Any':Any,'VIEW_OPTIONS':VIEW_OPTIONS,'RANGES':RANGES}
exec(compile(ast.Module(body=[flatten],type_ignores=[]),'actual settings field builder','exec'),namespace)
flatten_config=namespace['_flatten_config']
configuration=copy.deepcopy(DEFAULTS)
configuration['app']['auto_refresh_on_load']=False
configuration['weather']['enabled']=False
configuration['ui'].update(subscription_font_size=16,item_font_size=16,summary_font_size=16)


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

with connect(DB) as c:
 for group,title,stamp in [(60,'Monthly journal','2026-08-01'),(61,'Archive journal','2026-07-01')]:
  c.execute("INSERT INTO groups(id,title,created_at,review_display_mode) VALUES(?,?,?,'direct')",(group,title,stamp))
  c.execute("INSERT INTO feeds(id,group_id,title,xml_url,created_at) VALUES(?,?,?,?,?)",(group,group,title,f'https://example.test/{group}',stamp))
  c.execute("INSERT INTO items(feed_id,stable_id,title,url,published_at,discovered_at,description_text) VALUES(?,?,?,?,?,?,?)",(group,str(group),title+' article',f'https://example.test/{group}',stamp,stamp,'Stored source description.'))

ENV=Environment(loader=FileSystemLoader(ROOT/'rss_reader/templates'),undefined=ChainableUndefined,autoescape=select_autoescape(['html']))
ENV.filters['summary_markup']=render_summary_markdown
ENV.globals.update(csrf_token=lambda:'test-token', url_for=lambda endpoint,**kw:'/static/'+kw['filename']+('?v='+kw['v'] if kw.get('v') else '') if endpoint=='static' else '/'+endpoint)
# Compile EVERY template; render actual index.html with the same review data shape.
for name in ENV.list_templates(): ENV.get_template(name)

def html(group):
 isarxiv=group==1
 with connect(DB) as c:
  scope=resolve_review_scope(c,group_id=group)
  selected=default_review_preset(scope,c,today='2026-08-18',preference=configuration['ui']['review_default_view'])
 data=dict(ui=configuration['ui'],reader_options=reader_options(configuration['ui']),review_today='2026-08-18',selected_group_id=group,selected_feed_id=None,review_preference_group_id=group,review_display_mode=scope.review_display_mode,review_default_preset=selected,review_default_sort='ai' if isarxiv else 'date',scope_title=scope.title,scope_ai_mode='arxiv' if isarxiv else 'automatic',scope_ai_own_mode='arxiv' if isarxiv else 'automatic',is_arxiv_scope=isarxiv,groups=[],feeds=[],tree=[],locks={},settings_sections=[],system_notices=[],settings_ai_groups=[],settings_ai_feeds=[],summary_minimum_relevance=70,auto_refresh=False,refresh_interval_minutes=30,app_mode='development',arxiv_available=True,config_fields=flatten_config(configuration),arxiv_fields=[])
 page=BeautifulSoup(ENV.get_template('index.html').render(**data),'html.parser')
 for script in page.find_all('script'):
  if script.get('type')!='application/json' and not any(v in script.get('src','') for v in ['review-state.js','review.js','reader-tools.js','layout-init.js','app.js']): script.decompose()
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
     filters=parse_review_filters(args,today='2026-08-18',default_preset=default_review_preset(scope,c,today='2026-08-18'),default_sort=default_review_sort(scope),default_min_ai=70)
    if path=='/api/review/days': result=list_review_days(c,scope,filters,minimum_relevance=70,before=args.get('before',''))
    elif re.fullmatch(r'/api/review/days/[^/]+/items',path):result=list_review_day_items(c,scope,path.split('/')[4],filters,minimum_relevance=70,cursor=args.get('cursor',''))
    elif re.fullmatch(r'/api/review/items/\d+',path):result=review_item_details(c,scope,int(path.split('/')[-1]),minimum_relevance=70)
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
    elif re.fullmatch(r'/api/groups/\d+',path) and request.method=='PATCH':
     group_id=int(path.split('/')[-1]); mode=payload['review_display_mode']
     assert mode in ('daily','direct')
     c.execute('UPDATE groups SET review_display_mode=? WHERE id=?',(mode,group_id))
     result={'review_display_mode':mode}
    elif path=='/api/config':
     candidate=copy.deepcopy(configuration)
     for path_name,value in payload.get('values',{}).items():
      keys=path_name.split('.');target=candidate
      for key in keys[:-1]:target=target[key]
      key=keys[-1];previous=target[key]
      if isinstance(previous,bool): parsed=value
      elif isinstance(previous,int): parsed=int(value)
      elif isinstance(previous,float): parsed=float(value)
      else: parsed=str(value)
      target[key]=parsed
     validate_config(candidate)
     configuration.clear();configuration.update(candidate)
     save_config(Config(LOG/'config.toml',configuration))
     result={'status':'ok'}
    elif path=='/api/weather':result={'enabled':False}
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
 def new_page(group, *, blocked_speech=False):
  page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
  page.clock.install(time=datetime(2026,8,18,12,tzinfo=UTC))
  page.expose_binding('__fixture',bridge)
  doc=BeautifulSoup(html(group),'html.parser')
  for node in doc.find_all(['script','link']):
   if node.get('type')!='application/json': node.decompose()
  page.set_content(str(doc))
  page.add_style_tag(content=(ROOT/'rss_reader/static/app.css').read_text())
  page.evaluate("""() => {
    window.__pending = 0; window.__requests = 0;
    window.fetch = async (url, options={}) => {
      window.__pending++; window.__requests++;
      try { const result = await window.__fixture(String(url), options); return new Response(result.body, {status: result.status, headers:{'Content-Type':'application/json'}}); }
      finally { window.__pending--; }
    };
    // Simulated local native voice: verifies controls/state, never emits sound.
    window.__speech = {utterances: [], cancelled:0, speaking:false, pending:false, paused:false,
      getVoices:()=>[{name:'Local test voice',lang:'en-US',voiceURI:'local-test',localService:true}],
      addEventListener:()=>{},speak(u){this.utterances.push(u);this.speaking=true;},
      cancel(){this.cancelled++;this.speaking=false;},pause(){this.paused=true;},resume(){this.paused=false;}};
    Object.defineProperty(window,'speechSynthesis',{value:window.__speech,configurable:true});
    Object.defineProperty(window,'SpeechSynthesisUtterance',{value:function(text){this.text=text;},configurable:true});
    history.replaceState = (_state, _unused, url) => { window.__lastURL = String(url); };
  }""")
  if blocked_speech:page.evaluate("Object.defineProperty(window, 'speechSynthesis', {get(){throw new Error('privacy settings');},configurable:true})")
  for file in ['layout-init.js','review-state.js','reader-tools.js','review.js','app.js']:
   page.add_script_tag(content=(ROOT/'rss_reader/static'/file).read_text())
  page.locator('.review-item').first.wait_for()
  return page
 def settled(page):
  page.wait_for_function('window.__pending === 0');page.wait_for_timeout(40)
 def select(page,control,value):
  page.locator('#'+control).select_option(value);settled(page)
 page=new_page(1)
 # RC4: prioritize score -> disclosure arrow -> title. Secondary actions are
 # at the right, never a long View content button before the title.
 def layout_box(selector):
  return page.locator(selector).first.bounding_box()
 date,counts=layout_box('.review-day-title strong'),layout_box('.review-day-title > span')
 assert date and counts and counts['x'] >= date['x']+date['width']-2 and abs(counts['y']-date['y'])<17,(date,counts)
 score,arrow,title,read=(layout_box('.review-item .review-score'),layout_box('.review-item [data-action="toggle-item"]'),layout_box('.review-item-title'),layout_box('.review-item [data-action="read"]'))
 assert score and arrow and title and read,(score,arrow,title,read)
 assert score['x']+score['width'] <= arrow['x']+1,(score,arrow)
 assert arrow['x']+arrow['width'] <= title['x']+2,(arrow,title)
 assert title['x']+title['width'] <= read['x']+2,(title,read)
 assert abs(score['y']-arrow['y'])<12 and abs(arrow['y']-title['y'])<12,(score,arrow,title)
 toggle=page.locator('.review-item [data-action="toggle-item"]').first
 assert toggle.get_attribute('aria-label')=='View content'
 assert toggle.get_attribute('aria-expanded')=='false'
 assert 'View content' not in page.locator('.review-card-toolbar').first.inner_text()
 audio,slider=layout_box('#review-audio-disclosure > summary'),layout_box('.reader-text-size')
 assert audio and slider and slider['x']>=audio['x']+audio['width']-1 and abs(slider['y']-audio['y'])<20,(audio,slider)
 overview=page.locator('#review-result-status').inner_text()
 assert overview.endswith('– 2026-08-17 – 2026-08-18'),overview
 assert not page.locator('#review-active-filters').inner_text().count('2026-08-17')
 checks.append('RC4 desktop: AI score, icon toggle, title, trailing actions; no extra visible View content label')
 # The disclosure remains operable without a mouse and preserves its
 # accessible name when the complete day is rerendered.
 keyboard=page.locator('.review-item [data-action="toggle-item"]').first
 keyboard.focus();keyboard.press('Enter');settled(page)
 keyboard=page.locator('.review-item [data-action="toggle-item"]').first
 assert keyboard.get_attribute('aria-expanded')=='true'
 assert keyboard.get_attribute('aria-label')=='Hide content'
 keyboard.focus();keyboard.press('Space');settled(page)
 keyboard=page.locator('.review-item [data-action="toggle-item"]').first
 assert keyboard.get_attribute('aria-expanded')=='false'
 assert keyboard.get_attribute('aria-label')=='View content'
 if page.locator('.review-item-title[href]').count():
  assert page.locator('.review-item-title[href]').first.get_attribute('target')=='_blank'
 checks.append('RC4 keyboard Enter/Space toggles accessible disclosure without altering title external navigation')
 assert page.locator('.review-item').count()==25
 assert page.locator('#review-preset').input_value()=='current-week'
 assert page.locator('.review-rationale-preview').count()>0
 card=page.locator('.review-item').first;identifier=card.get_attribute('data-item-id')
 card.locator('[data-action="toggle-item"]').click();settled(page)
 assert card.locator('[data-action="toggle-item"]').get_attribute('aria-label') == 'Hide content'
 assert card.locator('[data-action="toggle-item"]').get_attribute('aria-expanded') == 'true'
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
 for preset in ['current-week','current-month','last-5-days','best','best-unread','best-read','today','awaiting-ai','starred','everything']:
  select(page,'review-preset',preset)
  assert page.locator('#review-preset').input_value()==preset
  assert not page.locator('.review-error-page').count(),preset
 checks.append('all ten presets executed with SQL-backed transport fixture')
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
 assert 'Check feeds of YouTube' in page.locator('#scope-refresh-button').inner_text()
 assert 'Update summaries of YouTube' in page.locator('#scope-update-button').inner_text()
 assert page.locator('.review-item').count()==10
 page.locator('#summary-ai-config > summary').click()
 page.locator('#review-interests summary').click();settled(page)
 page.wait_for_function('document.querySelector("#review-interests-text").placeholder.includes("Global")')
 page.locator('#review-interests-text').fill('Prefer formal logic tutorials.')
 page.locator('#review-interests-save').click();settled(page)
 assert 'Saved.' in page.locator('#review-interests-status').inner_text()
 assert page.locator('#summary-ai-config').evaluate('(el)=>el.open')
 checks.append('source interest editor inside scope configuration persists SQLite preference without closing its feedback')
 page.locator('#summary-ai-config > summary').click()
 assert not page.locator('#review-audio-disclosure').evaluate('(el)=>el.open')
 assert page.locator('#review-result-status').evaluate('(el)=>Boolean(el.closest(".review-heading-title"))')
 checks.append('audio hidden by default; counts and date chips next to source title')
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
 # Compact expanded controls share the same toolbar as the score/content actions.
 assert page.locator('[data-action="speak-item"]').first.evaluate('(el)=>Boolean(el.closest(".review-card-toolbar"))')
 page.locator('[data-action="speak-item"]').first.click();page.wait_for_timeout(100)
 assert page.locator('#review-audio-disclosure').evaluate('(el)=>el.open')
 assert page.evaluate('window.__speech.utterances.length')==1
 page.locator('#reader-pause').click()
 assert page.locator('#review-audio-disclosure').evaluate('(el)=>el.open')
 assert page.locator('#reader-pause').inner_text()=='Resume'
 page.locator('#reader-pause').click()
 page.locator('#reader-hide-audio').click();page.wait_for_timeout(50)
 assert not page.locator('#review-audio-disclosure').evaluate('(el)=>el.open')
 assert page.evaluate('window.__speech.cancelled')==1
 checks.append('read aloud opens controls; pause/resume do not close them; hiding stops speech')
 before=page.locator('.review-item-title').first.evaluate('(el)=>getComputedStyle(el).fontSize')
 page.locator('#reader-font-size').fill('22');page.locator('#reader-font-size').dispatch_event('input');page.locator('#reader-font-size').dispatch_event('change');settled(page)
 assert page.locator('.review-item-title').first.evaluate('(el)=>getComputedStyle(el).fontSize')=='22px',before
 assert page.locator('.review-detail-section .review-markdown').first.evaluate('(el)=>getComputedStyle(el).fontSize')=='22px'
 assert configuration['ui']['item_font_size']==22 and configuration['ui']['summary_font_size']==22
 assert page.locator('[data-config-path="ui.item_font_size"]').input_value()=='22', (errors,page.locator('[data-config-path="ui.item_font_size"]').input_value(),page.locator("#reader-font-status").inner_text())
 checks.append('text slider changes title/content computed sizes and persists both values; general settings synchronized')
 page.locator('#reader-font-size').fill('16');page.locator('#reader-font-size').dispatch_event('input');page.locator('#reader-font-size').dispatch_event('change');settled(page)
 page.locator('#review-audio-disclosure > summary').click()
 page.locator('[data-settings-open="settings-reader-audio"]').click()
 assert page.locator('#settings-dialog').evaluate('(el)=>el.open')
 assert page.locator('#settings-reader-audio').is_visible()
 for key in reader_options(configuration['ui']):
  assert page.locator(f'[data-config-path="ui.{key}"]').count()==1,key
 page.locator('#settings-close-button').click()
 checks.append('all new reader/audio preferences accessible from general Settings; audio shortcut opens correct panel')
 page.screenshot(path=str(LOG/'review-wide.png'),full_page=False)
 # Overlong titles must wrap inside the title column rather than push icon
 # controls outside the viewport or create an extra actions-only line.
 long_title='A comprehensive theoretical investigation into logic based learning and reasoning under imprecise uncertainty with verified constraints and multiple nested concept explanations'
 page.locator('.review-item-title').first.evaluate('(el,t)=>{el.textContent=t}',long_title)
 for width in (320, 375, 430, 620, 768, 900, 1200, 1500):
  page.set_viewport_size({'width':width,'height':900})
  page.wait_for_timeout(30)
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),f'horizontal page overflow at {width}px'
  assert page.locator('.review-item-title').first.is_visible(),f'title inaccessible at {width}px'
  if width >= 1200:
   score,arrow,title,read=(layout_box('.review-item .review-score'),layout_box('.review-item [data-action="toggle-item"]'),layout_box('.review-item-title'),layout_box('.review-item [data-action="read"]'))
   assert score['x']<arrow['x']<title['x']<read['x'],(width,score,arrow,title,read)
  if width <= 430:
   title,score,arrow,meta,read=(layout_box('.review-item-title'),layout_box('.review-item .review-score'),layout_box('.review-item [data-action="toggle-item"]'),layout_box('.review-item-meta'),layout_box('.review-item [data-action="read"]'))
   assert score['x']<arrow['x']<title['x'],(width,score,arrow,title)
   assert meta and read and abs(meta['y']-read['y'])<25,(width,meta,read)
 page.set_viewport_size({'width':430,'height':900});page.screenshot(path=str(LOG/'review-narrow.png'),full_page=False)
 # RC4 narrow layout keeps score/toggle/title together and actions beside metadata;
 # the text-size slider stays immediately to the right of Audio reading.
 audio,slider=layout_box('#review-audio-disclosure > summary'),layout_box('.reader-text-size')
 assert audio and slider and slider['x']>=audio['x']+audio['width']-1 and abs(slider['y']-audio['y'])<20,(audio,slider)
 assert page.locator('.review-item-title').first.is_visible()
 assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
 checks.append('RC4 narrow layout: score/disclosure/title grouped, actions beside metadata, no overflow')
 for selector in ['.review-heading-title','.review-heading-actions','#scope-refresh-button','#scope-update-button','#review-audio-disclosure','#reader-font-size']:
  box=page.locator(selector).bounding_box()
  assert box and box['x']>=-1 and box['x']+box['width']<=431,(selector,box)
 assert page.locator('.review-heading').bounding_box()['height']<310
 checks.append('wide and narrow layouts captured; every header control fits 430px viewport')
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
 for group,expected in [(60,'current-month'),(61,'everything')]:
  page.close();page=new_page(group)
  assert page.locator('#review-preset').input_value()==expected
  assert page.locator('.review-item').count()==1
  checks.append(f'group {group}: adaptive {expected} displays older-source item immediately')
 # Layout changes and Reset must preserve the scope's adaptive default.
 page.close();page=new_page(60)
 select(page,'review-display-mode','daily')
 assert page.locator('#review-preset').input_value()=='current-month'
 select(page,'review-preset','everything')
 page.locator('#review-advanced > summary').click();page.locator('#review-reset').click();settled(page)
 assert page.locator('#review-preset').input_value()=='current-month'
 assert page.locator('.review-item').count()==1
 checks.append('layout save then filter Reset preserves monthly source default')
 # Exercise real Settings form serialization, numeric bounds, save and reload.
 page.locator('#settings-button').click()
 page.locator('[data-settings-target="settings-reader-audio"]').click()
 page.locator('[data-config-path="ui.review_default_view"]').select_option('current-month')
 page.locator('[data-config-path="ui.review_page_size"]').select_option('50')
 page.locator('[data-config-path="ui.tts_volume"]').fill('0.4')
 page.locator('[data-config-path="ui.tts_rate"]').fill('0.9')
 page.locator('[data-config-path="ui.tts_chunk_chars"]').fill('800')
 page.locator('[data-config-path="ui.tts_pause_ms"]').fill('160')
 page.locator('[data-config-path="ui.review_compact_cards"]').uncheck()
 page.locator('#save-settings-button').click();settled(page)
 assert configuration['ui']['review_default_view']=='current-month'
 assert configuration['ui']['tts_volume']==0.4 and configuration['ui']['tts_rate']==0.9
 assert configuration['ui']['review_page_size']==50 and not configuration['ui']['review_compact_cards']
 assert page.locator('#save-settings-button').is_disabled()
 page.close();page=new_page(50)
 assert page.locator('#review-preset').input_value()=='current-month'
 assert page.locator('#review-page-size').input_value()=='50'
 assert 'compact-cards' not in page.locator('#review-app').get_attribute('class')
 page.locator('#review-audio-disclosure > summary').click();page.locator('#review-speak-titles').click()
 assert page.evaluate('window.__speech.utterances[0].volume')==0.4
 assert page.evaluate('window.__speech.utterances[0].rate')==0.9
 page.locator('#reader-stop').click()
 checks.append('general Settings form persists and reloads view, page size, density and voice volume/speed')
 page.close();page=new_page(50,blocked_speech=True)
 page.locator('#review-audio-disclosure > summary').click();page.locator('#review-speak-titles').click()
 assert 'No local system voice' in page.locator('#reader-speech-status').inner_text()
 select(page,'review-preset','everything');assert page.locator('.review-item').count()==10
 page.locator('#settings-button').click();assert page.locator('#settings-dialog').evaluate('(el)=>el.open')
 checks.append('blocked speech API and browser storage leave filters/settings usable with explicit unavailable audio status')
 # Settings switches remove optional controls, rather than leaving dead actions.
 configuration['ui'].update(audio_reading_enabled=False,article_theatre_enabled=False,youtube_embed_enabled=False,review_show_stats=False,review_show_rationale=False,review_show_tags=False)
 page.close();page=new_page(50)
 assert not page.locator('#review-audio-disclosure').is_visible()
 assert page.locator('[data-action="play-video"]').count()==0
 page.locator('[data-action="toggle-item"]').first.click();settled(page)
 assert page.locator('[data-action="speak-item"]').count()==0
 assert page.locator('[data-action="article-theatre"]').count()==0
 assert not page.locator('.review-scope-meta').is_visible()
 checks.append('settings-disable states remove speech, embeds, stats and item controls')
 assert not errors, errors
 browser.close()
(LOG/'browser-result.json').write_text(json.dumps({'passed':checks,'errors':errors,'api_requests':len(requests),'transport':'local binding to actual SQLite handlers; Flask HTTP not exercised'},indent=2))
print(json.dumps({'passed':checks,'errors':errors,'api_requests':len(requests)},indent=2))
