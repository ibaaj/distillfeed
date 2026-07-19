from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Mapping


def load_plugin_config(
    main_config: Any, *, environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source_environment = os.environ if environment is None else environment
    configured = source_environment.get("DISTILLFEED_ARXIV_CONFIG", "").strip()
    path = Path(configured).expanduser() if configured else main_config.path.parent / "arxiv-digest.toml"
    path = path.resolve()
    source = path if path.is_file() else Path(__file__).resolve().parent / "resources" / "arxiv-digest.example.toml"
    if not source.is_file():
        raise RuntimeError(
            f"arXiv plugin configuration not found: {path}, and its packaged neutral default is missing."
        )
    with source.open("rb") as handle:
        values = tomllib.load(handle)
    for section in ("app", "arxiv", "filters", "llm"):
        if not isinstance(values.get(section), dict):
            raise RuntimeError(f"arXiv plugin configuration requires [{section}]")
    categories = values["arxiv"].get("categories")
    if not isinstance(categories, list) or not categories or not all(
        isinstance(value, str) and value.strip() for value in categories
    ):
        raise RuntimeError("[arxiv].categories must be a non-empty string list")
    model = str(values["llm"].get("model", "")).strip()
    if model not in {"gpt-5.4-nano", "gpt-5.4-mini"}:
        raise RuntimeError("arXiv scoring and digest model must be gpt-5.4-nano or gpt-5.4-mini")
    if not str(values["llm"].get("system_prompt", "")).strip():
        raise RuntimeError("[llm].system_prompt is required")
    # Forward-compatible neutral defaults are materialized in memory so an
    # older TOML can expose and save newly added controls in Settings.
    values["llm"].setdefault("ranking_batch_size", 20)
    values["filters"].setdefault("category_bonus", "")
    values["filters"].setdefault("category_bonus_points", 0)
    values["filters"].setdefault("cross_category_bonuses", [])
    values["filters"].setdefault("cross_category_bonus_points", 0)
    values["filters"].setdefault("no_signal_penalty", 0)
    values["filters"].setdefault("category_bridge_bonus", 0)
    values["_path"] = str(path)
    return values


def settings_fields(main_config: Any) -> list[dict[str, Any]]:
    cfg = load_plugin_config(main_config)
    category = "arXiv digest"

    def field(path: str, label: str, value: Any, *, common: bool = True, widget: str | None = None, rows: int = 5, options: list[str] | None = None) -> dict[str, Any]:
        result = {
            "path": path, "label": label, "value": value, "type": type(value).__name__,
            "category": category, "common": common,
        }
        if widget:
            result.update({"widget": widget, "rows": rows})
        if options:
            result["options"] = options
        return result

    list_value = lambda section, key: "\n".join(str(value) for value in cfg[section].get(key, []))
    return [
        field("arxiv.categories", "Categories (comma-separated)", ", ".join(cfg["arxiv"]["categories"])),
        field("llm.model", "arXiv AI model", str(cfg["llm"]["model"]), options=["gpt-5.4-nano", "gpt-5.4-mini"]),
        field("filters.broad_candidate_threshold", "Minimum local score sent to the LLM", int(cfg["filters"].get("broad_candidate_threshold", 0))),
        field("filters.final_keep_threshold", "Keep papers with a combined arXiv score of at least", int(cfg["filters"].get("final_keep_threshold", 25))),
        field("notifications.ntfy.enabled", "Send arXiv alerts to other devices with ntfy", bool(cfg.get("notifications", {}).get("ntfy", {}).get("enabled", False))),
        field("notifications.ntfy.topic", "arXiv ntfy topic", str(cfg.get("notifications", {}).get("ntfy", {}).get("topic", ""))),
        field("notifications.ntfy.minimum_llm_score", "Minimum arXiv score for a device alert", int(cfg.get("notifications", {}).get("ntfy", {}).get("minimum_llm_score", 88))),
        field("notifications.ntfy.max_items_per_digest", "Maximum arXiv device alerts per digest", int(cfg.get("notifications", {}).get("ntfy", {}).get("max_items_per_digest", 5))),
        field("notifications.ntfy.send_on_manual_refresh", "Also send device alerts after a manual refresh", bool(cfg.get("notifications", {}).get("ntfy", {}).get("send_on_manual_refresh", False))),
        field("notifications.ntfy.server_url", "arXiv ntfy server URL", str(cfg.get("notifications", {}).get("ntfy", {}).get("server_url", "https://ntfy.sh")), common=False),
        field("notifications.ntfy.token_env", "arXiv ntfy token environment variable", str(cfg.get("notifications", {}).get("ntfy", {}).get("token_env", "ARXIV_NTFY_TOKEN")), common=False),
        field("notifications.ntfy.priority", "arXiv device alert priority", str(cfg.get("notifications", {}).get("ntfy", {}).get("priority", "high")), common=False, options=["min", "low", "default", "high", "max"]),
        field("notifications.ntfy.timeout_seconds", "arXiv ntfy timeout (seconds)", int(cfg.get("notifications", {}).get("ntfy", {}).get("timeout_seconds", 10)), common=False),
        field("filters.local_keep_threshold", "Local-only keep threshold", int(cfg["filters"].get("local_keep_threshold", 6)), common=False),
        field("filters.local_weight", "Local score weight", float(cfg["filters"].get("local_weight", 1.0)), common=False),
        field("filters.llm_weight", "LLM score weight", float(cfg["filters"].get("llm_weight", .35)), common=False),
        field("filters.category_bonus", "Bonus category (optional)", str(cfg["filters"].get("category_bonus", "")), common=False),
        field("filters.category_bonus_points", "Bonus category points", int(cfg["filters"].get("category_bonus_points", 0)), common=False),
        field("filters.cross_category_bonuses", "Cross-category bonuses (one pair per line)", list_value("filters", "cross_category_bonuses"), common=False, widget="textarea", rows=4),
        field("filters.cross_category_bonus_points", "Cross-category bonus points", int(cfg["filters"].get("cross_category_bonus_points", 0)), common=False),
        field("filters.no_signal_penalty", "No-signal score adjustment", int(cfg["filters"].get("no_signal_penalty", 0)), common=False),
        field("filters.category_bridge_bonus", "Category bridge points", int(cfg["filters"].get("category_bridge_bonus", 0)), common=False),
        field("arxiv.initial_lookback_days", "On the first update, retrieve the previous days", int(cfg["arxiv"].get("initial_lookback_days", 3)), common=False),
        field("arxiv.resume_overlap_minutes", "Backfill overlap (minutes)", int(cfg["arxiv"].get("resume_overlap_minutes", 90)), common=False),
        field("arxiv.rss_pause_seconds", "Pause between RSS requests (seconds)", float(cfg["arxiv"].get("rss_pause_seconds", 5)), common=False),
        field("arxiv.api_pause_seconds", "Pause between API requests (seconds)", float(cfg["arxiv"].get("api_pause_seconds", 5)), common=False),
        field("arxiv.api_page_size", "arXiv API page size", int(cfg["arxiv"].get("api_page_size", 100)), common=False),
        field("arxiv.api_backfill_enabled", "Use API backfill for missed announcements", bool(cfg["arxiv"].get("api_backfill_enabled", True)), common=False),
        field("arxiv.api_interval_hours", "Minimum hours between API backfills", int(cfg["arxiv"].get("api_interval_hours", 20)), common=False),
        field("llm.max_candidates", "Maximum papers ranked per digest", int(cfg["llm"].get("max_candidates", 100)), common=False),
        field("llm.ranking_batch_size", "Papers per ranking request", int(cfg["llm"].get("ranking_batch_size", 20)), common=False),
        field("filters.preferred_authors", "Preferred authors (one per line)", list_value("filters", "preferred_authors"), common=False, widget="textarea", rows=8),
        field("filters.blocked_authors_exact", "Blocked authors (one per line)", list_value("filters", "blocked_authors_exact"), common=False, widget="textarea", rows=5),
        field("filters.positive_keywords_strong", "Strong keywords (one per line)", list_value("filters", "positive_keywords_strong"), common=False, widget="textarea", rows=10),
        field("filters.positive_keywords_medium", "Medium keywords (one per line)", list_value("filters", "positive_keywords_medium"), common=False, widget="textarea", rows=10),
        field("filters.positive_keywords_weak", "Weak keywords (one per line)", list_value("filters", "positive_keywords_weak"), common=False, widget="textarea", rows=8),
        field("filters.negative_keywords", "Negative keywords (one per line)", list_value("filters", "negative_keywords"), common=False, widget="textarea", rows=8),
        field("llm.system_prompt", "Paper-ranking prompt", str(cfg["llm"]["system_prompt"]), common=False, widget="textarea", rows=12),
    ]


def _validate_editable(cfg: dict[str, Any]) -> None:
    categories = cfg["arxiv"]["categories"]
    if not categories or len(categories) > 20 or any(
        not re.fullmatch(r"[A-Za-z-]+\.[A-Za-z-]+", value) for value in categories
    ):
        raise ValueError("arXiv categories must look like cs.AI and contain at most 20 entries")
    filters = cfg["filters"]
    if not -999 <= int(filters["broad_candidate_threshold"]) <= 999:
        raise ValueError("The broad candidate threshold must be between -999 and 999")
    if not -999 <= int(filters["local_keep_threshold"]) <= 999:
        raise ValueError("The local keep threshold must be between -999 and 999")
    if not -999 <= int(filters["final_keep_threshold"]) <= 999:
        raise ValueError("The combined keep threshold must be between -999 and 999")
    if not 0 <= float(filters["local_weight"]) <= 10 or not 0 <= float(filters["llm_weight"]) <= 10:
        raise ValueError("arXiv score weights must be between 0 and 10")
    bonus_category = str(filters.get("category_bonus", "")).strip()
    if bonus_category and not re.fullmatch(r"[A-Za-z-]+\.[A-Za-z-]+", bonus_category):
        raise ValueError("The arXiv bonus category must look like cs.LO or be empty")
    for key in (
        "category_bonus_points", "cross_category_bonus_points",
        "no_signal_penalty", "category_bridge_bonus",
    ):
        if not -100 <= int(filters.get(key, 0)) <= 100:
            raise ValueError(f"{key} must be between -100 and 100")
    pairs = filters.get("cross_category_bonuses", [])
    if len(pairs) > 50 or any(
        not re.fullmatch(r"[A-Za-z-]+\.[A-Za-z-]+\+[A-Za-z-]+\.[A-Za-z-]+", str(pair))
        for pair in pairs
    ):
        raise ValueError("Cross-category bonuses must look like cs.LO+cs.AI")
    arxiv = cfg["arxiv"]
    if not 0 <= int(arxiv["initial_lookback_days"]) <= 365:
        raise ValueError("Initial arXiv lookback must be between 0 and 365 days")
    if not 0 <= int(arxiv["resume_overlap_minutes"]) <= 1440:
        raise ValueError("arXiv overlap must be between 0 and 1440 minutes")
    if not 0 <= float(arxiv["rss_pause_seconds"]) <= 120 or not 0 <= float(arxiv["api_pause_seconds"]) <= 120:
        raise ValueError("arXiv request pauses must be between 0 and 120 seconds")
    if not 1 <= int(arxiv["api_page_size"]) <= 2000 or not 1 <= int(arxiv["api_interval_hours"]) <= 720:
        raise ValueError("arXiv API page size or interval is outside its safe range")
    if str(cfg["llm"]["model"]) not in {"gpt-5.4-nano", "gpt-5.4-mini"}:
        raise ValueError("The arXiv plugin model must be gpt-5.4-nano or gpt-5.4-mini")
    if not 1 <= int(cfg["llm"]["max_candidates"]) <= 500:
        raise ValueError("Maximum arXiv ranking candidates must be between 1 and 500")
    if not 1 <= int(cfg["llm"].get("ranking_batch_size", 20)) <= 50:
        raise ValueError("The arXiv ranking batch size must be between 1 and 50")
    if not str(cfg["llm"]["system_prompt"]).strip():
        raise ValueError("The arXiv paper-ranking prompt cannot be empty")
    ntfy = cfg.setdefault("notifications", {}).setdefault("ntfy", {})
    if not 0 <= int(ntfy["minimum_llm_score"]) <= 100:
        raise ValueError("The arXiv ntfy score must be between 0 and 100")
    if not 1 <= int(ntfy["max_items_per_digest"]) <= 20:
        raise ValueError("Maximum arXiv pushes must be between 1 and 20")
    topic = str(ntfy["topic"]).strip()
    if ntfy["enabled"] and not topic:
        raise ValueError("An arXiv ntfy topic is required when pushes are enabled")
    if topic and not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        raise ValueError("The arXiv ntfy topic may contain only letters, numbers, - and _")
    if not str(ntfy["server_url"]).startswith(("http://", "https://")):
        raise ValueError("The arXiv ntfy server must be an HTTP or HTTPS URL")
    if str(ntfy["priority"]) not in {"min", "low", "default", "high", "max"}:
        raise ValueError("The arXiv ntfy priority is invalid")
    if not 1 <= int(ntfy["timeout_seconds"]) <= 60:
        raise ValueError("The arXiv ntfy timeout must be between 1 and 60 seconds")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(ntfy["token_env"])):
        raise ValueError("The arXiv ntfy token environment variable is invalid")
    for key in (
        "preferred_authors", "blocked_authors_exact", "positive_keywords_strong",
        "positive_keywords_medium", "positive_keywords_weak", "negative_keywords",
    ):
        values = filters[key]
        if len(values) > 1000 or any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError(f"Invalid or excessive values in {key}")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported arXiv TOML value: {type(value).__name__}")


