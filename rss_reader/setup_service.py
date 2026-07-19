from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .config import DEFAULTS, Config, ensure_runtime_directories, load_config, save_config, validate_config
from .db import connect, initialize, transaction
from .opml import build_tree_from_database, import_groups, parse_opml_bytes, write_database_opml
from .plugins import available_plugin_names, set_plugin_runtime_state
from .secret_store import write_secret_store
from .setup_state import (
    CommitEvent,
    CommitPhase,
    SetupEvent,
    SetupPhase,
    TransitionError,
    commit_transition,
    setup_transition,
)


SETUP_SCHEMA_VERSION = 1
MANIFEST_NAME = "setup.json"
SECRET_RELATIVE_PATH = Path("private/secrets.json")


class SetupValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]):
        super().__init__("Setup choices need attention")
        self.errors = dict(errors)


class SetupCommitError(RuntimeError):
    pass


class SetupRecoveryRequired(SetupCommitError):
    """Setup cannot prove rollback and must not retry inside this session."""

    pass


def _strict_bool(value: Any, field_name: str, errors: dict[str, str]) -> bool:
    if isinstance(value, bool):
        return value
    errors[field_name] = "Choose yes or no."
    return False


def _integer(
    value: Any, field_name: str, minimum: int, maximum: int, errors: dict[str, str]
) -> int:
    try:
        result = int(value)
        if isinstance(value, bool) or str(result) != str(value).strip():
            raise ValueError
    except (TypeError, ValueError):
        errors[field_name] = "Enter a whole number."
        return minimum
    if not minimum <= result <= maximum:
        errors[field_name] = f"Enter a value from {minimum} to {maximum}."
    return result


def _number(
    value: Any, field_name: str, minimum: float, maximum: float, errors: dict[str, str]
) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
    except (TypeError, ValueError):
        errors[field_name] = "Enter a number."
        return minimum
    if not math.isfinite(result) or not minimum <= result <= maximum:
        errors[field_name] = f"Enter a value from {minimum:g} to {maximum:g}."
    return result


def _text(
    value: Any,
    field_name: str,
    maximum: int,
    errors: dict[str, str],
    *,
    required: bool = False,
    multiline: bool = False,
) -> str:
    if not isinstance(value, str):
        errors[field_name] = "Enter text."
        return ""
    result = (
        value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if multiline
        else value.strip()
    )
    if required and not result:
        errors[field_name] = "This value is required."
    elif len(result) > maximum or "\x00" in result or (
        not multiline and any(character in result for character in "\r\n")
    ):
        suffix = "." if multiline else " on one line."
        errors[field_name] = f"Use at most {maximum} characters{suffix}"
    return result


def _choice(
    value: Any, field_name: str, choices: set[str], errors: dict[str, str]
) -> str:
    result = str(value or "")
    if result not in choices:
        errors[field_name] = "Choose one of the displayed options."
        return sorted(choices)[0]
    return result


