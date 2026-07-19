from __future__ import annotations

import json
import os
import stat

import pytest

from rss_reader.secret_store import (
    SecretStoreError,
    load_secret_store,
    merged_secret_environment,
    write_secret_store,
)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_secret_store_is_strict_json_owner_only_and_never_shell_sourced(tmp_path):
    path = tmp_path / "private" / "secrets.json"
    shell_text = "sk-'; $(touch should-never-exist); `id`; $HOME; & | < > \\\""
    write_secret_store(path, {"OPENAI_API_KEY": shell_text, "NTFY_TOKEN": "token:$()"})

    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "version": 1,
        "environment": {
            "NTFY_TOKEN": "token:$()",
            "OPENAI_API_KEY": shell_text,
        },
    }
    assert load_secret_store(path) == document["environment"]
    assert not (tmp_path / "should-never-exist").exists()


def test_process_environment_explicitly_overrides_store_without_mutating_it(tmp_path):
    path = tmp_path / "private" / "secrets.json"
    write_secret_store(path, {
        "OPENAI_API_KEY": "stored-openai",
        "NTFY_TOKEN": "stored-ntfy",
    })
    merged = merged_secret_environment(
        path,
        {
            "OPENAI_API_KEY": "process-openai",
            "NTFY_TOKEN": "",
            "UNRELATED": "ignored",
        },
    )
    assert merged == {
        "OPENAI_API_KEY": "process-openai",
        "NTFY_TOKEN": "stored-ntfy",
    }
    assert load_secret_store(path)["OPENAI_API_KEY"] == "stored-openai"


@pytest.mark.parametrize(
    "values",
    [
        {"UNKNOWN": "secret"},
        {"OPENAI_API_KEY": ""},
        {"OPENAI_API_KEY": "one\ntwo"},
        {"OPENAI_API_KEY": "one\rtwo"},
        {"OPENAI_API_KEY": "nul\x00value"},
        {"OPENAI_API_KEY": "x" * 4097},
        {"OPENAI_API_KEY": 123},
    ],
)
def test_secret_store_rejects_unknown_malformed_and_multiline_values(tmp_path, values):
    path = tmp_path / "private" / "secrets.json"
    with pytest.raises(SecretStoreError):
        write_secret_store(path, values)  # type: ignore[arg-type]
    assert not path.exists()


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": 1},
        {"version": 2, "environment": {}},
        {"version": 1, "environment": [], "extra": False},
        {"version": 1, "environment": {"PATH": "/tmp"}},
    ],
)
def test_secret_store_rejects_malformed_documents(tmp_path, document):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(SecretStoreError):
        load_secret_store(path)


def test_secret_store_refuses_links_non_files_and_broad_permissions(tmp_path):
    real = tmp_path / "real.json"
    write_secret_store(real, {"OPENAI_API_KEY": "private"})
    linked = tmp_path / "linked.json"
    linked.symlink_to(real)
    with pytest.raises(SecretStoreError, match="symbolic link"):
        load_secret_store(linked)
    with pytest.raises(SecretStoreError, match="symbolic link"):
        write_secret_store(linked, {"OPENAI_API_KEY": "replacement"})
    assert load_secret_store(real) == {"OPENAI_API_KEY": "private"}

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(SecretStoreError, match="not a regular file"):
        load_secret_store(directory)

    real.chmod(0o640)
    with pytest.raises(SecretStoreError, match="permissions are too broad"):
        load_secret_store(real)


def test_empty_secret_set_removes_existing_file_but_not_private_directory(tmp_path):
    path = tmp_path / "private" / "secrets.json"
    write_secret_store(path, {"OPENAI_API_KEY": "private"})
    write_secret_store(path, {})
    assert not path.exists()
    assert path.parent.is_dir()
    assert _mode(path.parent) == 0o700
    assert load_secret_store(path) == {}


def test_atomic_replacement_never_leaves_temporary_secret_files(tmp_path):
    path = tmp_path / "private" / "secrets.json"
    write_secret_store(path, {"OPENAI_API_KEY": "first"})
    write_secret_store(path, {"OPENAI_API_KEY": "second"})
    assert load_secret_store(path) == {"OPENAI_API_KEY": "second"}
    assert list(path.parent.iterdir()) == [path]
    assert _mode(path) == 0o600
