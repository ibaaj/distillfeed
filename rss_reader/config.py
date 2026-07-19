from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import tempfile
import tomllib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULTS: dict[str, Any] = {
    "plugins": {
        "enabled": "",
        "arxiv_digest_enabled": False,
    },
    "app": {
        "mode": "local",
        "database_path": "data/reader.sqlite3",
        "working_opml_path": "data/subscriptions.opml",
        "opml_source": "",
        "starter_subscriptions": True,
        "host": "127.0.0.1",
        "port": 8080,
        "trusted_hosts": "127.0.0.1,localhost",
        "debug": False,
        "log_level": "INFO",
        "auto_refresh_on_load": True,
        "background_scheduler_enabled": False,
        "refresh_interval_minutes": 30,
        "auto_summarize_after_refresh": False,
        "summary_language": "English",
        "interest_profile": "",
        "retention_days": 0,
    },
    "feeds": {
        "timeout_seconds": 20,
        "max_response_bytes": 10 * 1024 * 1024,
        "max_entries_per_feed_update": 200,
        "initial_import_max_entries_per_feed": 20,
        "initial_import_max_age_days": 30,
        "max_workers": 8,
        "max_workers_per_host": 2,
        "user_agent": "DistillFeed/0.22.0 (+private single-user feed reader)",
        "allow_private_urls": False,
        "generated_feed_directory": "",
        "retry_base_minutes": 15,
        "retry_max_hours": 24,
    },
    "llm": {
        "enabled": True,
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "http://127.0.0.1:11434/v1/",
        "model": "gpt-5.4-nano",
        "reasoning_effort": "none",
        "automatic_cooldown_minutes": 30,
        "review_workload": "balanced",
        "max_entries_total": 160,
        "max_entries_per_feed": 20,
        "max_description_chars": 1500,
        "max_input_chars": 400_000,
        "max_output_tokens": 16_000,
        "candidate_max_age_days": 30,
        "minimum_relevance": 70,
        "maximum_summary_items": 25,
        "rolling_digest_hours": 24,
        "estimated_output_tokens_per_item": 120,
        "estimated_output_tokens_per_group": 250,
        "output_token_safety_margin": 1000,
        "monthly_budget_usd": 0.0,
        "pricing": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    },
    "auth": {"enabled": False, "username": "reader", "password_env": "RSSREADER_PASSWORD"},
    "ui": {
        "dark_mode": False,
        "groups_expanded_by_default": False,
        "offline_cache_enabled": False,
        "completion_notifications": False,
        "subscription_font_size": 14,
        "item_font_size": 14,
        "summary_font_size": 16,
    },
    "weather": {
        "enabled": True,
        "language": "English",
        "location_name": "Paris",
        "latitude": 48.8566,
        "longitude": 2.3522,
        "timezone": "Europe/Paris",
        "refresh_minutes": 15,
    },
    "notifications": {
        "ntfy": {
            "enabled": False,
            "server_url": "https://ntfy.sh",
            "topic": "",
            "token_env": "NTFY_TOKEN",
            "minimum_relevance": 85,
            "max_items_per_summary": 5,
            "priority": "high",
            "timeout_seconds": 10,
        },
    },
}


# Standard API prices per one million text tokens. These presets keep the cost
# explorer correct when a user switches between DistillFeed's recommended
# OpenAI models; custom providers retain their explicitly configured prices.
OPENAI_MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


