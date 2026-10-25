"""Regression for the arXiv AI settings flyout's unreadable hover style."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_arxiv_config_menu_keeps_primary_button_contrast_on_hover_and_keyboard_focus():
    css = (ROOT / 'rss_reader/static/app.css').read_text(encoding='utf-8')
    template = (ROOT / 'rss_reader/templates/index.html').read_text(encoding='utf-8')
    assert 'id="arxiv-scope-config"' in template
    assert 'class="scope-config-popover arxiv-config-popover"' in template
    assert 'data-settings-open="settings-arxiv"' in template
    assert '#arxiv-scope-config > .arxiv-config-popover > button:not(:disabled):is(:hover, :focus-visible)' in css
    assert 'background: color-mix(in srgb, var(--accent) 87%, var(--text));' in css
    assert 'color: var(--bg);' in css
    assert '#arxiv-scope-config > .arxiv-config-popover > button:focus-visible' in css
    assert 'outline-offset: 2px;' in css


def test_rc9_stylesheet_and_scripts_are_not_stuck_in_rc8_service_worker_cache():
    template = (ROOT / 'rss_reader/templates/index.html').read_text(encoding='utf-8')
    script = (ROOT / 'rss_reader/static/app.js').read_text(encoding='utf-8')
    worker = (ROOT / 'rss_reader/static/service-worker.js').read_text(encoding='utf-8')
    assert "filename='app.css', v='0.25.0-rc9'" in template
    assert 'service-worker.js?v=0.25.0-rc9' in script
    assert "distillfeed-v250-rc9" in worker
    assert '/static/app.css?v=0.25.0-rc9' in worker
