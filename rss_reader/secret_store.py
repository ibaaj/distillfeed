from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Mapping


ALLOWED_SECRET_NAMES = frozenset({
    "OPENAI_API_KEY",
    "RSSREADER_PASSWORD",
    "NTFY_TOKEN",
    "ARXIV_NTFY_TOKEN",
})


class SecretStoreError(RuntimeError):
    """A private secret store is malformed or has unsafe filesystem metadata."""


def _reject_link(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise SecretStoreError(f"Refusing symbolic link: {path}")


def _validate(values: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    unknown = sorted(set(values) - ALLOWED_SECRET_NAMES)
    if unknown:
        raise SecretStoreError("Unknown secret name(s): " + ", ".join(unknown))
    for name, value in values.items():
        if not isinstance(value, str):
            raise SecretStoreError(f"{name} must be text")
        if not value or len(value) > 4096 or any(character in value for character in "\r\n\x00"):
            raise SecretStoreError(f"{name} must be non-empty single-line text")
        result[name] = value
    return result


def load_secret_store(path: Path) -> dict[str, str]:
    """Read the strict JSON store without evaluating any of its contents."""
    _reject_link(path)
    if not path.exists():
        return {}
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretStoreError(f"Secret store is not a regular file: {path}")
    if metadata.st_mode & 0o077:
        raise SecretStoreError(
            f"Secret store permissions are too broad: {path}; expected mode 0600"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SecretStoreError(f"Secret store cannot be read: {path}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "environment"}:
        raise SecretStoreError("Secret store must contain version and environment")
    if document["version"] != 1 or not isinstance(document["environment"], dict):
        raise SecretStoreError("Unsupported secret-store format")
    return _validate(document["environment"])


def write_secret_store(path: Path, values: Mapping[str, str]) -> None:
    """Atomically write an owner-only JSON store; an empty mapping removes it."""
    normalized = _validate(values)
    _reject_link(path.parent)
    _reject_link(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if not normalized:
        if path.exists():
            path.unlink()
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": 1, "environment": normalized},
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def merged_secret_environment(path: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return stored values overridden by explicitly supplied process values."""
    result = load_secret_store(path)
    source = os.environ if environ is None else environ
    for name in ALLOWED_SECRET_NAMES:
        if source.get(name):
            result[name] = str(source[name])
    return result
