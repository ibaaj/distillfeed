from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import rss_reader.setup_service as setup_service_module
from distillfeed_arxiv.config import load_plugin_config
from rss_reader.config import load_config
from rss_reader.db import connect
from rss_reader.opml import build_tree_from_database, parse_opml_bytes
from rss_reader.secret_store import load_secret_store
from rss_reader.setup_service import (
    MANIFEST_NAME,
    SECRET_RELATIVE_PATH,
    SETUP_SCHEMA_VERSION,
    SetupCommitError,
    SetupCommitter,
    SetupRecoveryRequired,
    normalize_setup_payload,
    preset_payload,
    public_review,
    verify_instance,
)
from rss_reader.setup_state import CommitEvent, CommitPhase


FAULT_STEPS = (
    "stage-created",
    "files-staged",
    "database-staged",
    "stage-verified",
    "stage-marked",
    "instance-published",
    "postcheck-complete",
)


class InjectedSetupFailure(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _ports_are_deterministically_available(monkeypatch):
    # Port conflict semantics have a dedicated transition test below. The
    # filesystem transaction tests must not depend on what the test host runs.
    monkeypatch.setattr("rss_reader.setup_service._port_available", lambda _port: True)


def _draft(profile="recommended", **changes):
    payload = preset_payload(profile)
    payload.update(changes)
    if payload["ai_provider"] == "openai" and not payload["openai_key"]:
        payload["openai_key"] = "test-key-never-sent"
    return normalize_setup_payload(payload, environment={}, check_port=False)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _managed_paths(state_root: Path):
    return sorted(state_root.glob(".setup-stage-*")), state_root / "instance"


def _assert_no_partial_instance(state_root: Path):
    stages, instance = _managed_paths(state_root)
    assert not instance.exists() and not instance.is_symlink()
    assert stages == []


def test_atomic_commit_publishes_a_complete_private_recommended_instance(tmp_path):
    state_root = tmp_path / ".distillfeed"
    committer = SetupCommitter(state_root)
    result = committer.commit(_draft())
    instance = state_root / "instance"

    assert result.instance_path == instance
    assert result.config_path == instance / "config.toml"
    assert result.reader_url == "http://127.0.0.1:8080/"
    assert result.environment == {}
    assert result.public() == {
        "instance_path": str(instance),
        "reader_url": "http://127.0.0.1:8080/",
        "message": "Setup is complete. DistillFeed is starting.",
    }
    assert list(state_root.glob(".setup-stage-*")) == []
    verified = verify_instance(instance)
    assert verified["config"] == str(instance / "config.toml")

    for root, directories, files in os.walk(instance):
        assert _mode(Path(root)) == 0o700
        for directory in directories:
            assert _mode(Path(root) / directory) == 0o700
        for filename in files:
            assert _mode(Path(root) / filename) == 0o600

    config = load_config(result.config_path)
    assert config.get("app", "mode") == "local"
    assert config.get("app", "host") == "127.0.0.1"
    assert config.get("app", "trusted_hosts") == "127.0.0.1,localhost"
    assert config.get("app", "starter_subscriptions") is True
    assert config.get("llm", "enabled") is False
    assert config.get("weather", "enabled") is False
    assert config.get("notifications", "ntfy")["enabled"] is False
    assert config.database_path == instance / "data" / "reader.sqlite3"
    assert config.working_opml_path == instance / "data" / "subscriptions.opml"

    with connect(config.database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM notification_deliveries"
        ).fetchone()[0] == 0
        assert parse_opml_bytes(config.working_opml_path.read_bytes()) == (
            build_tree_from_database(connection)
        )

    manifest = json.loads((instance / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema"] == SETUP_SCHEMA_VERSION
    assert manifest["state"] == "ready"
    assert manifest["profile"] == "recommended"
    assert manifest["secret_store_present"] is False
    assert manifest["installation_id"]
    assert not (instance / SECRET_RELATIVE_PATH).exists()
    assert committer.commit_phase is CommitPhase.COMPLETE
    assert committer.commit_history == [
        (CommitEvent.BEGIN.value, CommitPhase.STAGING_FILES.value),
        (CommitEvent.FILES_STAGED.value, CommitPhase.STAGING_DATABASE.value),
        (CommitEvent.DATABASE_STAGED.value, CommitPhase.VERIFYING_STAGE.value),
        (CommitEvent.VERIFIED.value, CommitPhase.MARKING_STAGE.value),
        (CommitEvent.MARKED.value, CommitPhase.PUBLISHING.value),
        (CommitEvent.PUBLISHED.value, CommitPhase.POSTCHECK.value),
        (CommitEvent.POSTCHECKED.value, CommitPhase.COMPLETE.value),
    ]


def test_empty_subscription_profile_still_has_exact_database_opml_bisimulation(tmp_path):
    state_root = tmp_path / ".distillfeed"
    result = SetupCommitter(state_root).commit(_draft(subscriptions="empty"))
    config = load_config(result.config_path)
    with connect(config.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 0
        assert parse_opml_bytes(config.working_opml_path.read_bytes()) == (
            build_tree_from_database(connection)
        )
    verify_instance(result.instance_path)


def test_demo_commit_keeps_ordinary_and_arxiv_windows_distinct_and_unadvanced(tmp_path):
    state_root = tmp_path / ".distillfeed"
    result = SetupCommitter(state_root).commit(_draft("demo"))
    config = load_config(result.config_path)

    assert result.reader_url == "http://127.0.0.1:8081/"
    assert config.get("plugins", "arxiv_digest_enabled") is True
    assert config.get("llm", "minimum_relevance") == 40
    assert config.get("llm", "rolling_digest_hours") == 7 * 24
    assert config.get("llm", "candidate_max_age_days") == 30
    assert config.get("llm", "monthly_budget_usd") == 2.0
    assert config.get("app", "background_scheduler_enabled") is False
    assert config.get("app", "auto_summarize_after_refresh") is False

    arxiv = load_plugin_config(config)
    assert arxiv["arxiv"]["categories"] == ["cs.AI"]
    assert arxiv["arxiv"]["initial_lookback_days"] == 7
    assert arxiv["arxiv"]["api_backfill_enabled"] is True
    assert arxiv["filters"]["final_keep_threshold"] == 25

    with connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='distillfeed_arxiv_state'"
        ).fetchone()
        assert connection.execute(
            "SELECT value FROM distillfeed_arxiv_state WHERE key='last_complete_at'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM feeds WHERE xml_url='plugin://arxiv/cs.AI'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0
    verify_instance(result.instance_path)


def test_demo_setup_ignores_an_expert_arxiv_recipe_override(tmp_path, monkeypatch):
    external_recipe = tmp_path / "expert-arxiv.toml"
    neutral_recipe = (
        Path(__file__).parents[1]
        / "distillfeed_arxiv"
        / "resources"
        / "arxiv-digest.example.toml"
    ).read_text(encoding="utf-8")
    sentinel = "PRIVATE_SETUP_SENTINEL"
    assert "preferred_authors = []" in neutral_recipe
    external_recipe.write_text(
        neutral_recipe.replace(
            "preferred_authors = []",
            f'preferred_authors = ["{sentinel}"]',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISTILLFEED_ARXIV_CONFIG", str(external_recipe))

    result = SetupCommitter(tmp_path / ".distillfeed").commit(_draft("demo"))
    generated_recipe = result.instance_path / "arxiv-digest.toml"

    assert sentinel not in generated_recipe.read_text(encoding="utf-8")
    generated = load_plugin_config(load_config(result.config_path), environment={})
    assert generated["filters"]["preferred_authors"] == []


def test_setup_performs_no_network_model_weather_or_notification_work(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("setup attempted an external operation")

    monkeypatch.setattr("rss_reader.service.safe_get", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.fetch.fetch_rss", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.fetch.fetch_api_window", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.llm.rerank", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.llm.daily_digest", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.notifications.deliver_arxiv_pushes", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.plugin.fetch_rss", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.plugin.fetch_api_window", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.plugin.rerank", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.plugin.daily_digest", forbidden)
    monkeypatch.setattr("distillfeed_arxiv.plugin.deliver_arxiv_pushes", forbidden)
    monkeypatch.setattr("rss_reader.notifications.deliver_ntfy_for_job", forbidden)

    result = SetupCommitter(tmp_path / ".distillfeed").commit(_draft("demo"))
    config = load_config(result.config_path)
    with connect(config.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM notification_deliveries"
        ).fetchone()[0] == 0


def test_secrets_are_absent_from_review_config_database_opml_and_manifest(tmp_path):
    state_root = tmp_path / ".distillfeed"
    openai_secret = "sk-PRIVATE-$(touch PWNED);`id`;$HOME;&|<>-8ca2b7"
    ntfy_secret = "ntfy-PRIVATE-';$(uname)-936da2"
    draft = _draft(
        "demo",
        openai_key=openai_secret,
        ntfy_enabled=True,
        ntfy_topic="private-demo-topic",
        ntfy_token=ntfy_secret,
        auto_summarize=True,
    )
    review = public_review(draft, state_root / "instance")
    assert openai_secret not in repr(review)
    assert ntfy_secret not in repr(review)

    result = SetupCommitter(state_root).commit(draft)
    assert result.environment == {
        "OPENAI_API_KEY": openai_secret,
        "NTFY_TOKEN": ntfy_secret,
    }
    secret_path = result.instance_path / SECRET_RELATIVE_PATH
    assert load_secret_store(secret_path) == result.environment
    assert _mode(secret_path) == 0o600
    assert _mode(secret_path.parent) == 0o700

    secret_files = []
    for path in result.instance_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if openai_secret.encode() in data or ntfy_secret.encode() in data:
                secret_files.append(path.relative_to(result.instance_path))
    assert secret_files == [SECRET_RELATIVE_PATH]
    assert not (tmp_path / "PWNED").exists()
    assert openai_secret not in repr(result)
    assert ntfy_secret not in repr(result)
    manifest = json.loads((result.instance_path / MANIFEST_NAME).read_text())
    assert manifest["secret_store_present"] is True
    assert openai_secret not in repr(manifest)
    assert ntfy_secret not in repr(manifest)


def test_launch_only_secret_is_returned_to_process_and_never_persisted(tmp_path):
    secret = "launch-only-openai-secret"
    result = SetupCommitter(tmp_path / ".distillfeed").commit(
        _draft("demo", openai_key=secret, store_openai_key=False)
    )
    assert result.environment == {"OPENAI_API_KEY": secret}
    assert not (result.instance_path / SECRET_RELATIVE_PATH).exists()
    for path in result.instance_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_publish_boundary_exposes_either_no_instance_or_a_complete_instance(tmp_path):
    state_root = tmp_path / ".distillfeed"
    observations = {}

    def observe(step):
        stages = list(state_root.glob(".setup-stage-*"))
        instance = state_root / "instance"
        observations[step] = {
            "stage_count": len(stages),
            "instance": instance.exists(),
            "manifest": (instance / MANIFEST_NAME).is_file(),
            "config": (instance / "config.toml").is_file(),
            "database": (instance / "data" / "reader.sqlite3").is_file(),
            "opml": (instance / "data" / "subscriptions.opml").is_file(),
        }
        if step in FAULT_STEPS[:5]:
            assert not instance.exists()
            assert len(stages) == 1
        else:
            assert not stages
            assert instance.is_dir()
            assert all(observations[step][name] for name in ("manifest", "config", "database", "opml"))

    result = SetupCommitter(state_root, fault=observe).commit(_draft(subscriptions="empty"))
    assert tuple(observations) == FAULT_STEPS
    assert observations["stage-created"]["instance"] is False
    assert observations["stage-marked"]["instance"] is False
    assert observations["instance-published"]["manifest"] is True
    verify_instance(result.instance_path)


@pytest.mark.parametrize("failing_step", FAULT_STEPS)
def test_fault_at_every_commit_step_rolls_back_without_partial_or_stale_stage(
    tmp_path, failing_step,
):
    state_root = tmp_path / ".distillfeed"

    def fail(step):
        if step == failing_step:
            raise InjectedSetupFailure(step)

    committer = SetupCommitter(state_root, fault=fail)
    with pytest.raises(InjectedSetupFailure, match=failing_step):
        committer.commit(_draft(subscriptions="empty"))
    _assert_no_partial_instance(state_root)
    assert committer.commit_phase is CommitPhase.ROLLED_BACK
    assert committer.commit_history[-1] == (
        CommitEvent.ROLLBACK.value,
        CommitPhase.ROLLED_BACK.value,
    )

    # The same path is immediately retryable after every recoverable failure.
    result = SetupCommitter(state_root).commit(_draft(subscriptions="empty"))
    verify_instance(result.instance_path)


def test_failure_while_writing_stage_ownership_marker_leaves_no_unknown_stage(
    tmp_path, monkeypatch,
):
    """The earliest disk failure must not create a permanent setup dead end."""
    state_root = tmp_path / ".distillfeed"
    real_write = setup_service_module._write_private_text

    def fail_marker(path, text):
        if Path(path).name == ".distillfeed-stage":
            raise OSError("injected marker write failure")
        return real_write(path, text)

    monkeypatch.setattr("rss_reader.setup_service._write_private_text", fail_marker)
    committer = SetupCommitter(state_root)
    with pytest.raises(OSError, match="marker write failure"):
        committer.commit(_draft(subscriptions="empty"))
    assert committer.commit_phase is CommitPhase.ROLLED_BACK
    _assert_no_partial_instance(state_root)


def test_same_committer_can_retry_cleanly_after_a_rolled_back_failure(tmp_path):
    state_root = tmp_path / ".distillfeed"
    should_fail = True

    def fail_once(step):
        nonlocal should_fail
        if should_fail and step == "database-staged":
            should_fail = False
            raise InjectedSetupFailure(step)

    committer = SetupCommitter(state_root, fault=fail_once)
    with pytest.raises(InjectedSetupFailure):
        committer.commit(_draft(subscriptions="empty"))
    assert committer.commit_phase is CommitPhase.ROLLED_BACK
    _assert_no_partial_instance(state_root)

    result = committer.commit(_draft(subscriptions="empty"))
    assert committer.commit_phase is CommitPhase.COMPLETE
    assert committer.commit_history[0] == (
        CommitEvent.BEGIN.value,
        CommitPhase.STAGING_FILES.value,
    )
    assert all(event != CommitEvent.ROLLBACK.value for event, _phase in committer.commit_history)
    verify_instance(result.instance_path)


def test_port_lost_between_review_and_publish_rolls_back_to_retryable_absence(
    tmp_path, monkeypatch,
):
    state_root = tmp_path / ".distillfeed"
    availability = iter((True, False))
    monkeypatch.setattr(
        "rss_reader.setup_service._port_available",
        lambda _port: next(availability),
    )
    committer = SetupCommitter(state_root)
    with pytest.raises(SetupCommitError, match="became unavailable"):
        committer.commit(_draft(subscriptions="empty"))
    assert committer.commit_phase is CommitPhase.ROLLED_BACK
    assert committer.commit_history[-2:] == [
        (CommitEvent.MARKED.value, CommitPhase.PUBLISHING.value),
        (CommitEvent.ROLLBACK.value, CommitPhase.ROLLED_BACK.value),
    ]
    _assert_no_partial_instance(state_root)


def test_failed_rollback_enters_explicit_nonretryable_recovery_state(
    tmp_path, monkeypatch,
):
    state_root = tmp_path / ".distillfeed"
    committer = SetupCommitter(
        state_root,
        fault=lambda step: (_ for _ in ()).throw(InjectedSetupFailure(step))
        if step == "instance-published"
        else None,
    )
    real_replace = os.replace

    def fail_only_publish_rollback(source, destination):
        if Path(source) == committer.instance and Path(destination).name.startswith(
            ".setup-stage-"
        ):
            raise OSError("injected rollback filesystem failure")
        return real_replace(source, destination)

    monkeypatch.setattr("rss_reader.setup_service.os.replace", fail_only_publish_rollback)
    with pytest.raises(SetupRecoveryRequired, match="could not confirm a safe rollback"):
        committer.commit(_draft(subscriptions="empty"))
    assert committer.commit_phase is CommitPhase.RECOVERY_REQUIRED
    assert committer.commit_history[-1] == (
        CommitEvent.ROLLBACK_FAILED.value,
        CommitPhase.RECOVERY_REQUIRED.value,
    )
    assert committer.instance.is_dir()
    verify_instance(committer.instance)
    with pytest.raises(SetupCommitError, match="recovery is required"):
        committer.commit(_draft(subscriptions="empty"))


def test_commit_refuses_existing_instance_without_changing_any_byte(tmp_path):
    state_root = tmp_path / ".distillfeed"
    instance = state_root / "instance"
    instance.mkdir(parents=True)
    sentinel = instance / "owned-by-user"
    sentinel.write_bytes(b"preserve exactly")

    with pytest.raises(SetupCommitError, match="already exists"):
        SetupCommitter(state_root).commit(_draft(subscriptions="empty"))
    assert sentinel.read_bytes() == b"preserve exactly"
    assert list(instance.iterdir()) == [sentinel]
    assert list(state_root.glob(".setup-stage-*")) == []


def test_commit_refuses_instance_symlink_and_preserves_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    state_root = tmp_path / ".distillfeed"
    state_root.mkdir()
    (state_root / "instance").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SetupCommitError, match="symbolic link"):
        SetupCommitter(state_root).commit(_draft(subscriptions="empty"))
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert list(outside.iterdir()) == [sentinel]


def test_commit_refuses_symlinked_state_root_instead_of_resolving_through_it(tmp_path):
    real_root = tmp_path / "outside-managed-root"
    real_root.mkdir()
    linked_root = tmp_path / ".distillfeed"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(SetupCommitError, match="symbolic link"):
        SetupCommitter(linked_root).commit(_draft(subscriptions="empty"))
    assert list(real_root.iterdir()) == []


def test_unrelated_unmarked_directory_is_never_deleted_during_rollback(tmp_path):
    state_root = tmp_path / ".distillfeed"
    unrelated = state_root / ".setup-stage-user-owned"
    unrelated.mkdir(parents=True)
    sentinel = unrelated / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")

    with pytest.raises(InjectedSetupFailure):
        SetupCommitter(
            state_root,
            fault=lambda step: (_ for _ in ()).throw(InjectedSetupFailure(step))
            if step == "files-staged"
            else None,
        ).commit(_draft(subscriptions="empty"))
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (state_root / "instance").exists()
    assert list(unrelated.iterdir()) == [sentinel]


def test_two_committers_cannot_publish_two_instances_or_overwrite_winner(tmp_path):
    state_root = tmp_path / ".distillfeed"
    both_staged = Barrier(2, timeout=10)

    def synchronize(step):
        if step == "stage-created":
            both_staged.wait()

    def commit(port):
        return SetupCommitter(state_root, fault=synchronize).commit(
            _draft(subscriptions="empty", port=port)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(commit, 8080), pool.submit(commit, 8081)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=20))
            except Exception as exc:  # One atomic publication must lose.
                outcomes.append(exc)

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert list(state_root.glob(".setup-stage-*")) == []
    verified = verify_instance(state_root / "instance")
    assert verified["reader_url"] in {
        "http://127.0.0.1:8080/",
        "http://127.0.0.1:8081/",
    }


def test_arxiv_watermark_is_forbidden_and_publish_hash_is_checked_only_at_commit(tmp_path):
    result = SetupCommitter(tmp_path / ".distillfeed").commit(_draft("demo"))
    config = load_config(result.config_path)
    with connect(config.database_path) as connection:
        connection.execute(
            "INSERT INTO distillfeed_arxiv_state(key,value) VALUES('last_complete_at', ?)",
            ("2026-07-19T00:00:00+00:00",),
        )
    with pytest.raises(SetupCommitError, match="watermark"):
        verify_instance(result.instance_path, require_pristine_setup=True)

    with connect(config.database_path) as connection:
        connection.execute(
            "DELETE FROM distillfeed_arxiv_state WHERE key='last_complete_at'"
        )
    result.config_path.write_text(
        result.config_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    # Settings are deliberately editable after installation, so an ordinary
    # launch accepts valid changed TOML. The stricter reviewed hash is supplied
    # only during the setup publish transition.
    verify_instance(result.instance_path)
    manifest = json.loads((result.instance_path / MANIFEST_NAME).read_text())
    with pytest.raises(SetupCommitError, match="published configuration"):
        verify_instance(
            result.instance_path,
            expected_config_sha256=manifest["config_sha256"],
        )


def test_structural_verification_allows_relaunch_after_normal_runtime_history(tmp_path):
    """A healthy used reader must never be mistaken for a dirty setup stage."""
    result = SetupCommitter(tmp_path / ".distillfeed").commit(_draft("demo"))
    config = load_config(result.config_path)
    with connect(config.database_path) as connection:
        connection.execute(
            """INSERT INTO refresh_runs(
                   started_at,completed_at,status,feeds_attempted,feeds_succeeded,new_items
               ) VALUES(?,?,?,?,?,?)""",
            (
                "2026-07-19T10:00:00+00:00",
                "2026-07-19T10:00:01+00:00",
                "success",
                4,
                4,
                12,
            ),
        )
        connection.execute(
            "INSERT INTO distillfeed_arxiv_state(key,value) VALUES('last_complete_at', ?)",
            ("2026-07-19T10:00:01+00:00",),
        )

    verified = verify_instance(result.instance_path)
    assert verified["reader_url"] == "http://127.0.0.1:8081/"
    with pytest.raises(SetupCommitError, match="watermark"):
        verify_instance(result.instance_path, require_pristine_setup=True)
