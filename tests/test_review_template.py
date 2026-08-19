from __future__ import annotations

from pathlib import Path

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]


def test_reader_template_renders_unified_review_shell_without_legacy_panels():
    environment = Environment(
        loader=FileSystemLoader(ROOT / "rss_reader" / "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=ChainableUndefined,
    )
    environment.globals.update(
        csrf_token=lambda: "test-token",
        url_for=lambda endpoint, **values: "/static/" + str(values.get("filename", endpoint)),
    )
    rendered = environment.get_template("index.html").render(
        ui={
            "subscription_font_size": 18,
            "item_font_size": 14,
            "summary_font_size": 17,
            "offline_cache_enabled": False,
            "completion_notifications": False,
            "dark_mode": False,
            "groups_expanded_by_default": False,
        },
        tree=[], groups=[], locks=[], system_notices=[], notification_count=0,
        selected_group_id=1, selected_feed_id=None, is_arxiv_scope=True,
        review_preference_group_id=1, review_display_mode="direct",
        scope_title="arXiv Digest", item_sort_profile="ai", app_mode="development",
        auto_refresh=False, refresh_interval_minutes=30, arxiv_available=True,
        generated_feeds_enabled=False, ungrouped_id=0, scope_pending_items=303,
        scope_pending_days=12, scope_missing_daily_digests=4,
    )

    assert 'id="review-app"' in rendered
    assert 'id="review-day-list"' in rendered
    assert '<meta name="review-display-mode" content="direct">' in rendered
    assert '<option value="direct" selected>Show items directly</option>' in rendered
    assert "Update remaining arXiv" in rendered
    assert "review-state.js" in rendered
    assert "review.js" in rendered
    assert 'id="items-panel"' not in rendered
    assert 'id="summary-panel"' not in rendered
    assert "block.items" not in rendered


def test_ordinary_reader_template_defaults_to_everything_and_newest_first():
    environment = Environment(
        loader=FileSystemLoader(ROOT / "rss_reader" / "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=ChainableUndefined,
    )
    environment.globals.update(
        csrf_token=lambda: "test-token",
        url_for=lambda endpoint, **values: "/static/" + str(values.get("filename", endpoint)),
    )
    rendered = environment.get_template("index.html").render(
        ui={
            "subscription_font_size": 18,
            "item_font_size": 14,
            "summary_font_size": 17,
            "offline_cache_enabled": False,
            "completion_notifications": False,
            "dark_mode": False,
            "groups_expanded_by_default": False,
        },
        tree=[], groups=[], locks=[], system_notices=[], notification_count=0,
        selected_group_id=2, selected_feed_id=None, is_arxiv_scope=False,
        review_preference_group_id=2, review_display_mode="direct",
        review_default_preset="everything", review_default_sort="date",
        scope_title="YouTube", item_sort_profile="date", app_mode="development",
        auto_refresh=False, refresh_interval_minutes=30, arxiv_available=True,
        generated_feeds_enabled=False, ungrouped_id=0, scope_pending_items=0,
        scope_pending_days=0, scope_missing_daily_digests=0,
    )

    assert '<meta name="review-default-preset" content="everything">' in rendered
    assert '<meta name="review-default-sort" content="date">' in rendered
    assert '<option value="everything" selected>Everything</option>' in rendered
    assert '<option value="date" selected>Newest first</option>' in rendered
