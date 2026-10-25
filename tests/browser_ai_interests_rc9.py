"""RC9 browser regression for ordinary-source AI configuration: run with Python.

Uses the packaged Jinja/JS/CSS with an intercepted API and no live external calls.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from bs4 import BeautifulSoup
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'rss_reader/templates'
STATIC = ROOT / 'rss_reader/static'
OUT = Path(os.environ.get('RC9_SCREENSHOTS', '/mnt/data/rc9_work'))
OUT.mkdir(parents=True, exist_ok=True)

env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=ChainableUndefined, autoescape=select_autoescape(['html']))
env.filters['summary_markup'] = lambda value: value
env.globals.update(csrf_token=lambda: 'fixture-csrf', url_for=lambda endpoint, **kw: '/static/' + str(kw.get('filename', endpoint)))
for template in env.list_templates():
    env.get_template(template)


def html(group_id: int = 50, *, arxiv: bool = False) -> str:
    data = dict(
        ui=dict(subscription_font_size=16, item_font_size=14, summary_font_size=17,
                offline_cache_enabled=False, dark_mode=False, review_compact_cards=True),
        selected_group_id=group_id, selected_feed_id=None,
        review_preference_group_id=group_id, review_display_mode='daily',
        review_default_preset='current-week' if arxiv else 'everything',
        review_default_sort='ai' if arxiv else 'date',
        scope_title='arXiv Digest' if arxiv else 'YouTube',
        scope_ai_mode='arxiv' if arxiv else 'automatic',
        scope_ai_own_mode='automatic', scope_ai_priority='normal',
        scope_summary_interval_hours=0, scope_summary_item_budget=0,
        is_arxiv_scope=arxiv, groups=[], feeds=[], tree=[], locks={},
        settings_sections=[], system_notices=[], settings_ai_groups=[],
        settings_ai_feeds=[], summary_minimum_relevance=70,
        app_mode='development', auto_refresh=False, refresh_interval_minutes=30,
        arxiv_available=True,
    )
    soup = BeautifulSoup(env.get_template('index.html').render(**data), 'html.parser')
    for tag in soup.find_all('script'):
        if tag.get('src'):
            tag.decompose()
    for tag in soup.find_all('link'):
        if tag.get('rel') == ['stylesheet']:
            tag.decompose()
    return str(soup)


with sync_playwright() as browser_tools:
    browser = browser_tools.chromium.launch(executable_path=os.environ.get('CHROMIUM', '/usr/bin/chromium'),
                                           headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
    shared = {'text': 'I follow possibilistic learning, rule systems, and logical reasoning.',
              'fail_next': False, 'get_count': 0, 'post_count': 0}
    errors = []
    assertions = 0

    def check(condition: bool, message: str) -> None:
        global assertions
        assert condition, message
        assertions += 1

    def bridge(_source, path: str, method: str, body: str | None):
        path = str(path)
        if path.startswith('/api/review/interests'):
            if method == 'POST':
                shared['text'] = json.loads(body or '{}').get('value', '')
                shared['post_count'] += 1
                return {'status': 200, 'body': {'value': shared['text']}}
            shared['get_count'] += 1
            if shared['fail_next']:
                shared['fail_next'] = False
                return {'status': 503, 'body': {'error': 'Temporary API failure'}}
            return {'status': 200, 'body': {
                'value': shared['text'],
                'effective': shared['text'] or 'Inherited guidance from the parent/group.',
                'inherited': not bool(shared['text']),
            }}
        if path.startswith('/api/review/days'):
            return {'status': 200, 'body': {
                'days': [], 'sources': [], 'counts': {'total': 0, 'unread': 0},
                'default_open_day': None, 'scope': {'review_display_mode': 'daily'},
            }}
        if path == '/api/weather':
            return {'status': 200, 'body': {'enabled': False}}
        return {'status': 200, 'body': {'status': 'ok'}}

    def load(width: int, *, dark: bool = False):
        page = browser.new_page(viewport={'width': width, 'height': 800})
        page.on('pageerror', lambda error: errors.append(f'{width}: {error}'))
        page.set_content(html())
        if dark:
            page.locator('body').evaluate("node => node.classList.add('dark-mode')")
        page.add_style_tag(content=(STATIC / 'app.css').read_text())
        page.expose_binding('__rc9_bridge', bridge)
        page.evaluate('''() => {
          window.fetch = async (url, options = {}) => {
            const result = await window.__rc9_bridge(String(url), options.method || 'GET', options.body || null);
            return new Response(JSON.stringify(result.body), {
              status: result.status, headers: { 'Content-Type': 'application/json' },
            });
          };
        }''')
        for fname in ('review-state.js', 'reader-tools.js', 'review.js', 'app.js'):
            page.add_script_tag(content=(STATIC / fname).read_text())
        page.locator('#summary-ai-config > summary').click()
        page.locator('#review-interests-status').wait_for(state='visible')
        page.wait_for_function('!document.querySelector("#review-interests-text").disabled')
        return page

    for width in (320, 375, 430, 620, 621, 764, 1024, 1440):
        page = load(width)
        section = page.locator('#review-interests')
        check(section.is_visible(), f'{width}: interests section not visible')
        check(section.locator('h3').inner_text() == 'Topics and interests', f'{width}: heading')
        check(section.locator('summary').count() == 0, f'{width}: nested disclosure still exists')
        check(page.locator('#review-interests-text').input_value() == shared['text'], f'{width}: saved interests not visible')
        check('custom profile' in page.locator('#review-interests-status').inner_text(), f'{width}: own profile status')
        check(page.locator('#review-interests-retry').is_hidden(), f'{width}: spurious retry')
        dimensions = page.evaluate('''() => {
          const popup = document.querySelector('#summary-ai-config > .scope-config-popover');
          const r = popup.getBoundingClientRect();
          const selectors = ['#review-interests','#review-interests-text','#review-interests-save',
              '#summary-ai-config-save','#review-interests-status','.scope-global-settings',
              '[data-summary-source-field="ai_mode"]'];
          return {popup:{left:r.left,right:r.right,width:r.width,clientWidth:popup.clientWidth,scrollWidth:popup.scrollWidth},
             children:selectors.map(selector=>{const el=popup.querySelector(selector);const a=el.getBoundingClientRect();return {selector,left:a.left,right:a.right,width:a.width};}),
             viewport:document.documentElement.clientWidth,
             documentWidth:document.documentElement.scrollWidth};
        }''')
        check(dimensions['popup']['left'] >= -1 and dimensions['popup']['right'] <= width+1,
              f'{width}: popover outside viewport: {dimensions}')
        check(dimensions['popup']['scrollWidth'] <= dimensions['popup']['clientWidth'] + 1,
              f'{width}: popover horizontal scrolling: {dimensions}')
        for child in dimensions['children']:
            check(child['left'] >= dimensions['popup']['left'] - 1 and child['right'] <= dimensions['popup']['right'] + 1,
                  f'{width}: {child["selector"]} overflowed: {dimensions}')
        check(dimensions['documentWidth'] <= width + 2, f'{width}: document overflow')
        if width == 1440:
            page.screenshot(path=str(OUT / 'rc9-ai-interests-desktop.png'))
        if width == 375:
            page.locator('#review-interests-text').click(timeout=2500)
            page.screenshot(path=str(OUT / 'rc9-ai-interests-mobile.png'))
        page.close()

    page = load(764)
    old_get = shared['get_count']
    check(page.locator('#review-interests-save').is_enabled(), 'save disabled after successful load')
    page.locator('#review-interests-text').fill('Updated long-term topics: formal logic, causal learning, and uncertainty.')
    page.locator('#review-interests-save').click()
    page.wait_for_function('document.querySelector("#review-interests-status").textContent.startsWith("Saved.")')
    check(shared['post_count'] == 1 and shared['text'].startswith('Updated long-term'), 'save not persisted')
    check(page.locator('#summary-ai-config').get_attribute('open') is not None, 'menu closed after save')
    page.locator('#summary-ai-config > summary').click()
    page.locator('#summary-ai-config > summary').click()
    check(shared['get_count'] == old_get, 'reopening same menu refetched interests')
    check(page.locator('#review-interests-text').input_value() == shared['text'], 'saved text vanished')
    page.close()
    page = load(764)
    check(page.locator('#review-interests-text').input_value() == shared['text'], 'saved value lost on new page')
    page.close()

    shared['text'] = ''
    page = load(430)
    check(page.locator('#review-interests-text').input_value() == '', 'inherited value must remain empty')
    check('Inherited guidance' in page.locator('#review-interests-text').get_attribute('placeholder'), 'inherited guidance not shown')
    check('Inheriting' in page.locator('#review-interests-status').inner_text(), 'inheritance not communicated')
    page.close()

    shared['fail_next'] = True
    # In failure mode, avoid load() waiting for input to be enabled.
    page = browser.new_page(viewport={'width': 764, 'height': 800})
    page.on('pageerror', lambda error: errors.append(f'retry: {error}'))
    page.set_content(html())
    page.add_style_tag(content=(STATIC / 'app.css').read_text())
    page.expose_binding('__rc9_bridge', bridge)
    page.evaluate('''() => {window.fetch = async (url, options={}) => {
      const result=await window.__rc9_bridge(String(url), options.method || 'GET', options.body || null);
      return new Response(JSON.stringify(result.body), {status:result.status, headers:{'Content-Type':'application/json'}});
    };}''')
    for fname in ('review-state.js','reader-tools.js','review.js','app.js'):
        page.add_script_tag(content=(STATIC/fname).read_text())
    page.locator('#summary-ai-config > summary').click()
    page.wait_for_function('document.querySelector("#review-interests-status").textContent.includes("Temporary API failure")')
    check(page.locator('#review-interests-save').is_disabled(), 'failure enabled destructive save')
    check(page.locator('#review-interests-retry').is_visible(), 'retry absent after load failure')
    page.locator('#review-interests-retry').click()
    page.wait_for_function('!document.querySelector("#review-interests-text").disabled')
    check(page.locator('#review-interests-retry').is_hidden(), 'retry remained visible after recovery')
    page.close()

    # Dark mode must keep interests interactive and avoid a blocking backdrop.
    page = load(375, dark=True)
    check(page.locator('#review-interests').is_visible(), 'dark interests unavailable')
    check(page.locator('#popup-backdrop').is_hidden(), 'dark mobile backdrop blocks input')
    page.locator('#review-interests-text').click(timeout=2500)
    page.close()

    # Specialist arXiv popup must also be clickable on mobile (no overlay).
    page = browser.new_page(viewport={'width': 375, 'height': 800})
    page.on('pageerror', lambda error: errors.append(f'arxiv: {error}'))
    page.set_content(html(1, arxiv=True))
    page.add_style_tag(content=(STATIC / 'app.css').read_text())
    page.expose_binding('__rc9_bridge', bridge)
    page.evaluate("""() => {window.fetch = async (url, options={}) => {
      const result=await window.__rc9_bridge(String(url), options.method || 'GET', options.body || null);
      return new Response(JSON.stringify(result.body), {status:result.status, headers:{'Content-Type':'application/json'}});
    };}""")
    for fname in ('review-state.js', 'reader-tools.js', 'review.js', 'app.js'):
        page.add_script_tag(content=(STATIC / fname).read_text())
    page.locator('#arxiv-scope-config > summary').click()
    check(page.locator('#popup-backdrop').is_hidden(), 'arXiv settings blocked by mobile backdrop')
    page.locator('#arxiv-scope-config > .arxiv-config-popover > button').click(timeout=2500)
    check(page.locator('#settings-dialog').evaluate('(node)=>node.open'), 'arXiv settings button unresponsive')
    page.close()

    check('id="review-interests"' in html(), 'ordinary scope editor missing')
    check('id="review-interests"' not in html(1, arxiv=True), 'ordinary interests leaked into arXiv')
    check(not errors, f'Browser JS exceptions: {errors}')
    print(f'RC9 real template/CSS/JS browser checks passed: {assertions} assertions, 8 viewport widths, save/reload/retry; errors={errors}')
    browser.close()
