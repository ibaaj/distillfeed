from __future__ import annotations

import copy
from pathlib import Path

import pytest

from rss_reader.setup_service import (
    EXPECTED_SETUP_FIELDS,
    SetupValidationError,
    _config_for_draft,
    normalize_setup_payload,
    preset_payload,
    public_review,
)


def _invalid(payload, field: str, *, environment=None):
    with pytest.raises(SetupValidationError) as caught:
        normalize_setup_payload(
            payload,
            environment={} if environment is None else environment,
            check_port=False,
        )
    assert field in caught.value.errors
    return caught.value.errors[field]


def test_demo_profile_is_the_exact_release_contract():
    payload = preset_payload("demo")
    assert set(payload) == EXPECTED_SETUP_FIELDS
    assert payload == {
        "profile": "demo",
        "port": 8081,
        "subscriptions": "starter",
        "language": "English",
        "interest_profile": (
            "Programming, open source, artificial intelligence, science, and technology."
        ),
        "weather_enabled": False,
        "weather_location": "Paris",
        "weather_latitude": 48.8566,
        "weather_longitude": 2.3522,
        "weather_timezone": "Europe/Paris",
        "ai_provider": "openai",
        "model": "gpt-5.4-nano",
        "ollama_url": "http://127.0.0.1:11434/v1/",
        "summary_threshold": 40,
        "summary_window_days": 7,
        "candidate_age_days": 30,
        "review_workload": "balanced",
        "monthly_budget_usd": 2.0,
        "store_openai_key": True,
        "openai_key": "",
        "use_environment_openai_key": False,
        "arxiv_enabled": True,
        "arxiv_categories": "cs.AI",
        "arxiv_lookback_days": 7,
        "arxiv_final_threshold": 25,
        "refresh_on_open": True,
        "background_updates": False,
        "refresh_interval_minutes": 30,
        "auto_summarize": False,
        "ntfy_enabled": False,
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "ntfy_threshold": 85,
        "store_ntfy_token": True,
        "ntfy_token": "",
    }

    payload["openai_key"] = "demo-key"
    draft = normalize_setup_payload(payload, environment={}, check_port=False)
    assert draft.port == 8081
    assert draft.ai_provider == "openai"
    assert draft.summary_threshold == 40
    assert draft.summary_window_days == 7
    assert draft.candidate_age_days == 30
    assert draft.arxiv_categories == ("cs.AI",)
    assert draft.arxiv_lookback_days == 7
    assert draft.background_updates is False
    assert draft.auto_summarize is False
    assert draft.weather_enabled is False
    assert draft.ntfy_enabled is False


def test_recommended_profile_is_safe_fast_path_with_no_credentials_or_paid_work():
    payload = preset_payload("recommended")
    draft = normalize_setup_payload(payload, environment={}, check_port=False)
    assert draft.port == 8080
    assert draft.subscriptions == "starter"
    assert draft.ai_provider == "disabled"
    assert draft.ai_enabled is False
    assert draft.arxiv_enabled is False
    assert draft.weather_enabled is False
    assert draft.ntfy_enabled is False
    assert draft.background_updates is False
    assert draft.auto_summarize is False
    assert draft.openai_key == ""
    assert draft.ntfy_token == ""


def test_profile_names_are_closed_and_return_independent_payloads():
    with pytest.raises(ValueError, match="Unknown setup profile"):
        preset_payload("surprise")
    first = preset_payload("recommended")
    second = preset_payload("recommended")
    first["model"] = "mutated"
    assert second["model"] == "gpt-5.4-nano"
    assert preset_payload("guided")["profile"] == "guided"


def test_review_keeps_four_time_and_score_concepts_visibly_separate(tmp_path):
    payload = preset_payload("demo")
    payload.update({
        "openai_key": "private-value",
        "summary_threshold": 41,
        "summary_window_days": 8,
        "candidate_age_days": 31,
        "arxiv_lookback_days": 9,
        "arxiv_final_threshold": 26,
    })
    draft = normalize_setup_payload(payload, environment={}, check_port=False)
    review = public_review(draft, tmp_path / ".distillfeed" / "instance")

    assert review["ordinary_threshold"] == "41/100"
    assert review["ordinary_evidence_window"] == "Previous 8 day(s)"
    assert review["candidate_age_limit"] == "31 day(s)"
    assert "first retrieval looks back 9 day(s)" in review["arxiv"]
    assert review["access"] == "Only this computer · 127.0.0.1"
    assert review["device_alerts"].endswith("System notices remain inside DistillFeed")
    assert any("contacts no feeds" in guarantee for guarantee in review["guarantees"])
    assert any(
        "AI is used only when you explicitly update a summary" in guarantee
        for guarantee in review["guarantees"]
    )
    assert "private-value" not in repr(review)
    assert "private-value" not in repr(draft)


