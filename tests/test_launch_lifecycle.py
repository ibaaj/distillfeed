from __future__ import annotations

import json
import os
import copy
import socket
from pathlib import Path

import pytest

from rss_reader.config import DEFAULTS, Config, load_config, save_config
from rss_reader.launcher import (
    STAGE_MARKER,
    STAGE_MARKER_CONTENT,
    LaunchError,
    TargetKind,
    _apply_managed_secrets,
    classify_target,
    clean_stale_setup_stages,
    launcher_lock,
    main as launcher_main,
    serve_reader,
    verify_managed_instance,
)
from rss_reader.secret_store import write_secret_store
from rss_reader.setup_service import (
    SECRET_RELATIVE_PATH,
    SetupCommitter,
    SetupRecoveryRequired,
    normalize_setup_payload,
    preset_payload,
)


def test_target_classification_never_creates_or_overwrites_configuration(tmp_path):
    assert classify_target(tmp_path, environ={}).kind == TargetKind.FIRST_RUN

    legacy = tmp_path / "config.toml"
    legacy.write_text("[app]\n", encoding="utf-8")
    target = classify_target(tmp_path, environ={})
    assert target.kind == TargetKind.LEGACY
    assert target.config_path == legacy.resolve()
    assert legacy.read_text(encoding="utf-8") == "[app]\n"

    external = tmp_path / "external.toml"
    external.write_text("[app]\n", encoding="utf-8")
    target = classify_target(
        tmp_path, environ={"RSSREADER_CONFIG": "external.toml"}
    )
    assert target.kind == TargetKind.EXTERNAL
    assert target.config_path == external.resolve()


def test_explicit_missing_configuration_is_an_error_not_first_setup(tmp_path):
    with pytest.raises(LaunchError, match="does not exist"):
        classify_target(
            tmp_path, environ={"RSSREADER_CONFIG": "missing.toml"}
        )
    assert not (tmp_path / ".distillfeed" / "instance").exists()


def test_ambiguous_legacy_and_managed_targets_are_not_guessed(tmp_path):
    (tmp_path / "config.toml").write_text("[app]\n", encoding="utf-8")
    instance = tmp_path / ".distillfeed" / "instance"
    instance.mkdir(parents=True)
    (instance / "config.toml").write_text("[app]\n", encoding="utf-8")
    with pytest.raises(LaunchError, match="Two DistillFeed configurations"):
        classify_target(tmp_path, environ={})


def test_cleanup_removes_only_exactly_marked_direct_stages(tmp_path):
    state_root = tmp_path / ".distillfeed"
    state_root.mkdir()
    marked = state_root / ".setup-stage-marked"
    marked.mkdir()
    (marked / STAGE_MARKER).write_bytes(STAGE_MARKER_CONTENT)
    (marked / "partial-config").write_text("partial", encoding="utf-8")

    wrong_marker = state_root / ".setup-stage-wrong"
    wrong_marker.mkdir()
    (wrong_marker / STAGE_MARKER).write_text("not ours\n", encoding="utf-8")
    unmarked = state_root / ".setup-stage-unmarked"
    unmarked.mkdir()
    unrelated = state_root / "some-other-directory"
    unrelated.mkdir()

    removed = clean_stale_setup_stages(state_root)

    assert removed == [marked]
    assert not marked.exists()
    assert wrong_marker.is_dir()
    assert unmarked.is_dir()
    assert unrelated.is_dir()


def test_second_launcher_fails_fast_without_mutating_the_first_lock(tmp_path):
    with launcher_lock(tmp_path) as state_root:
        lock_path = state_root / "launch.lock"
        first_pid = lock_path.read_text(encoding="ascii")
        with pytest.raises(LaunchError, match="already starting or running"):
            with launcher_lock(tmp_path):
                pass
        assert lock_path.read_text(encoding="ascii") == first_pid


def _committed_instance(tmp_path: Path) -> Path:
    payload = preset_payload("recommended")
    payload["port"] = 48181
    draft = normalize_setup_payload(payload, environment={}, check_port=False)
    result = SetupCommitter(tmp_path / ".distillfeed").commit(draft)
    return result.instance_path


