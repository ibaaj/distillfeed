from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Callable

from .config import Config
from .notifications import deliver_ntfy_for_run

LOGGER = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "distillfeed.plugins"


@dataclass(frozen=True)
class RefreshContext:
    """Stable, deliberately small interface supplied to installed plugins."""

    connection: Any
    config: Config
    feed_id: int | None
    group_id: int | None
    force: bool
    automatic: bool
    notify_run: Callable[[int], dict[str, Any]]
    cancel_requested: Callable[[], bool] = lambda: False


@dataclass(frozen=True)
class SummaryContext:
    """Scope and runtime state supplied to an optional plugin summary pass."""

    connection: Any
    config: Config
    feed_id: int | None
    group_id: int | None
    automatic: bool
    cancel_requested: Callable[[], bool] = lambda: False


def enabled_plugin_names(config: Config) -> tuple[str, ...]:
    raw = str(config.get("plugins", "enabled", ""))
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if bool(config.get("plugins", "arxiv_digest_enabled", False)):
        names.append("arxiv_digest")
    return tuple(dict.fromkeys(names))


def available_plugin_names() -> tuple[str, ...]:
    return tuple(sorted(point.name for point in entry_points(group=ENTRY_POINT_GROUP)))


def set_plugin_runtime_state(
    connection: Any, config: Config, name: str, enabled: bool,
) -> None:
    """Apply a plugin lifecycle transition without discarding stored content."""
    available = {point.name: point for point in entry_points(group=ENTRY_POINT_GROUP)}
    point = available.get(name)
    if point is None:
        if enabled:
            raise RuntimeError(f"Plugin is not installed: {name}")
        return
    candidate = point.load()
    plugin = candidate() if isinstance(candidate, type) else candidate
    if enabled:
        initialize = getattr(plugin, "initialize", None)
        if not callable(initialize):
            raise RuntimeError(f"DistillFeed plugin {name!r} has no initialize() method")
        initialize(connection, config)
        return
    disable = getattr(plugin, "disable", None)
    if callable(disable):
        disable(connection, config)


def load_plugins(config: Config) -> list[Any]:
    requested = enabled_plugin_names(config)
    if not requested:
        return []
    available = {point.name: point for point in entry_points(group=ENTRY_POINT_GROUP)}
    missing = [name for name in requested if name not in available]
    if missing:
        raise RuntimeError(
            "Enabled DistillFeed plugin(s) are not installed: " + ", ".join(missing)
        )
    loaded: list[Any] = []
    for name in requested:
        candidate = available[name].load()
        plugin = candidate() if isinstance(candidate, type) else candidate
        if not callable(getattr(plugin, "initialize", None)):
            raise RuntimeError(f"DistillFeed plugin {name!r} has no initialize() method")
        loaded.append(plugin)
    return loaded


def load_available_plugins() -> list[Any]:
    """Load installed plugins for configuration, whether enabled or not.

    Runtime hooks still use :func:`load_plugins`.  Keeping discovery separate
    lets a disabled plugin expose its setup form, so enabling it and saving its
    initial configuration is one atomic user action.
    """
    loaded: list[Any] = []
    for point in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda item: item.name):
        candidate = point.load()
        plugin = candidate() if isinstance(candidate, type) else candidate
        loaded.append(plugin)
    return loaded


def initialize_plugins(connection: Any, config: Config) -> list[Any]:
    plugins = load_plugins(config)
    for plugin in plugins:
        plugin.initialize(connection, config)
    return plugins