@dataclass(frozen=True)
class Config:
    path: Path
    data: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        return self.data[name]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    def path_value(self, key: str) -> Path:
        value = Path(str(self.data["app"][key])).expanduser()
        return value if value.is_absolute() else (self.path.parent / value).resolve()

    @property
    def database_path(self) -> Path:
        return self.path_value("database_path")

    @property
    def working_opml_path(self) -> Path:
        return self.path_value("working_opml_path")

    @property
    def generated_feed_directory(self) -> Path | None:
        value = str(self.get("feeds", "generated_feed_directory", "")).strip()
        if not value:
            return None
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.path.parent / path).resolve()

    @property
    def application_password(self) -> str | None:
        auth = self.section("auth")
        if not auth.get("enabled"):
            return None
        return os.environ.get(str(auth.get("password_env", "RSSREADER_PASSWORD")))


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path or os.environ.get("RSSREADER_CONFIG", "config.toml")).expanduser().resolve()
    values = copy.deepcopy(DEFAULTS)
    if config_path.exists():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
        _merge(values, loaded)
        # Migrate the former entry-point-only switch in memory. The next save
        # writes the explicit bundled-plugin checkbox and removes ambiguity.
        loaded_plugins = loaded.get("plugins", {})
        if isinstance(loaded_plugins, dict) and "arxiv_digest_enabled" not in loaded_plugins:
            legacy = str(loaded_plugins.get("enabled", ""))
            if "arxiv_digest" in {part.strip() for part in legacy.split(",")}:
                values["plugins"]["arxiv_digest_enabled"] = True
    if os.environ.get("DISTILLFEED_MODE"):
        values["app"]["mode"] = os.environ["DISTILLFEED_MODE"]
    if os.environ.get("DISTILLFEED_AUTH_ENABLED"):
        raw_auth = os.environ["DISTILLFEED_AUTH_ENABLED"].strip().casefold()
        if raw_auth not in {"true", "false", "1", "0"}:
            raise ValueError("DISTILLFEED_AUTH_ENABLED must be true or false")
        values["auth"]["enabled"] = raw_auth in {"true", "1"}
    _validate(values)
    return Config(config_path, values)