@pytest.mark.parametrize("budget", [0.0, 3.5])
def test_review_never_promises_manual_only_ai_when_automatic_summaries_are_enabled(
    tmp_path, budget,
):
    payload = preset_payload("demo")
    payload.update({
        "openai_key": "private-value",
        "auto_summarize": True,
        "monthly_budget_usd": budget,
    })
    draft = normalize_setup_payload(payload, environment={}, check_port=False)
    review = public_review(draft, tmp_path / "instance")
    guarantees = " ".join(review["guarantees"])
    assert "automatic feed checks may update summaries and use the provider" in guarantees
    assert "AI is used only when you explicitly update" not in guarantees
    if budget:
        assert "local monthly spending guard applies" in guarantees
    else:
        assert "no local monthly spending limit" in guarantees


def test_validation_rejects_non_object_unknown_and_missing_fields():
    _invalid([], "form")
    unknown = preset_payload("recommended") | {"future_field": "surprise"}
    assert "future_field" in _invalid(unknown, "form")
    missing = preset_payload("recommended")
    del missing["candidate_age_days"]
    assert "candidate_age_days" in _invalid(missing, "form")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("port", 1023, "1024"),
        ("port", 65536, "65535"),
        ("port", True, "whole number"),
        ("port", 8080.5, "whole number"),
        ("summary_threshold", -1, "0"),
        ("summary_threshold", 101, "100"),
        ("summary_window_days", 0, "1"),
        ("summary_window_days", 366, "365"),
        ("candidate_age_days", -1, "0"),
        ("candidate_age_days", 3651, "3650"),
        ("arxiv_lookback_days", 0, "1"),
        ("arxiv_lookback_days", 366, "365"),
        ("arxiv_final_threshold", -1, "0"),
        ("arxiv_final_threshold", 101, "100"),
        ("refresh_interval_minutes", 14, "15"),
        ("refresh_interval_minutes", 10081, "10080"),
        ("ntfy_threshold", -1, "0"),
        ("ntfy_threshold", 101, "100"),
        ("monthly_budget_usd", -0.01, "0"),
        ("monthly_budget_usd", float("nan"), "0"),
        ("monthly_budget_usd", float("inf"), "0"),
        ("monthly_budget_usd", True, "number"),
        ("weather_latitude", False, "number"),
        ("weather_longitude", True, "number"),
    ],
)
def test_numeric_validation_is_bounded_and_rejects_ambiguous_types(field, value, message):
    payload = preset_payload("recommended")
    payload[field] = value
    assert message in _invalid(payload, field)


@pytest.mark.parametrize(
    "field",
    [
        "weather_enabled",
        "store_openai_key",
        "use_environment_openai_key",
        "arxiv_enabled",
        "refresh_on_open",
        "background_updates",
        "auto_summarize",
        "ntfy_enabled",
        "store_ntfy_token",
    ],
)
@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_boolean_choices_are_json_booleans_not_truthy_values(field, value):
    payload = preset_payload("recommended")
    payload[field] = value
    assert "yes or no" in _invalid(payload, field)


def test_port_conflict_is_reported_before_commit(monkeypatch):
    monkeypatch.setattr("rss_reader.setup_service._port_available", lambda _port: False)
    with pytest.raises(SetupValidationError) as caught:
        normalize_setup_payload(preset_payload("recommended"), environment={})
    assert caught.value.errors["port"] == "Port 8080 is already in use. Choose another port."


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile", "fast-ish"),
        ("subscriptions", "import-my-home"),
        ("language", "Esperanto"),
        ("ai_provider", "custom-shell"),
        ("review_workload", "unlimited"),
    ],
)
def test_enumerations_are_closed_to_displayed_options(field, value):
    payload = preset_payload("recommended")
    payload[field] = value
    assert "displayed options" in _invalid(payload, field)


def test_openai_requires_exactly_an_entered_or_explicit_environment_key():
    payload = preset_payload("demo")
    assert "OpenAI API key" in _invalid(payload, "openai_key", environment={})

    environment_payload = copy.deepcopy(payload)
    environment_payload["use_environment_openai_key"] = True
    draft = normalize_setup_payload(
        environment_payload,
        environment={"OPENAI_API_KEY": "environment-secret"},
        check_port=False,
    )
    assert draft.openai_key == "environment-secret"

    environment_payload["openai_key"] = "entered-but-not-selected"
    assert normalize_setup_payload(
        environment_payload,
        environment={"OPENAI_API_KEY": "chosen-environment-secret"},
        check_port=False,
    ).openai_key == "chosen-environment-secret"

    payload["openai_key"] = "entered-secret"
    assert normalize_setup_payload(
        payload,
        environment={"OPENAI_API_KEY": "ignored-environment-secret"},
        check_port=False,
    ).openai_key == "entered-secret"