def _port_available(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@dataclass(frozen=True)
class SetupDraft:
    profile: str
    port: int
    subscriptions: str
    language: str
    interest_profile: str
    weather_enabled: bool
    weather_location: str
    weather_latitude: float
    weather_longitude: float
    weather_timezone: str
    ai_provider: str
    model: str
    ollama_url: str
    summary_threshold: int
    summary_window_days: int
    candidate_age_days: int
    review_workload: str
    monthly_budget_usd: float
    store_openai_key: bool
    arxiv_enabled: bool
    arxiv_categories: tuple[str, ...]
    arxiv_lookback_days: int
    arxiv_final_threshold: int
    refresh_on_open: bool
    background_updates: bool
    refresh_interval_minutes: int
    auto_summarize: bool
    ntfy_enabled: bool
    ntfy_server: str
    ntfy_topic: str
    ntfy_threshold: int
    store_ntfy_token: bool
    openai_key: str = field(default="", repr=False, compare=False)
    ntfy_token: str = field(default="", repr=False, compare=False)

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider != "disabled"


def preset_payload(profile: str = "recommended") -> dict[str, Any]:
    if profile not in {"recommended", "guided", "demo"}:
        raise ValueError("Unknown setup profile")
    demo = profile == "demo"
    return {
        "profile": profile,
        "port": 8081 if demo else 8080,
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
        "ai_provider": "openai" if demo else "disabled",
        "model": "gpt-5.4-nano",
        "ollama_url": "http://127.0.0.1:11434/v1/",
        "summary_threshold": 40 if demo else 70,
        "summary_window_days": 7,
        "candidate_age_days": 30,
        "review_workload": "balanced",
        "monthly_budget_usd": 2.0 if demo else 0.0,
        "store_openai_key": True,
        "openai_key": "",
        "use_environment_openai_key": False,
        "arxiv_enabled": demo,
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


EXPECTED_SETUP_FIELDS = frozenset(preset_payload("recommended"))


def normalize_setup_payload(
    payload: Any,
    *,
    environment: Mapping[str, str] | None = None,
    check_port: bool = True,
) -> SetupDraft:
    errors: dict[str, str] = {}
    if not isinstance(payload, dict):
        raise SetupValidationError({"form": "The setup request must be a JSON object."})
    unknown = sorted(set(payload) - EXPECTED_SETUP_FIELDS)
    missing = sorted(EXPECTED_SETUP_FIELDS - set(payload))
    if unknown:
        errors["form"] = "Unknown setup field(s): " + ", ".join(unknown)
    if missing:
        errors["form"] = "Missing setup field(s): " + ", ".join(missing)

    base = preset_payload("recommended") | payload
    profile = _choice(base["profile"], "profile", {"recommended", "guided", "demo"}, errors)
    port = _integer(base["port"], "port", 1024, 65535, errors)
    if "port" not in errors and check_port and not _port_available(port):
        errors["port"] = f"Port {port} is already in use. Choose another port."
    subscriptions = _choice(
        base["subscriptions"], "subscriptions", {"starter", "empty"}, errors
    )
    language = _choice(base["language"], "language", {"English", "French"}, errors)
    interests = _text(
        base["interest_profile"], "interest_profile", 2000, errors, multiline=True
    )
    weather_enabled = _strict_bool(base["weather_enabled"], "weather_enabled", errors)
    weather_location = _text(
        base["weather_location"], "weather_location", 120, errors, required=weather_enabled
    )
    weather_latitude = _number(
        base["weather_latitude"], "weather_latitude", -90, 90, errors
    )
    weather_longitude = _number(
        base["weather_longitude"], "weather_longitude", -180, 180, errors
    )
    weather_timezone = _text(
        base["weather_timezone"], "weather_timezone", 120, errors, required=weather_enabled
    )
    if weather_enabled and weather_timezone != "auto":
        try:
            ZoneInfo(weather_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors["weather_timezone"] = "Enter a recognized IANA timezone such as Europe/Paris."
    ai_provider = _choice(
        base["ai_provider"], "ai_provider", {"disabled", "openai", "ollama"}, errors
    )
    model = _text(base["model"], "model", 200, errors, required=ai_provider != "disabled")
    ollama_url = _text(
        base["ollama_url"], "ollama_url", 1000, errors, required=ai_provider == "ollama"
    )
    if ai_provider == "ollama" and not ollama_url.startswith(("http://", "https://")):
        errors["ollama_url"] = "Enter an HTTP or HTTPS Ollama API address."
    summary_threshold = _integer(base["summary_threshold"], "summary_threshold", 0, 100, errors)
    summary_window_days = _integer(base["summary_window_days"], "summary_window_days", 1, 365, errors)
    candidate_age_days = _integer(base["candidate_age_days"], "candidate_age_days", 0, 3650, errors)
    workload = _choice(
        base["review_workload"], "review_workload", {"focused", "balanced", "wide"}, errors
    )
    budget = _number(base["monthly_budget_usd"], "monthly_budget_usd", 0, 10000, errors)
    store_openai = _strict_bool(base["store_openai_key"], "store_openai_key", errors)
    use_environment_key = _strict_bool(
        base["use_environment_openai_key"], "use_environment_openai_key", errors
    )
    entered_openai_key = _text(base["openai_key"], "openai_key", 4096, errors)
    environment = os.environ if environment is None else environment
    openai_key = (
        str(environment.get("OPENAI_API_KEY", "")).strip()
        if use_environment_key
        else entered_openai_key
    )
    if ai_provider == "openai" and not openai_key:
        errors["openai_key"] = (
            "Enter an OpenAI API key, use the key already set in this terminal, "
            "or choose Configure AI later."
        )
    if openai_key and any(character in openai_key for character in "\r\n\x00"):
        errors["openai_key"] = "The API key must be one line."

    arxiv_enabled = _strict_bool(base["arxiv_enabled"], "arxiv_enabled", errors)
    categories_text = _text(
        base["arxiv_categories"], "arxiv_categories", 500, errors,
        required=arxiv_enabled, multiline=True,
    )
    categories = tuple(
        dict.fromkeys(part.strip() for part in categories_text.replace("\n", ",").split(",") if part.strip())
    )
    if arxiv_enabled and (
        not categories
        or len(categories) > 20
        or any(not re.fullmatch(r"[A-Za-z-]+\.[A-Za-z-]+", value) for value in categories)
    ):
        errors["arxiv_categories"] = "Use arXiv categories such as cs.AI, with at most 20 categories."
    if arxiv_enabled and ai_provider != "openai":
        errors["arxiv_enabled"] = (
            "The bundled arXiv ranking workflow currently requires OpenAI. "
            "Choose OpenAI or start without arXiv."
        )
    arxiv_lookback = _integer(
        base["arxiv_lookback_days"], "arxiv_lookback_days", 1, 365, errors
    )
    arxiv_threshold = _integer(
        base["arxiv_final_threshold"], "arxiv_final_threshold", 0, 100, errors
    )
    refresh_on_open = _strict_bool(base["refresh_on_open"], "refresh_on_open", errors)
    background = _strict_bool(base["background_updates"], "background_updates", errors)
    refresh_interval = _integer(
        base["refresh_interval_minutes"], "refresh_interval_minutes", 15, 10080, errors
    )
    auto_summarize = _strict_bool(base["auto_summarize"], "auto_summarize", errors)
    if auto_summarize and ai_provider == "disabled":
        errors["auto_summarize"] = "Configure AI before enabling automatic summaries."
    if auto_summarize and not (refresh_on_open or background):
        errors["auto_summarize"] = (
            "Choose an automatic feed-check method before enabling automatic summaries."
        )

    ntfy_enabled = _strict_bool(base["ntfy_enabled"], "ntfy_enabled", errors)
    ntfy_server = _text(base["ntfy_server"], "ntfy_server", 1000, errors, required=ntfy_enabled)
    if ntfy_enabled and not ntfy_server.startswith(("http://", "https://")):
        errors["ntfy_server"] = "Enter an HTTP or HTTPS ntfy server address."
    ntfy_topic = _text(base["ntfy_topic"], "ntfy_topic", 64, errors, required=ntfy_enabled)
    if ntfy_topic:
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", ntfy_topic):
            errors["ntfy_topic"] = "Use only letters, numbers, hyphens, and underscores."
    ntfy_threshold = _integer(base["ntfy_threshold"], "ntfy_threshold", 0, 100, errors)
    store_ntfy = _strict_bool(base["store_ntfy_token"], "store_ntfy_token", errors)
    ntfy_token = _text(base["ntfy_token"], "ntfy_token", 4096, errors)
    if ntfy_enabled and not auto_summarize:
        errors["ntfy_enabled"] = (
            "Device article alerts are sent after automatic summaries. Enable automatic summaries or configure ntfy later."
        )

    if errors:
        raise SetupValidationError(errors)
    if not weather_enabled:
        weather_location = "Paris"
        weather_latitude = 48.8566
        weather_longitude = 2.3522
        weather_timezone = "Europe/Paris"
    if ai_provider != "ollama":
        ollama_url = "http://127.0.0.1:11434/v1/"
    if ai_provider != "openai":
        openai_key = ""
    if not ntfy_enabled:
        ntfy_server = "https://ntfy.sh"
        ntfy_topic = ""
        ntfy_token = ""
    draft = SetupDraft(
        profile=profile,
        port=port,
        subscriptions=subscriptions,
        language=language,
        interest_profile=interests,
        weather_enabled=weather_enabled,
        weather_location=weather_location or "Paris",
        weather_latitude=weather_latitude,
        weather_longitude=weather_longitude,
        weather_timezone=weather_timezone or "Europe/Paris",
        ai_provider=ai_provider,
        model=model or "gpt-5.4-nano",
        ollama_url=ollama_url or "http://127.0.0.1:11434/v1/",
        summary_threshold=summary_threshold,
        summary_window_days=summary_window_days,
        candidate_age_days=candidate_age_days,
        review_workload=workload,
        monthly_budget_usd=budget,
        store_openai_key=store_openai,
        arxiv_enabled=arxiv_enabled,
        arxiv_categories=categories,
        arxiv_lookback_days=arxiv_lookback,
        arxiv_final_threshold=arxiv_threshold,
        refresh_on_open=refresh_on_open,
        background_updates=background,
        refresh_interval_minutes=refresh_interval,
        auto_summarize=auto_summarize,
        ntfy_enabled=ntfy_enabled,
        ntfy_server=ntfy_server or "https://ntfy.sh",
        ntfy_topic=ntfy_topic,
        ntfy_threshold=ntfy_threshold,
        store_ntfy_token=store_ntfy,
        openai_key=openai_key,
        ntfy_token=ntfy_token,
    )
    try:
        _config_for_draft(Path("config.toml"), draft)
    except (TypeError, ValueError) as exc:
        # Review is a commit precondition: a payload may not reach REVIEWED if
        # the exact generated core configuration would later be rejected.
        raise SetupValidationError({"form": str(exc)}) from exc
    return draft


def public_review(draft: SetupDraft, instance_path: Path) -> dict[str, Any]:
    provider = {
        "disabled": "Configure AI later",
        "openai": f"OpenAI · {draft.model}",
        "ollama": f"Local Ollama · {draft.model}",
    }[draft.ai_provider]
    return {
        "profile": draft.profile,
        "instance_path": str(instance_path),
        "reader_url": f"http://127.0.0.1:{draft.port}/",
        "access": "Only this computer · 127.0.0.1",
        "subscriptions": "Public starter feeds" if draft.subscriptions == "starter" else "Empty reader",
        "language": draft.language,
        "weather": draft.weather_location if draft.weather_enabled else "Off",
        "ai": provider,
        "api_key": (
            "Stored in the private local secret store"
            if draft.ai_provider == "openai" and draft.store_openai_key
            else "Available for this launch only"
            if draft.ai_provider == "openai"
            else "Not required"
        ),
        "ordinary_threshold": f"{draft.summary_threshold}/100",
        "ordinary_evidence_window": f"Previous {draft.summary_window_days} day(s)",
        "candidate_age_limit": f"{draft.candidate_age_days} day(s)",
        "monthly_budget": (
            f"${draft.monthly_budget_usd:.2f} local guard"
            if draft.monthly_budget_usd
            else "No local spending guard"
        ),
        "arxiv": (
            f"On · {', '.join(draft.arxiv_categories)} · first retrieval looks back "
            f"{draft.arxiv_lookback_days} day(s)"
            if draft.arxiv_enabled
            else "Off"
        ),
        "updates": (
            ("Check while open" if draft.refresh_on_open else "Manual feed checks")
            + (f" · server schedule every {draft.refresh_interval_minutes} minutes" if draft.background_updates else " · no background schedule")
            + (" · automatic AI summaries" if draft.auto_summarize else " · AI updates only when requested")
        ),
        "device_alerts": (
            f"ntfy on · threshold {draft.ntfy_threshold}/100"
            if draft.ntfy_enabled
            else "Off · System notices remain inside DistillFeed"
        ),
        "guarantees": [
            "Setup contacts no feeds, AI provider, arXiv endpoint, weather service, or ntfy server.",
            "Nothing is exposed outside this computer.",
            (
                "Setup makes no AI request. After setup, configured automatic feed checks may update summaries and use the provider; the local monthly spending guard applies."
                if draft.auto_summarize and draft.monthly_budget_usd
                else "Setup makes no AI request. After setup, configured automatic feed checks may update summaries and use the provider; no local monthly spending limit is configured."
                if draft.auto_summarize
                else "Setup makes no AI request. After setup, AI is used only when you explicitly update a summary."
            ),
        ],
    }


def _write_private_text(path: Path, text: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise SetupCommitError(f"Refusing symbolic link at {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _config_for_draft(path: Path, draft: SetupDraft) -> Config:
    data = copy.deepcopy(DEFAULTS)
    data["plugins"]["arxiv_digest_enabled"] = draft.arxiv_enabled
    data["app"].update({
        "mode": "local",
        "database_path": "data/reader.sqlite3",
        "working_opml_path": "data/subscriptions.opml",
        "opml_source": "",
        "starter_subscriptions": draft.subscriptions == "starter",
        "host": "127.0.0.1",
        "port": draft.port,
        "trusted_hosts": "127.0.0.1,localhost",
        "auto_refresh_on_load": draft.refresh_on_open,
        "background_scheduler_enabled": draft.background_updates,
        "refresh_interval_minutes": draft.refresh_interval_minutes,
        "auto_summarize_after_refresh": draft.auto_summarize,
        "summary_language": draft.language,
        "interest_profile": draft.interest_profile,
    })
    data["llm"].update({
        "enabled": draft.ai_enabled,
        "provider": "openai" if draft.ai_provider == "disabled" else draft.ai_provider,
        "base_url": draft.ollama_url,
        "model": draft.model,
        "review_workload": draft.review_workload,
        "candidate_max_age_days": draft.candidate_age_days,
        "minimum_relevance": draft.summary_threshold,
        "rolling_digest_hours": draft.summary_window_days * 24,
        "monthly_budget_usd": draft.monthly_budget_usd,
    })
    data["weather"].update({
        "enabled": draft.weather_enabled,
        "language": draft.language,
        "location_name": draft.weather_location,
        "latitude": draft.weather_latitude,
        "longitude": draft.weather_longitude,
        "timezone": draft.weather_timezone,
    })
    data["notifications"]["ntfy"].update({
        "enabled": draft.ntfy_enabled,
        "server_url": draft.ntfy_server,
        "topic": draft.ntfy_topic,
        "minimum_relevance": draft.ntfy_threshold,
    })
    validate_config(data)
    return Config(path, data)


def _write_arxiv_config(config: Config, draft: SetupDraft) -> None:
    from distillfeed_arxiv.config import _dump_toml, _validate_editable, load_plugin_config

    # First-run setup must be reproducible. An expert shell override may select
    # a recipe for an existing installation, but it must not silently seed a
    # new managed instance with unrelated private preferences.
    values = copy.deepcopy(load_plugin_config(config, environment={}))
    values.pop("_path", None)
    values["arxiv"]["categories"] = list(draft.arxiv_categories or ("cs.AI",))
    values["arxiv"]["initial_lookback_days"] = draft.arxiv_lookback_days
    values["arxiv"]["api_backfill_enabled"] = True
    values["filters"]["final_keep_threshold"] = draft.arxiv_final_threshold
    values["llm"]["model"] = draft.model if draft.model in {"gpt-5.4-nano", "gpt-5.4-mini"} else "gpt-5.4-nano"
    values.setdefault("notifications", {}).setdefault("ntfy", {})["enabled"] = False
    values["notifications"]["ntfy"]["topic"] = ""
    _validate_editable(values)
    _write_private_text(config.path.parent / "arxiv-digest.toml", _dump_toml(values))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _harden_instance_permissions(instance: Path) -> None:
    for root, directories, files in os.walk(instance, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise SetupCommitError(f"Refusing symbolic link inside instance: {root_path}")
        os.chmod(root_path, 0o700)
        for name in directories:
            path = root_path / name
            if path.is_symlink():
                raise SetupCommitError(f"Refusing symbolic link inside instance: {path}")
            os.chmod(path, 0o700)
        for name in files:
            path = root_path / name
            if path.is_symlink():
                raise SetupCommitError(f"Refusing symbolic link inside instance: {path}")
            os.chmod(path, 0o600)


def verify_instance(
    instance: Path,
    *,
    require_manifest: bool = True,
    require_pristine_setup: bool = False,
    expected_config_sha256: str | None = None,
) -> dict[str, Any]:
    if instance.is_symlink() or not instance.is_dir():
        raise SetupCommitError("The DistillFeed instance directory is missing or unsafe")
    config_path = instance / "config.toml"
    if config_path.is_symlink() or not config_path.is_file():
        raise SetupCommitError("The committed configuration is missing or unsafe")
    config = load_config(config_path)
    try:
        config.database_path.relative_to(instance)
        config.working_opml_path.relative_to(instance)
    except ValueError as exc:
        raise SetupCommitError("Managed database and OPML paths must stay inside the instance") from exc
    if not config.database_path.is_file() or not config.working_opml_path.is_file():
        raise SetupCommitError("The managed database or OPML file is missing")
    with connect(config.database_path) as connection:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        opml_matches = parse_opml_bytes(config.working_opml_path.read_bytes()) == build_tree_from_database(connection)
        external_runs = {
            "refresh_runs": int(connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0]),
            "llm_runs": int(connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0]),
            "notifications": int(connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]),
        }
        if config.get("plugins", "arxiv_digest_enabled", False):
            if "arxiv_digest" not in available_plugin_names():
                raise SetupCommitError("The bundled arXiv plugin entry point is missing")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='distillfeed_arxiv_state'"
            ).fetchone()
            if not table:
                raise SetupCommitError("The enabled arXiv plugin was not initialized")
            watermark = connection.execute(
                "SELECT value FROM distillfeed_arxiv_state WHERE key='last_complete_at'"
            ).fetchone()
            if require_pristine_setup and watermark:
                raise SetupCommitError("Setup must not advance the arXiv retrieval watermark")
    if quick != "ok" or foreign_keys or not opml_matches:
        raise SetupCommitError("The staged database or OPML verification failed")
    if require_pristine_setup and any(external_runs.values()):
        raise SetupCommitError("Setup unexpectedly recorded external work")
    manifest = None
    if require_manifest:
        manifest_path = instance / MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SetupCommitError("The setup completion manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SetupCommitError("The setup completion manifest is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("state") != "ready":
            raise SetupCommitError("The setup completion manifest is not ready")
        if manifest.get("version") != __version__ or manifest.get("schema") != SETUP_SCHEMA_VERSION:
            raise SetupCommitError("The setup completion manifest belongs to another release")
        initial_hash = str(manifest.get("config_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", initial_hash):
            raise SetupCommitError("The setup completion manifest has an invalid configuration hash")
        try:
            uuid.UUID(str(manifest.get("installation_id", "")))
            datetime.fromisoformat(str(manifest.get("created_at", "")))
        except ValueError as exc:
            raise SetupCommitError("The setup completion manifest has invalid identity data") from exc
        # This hash records what setup originally published. Configuration is
        # deliberately editable afterward, so normal launches validate current
        # TOML rather than treating a Settings change as installation damage.
        if expected_config_sha256 is not None and (
            initial_hash != expected_config_sha256
            or _sha256(config_path) != expected_config_sha256
        ):
            raise SetupCommitError("The published configuration does not match the reviewed setup")
    _harden_instance_permissions(instance)
    return {
        "config": str(config_path),
        "reader_url": f"http://127.0.0.1:{int(config.get('app', 'port'))}/",
        "manifest": manifest,
    }


@dataclass(frozen=True)
class CommitResult:
    instance_path: Path
    config_path: Path
    reader_url: str
    environment: dict[str, str] = field(repr=False)

    def public(self) -> dict[str, str]:
        return {
            "instance_path": str(self.instance_path),
            "reader_url": self.reader_url,
            "message": "Setup is complete. DistillFeed is starting.",
        }


class SetupCommitter:
    def __init__(
        self,
        state_root: Path,
        *,
        fault: Callable[[str], None] | None = None,
    ):
        # Do not resolve away a final-component symlink: private managed state
        # must never be redirected through one.
        self.state_root = state_root.expanduser().absolute()
        self.instance = self.state_root / "instance"
        self.fault = fault or (lambda _step: None)
        self.commit_phase = CommitPhase.IDLE
        self.commit_history: list[tuple[str, str]] = []
        self._lock = threading.RLock()

    def _step(self, name: str) -> None:
        self.fault(name)

    def _event(self, event: CommitEvent) -> None:
        self.commit_phase = commit_transition(self.commit_phase, event)
        self.commit_history.append((event.value, self.commit_phase.value))

    def commit(self, draft: SetupDraft) -> CommitResult:
        with self._lock:
            return self._commit_locked(draft)

    def _commit_locked(self, draft: SetupDraft) -> CommitResult:
        if self.state_root.is_symlink() or self.instance.is_symlink():
            raise SetupCommitError("Refusing a symbolic link in the managed setup path")
        if self.commit_phase == CommitPhase.RECOVERY_REQUIRED:
            raise SetupCommitError("Setup recovery is required before another commit can start")
        if self.instance.exists():
            raise SetupCommitError(
                "A managed instance already exists. Change its settings inside DistillFeed; setup will not overwrite it."
            )
        if not _port_available(draft.port):
            raise SetupCommitError(
                f"Port {draft.port} became unavailable. Return to review and choose another port."
            )
        self.commit_phase = CommitPhase.IDLE
        self.commit_history = []
        self._event(CommitEvent.BEGIN)
        stage: Path | None = None
        stage_identity: tuple[int, int] | None = None
        published = False
        try:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.state_root, 0o700)
            stage = Path(tempfile.mkdtemp(prefix=".setup-stage-", dir=self.state_root))
            stage_metadata = stage.lstat()
            stage_identity = (stage_metadata.st_dev, stage_metadata.st_ino)
            os.chmod(stage, 0o700)
            _write_private_text(stage / ".distillfeed-stage", "DistillFeed setup staging directory\n")
            self._step("stage-created")
            config = _config_for_draft(stage / "config.toml", draft)
            save_config(config)
            os.chmod(config.path, 0o600)
            if draft.arxiv_enabled:
                _write_arxiv_config(config, draft)
            secrets: dict[str, str] = {}
            environment: dict[str, str] = {}
            if draft.ai_provider == "openai" and draft.openai_key:
                environment["OPENAI_API_KEY"] = draft.openai_key
                if draft.store_openai_key:
                    secrets["OPENAI_API_KEY"] = draft.openai_key
            if draft.ntfy_enabled and draft.ntfy_token:
                environment["NTFY_TOKEN"] = draft.ntfy_token
                if draft.store_ntfy_token:
                    secrets["NTFY_TOKEN"] = draft.ntfy_token
            write_secret_store(stage / SECRET_RELATIVE_PATH, secrets)
            self._step("files-staged")
            self._event(CommitEvent.FILES_STAGED)

            ensure_runtime_directories(config)
            initialize(config.database_path)
            with connect(config.database_path) as connection:
                if draft.subscriptions == "starter":
                    starter = Path(__file__).resolve().parent / "resources" / "starter-subscriptions.opml"
                    # import_groups owns its transaction. A surrounding BEGIN
                    # would make every starter/demo installation fail with a
                    # nested-transaction error.
                    import_groups(connection, parse_opml_bytes(starter.read_bytes()))
                with transaction(connection, immediate=True):
                    if draft.arxiv_enabled:
                        set_plugin_runtime_state(connection, config, "arxiv_digest", True)
                    write_database_opml(connection, config.working_opml_path)
            self._step("database-staged")
            self._event(CommitEvent.DATABASE_STAGED)
            _harden_instance_permissions(stage)
            verify_instance(
                stage,
                require_manifest=False,
                require_pristine_setup=True,
            )
            self._step("stage-verified")
            self._event(CommitEvent.VERIFIED)

            initial_config_hash = _sha256(config.path)
            manifest = {
                "schema": SETUP_SCHEMA_VERSION,
                "state": "ready",
                "version": __version__,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "profile": draft.profile,
                "config_sha256": initial_config_hash,
                "secret_store_present": bool(secrets),
                "installation_id": str(uuid.uuid4()),
            }
            _write_private_text(
                stage / MANIFEST_NAME,
                json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            )
            _harden_instance_permissions(stage)
            self._step("stage-marked")
            self._event(CommitEvent.MARKED)
            # A Review page may remain open while another process takes the
            # chosen port. Recheck at the publish boundary and fail explicitly.
            if not _port_available(draft.port):
                raise SetupCommitError(
                    f"Port {draft.port} became unavailable. Return to review and choose another port."
                )
            os.replace(stage, self.instance)
            published = True
            parent_descriptor = os.open(self.state_root, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
            self._step("instance-published")
            self._event(CommitEvent.PUBLISHED)
            verified = verify_instance(
                self.instance,
                require_manifest=True,
                require_pristine_setup=True,
                expected_config_sha256=initial_config_hash,
            )
            self._step("postcheck-complete")
            self._event(CommitEvent.POSTCHECKED)
            return CommitResult(
                instance_path=self.instance,
                config_path=self.instance / "config.toml",
                reader_url=str(verified["reader_url"]),
                environment=environment,
            )
        except Exception as original_error:
            # A failed post-check is rolled back to a non-live marked stage.  No
            # application launch can mistake it for a completed instance.
            rollback_error: Exception | None = None
            if published and self.instance.exists() and stage is not None and not stage.exists():
                try:
                    os.replace(self.instance, stage)
                    published = False
                except OSError as exc:
                    rollback_error = exc
            if rollback_error is None and stage is not None and stage.exists():
                marker = stage / ".distillfeed-stage"
                try:
                    current_metadata = stage.lstat()
                    recognized = (
                        stage_identity
                        == (current_metadata.st_dev, current_metadata.st_ino)
                        and stage.parent == self.state_root
                        and stage.name.startswith(".setup-stage-")
                        and stat.S_ISDIR(current_metadata.st_mode)
                    ) or (
                        stage.parent == self.state_root
                        and not marker.is_symlink()
                        and marker.is_file()
                    )
                    if not recognized:
                        raise SetupCommitError("Refusing to remove an unrecognized setup directory")
                    shutil.rmtree(stage)
                except Exception as exc:
                    rollback_error = exc
            if rollback_error is not None:
                self._event(CommitEvent.ROLLBACK_FAILED)
                raise SetupRecoveryRequired(
                    "Setup could not confirm a safe rollback. A managed instance or staging "
                    f"files may remain in {self.state_root}; preserve that folder and relaunch "
                    "DistillFeed so it can verify the durable state before doing anything else."
                ) from rollback_error
            self._event(CommitEvent.ROLLBACK)
            raise original_error


class SetupSession:
    """Thread-safe server-side setup state; secrets never leave this object."""

    def __init__(self, committer: SetupCommitter):
        self.committer = committer
        self.phase = SetupPhase.LISTENING
        self.review_token: str | None = None
        self.draft: SetupDraft | None = None
        self.result: CommitResult | None = None
        self.last_error: str | None = None
        self.history: list[tuple[str, str]] = []
        self._lock = threading.RLock()

    def _event(self, event: SetupEvent) -> None:
        self.phase = setup_transition(self.phase, event)
        self.history.append((event.value, self.phase.value))

    def sanitize_error(self, value: Any) -> str:
        """Remove exact write-only credentials before retaining an error."""
        text = str(value or "")
        draft = self.draft
        if draft is not None:
            for secret in (draft.openai_key, draft.ntfy_token):
                if secret:
                    text = text.replace(secret, "[redacted secret]")
        return " ".join(text.split())[:1000]

    def bootstrap(self) -> None:
        with self._lock:
            self._event(SetupEvent.BOOTSTRAP)

    def validate(
        self, payload: Any, *, environment: Mapping[str, str] | None = None,
        check_port: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        with self._lock:
            # Validation is a compound transition: REVIEWED first returns to
            # editing and FAILED first enters its retry path. Reject every
            # other source before normalization or assignment so even direct
            # service callers cannot mutate an absorbing/committing session.
            if self.phase not in {
                SetupPhase.EDITING,
                SetupPhase.REVIEWED,
                SetupPhase.FAILED,
            }:
                raise TransitionError(
                    f"Setup event {SetupEvent.VALIDATE.value!r} is invalid while "
                    f"{self.phase.value!r}"
                )
            if self.phase == SetupPhase.FAILED:
                self._event(SetupEvent.RETRY)
            if self.phase == SetupPhase.REVIEWED:
                self._event(SetupEvent.EDIT)
            try:
                draft = normalize_setup_payload(
                    payload, environment=environment, check_port=check_port,
                )
            except SetupValidationError:
                if self.phase == SetupPhase.EDITING:
                    self._event(SetupEvent.INVALID)
                raise
            self.draft = draft
            self.review_token = uuid.uuid4().hex
            self.last_error = None
            self._event(SetupEvent.VALIDATE)
            return self.review_token, public_review(draft, self.committer.instance)

    def edit(self) -> None:
        with self._lock:
            self._event(SetupEvent.EDIT)
            self.review_token = None

    def complete(self, review_token: str) -> CommitResult:
        with self._lock:
            if self.phase == SetupPhase.COMPLETE:
                if self.result is None or review_token != self.review_token:
                    raise SetupCommitError("This setup completion token is no longer valid")
                self._event(SetupEvent.REPLAY)
                return self.result
            if self.phase != SetupPhase.REVIEWED or review_token != self.review_token or self.draft is None:
                raise SetupCommitError("Review the current settings before creating the reader")
            self._event(SetupEvent.APPLY)
            try:
                self.result = self.committer.commit(self.draft)
            except SetupRecoveryRequired as exc:
                self.last_error = self.sanitize_error(exc)
                self._event(SetupEvent.REQUIRE_RECOVERY)
                # No in-process action may reuse an ambiguous apply. Drop the
                # write-only draft and token as soon as its error is redacted.
                self.review_token = None
                self.draft = None
                raise
            except Exception as exc:
                self.last_error = self.sanitize_error(exc)
                self._event(SetupEvent.FAIL)
                raise
            self._event(SetupEvent.SUCCEED)
            return self.result

    def cancel(self) -> None:
        with self._lock:
            self._event(SetupEvent.CANCEL)

    def timeout(self) -> None:
        with self._lock:
            if self.phase not in {
                SetupPhase.COMPLETE,
                SetupPhase.RECOVERY_REQUIRED,
                SetupPhase.CANCELLED,
                SetupPhase.TIMED_OUT,
            }:
                self._event(SetupEvent.TIMEOUT)

    def public_state(self) -> dict[str, Any]:
        with self._lock:
            state: dict[str, Any] = {
                "phase": self.phase.value,
                "reviewed": self.phase == SetupPhase.REVIEWED,
                "complete": self.phase == SetupPhase.COMPLETE,
                "recovery_required": self.phase == SetupPhase.RECOVERY_REQUIRED,
                "error": self.last_error,
            }
            if self.phase == SetupPhase.RECOVERY_REQUIRED:
                state["recovery_path"] = str(self.committer.state_root)
            return state