def _validate(values: dict[str, Any]) -> None:
    app = values["app"]
    feeds = values["feeds"]
    llm = values["llm"]
    auth = values["auth"]
    weather = values["weather"]
    ntfy = values["notifications"]["ntfy"]
    plugins = values["plugins"]
    if not isinstance(plugins["arxiv_digest_enabled"], bool):
        raise ValueError("plugins.arxiv_digest_enabled must be true or false")
    enabled_plugins = str(plugins["enabled"]).strip()
    if any(character in enabled_plugins for character in "\r\n"):
        raise ValueError("plugins.enabled must be a comma-separated single line")
    for name in (part.strip() for part in enabled_plugins.split(",")):
        if name and not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError(f"Invalid plugin entry point name: {name}")
    if str(app["mode"]) not in {"local", "development", "production"}:
        raise ValueError("app.mode must be local, development, or production")
    if str(app["mode"]) == "production" and bool(app["debug"]):
        raise ValueError("app.debug must be false in production mode")
    for key in ("database_path", "working_opml_path"):
        if not str(app[key]).strip():
            raise ValueError(f"app.{key} cannot be empty")
    if not isinstance(app["starter_subscriptions"], bool):
        raise ValueError("app.starter_subscriptions must be true or false")
    if not str(app["host"]).strip() or "\n" in str(app["host"]):
        raise ValueError("app.host must be a non-empty single line")
    trusted_hosts = [part.strip().casefold() for part in str(app["trusted_hosts"]).split(",")]
    if not trusted_hosts or any(
        not part
        or len(part) > 253
        or not re.fullmatch(r"[A-Za-z0-9.-]+", part)
        for part in trusted_hosts
    ):
        raise ValueError("app.trusted_hosts must be a comma-separated list of hostnames")
    if str(app["mode"]) == "local":
        if str(app["host"]).strip().casefold() not in {"127.0.0.1", "localhost"}:
            raise ValueError("app.host must be 127.0.0.1 or localhost in local mode")
        if any(host not in {"127.0.0.1", "localhost"} for host in trusted_hosts):
            raise ValueError("app.trusted_hosts may contain only 127.0.0.1 and localhost in local mode")
    if int(values["app"]["port"]) not in range(1, 65536):
        raise ValueError("app.port must be between 1 and 65535")
    if str(app["log_level"]).upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("app.log_level is not a supported logging level")
    if int(values["app"]["refresh_interval_minutes"]) <= 0:
        raise ValueError("app.refresh_interval_minutes must be positive")
    if not isinstance(app["background_scheduler_enabled"], bool):
        raise ValueError("app.background_scheduler_enabled must be true or false")
    if str(app["summary_language"]) not in {"English", "French"}:
        raise ValueError("app.summary_language must be English or French")
    if int(app["retention_days"]) < 0:
        raise ValueError("app.retention_days cannot be negative")
    if len(str(app["interest_profile"])) > 2000:
        raise ValueError("app.interest_profile cannot exceed 2000 characters")
    for key in (
        "max_entries_total", "max_entries_per_feed", "max_description_chars", "max_input_chars",
        "max_output_tokens", "estimated_output_tokens_per_item", "estimated_output_tokens_per_group",
        "rolling_digest_hours", "maximum_summary_items",
    ):
        if int(llm[key]) <= 0:
            raise ValueError(f"llm.{key} must be positive")
    if str(llm["review_workload"]) not in {"focused", "balanced", "wide"}:
        raise ValueError("llm.review_workload must be focused, balanced, or wide")
    if int(llm["max_entries_total"]) > 200:
        raise ValueError("llm.max_entries_total cannot exceed 200 items per provider request")
    if int(llm["max_entries_per_feed"]) > int(llm["max_entries_total"]):
        raise ValueError("llm.max_entries_per_feed cannot exceed max_entries_total")
    if int(llm["automatic_cooldown_minutes"]) < 0 or int(llm["candidate_max_age_days"]) < 0:
        raise ValueError("LLM cooldown and candidate age cannot be negative")
    if not 0 <= int(llm["minimum_relevance"]) <= 100:
        raise ValueError("llm.minimum_relevance must be between 0 and 100")
    if int(llm["output_token_safety_margin"]) < 0:
        raise ValueError("llm.output_token_safety_margin cannot be negative")
    if int(llm["output_token_safety_margin"]) >= int(llm["max_output_tokens"]):
        raise ValueError("llm.output_token_safety_margin must be lower than max_output_tokens")
    minimum_output_budget = (
        int(llm["output_token_safety_margin"])
        + int(llm["estimated_output_tokens_per_item"])
        + int(llm["estimated_output_tokens_per_group"])
    )
    if minimum_output_budget > int(llm["max_output_tokens"]):
        raise ValueError("llm.max_output_tokens is too small for even one item and one group")
    if not str(llm["model"]).strip() or "\n" in str(llm["model"]):
        raise ValueError("llm.model must be a non-empty single line")
    if str(llm["provider"]).casefold() not in {"openai", "ollama"}:
        raise ValueError("llm.provider must be openai or ollama")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(llm["api_key_env"])):
        raise ValueError("llm.api_key_env must be a valid environment-variable name")
    base_url = str(llm["base_url"]).strip()
    if not base_url.startswith(("http://", "https://")) or any(char in base_url for char in "\r\n"):
        raise ValueError("llm.base_url must be an HTTP or HTTPS URL")
    for key, value in llm["pricing"].items():
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"llm.pricing.{key} must be a finite non-negative number")
    monthly_budget = float(llm["monthly_budget_usd"])
    if not math.isfinite(monthly_budget) or monthly_budget < 0:
        raise ValueError("llm.monthly_budget_usd must be a finite non-negative number")
    for key in (
        "timeout_seconds", "max_response_bytes", "max_entries_per_feed_update",
        "initial_import_max_entries_per_feed", "max_workers", "max_workers_per_host",
        "retry_base_minutes", "retry_max_hours",
    ):
        if int(feeds[key]) <= 0:
            raise ValueError(f"feeds.{key} must be positive")
    if int(feeds["initial_import_max_age_days"]) < 0:
        raise ValueError("feeds.initial_import_max_age_days cannot be negative")
    if int(feeds["initial_import_max_entries_per_feed"]) > int(feeds["max_entries_per_feed_update"]):
        raise ValueError("feeds.initial_import_max_entries_per_feed cannot exceed the update limit")
    if int(feeds["max_workers_per_host"]) > int(feeds["max_workers"]):
        raise ValueError("feeds.max_workers_per_host cannot exceed feeds.max_workers")
    user_agent = str(feeds["user_agent"]).strip()
    if not user_agent or len(user_agent) > 500 or "\n" in user_agent or "\r" in user_agent:
        raise ValueError("feeds.user_agent must be a single non-empty line of at most 500 characters")
    generated_directory = str(feeds["generated_feed_directory"]).strip()
    if len(generated_directory) > 4096 or any(
        character in generated_directory for character in "\r\n\x00"
    ):
        raise ValueError("feeds.generated_feed_directory must be a single path")
    for key in ("subscription_font_size", "item_font_size", "summary_font_size"):
        if not 10 <= int(values["ui"][key]) <= 24:
            raise ValueError(f"ui.{key} must be between 10 and 24")
    if int(values["weather"]["refresh_minutes"]) <= 0:
        raise ValueError("weather.refresh_minutes must be positive")
    if weather["language"] not in {"English", "French"}:
        raise ValueError("weather.language must be English or French")
    latitude = float(weather["latitude"])
    longitude = float(weather["longitude"])
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("weather.latitude must be between -90 and 90")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("weather.longitude must be between -180 and 180")
    if not str(weather["location_name"]).strip():
        raise ValueError("weather.location_name cannot be empty")
    if str(weather["timezone"]) != "auto":
        try:
            ZoneInfo(str(weather["timezone"]))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("weather.timezone is not a recognized IANA timezone") from exc
    if llm["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("Unsupported llm.reasoning_effort")
    if auth["enabled"] and not str(auth["username"]).strip():
        raise ValueError("auth.username cannot be empty when authentication is enabled")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(auth["password_env"])):
        raise ValueError("auth.password_env must be a valid environment-variable name")
    server_url = str(ntfy["server_url"]).strip()
    if not server_url.startswith(("http://", "https://")) or any(
        character in server_url for character in "\r\n"
    ):
        raise ValueError("notifications.ntfy.server_url must be an HTTP or HTTPS URL")
    topic = str(ntfy["topic"]).strip()
    if topic and not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        raise ValueError("notifications.ntfy.topic may contain only letters, numbers, - and _")
    if ntfy["enabled"] and not topic:
        raise ValueError("notifications.ntfy.topic is required when ntfy is enabled")
    if not 0 <= int(ntfy["minimum_relevance"]) <= 100:
        raise ValueError("notifications.ntfy.minimum_relevance must be between 0 and 100")
    if not 1 <= int(ntfy["max_items_per_summary"]) <= 20:
        raise ValueError("notifications.ntfy.max_items_per_summary must be between 1 and 20")
    if str(ntfy["priority"]) not in {"min", "low", "default", "high", "max"}:
        raise ValueError("notifications.ntfy.priority is invalid")
    if not 1 <= int(ntfy["timeout_seconds"]) <= 60:
        raise ValueError("notifications.ntfy.timeout_seconds must be between 1 and 60")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(ntfy["token_env"])):
        raise ValueError("notifications.ntfy.token_env must be a valid environment-variable name")


