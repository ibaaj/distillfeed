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
    assert "DIRECT_LOAD_MARGIN = 600" in script
    assert "DIRECT_LOAD_CONCURRENCY = 3" in script
    assert "getBoundingClientRect" in script
    assert "daysRoot?.addEventListener('scroll', scheduleDirectLoads" in script
    assert "details.dataset.openState" in script
    assert "preferenceGroupId" in script
    assert "review_display_mode" in script
    assert "initialDisplayMode === 'direct' ? 'catch-up' : 'best-unread'" in script
    assert "state.displayMode === 'direct' ? 'catch-up' : 'best-unread'" in script
    assert "normalizeDisplayMode" in state_script
    assert "review-source-separator" in script
    assert ".review-source-links" in stylesheet and "gap: 12px" in stylesheet
    assert ".review-source-separator" in stylesheet
    assert "Stored item summary" not in script
    assert "block.items" not in template
    assert "v='0.24.1-final'" in template
    assert "service-worker.js?v=0.24.1-final" in (
        ROOT / "rss_reader" / "static" / "app.js"
    ).read_text("utf-8")
    assert "distillfeed-v241-final" in service_worker
    assert "?v=0.24.1-final" in service_worker