def _dump_toml(cfg: dict[str, Any]) -> str:
    lines: list[str] = []

    def section(name: str, values: dict[str, Any]) -> None:
        lines.append(f"[{name}]")
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        for key, value in values.items():
            if isinstance(value, dict):
                section(f"{name}.{key}", value)

    for name, values in cfg.items():
        if name != "_path":
            section(name, values)
    return "\n".join(lines).rstrip() + "\n"


def _atomic_save(cfg: dict[str, Any]) -> None:
    path = Path(cfg["_path"])
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_dump_toml(cfg)); handle.flush(); os.fsync(handle.fileno())
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def update_settings(main_config: Any, values: dict[str, Any]) -> None:
    cfg = load_plugin_config(main_config)
    candidate = copy.deepcopy(cfg)
    list_paths = {
        "filters.preferred_authors", "filters.blocked_authors_exact",
        "filters.positive_keywords_strong", "filters.positive_keywords_medium",
        "filters.positive_keywords_weak", "filters.negative_keywords",
        "filters.cross_category_bonuses",
    }
    for path, raw in values.items():
        parts = path.split(".")
        target = candidate
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                raise ValueError(f"Unknown arXiv setting: {path}")
            target = target[part]
        key = parts[-1]
        if key not in target:
            raise ValueError(f"Unknown arXiv setting: {path}")
        current = target[key]
        if path == "arxiv.categories":
            converted = [part.strip() for part in re.split(r"[,\n]", str(raw)) if part.strip()]
        elif path in list_paths:
            converted = list(dict.fromkeys(line.strip() for line in str(raw).splitlines() if line.strip()))
        elif isinstance(current, bool):
            if not isinstance(raw, bool):
                raise ValueError(f"{path} must be true or false")
            converted = raw
        elif isinstance(current, int):
            converted = int(raw)
        elif isinstance(current, float):
            converted = float(raw)
        else:
            converted = str(raw)
        target[key] = converted
    if "llm.model" in values:
        prices = {
            "gpt-5.4-nano": (0.20, 1.25),
            "gpt-5.4-mini": (0.75, 4.50),
        }.get(str(candidate["llm"]["model"]))
        if prices:
            candidate["llm"]["input_price_per_million"] = prices[0]
            candidate["llm"]["output_price_per_million"] = prices[1]
    _validate_editable(candidate)
    _atomic_save(candidate)