def validate_config(values: dict[str, Any]) -> None:
    """Validate a complete effective configuration before persisting it."""
    _validate(values)


def ensure_runtime_directories(config: Config) -> None:
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.working_opml_path.parent.mkdir(parents=True, exist_ok=True)
    if config.generated_feed_directory is not None:
        config.generated_feed_directory.mkdir(parents=True, exist_ok=True, mode=0o750)


def flask_secret() -> str:
    """Return an ephemeral secret; persistent sessions are not used by this app."""
    return secrets.token_hex(32)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


def dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []

    def section(name: str, values: dict[str, Any]) -> None:
        scalars = {key: value for key, value in values.items() if not isinstance(value, dict)}
        children = {key: value for key, value in values.items() if isinstance(value, dict)}
        lines.append(f"[{name}]")
        for key, value in scalars.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        for key, value in children.items():
            section(f"{name}.{key}", value)

    for section_name, section_values in data.items():
        section(section_name, section_values)
    return "\n".join(lines).rstrip() + "\n"


def save_config(config: Config) -> None:
    """Atomically persist the complete effective configuration with one backup."""
    config.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config.path.name}.", suffix=".tmp", dir=config.path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(dump_toml(config.data))
            handle.flush()
            os.fsync(handle.fileno())
        if config.path.exists():
            shutil.copy2(config.path, config.path.with_suffix(config.path.suffix + ".bak"))
        os.replace(temporary_name, config.path)
        directory_fd = os.open(config.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
