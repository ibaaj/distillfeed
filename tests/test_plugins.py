from __future__ import annotations

import copy
from pathlib import Path

import pytest

from rss_reader.config import DEFAULTS, Config
from rss_reader.config import load_config, save_config
from rss_reader.cli import main
from rss_reader.plugins import (
    decorate_page,
    initialize_plugins,
    plugin_settings_fields,
    refresh_plugins,
    update_plugin_settings,
)
import rss_reader.plugins as plugin_module


class ExamplePlugin:
    name = "example"

    def __init__(self):
        self.initialized = 0
        self.updated = None

    def initialize(self, connection, config):
        self.initialized += 1

    def refresh(self, context):
        return {"attempted": 1, "succeeded": 1, "failed": 0, "new_items": 2, "status": "success"}

    def decorate_page(self, connection, config, data):
        data["decorated"] = True

    def settings_fields(self, config):
        return [{
            "path": "threshold", "label": "Threshold", "value": 80, "type": "int",
            "category": "Example", "common": True,
        }]

    def update_settings(self, config, values):
        self.updated = values


class Point:
    name = "example"

    def __init__(self, value): self.value = value
    def load(self): return self.value


def config(tmp_path: Path) -> Config:
    values = copy.deepcopy(DEFAULTS)
    values["plugins"]["enabled"] = "example"
    return Config(tmp_path / "config.toml", values)


def test_plugin_lifecycle_settings_and_page_decoration_are_scoped(monkeypatch, tmp_path):
    plugin = ExamplePlugin()
    monkeypatch.setattr(plugin_module, "entry_points", lambda **kwargs: [Point(plugin)])
    configured = config(tmp_path)
    connection = object()
    assert initialize_plugins(connection, configured) == [plugin]
    stats = refresh_plugins(connection, configured, automatic=True)
    assert stats["attempted"] == 1 and stats["succeeded"] == 1 and stats["new_items"] == 2
    assert stats["plugins"][0]["name"] == "example"
    data = {}
    decorate_page(connection, configured, data)
    assert data["decorated"] is True
    fields = plugin_settings_fields(configured)
    assert fields[0]["path"] == "plugin.example.threshold"
    update_plugin_settings(configured, {"plugin.example.threshold": "91"})
    assert plugin.updated == {"threshold": "91"}


def test_missing_enabled_plugin_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module, "entry_points", lambda **kwargs: [])
    with pytest.raises(RuntimeError, match="not installed: example"):
        initialize_plugins(object(), config(tmp_path))


def test_plugin_failure_is_isolated_and_marks_refresh_partial(monkeypatch, tmp_path):
    class Broken(ExamplePlugin):
        def refresh(self, context):
            raise RuntimeError("private source unavailable")

    monkeypatch.setattr(plugin_module, "entry_points", lambda **kwargs: [Point(Broken())])
    stats = refresh_plugins(object(), config(tmp_path))
    assert stats["failed"] == 1
    assert stats["plugins"] == [{
        "name": "example", "status": "failed", "error": "private source unavailable",
    }]


def test_cli_can_enable_and_recover_by_disabling_a_plugin(monkeypatch, tmp_path):
    configured = config(tmp_path)
    configured.data["plugins"]["enabled"] = ""
    configured.data["app"]["database_path"] = str(tmp_path / "reader.sqlite3")
    configured.data["app"]["working_opml_path"] = str(tmp_path / "subscriptions.opml")
    save_config(configured)
    monkeypatch.setattr(plugin_module, "entry_points", lambda **kwargs: [Point(ExamplePlugin())])
    assert main(["--config", str(configured.path), "plugin", "enable", "example"]) == 0
    assert load_config(configured.path).get("plugins", "enabled") == "example"
    # Disable still works after the package disappears, which is the recovery path.
    monkeypatch.setattr(plugin_module, "entry_points", lambda **kwargs: [])
    assert main(["--config", str(configured.path), "plugin", "disable", "example"]) == 0
    assert load_config(configured.path).get("plugins", "enabled") == ""
