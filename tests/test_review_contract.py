from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from rss_reader.review import (
    _AI_STATES,
    _DECISIONS,
    _PAGE_SIZES,
    _PRESETS,
    _READ_STATES,
    _SAVED_STATES,
    _SORTS,
)


ROOT = Path(__file__).resolve().parents[1]


class _Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_select = ""
        self.selects: dict[str, list[str]] = {}
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        identifier = values.get("id")
        if identifier:
            self.ids.add(identifier)
        if tag == "select" and identifier:
            self.current_select = identifier
            self.selects.setdefault(identifier, [])
        elif tag == "option" and self.current_select:
            self.selects[self.current_select].append(str(values.get("value") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.current_select = ""


def test_filter_toolbar_frontend_backend_contract_is_complete():
    template = (ROOT / "rss_reader" / "templates" / "index.html").read_text("utf-8")
    script = (ROOT / "rss_reader" / "static" / "review.js").read_text("utf-8")
    state_script = (ROOT / "rss_reader" / "static" / "review-state.js").read_text("utf-8")
    stylesheet = (ROOT / "rss_reader" / "static" / "app.css").read_text("utf-8")
    service_worker = (ROOT / "rss_reader" / "static" / "service-worker.js").read_text("utf-8")
    controls = _Controls()
    controls.feed(template)

    assert set(controls.selects["review-display-mode"]) == {"daily", "direct"}
    assert set(controls.selects["review-preset"]) == _PRESETS
    assert set(controls.selects["review-read"]) == _READ_STATES
    assert set(controls.selects["review-saved"]) == _SAVED_STATES
    assert set(controls.selects["review-ai-state"]) == _AI_STATES
    assert set(controls.selects["review-decision"]) == _DECISIONS
    assert set(controls.selects["review-sort"]) == _SORTS
    assert {int(value) for value in controls.selects["review-page-size"]} == _PAGE_SIZES
    assert {
        "review-display-mode", "review-search", "review-min-ai", "review-source",
        "review-from", "review-to",
    } <= controls.ids

    mappings = {
        "q": "review-search",
        "read": "review-read",
        "saved": "review-saved",
        "ai": "review-ai-state",
        "decision": "review-decision",
        "min_ai": "review-min-ai",
        "source": "review-source",
        "from": "review-from",
        "to": "review-to",
        "sort": "review-sort",
        "page_size": "review-page-size",
    }
    for field, identifier in mappings.items():
        assert identifier in controls.ids
        assert f"'{field}'" in state_script
        assert f"'{identifier}'" in script


    assert "review-preference-group-id" in template
    assert "review-display-mode" in template
    assert "review-default-preset" in template
    assert "review-default-sort" in template
    assert "DIRECT_LOAD_MARGIN = 600" in script
    assert "DIRECT_LOAD_CONCURRENCY = 3" in script
    assert "getBoundingClientRect" in script
    assert "daysRoot?.addEventListener('scroll', scheduleDirectLoads" in script
    assert "details.dataset.openState" in script
    assert "preferenceGroupId" in script
    assert "review_display_mode" in script
    assert "rawFilters.preset = State.defaultPreset(options)" in script
    assert "State.defaultPreset({ ...options, displayMode: state.displayMode })" in script
    assert "Changing the day layout must never silently hide or reveal items" in script
    assert "normalizeDisplayMode" in state_script
    assert 'class="review-item-title"' in script
    assert 'target="_blank"' in script
    assert 'data-action="open-item-link"' in script
    assert "window.setTimeout(() => changeRead(itemId), 0)" in script
    assert 'class="review-content-toggle"' in script
    assert "State.contentToggleLabel(open)" in script
    assert ".review-content-toggle" in stylesheet
    assert ".review-source-links" not in stylesheet
    assert "Stored item summary" not in script
    # RC5 retains the score/disclosure/title order; actions remain separately
    # grouped. A compact triangle replaces visible text without losing an
    # accessible name, tooltip, focusability or aria-expanded state.
    start = script.index('  function renderItem(item) {')
    end = script.index('  function renderDayItems(', start)
    card = script[start:end]
    assert card.index('<div class="review-title-line">') < card.index('<div class="review-item-actions"')
    assert card.index('review-score-${escapeHTML') < card.index('class="review-content-toggle"')
    assert card.index('class="review-content-toggle"') < card.index('${title}')
    assert 'aria-label="${escapeHTML(toggleLabel)}"' in card
    assert 'aria-expanded="${open}"' in card
    assert 'aria-controls="review-item-details-${item.id}"' in card
    assert '<span class="review-item-caret" aria-hidden="true"></span>' in card
    assert 'State.contentToggleLabel(open)' in card
    assert 'title="${escapeHTML(toggleLabel)}"' in card
    assert 'role="group" aria-label="Item actions"' in card
    assert 'grid-template-areas: "title actions" "meta meta"' in stylesheet
    assert 'grid-template-areas: "title title" "meta actions"' in stylesheet
    # RC5: visual Day view label hides the current selection but the real native
    # options remain available; statistics have their own responsive grid row.
    assert '<span aria-hidden="true">Day view</span>' in template
    assert '<select id="review-display-mode" aria-label="Day view:' in template
    assert 'AI for {{ scope_title }}</summary>' in template
    assert 'aria-label="Audio reading for {{ scope_title }}"' in template
    assert 'id="review-reader-utilities"' not in template  # Existing class/IDs unchanged.
    assert 'grid-template-areas: "title controls" "status status"' in stylesheet
    assert 'grid-area: status;' in stylesheet
    assert '.review-layout-control select {' in stylesheet
    assert 'opacity: 0;' in stylesheet
    assert '.review-card-toolbar .review-title-line {' in stylesheet
    assert 'align-items: center;' in stylesheet
    assert "block.items" not in template
    assert "v='0.25.0-rc9'" in template
    assert "service-worker.js?v=0.25.0-rc9" in (
        ROOT / "rss_reader" / "static" / "app.js"
    ).read_text("utf-8")
    assert "distillfeed-v250-rc9" in service_worker
    assert "?v=0.25.0-rc9" in service_worker
