import copy

import pytest

from rss_reader.config import DEFAULTS, Config, dump_toml, load_config, save_config, validate_config


def test_every_default_option_round_trips_through_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(copy.deepcopy(DEFAULTS)), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.data == DEFAULTS
    save_config(Config(path, copy.deepcopy(loaded.data)))
    assert load_config(path).data == DEFAULTS
    assert path.with_suffix(".toml.bak").is_file()


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("app", "database_path", "", "cannot be empty"),
        ("app", "mode", "staging", "local, development, or production"),
        ("app", "working_opml_path", "", "cannot be empty"),
        ("app", "host", "", "non-empty"),
        ("app", "port", 0, "between 1 and 65535"),
        ("app", "log_level", "LOUD", "logging level"),
        ("app", "refresh_interval_minutes", 0, "positive"),
        ("app", "summary_language", "Esperanto", "English or French"),
        ("app", "retention_days", -1, "cannot be negative"),
        ("app", "interest_profile", "x" * 2001, "2000"),
        ("feeds", "timeout_seconds", 0, "positive"),
        ("feeds", "max_response_bytes", 0, "positive"),
        ("feeds", "max_entries_per_feed_update", 0, "positive"),
        ("feeds", "initial_import_max_age_days", -1, "cannot be negative"),
        ("feeds", "max_workers", 0, "positive"),
        ("feeds", "retry_base_minutes", 0, "positive"),
        ("feeds", "user_agent", "bad\nheader", "single non-empty line"),
        ("llm", "model", "", "non-empty"),
        ("llm", "automatic_cooldown_minutes", -1, "cannot be negative"),
        ("llm", "candidate_max_age_days", -1, "cannot be negative"),
        ("llm", "max_entries_total", 0, "positive"),
        ("llm", "output_token_safety_margin", -1, "cannot be negative"),
        ("llm", "reasoning_effort", "extreme", "Unsupported"),
        ("auth", "password_env", "NOT-AN-ENV", "environment-variable"),
        ("weather", "language", "German", "English or French"),
        ("weather", "latitude", 91, "between -90 and 90"),
        ("weather", "longitude", -181, "between -180 and 180"),
        ("weather", "location_name", "", "cannot be empty"),
        ("weather", "timezone", "Mars/Olympus", "IANA timezone"),
        ("weather", "refresh_minutes", 0, "positive"),
        ("ui", "subscription_font_size", 9, "between 10 and 24"),
        ("ui", "item_font_size", 25, "between 10 and 24"),
        ("ui", "summary_font_size", 9, "between 10 and 24"),
        ("notifications.ntfy", "server_url", "ftp://example.test", "HTTP or HTTPS"),
        ("notifications.ntfy", "minimum_relevance", 101, "between 0 and 100"),
        ("notifications.ntfy", "max_items_per_summary", 0, "between 1 and 20"),
        ("notifications.ntfy", "priority", "urgent", "priority is invalid"),
        ("notifications.ntfy", "timeout_seconds", 61, "between 1 and 60"),
        ("notifications.ntfy", "token_env", "BAD-TOKEN", "environment-variable"),
    ],
)
def test_invalid_option_domains_are_rejected(section, key, value, message):
    values = copy.deepcopy(DEFAULTS)
    target = values
    for part in section.split("."):
        target = target[part]
    target[key] = value
    with pytest.raises(ValueError, match=message):
        validate_config(values)


def test_enabled_ntfy_requires_a_safe_topic():
    values = copy.deepcopy(DEFAULTS)
    values["notifications"]["ntfy"]["enabled"] = True
    with pytest.raises(ValueError, match="topic is required"):
        validate_config(values)
    values["notifications"]["ntfy"]["topic"] = "spaces are not valid"
    with pytest.raises(ValueError, match="letters, numbers"):
        validate_config(values)


def test_cross_option_constraints_and_pricing_are_rejected():
    values = copy.deepcopy(DEFAULTS)
    values["feeds"]["max_workers_per_host"] = values["feeds"]["max_workers"] + 1
    with pytest.raises(ValueError, match="cannot exceed"):
        validate_config(values)

    values = copy.deepcopy(DEFAULTS)
    values["llm"]["max_entries_total"] = 201
    with pytest.raises(ValueError, match="200 items"):
        validate_config(values)

    values = copy.deepcopy(DEFAULTS)
    values["llm"]["max_entries_total"] = 4
    values["llm"]["max_entries_per_feed"] = 5
    with pytest.raises(ValueError, match="cannot exceed max_entries_total"):
        validate_config(values)

    values = copy.deepcopy(DEFAULTS)
    values["app"].update({"mode": "production", "debug": True})
    with pytest.raises(ValueError, match="debug must be false"):
        validate_config(values)


def test_systemd_example_uses_writable_absolute_state_paths():
    config = load_config("deployment/config.systemd.toml.example")
    assert str(config.database_path) == "/var/lib/rssreader/reader.sqlite3"
    assert str(config.working_opml_path) == "/var/lib/rssreader/subscriptions.opml"


def test_remaining_cross_option_constraints_are_rejected():
    values = copy.deepcopy(DEFAULTS)
    values["feeds"]["initial_import_max_entries_per_feed"] = 201
    with pytest.raises(ValueError, match="update limit"):
        validate_config(values)

    values = copy.deepcopy(DEFAULTS)
    values["llm"]["output_token_safety_margin"] = values["llm"]["max_output_tokens"]
    with pytest.raises(ValueError, match="lower than"):
        validate_config(values)

    values = copy.deepcopy(DEFAULTS)
    values["llm"]["max_output_tokens"] = (
        values["llm"]["output_token_safety_margin"]
        + values["llm"]["estimated_output_tokens_per_item"]
        + values["llm"]["estimated_output_tokens_per_group"]
        - 1
    )
    with pytest.raises(ValueError, match="too small"):
        validate_config(values)

    values = copy.deepcopy(DEFAULTS)
    values["llm"]["pricing"]["output"] = float("nan")
    with pytest.raises(ValueError, match="finite non-negative"):
        validate_config(values)