def test_runtime_verification_accepts_legitimate_config_changes_after_setup(tmp_path):
    instance = _committed_instance(tmp_path)
    config = load_config(instance / "config.toml")
    config.data["app"]["port"] = 48182
    save_config(config)

    verified = verify_managed_instance(instance)

    assert verified.get("app", "port") == 48182
    # The setup receipt remains a historical commit record, not an immutable
    # hash that would dead-end after an ordinary Settings save.
    manifest = json.loads((instance / "setup.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "ready"


def test_runtime_verification_rejects_managed_paths_outside_instance(tmp_path):
    instance = _committed_instance(tmp_path)
    config = load_config(instance / "config.toml")
    config.data["app"]["database_path"] = str(tmp_path / "outside.sqlite3")
    save_config(config)

    with pytest.raises(LaunchError, match="must remain inside"):
        verify_managed_instance(instance)


def test_runtime_verification_recovers_derived_opml_after_interrupted_export(tmp_path):
    instance = _committed_instance(tmp_path)
    config = load_config(instance / "config.toml")
    config.working_opml_path.write_text("<partial", encoding="utf-8")

    verified = verify_managed_instance(instance)

    assert verified.path == config.path
    assert config.working_opml_path.read_text(encoding="utf-8").startswith("<?xml")


def test_environment_overrides_stored_secret_but_one_launch_choice_is_final(
    tmp_path, monkeypatch
):
    instance = tmp_path / "instance"
    path = instance / SECRET_RELATIVE_PATH
    write_secret_store(path, {"OPENAI_API_KEY": "stored-value"})
    monkeypatch.setenv("OPENAI_API_KEY", "terminal-value")
    # Register restoration before the production helper replaces the value
    # directly in os.environ.
    monkeypatch.setenv("DISTILLFEED_ARXIV_CONFIG", "expert-value-to-replace")

    _apply_managed_secrets(instance)
    assert os.environ["OPENAI_API_KEY"] == "terminal-value"

    _apply_managed_secrets(instance, one_launch={"OPENAI_API_KEY": "wizard-value"})
    assert os.environ["OPENAI_API_KEY"] == "wizard-value"
    assert os.environ["DISTILLFEED_ARXIV_CONFIG"] == str(instance / "arxiv-digest.toml")


def test_managed_recipe_is_pinned_and_an_optional_symlink_is_refused(tmp_path, monkeypatch):
    instance = _committed_instance(tmp_path)
    outside = tmp_path / "outside-arxiv.toml"
    outside.write_text("private expert recipe", encoding="utf-8")
    monkeypatch.setenv("DISTILLFEED_ARXIV_CONFIG", str(outside))

    _apply_managed_secrets(instance)
    assert os.environ["DISTILLFEED_ARXIV_CONFIG"] == str(instance / "arxiv-digest.toml")

    (instance / "arxiv-digest.toml").symlink_to(outside)
    with pytest.raises(LaunchError, match="arXiv settings path is unsafe"):
        verify_managed_instance(instance)


def test_secret_loader_refuses_a_private_directory_symlink(tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (instance / "private").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LaunchError, match="secret directory is unsafe"):
        _apply_managed_secrets(instance)


def test_occupied_port_is_reported_before_application_initialization(
    tmp_path, monkeypatch
):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    data = copy.deepcopy(DEFAULTS)
    data["app"]["port"] = port
    config_path = tmp_path / "config.toml"
    save_config(Config(config_path, data))
    initialized = False

    def unexpected_initialization(_config_path):
        nonlocal initialized
        initialized = True
        raise AssertionError("create_app must not run while the port is occupied")

    monkeypatch.setattr("rss_reader.launcher.create_app", unexpected_initialization)
    try:
        with pytest.raises(LaunchError, match="did not reinstall or change your setup"):
            serve_reader(config_path, open_browser=False)
    finally:
        listener.close()
    assert initialized is False


def test_launcher_reports_terminal_setup_recovery_without_traceback(
    tmp_path, monkeypatch, capsys,
):
    state_root = tmp_path / ".distillfeed"

    def require_recovery(*_args, **_kwargs):
        raise SetupRecoveryRequired(
            f"Preserve {state_root} and relaunch DistillFeed for verification."
        )

    monkeypatch.setattr("rss_reader.setup_web.run_setup", require_recovery)

    assert launcher_main(["--root", str(tmp_path), "--no-browser"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Preserve" in captured.err
    assert str(state_root) in captured.err
    assert "Traceback" not in captured.err


def test_launch_shells_are_small_strict_and_do_not_require_node():
    root = Path(__file__).resolve().parents[1]
    install = root / "install.sh"
    launch = root / "launch.sh"
    for script in (install, launch):
        assert os.access(script, os.X_OK)
        text = script.read_text(encoding="utf-8")
        assert "set -eu" in text
        assert "umask 077" in text
        assert "source " not in text
        assert "eval " not in text
    launch_text = launch.read_text(encoding="utf-8")
    install_text = install.read_text(encoding="utf-8")
    # importlib.metadata.EntryPoints uses name-based __getitem__ on current
    # Python releases. Freeze it to an ordinary tuple before the verifier loads
    # the one bundled plugin by numeric position.
    assert "entries = tuple(importlib.metadata.entry_points(" in install_text
    assert '"$ROOT/install.sh" --check' in launch_text
    assert "exec \"$ROOT/.venv/bin/python\" -m rss_reader.launcher" in launch_text
    combined = install_text + launch_text
    assert "npm " not in combined
    assert "node " not in combined.casefold()