def refresh_plugins(
    connection: Any,
    config: Config,
    *,
    feed_id: int | None = None,
    group_id: int | None = None,
    force: bool = False,
    automatic: bool = False,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    combined: dict[str, Any] = {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "new_items": 0,
        "plugins": [],
    }

    def notify(run_id: int) -> dict[str, Any]:
        return deliver_ntfy_for_run(connection, config, run_id)

    context = RefreshContext(
        connection=connection,
        config=config,
        feed_id=feed_id,
        group_id=group_id,
        force=force,
        automatic=automatic,
        notify_run=notify,
        cancel_requested=cancel_requested or (lambda: False),
    )
    for plugin in initialize_plugins(connection, config):
        if context.cancel_requested():
            combined["cancelled"] = True
            break
        name = str(getattr(plugin, "name", plugin.__class__.__name__))
        refresh = getattr(plugin, "refresh", None)
        if not callable(refresh):
            continue
        try:
            result = dict(refresh(context) or {})
            for key in ("attempted", "succeeded", "failed", "new_items"):
                value = int(result.get(key, 0))
                if value < 0:
                    raise ValueError(f"plugin statistic {key} cannot be negative")
                combined[key] += value
            combined["plugins"].append({"name": name, **result})
            if str(result.get("status", "")) == "cancelled":
                combined["cancelled"] = True
                break
        except Exception as exc:
            LOGGER.exception("DistillFeed plugin %s failed during refresh", name)
            combined["failed"] += 1
            combined["plugins"].append(
                {"name": name, "status": "failed", "error": str(exc)[:2000]}
            )
    return combined


def summarize_plugins(
    connection: Any,
    config: Config,
    *,
    feed_id: int | None = None,
    group_id: int | None = None,
    automatic: bool = False,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Let enabled plugins finish pending model work without fetching again."""
    combined: dict[str, Any] = {
        "succeeded": 0, "failed": 0, "blocked": 0, "plugins": [],
    }
    context = SummaryContext(
        connection=connection,
        config=config,
        feed_id=feed_id,
        group_id=group_id,
        automatic=automatic,
        cancel_requested=cancel_requested or (lambda: False),
    )
    for plugin in initialize_plugins(connection, config):
        if context.cancel_requested():
            combined["cancelled"] = True
            break
        summarize = getattr(plugin, "summarize", None)
        if not callable(summarize):
            continue
        name = str(getattr(plugin, "name", plugin.__class__.__name__))
        try:
            result = dict(summarize(context) or {})
            status = str(result.get("status", "success"))
            if status in {"failed", "llm-failed"}:
                combined["failed"] += 1
            elif status in {
                "blocked", "pending-llm-disabled", "missing-credential",
                "budget-blocked", "disabled",
            }:
                combined["blocked"] += 1
            elif status not in {"out-of-scope", "empty", "unchanged", "cancelled"}:
                combined["succeeded"] += 1
            combined["plugins"].append({"name": name, **result})
            if status == "cancelled":
                combined["cancelled"] = True
                break
        except Exception as exc:
            LOGGER.exception("DistillFeed plugin %s failed during summarization", name)
            combined["failed"] += 1
            combined["plugins"].append(
                {"name": name, "status": "failed", "error": str(exc)[:2000]}
            )
    return combined


def decorate_page(connection: Any, config: Config, data: dict[str, Any]) -> None:
    for plugin in initialize_plugins(connection, config):
        decorate = getattr(plugin, "decorate_page", None)
        if callable(decorate):
            decorate(connection, config, data)


def plugin_settings_fields(config: Config) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for plugin in load_available_plugins():
        provider = getattr(plugin, "settings_fields", None)
        if not callable(provider):
            continue
        name = str(getattr(plugin, "name", plugin.__class__.__name__))
        for supplied in provider(config):
            field = dict(supplied)
            relative = str(field.pop("path"))
            field["path"] = f"plugin.{name}.{relative}"
            field.setdefault("category", name)
            field.setdefault("common", True)
            fields.append(field)
    return fields


def plugin_settings_actions(config: Config) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for plugin in load_plugins(config):
        provider = getattr(plugin, "settings_actions", None)
        if not callable(provider):
            continue
        name = str(getattr(plugin, "name", plugin.__class__.__name__))
        for supplied in provider(config):
            action = dict(supplied)
            identifier = str(action.pop("action"))
            action["url"] = f"/api/plugins/{name}/actions/{identifier}"
            actions.append(action)
    return actions


def update_plugin_settings(config: Config, values: dict[str, Any]) -> None:
    if not values:
        return
    plugins = {
        str(getattr(plugin, "name", plugin.__class__.__name__)): plugin
        for plugin in load_available_plugins()
    }
    grouped: dict[str, dict[str, Any]] = {}
    for path, value in values.items():
        parts = str(path).split(".", 2)
        if len(parts) != 3 or parts[0] != "plugin" or parts[1] not in plugins:
            raise ValueError(f"Unknown plugin setting: {path}")
        grouped.setdefault(parts[1], {})[parts[2]] = value
    for name, plugin_values in grouped.items():
        updater = getattr(plugins[name], "update_settings", None)
        if not callable(updater):
            raise ValueError(f"Plugin {name} does not expose editable settings")
        updater(config, plugin_values)


def run_plugin_settings_action(config: Config, name: str, action: str) -> dict[str, Any]:
    for plugin in load_plugins(config):
        plugin_name = str(getattr(plugin, "name", plugin.__class__.__name__))
        if plugin_name != name:
            continue
        runner = getattr(plugin, "run_settings_action", None)
        if not callable(runner):
            raise ValueError(f"Plugin {name} does not expose settings actions")
        return dict(runner(config, action) or {})
    raise ValueError(f"Unknown enabled plugin: {name}")
