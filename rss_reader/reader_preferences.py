"""Validated reader preferences; no stored subscription or database migration."""
from __future__ import annotations
import math
from typing import Any, Mapping

VIEW_OPTIONS = (
    "adaptive", "current-week", "current-month", "last-5-days", "today",
    "best", "best-unread", "best-read", "awaiting-ai", "starred", "everything",
)
BOOLEAN_OPTIONS = (
    "review_compact_cards", "review_show_rationale", "review_show_tags",
    "review_show_stats", "audio_reading_enabled", "audio_controls_expanded",
    "youtube_embed_enabled", "article_theatre_enabled", "reader_theatre_wide",
)
RANGES = {
    "review_rationale_lines": (1, 4, int),
    "tts_volume": (0.0, 1.0, float),
    "tts_rate": (0.5, 2.0, float),
    "tts_chunk_chars": (180, 2400, int),
    "tts_pause_ms": (0, 1000, int),
}

def validate_reader_preferences(ui: Mapping[str, Any]) -> None:
    if ui.get("review_default_view", "adaptive") not in VIEW_OPTIONS:
        raise ValueError("ui.review_default_view must be a supported reader view")
    if ui.get("review_page_size", 25) not in (10, 25, 50):
        raise ValueError("ui.review_page_size must be 10, 25 or 50")
    for name in BOOLEAN_OPTIONS:
        if name in ui and not isinstance(ui[name], bool):
            raise ValueError(f"ui.{name} must be true or false")
    for name, (low, high, kind) in RANGES.items():
        if name not in ui:
            continue
        value = ui[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"ui.{name} must be a number between {low} and {high}")
        if not math.isfinite(value) or not low <= value <= high or (kind is int and int(value) != value):
            raise ValueError(f"ui.{name} must be between {low} and {high}")


def reader_options(ui: Mapping[str, Any]) -> dict[str, Any]:
    """Only public presentation settings are serialized into the reader."""
    keys = (*BOOLEAN_OPTIONS, *RANGES, "review_default_view", "review_page_size")
    return {key: ui[key] for key in keys if key in ui}