@pytest.mark.parametrize("key", ["line-one\nline-two", "line-one\rline-two", "nul\x00key"])
def test_secrets_must_be_single_line(key):
    payload = preset_payload("demo")
    payload["openai_key"] = key
    assert "one line" in _invalid(payload, "openai_key")


def test_arxiv_dependency_categories_and_duplicates_are_normalized():
    payload = preset_payload("demo")
    payload["openai_key"] = "key"
    payload["ai_provider"] = "disabled"
    assert "requires OpenAI" in _invalid(payload, "arxiv_enabled")

    payload.update({"ai_provider": "openai", "arxiv_categories": "cs.AI, invalid, cs.AI"})
    assert "arXiv categories" in _invalid(payload, "arxiv_categories")

    payload["arxiv_categories"] = "cs.AI, cs.LG, cs.AI"
    draft = normalize_setup_payload(payload, environment={}, check_port=False)
    assert draft.arxiv_categories == ("cs.AI", "cs.LG")


def test_ollama_url_auto_summaries_weather_and_ntfy_dependencies_are_explicit():
    payload = preset_payload("recommended")
    payload.update({"ai_provider": "ollama", "ollama_url": "file:///tmp/socket"})
    assert "HTTP or HTTPS" in _invalid(payload, "ollama_url")

    payload = preset_payload("recommended")
    payload["auto_summarize"] = True
    assert "Configure AI" in _invalid(payload, "auto_summarize")

    payload = preset_payload("recommended")
    payload.update({"weather_enabled": True, "weather_location": "", "weather_timezone": ""})
    errors = None
    with pytest.raises(SetupValidationError) as caught:
        normalize_setup_payload(payload, environment={}, check_port=False)
    errors = caught.value.errors
    assert {"weather_location", "weather_timezone"} <= set(errors)

    payload = preset_payload("recommended")
    payload.update({"ntfy_enabled": True, "ntfy_server": "ftp://ntfy", "ntfy_topic": "bad topic"})
    with pytest.raises(SetupValidationError) as caught:
        normalize_setup_payload(payload, environment={}, check_port=False)
    assert {"ntfy_server", "ntfy_topic"} <= set(caught.value.errors)


def test_invalid_weather_timezone_is_a_field_error_before_review_not_a_commit_failure():
    payload = preset_payload("recommended")
    payload.update({"weather_enabled": True, "weather_timezone": "Mars/Olympus_Mons"})
    assert "IANA timezone" in _invalid(payload, "weather_timezone")


def test_fields_reject_control_characters_and_excessive_text():
    payload = preset_payload("recommended")
    payload["interest_profile"] = "safe\rhidden"
    assert normalize_setup_payload(
        payload, environment={}, check_port=False,
    ).interest_profile == "safe\nhidden"
    payload = preset_payload("recommended")
    payload["interest_profile"] = "x" * 2001
    assert "2000" in _invalid(payload, "interest_profile")


@pytest.mark.parametrize(
    ("changes", "expected_field"),
    [
        ({"weather_enabled": False, "weather_timezone": "Mars/Not_A_Zone"}, "weather_timezone"),
        ({"ai_provider": "disabled", "ollama_url": "file:///tmp/not-http"}, "ollama_url"),
        ({"ntfy_enabled": False, "ntfy_server": "ftp://not-http"}, "ntfy_server"),
        ({"ai_provider": "disabled", "model": "line-one\nline-two"}, "model"),
        ({"ntfy_enabled": False, "ntfy_server": "https://good\nbad"}, "ntfy_server"),
    ],
)
def test_reviewed_draft_can_never_fail_later_config_validation(
    tmp_path, changes, expected_field,
):
    """Disabled/hidden stale values must be rejected or safely normalized."""
    payload = preset_payload("recommended")
    payload.update(changes)
    try:
        draft = normalize_setup_payload(payload, environment={}, check_port=False)
    except SetupValidationError as exc:
        assert expected_field in exc.errors
    else:
        # Passing Review is a promise that Apply can build a valid Config.
        _config_for_draft(tmp_path / "config.toml", draft)


@pytest.mark.parametrize(
    ("field", "changes"),
    [
        (
            "ntfy_token",
            {"ntfy_enabled": True, "ntfy_topic": "valid-topic", "ntfy_token": "one\ntwo"},
        ),
        (
            "model",
            {"ai_provider": "openai", "openai_key": "key", "model": "one\ntwo"},
        ),
        (
            "ollama_url",
            {"ai_provider": "ollama", "ollama_url": "https://good.example\nBad: injected"},
        ),
        (
            "ntfy_server",
            {"ntfy_enabled": True, "ntfy_topic": "valid-topic", "ntfy_server": "https://good.example\nbad"},
        ),
    ],
)
def test_single_line_secret_model_and_url_fields_are_rejected_before_review(field, changes):
    payload = preset_payload("recommended")
    payload.update(changes)
    assert "one line" in _invalid(payload, field)
