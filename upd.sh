#!/usr/bin/env bash

if ((BASH_VERSINFO[0] < 3 || (BASH_VERSINFO[0] == 3 && BASH_VERSINFO[1] < 2))); then
    printf 'Error: DistillFeed requires Bash 3.2 or newer.\n' >&2
    exit 1
fi

set -Eeuo pipefail
umask 077

EXPECTED_VERSION="0.22.0"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_ARCHIVE="$SCRIPT_DIR/distillfeed-$EXPECTED_VERSION.tar.gz"

ARCHIVE="${ARCHIVE:-$DEFAULT_ARCHIVE}"
CONFIG_ARGUMENT=""
ALLOW_ACTIVE=0
KEEP_STAGE=0
INSTALL_ARGUMENT=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] INSTALL_DIR

Upgrade an existing DistillFeed installation to $EXPECTED_VERSION without
network, feed-refresh, or AI-provider calls.

Options:
  --archive PATH    Release archive (default: $DEFAULT_ARCHIVE)
  --config PATH     Existing config.toml (default: the one unambiguous local instance)
  --allow-active    Bypass legacy process/database checks (filesystem locks remain mandatory)
  --keep-stage      Retain the temporary staging directory after success
  -h, --help        Show this help

Stop the DistillFeed web service and scheduled jobs before running this script.
The installation directory is mandatory and has no implicit default.
EOF
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '%s\n' "$*"
}

while (($#)); do
    case "$1" in
        --archive)
            (($# >= 2)) || fail "--archive requires a path"
            ARCHIVE="$2"
            shift 2
            ;;
        --config)
            (($# >= 2)) || fail "--config requires a path"
            CONFIG_ARGUMENT="$2"
            shift 2
            ;;
        --allow-active)
            ALLOW_ACTIVE=1
            shift
            ;;
        --keep-stage)
            KEEP_STAGE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            (($# == 1)) || fail "provide exactly one INSTALL_DIR"
            INSTALL_ARGUMENT="$1"
            shift
            ;;
        -*)
            fail "unknown option: $1"
            ;;
        *)
            [[ -z "$INSTALL_ARGUMENT" ]] || fail "provide exactly one INSTALL_DIR"
            INSTALL_ARGUMENT="$1"
            shift
            ;;
    esac
done

[[ -n "$INSTALL_ARGUMENT" ]] || { usage >&2; fail "INSTALL_DIR is required"; }

for command in python3 find cp mv rm chmod; do
    command -v "$command" >/dev/null 2>&1 || fail "required command is unavailable: $command"
done

python3 - <<'PY' || fail "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

[[ -d "$INSTALL_ARGUMENT" ]] || fail "installation directory not found: $INSTALL_ARGUMENT"
INSTALL_DIR="$(cd -- "$INSTALL_ARGUMENT" && pwd -P)"
[[ "$INSTALL_DIR" != "/" ]] || fail "refusing to update the filesystem root"
if [[ -n "${HOME:-}" ]]; then
    HOME_REAL="$(cd -- "$HOME" 2>/dev/null && pwd -P || true)"
    [[ -z "$HOME_REAL" || "$INSTALL_DIR" != "$HOME_REAL" ]] \
        || fail "refusing to use the home directory itself as INSTALL_DIR"
fi

[[ -f "$INSTALL_DIR/pyproject.toml" ]] \
    || fail "INSTALL_DIR is not a source installation: missing pyproject.toml"
[[ -d "$INSTALL_DIR/rss_reader" ]] \
    || fail "INSTALL_DIR is not a DistillFeed installation: missing rss_reader/"
[[ ! -L "$INSTALL_DIR/.venv" && -d "$INSTALL_DIR/.venv" ]] \
    || fail "existing virtual environment must be a real directory: $INSTALL_DIR/.venv"
[[ -x "$INSTALL_DIR/.venv/bin/python" ]] \
    || fail "existing virtual-environment Python is missing: $INSTALL_DIR/.venv/bin/python"

if [[ -z "$CONFIG_ARGUMENT" ]]; then
    LEGACY_CONFIG="$INSTALL_DIR/config.toml"
    MANAGED_INSTANCE="$INSTALL_DIR/.distillfeed/instance"
    LEGACY_PRESENT=0
    MANAGED_PRESENT=0
    [[ -e "$LEGACY_CONFIG" || -L "$LEGACY_CONFIG" ]] && LEGACY_PRESENT=1
    [[ -e "$MANAGED_INSTANCE" || -L "$MANAGED_INSTANCE" ]] && MANAGED_PRESENT=1
    if [[ "$LEGACY_PRESENT" -eq 1 && "$MANAGED_PRESENT" -eq 1 ]]; then
        fail "two local installations were found (config.toml and .distillfeed/instance); use --config to choose explicitly"
    elif [[ "$MANAGED_PRESENT" -eq 1 ]]; then
        [[ ! -L "$MANAGED_INSTANCE" && -d "$MANAGED_INSTANCE" ]] \
            || fail "managed instance path is unsafe or incomplete: $MANAGED_INSTANCE"
        [[ ! -L "$MANAGED_INSTANCE/config.toml" && -f "$MANAGED_INSTANCE/config.toml" ]] \
            || fail "managed configuration is missing or unsafe: $MANAGED_INSTANCE/config.toml"
        CONFIG_ARGUMENT="$MANAGED_INSTANCE/config.toml"
    elif [[ "$LEGACY_PRESENT" -eq 1 ]]; then
        [[ -f "$LEGACY_CONFIG" ]] || fail "legacy configuration is not a regular file: $LEGACY_CONFIG"
        CONFIG_ARGUMENT="$LEGACY_CONFIG"
    else
        fail "no existing configuration was found (expected config.toml or .distillfeed/instance/config.toml)"
    fi
elif [[ "$CONFIG_ARGUMENT" != /* ]]; then
    CONFIG_ARGUMENT="$INSTALL_DIR/$CONFIG_ARGUMENT"
fi
[[ -f "$CONFIG_ARGUMENT" ]] || fail "configuration file not found: $CONFIG_ARGUMENT"
CONFIG_PATH="$(python3 - "$CONFIG_ARGUMENT" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve(strict=True))
PY
)"

[[ -f "$ARCHIVE" ]] || fail "release archive not found: $ARCHIVE"
[[ ! -L "$ARCHIVE" ]] || fail "release archive must not be a symbolic link: $ARCHIVE"
ARCHIVE="$(python3 - "$ARCHIVE" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve(strict=True))
PY
)"

PYTHON="$INSTALL_DIR/.venv/bin/python"
"$PYTHON" - <<'PY' || fail "the existing virtual environment must use Python 3.11 or newer"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
"$PYTHON" -m pip --version >/dev/null 2>&1 \
    || fail "pip is unavailable in the existing virtual environment"
"$PYTHON" - "$INSTALL_DIR/pyproject.toml" "$EXPECTED_VERSION" <<'PY' \
    || fail "refusing to downgrade from a newer DistillFeed release"
import sys
import tomllib
import re

with open(sys.argv[1], "rb") as handle:
    current_text = str(tomllib.load(handle).get("project", {}).get("version", ""))
def release(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise SystemExit(f"Expected a three-part release version, found {value!r}")
    return tuple(int(part) for part in match.groups())

current = release(current_text)
target = release(sys.argv[2])
if current > target:
    raise SystemExit(f"Installed DistillFeed {current_text} is newer than target {sys.argv[2]}")
print(f"Upgrade path: {current_text} -> {sys.argv[2]}")
PY

# No updater phase is permitted to contact feeds, model providers, notification
# services, or package indexes.
ARXIV_CONFIG_OVERRIDE="${DISTILLFEED_ARXIV_CONFIG:-}"
if [[ -n "$ARXIV_CONFIG_OVERRIDE" && "$ARXIV_CONFIG_OVERRIDE" != /* ]]; then
    fail "DISTILLFEED_ARXIV_CONFIG must be absolute during an update"
fi
unset RSSREADER_CONFIG DISTILLFEED_MODE DISTILLFEED_ARXIV_CONFIG
unset OPENAI_API_KEY OPENAI_BASE_URL OPENAI_ORGANIZATION OPENAI_PROJECT
unset RSSREADER_PASSWORD NTFY_TOKEN ARXIV_NTFY_TOKEN
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV FLASK_APP FLASK_ENV FLASK_DEBUG
unset PYTEST_ADDOPTS PYTEST_PLUGINS COVERAGE_PROCESS_START
export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PYTHONNOUSERSITE=1
export PYTHONUNBUFFERED=1
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"

INSTALL_PARENT="$(dirname -- "$INSTALL_DIR")"
STAGE="$(mktemp -d "$INSTALL_PARENT/.distillfeed-$EXPECTED_VERSION-stage.XXXXXX")"
export PIP_CACHE_DIR="$STAGE/pip-cache"
EXTRACT_ROOT="$STAGE/extracted"
RELEASE="$EXTRACT_ROOT/distillfeed"
OLD_SNAPSHOT="$STAGE/old-source"
NEW_WHEEL_DIR="$STAGE/new-wheel"
OLD_WHEEL_DIR="$STAGE/old-wheel"
SMOKE_ROOT="$STAGE/smoke"
RUNTIME_STATE="$STAGE/runtime-paths.json"
LOCK_OWNER="updater-$EXPECTED_VERSION-$$-$(date +%s)"

BACKUP_DIR=""
DATA_STATE=""
OLD_WHEEL=""
NEW_WHEEL=""
MAINTENANCE_ACQUIRED=0
SWITCH_STARTED=0
COMMITTED=0
ROLLBACK_FAILED=0
LAUNCH_LOCK_FD=""
INSTALL_LOCK_FD=""

declare -a LEGACY_ENTRIES=(
    .dockerignore .gitattributes .github .gitignore
    AUDIT.md CHANGELOG.md CITATION.cff CONTRIBUTING.md Dockerfile LICENSE
    MATURITY_AUDIT_0.21.0.md MATURITY_AUDIT_0.21.1.md MATURITY_AUDIT_0.22.0.md
    Procfile QUALITY.md README.md SECURITY.md docs
    config.example.toml deployment distillfeed_arxiv examples pyproject.toml render.yaml
    install.sh launch.sh
    # In-place builds leave metadata that can shadow the newly installed wheel.
    rss_reader tests uv.lock distillfeed.egg-info
)
declare -a OLD_ENTRIES=()
declare -a NEW_ENTRIES=()
declare -a ALL_ENTRIES=()
MANAGED_ENTRY_KEYS=":"

safe_entry() {
    [[ "$1" =~ ^[A-Za-z0-9._-]+$ && "$1" != "." && "$1" != ".." ]]
}

# BSD find on macOS has no -mindepth/-maxdepth.  Expand exactly one directory
# level in a subshell so dotglob/nullglob cannot leak into updater state, and
# emit NUL delimiters so every legal filesystem name remains unambiguous.
top_level_entries() (
    local root="$1"
    local path
    shopt -s dotglob nullglob
    for path in "$root"/*; do
        printf '%s\0' "$path"
    done
)

release_filesystem_locks() {
    if [[ -n "$INSTALL_LOCK_FD" ]]; then
        exec 9>&-
        INSTALL_LOCK_FD=""
    fi
    if [[ -n "$LAUNCH_LOCK_FD" ]]; then
        exec 8>&-
        LAUNCH_LOCK_FD=""
    fi
}

release_maintenance_lock() {
    [[ "$MAINTENANCE_ACQUIRED" -eq 1 && -f "$RUNTIME_STATE" ]] || return 0
    if ! python3 - "$RUNTIME_STATE" "$LOCK_OWNER" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text("utf-8"))
database = Path(state["database_path"])
if not database.is_file():
    raise SystemExit(0)
with sqlite3.connect(database) as connection:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_locks'"
    ).fetchone()
    if table:
        connection.execute(
            "DELETE FROM job_locks WHERE name='maintenance' AND owner=?", (sys.argv[2],)
        )
PY
    then
        return 1
    fi
    MAINTENANCE_ACQUIRED=0
}

restore_data() {
    [[ -n "$DATA_STATE" && -f "$DATA_STATE" ]] || return 0
    python3 - "$DATA_STATE" <<'PY'
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

state_path = Path(sys.argv[1])
state = json.loads(state_path.read_text("utf-8"))
backup_root = state_path.parent

def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def restore_file(record: dict) -> None:
    target = Path(record["path"])
    if record["existed"]:
        source = backup_root / record["backup"]
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.rollback.", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(source, temporary)
            temporary.chmod(int(record["mode"]))
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
    elif target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            raise RuntimeError(f"Refusing to remove unexpected rollback directory: {target}")
        target.unlink()

database = state["database"]
database_path = Path(database["path"])
for suffix in ("-wal", "-shm"):
    Path(str(database_path) + suffix).unlink(missing_ok=True)
if database["existed"]:
    restore_file(database)
elif database_path.exists() or database_path.is_symlink():
    if database_path.is_dir() and not database_path.is_symlink():
        raise RuntimeError(f"Refusing to remove unexpected database directory: {database_path}")
    database_path.unlink()

for record in state["files"]:
    restore_file(record)
PY
}

restore_source() {
    [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR/source" ]] || return 0
    local entry source
    for entry in "${ALL_ENTRIES[@]}"; do
        safe_entry "$entry" || continue
        if [[ -e "$INSTALL_DIR/$entry" || -L "$INSTALL_DIR/$entry" ]]; then
            rm -rf "$INSTALL_DIR/$entry" || return 1
        fi
    done
    while IFS= read -r -d '' source; do
        entry="$(basename "$source")"
        safe_entry "$entry" || { printf 'Unsafe backup entry during rollback: %s\n' "$entry" >&2; return 1; }
        cp -R -p -P "$source" "$INSTALL_DIR/$entry" || return 1
    done < <(top_level_entries "$BACKUP_DIR/source")
}

rollback() {
    note "Upgrade failed after the switch began; restoring the previous release and data..." >&2
    restore_source || ROLLBACK_FAILED=1
    restore_data || ROLLBACK_FAILED=1
    if [[ -n "$OLD_WHEEL" && -f "$OLD_WHEEL" ]]; then
        (
            cd /
            "$PYTHON" -m pip install --no-index --no-deps --force-reinstall "$OLD_WHEEL"
        ) >/dev/null 2>&1 || ROLLBACK_FAILED=1
    else
        ROLLBACK_FAILED=1
    fi
    release_maintenance_lock || ROLLBACK_FAILED=1
    if [[ "$ROLLBACK_FAILED" -eq 0 ]]; then
        note "Rollback completed. The failed candidate remains available in the backup/stage logs." >&2
    else
        note "ROLLBACK INCOMPLETE. Preserve $BACKUP_DIR and $STAGE for manual recovery." >&2
    fi
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [[ "$status" -ne 0 && "$SWITCH_STARTED" -eq 1 && "$COMMITTED" -eq 0 ]]; then
        rollback
    else
        if ! release_maintenance_lock; then
            note "Failed to release the updater maintenance lock." >&2
            [[ "$status" -ne 0 ]] || status=2
        fi
    fi
    release_filesystem_locks
    if [[ "$status" -eq 0 && "$KEEP_STAGE" -eq 0 ]]; then
        rm -rf "$STAGE"
    else
        printf 'Staging directory retained: %s\n' "$STAGE" >&2
    fi
    if [[ "$ROLLBACK_FAILED" -ne 0 ]]; then
        exit 2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

note "DistillFeed updater for release $EXPECTED_VERSION"
note "Installation: $INSTALL_DIR"
note "Configuration: $CONFIG_PATH"
note "Archive: $ARCHIVE"
note "Staging: $STAGE"
if [[ "$ALLOW_ACTIVE" -eq 1 ]]; then
    note "WARNING: --allow-active disables legacy process/database checks; launcher and installer filesystem barriers remain mandatory." >&2
fi

# The 0.22 launcher and installer coordinate through two owner-only advisory
# locks.  Acquire both without changing their content and retain the inherited
# descriptors for the entire transaction.  --allow-active deliberately does
# not bypass these locks: racing source replacement against setup, serving, or
# package installation cannot be made rollback-safe.
note "Acquiring the launcher and installer filesystem barriers..."
python3 - "$INSTALL_DIR/.distillfeed" <<'PY'
import os
import stat
import sys
from pathlib import Path

state = Path(sys.argv[1])
try:
    metadata = state.lstat()
except FileNotFoundError:
    state.mkdir(mode=0o700)
    metadata = state.lstat()
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit(f"Unsafe DistillFeed state directory: {state}")
if metadata.st_mode & 0o077:
    raise SystemExit(f"DistillFeed state directory is not owner-only: {state}")
for name in ("launch.lock", "install.lock"):
    path = state / name
    try:
        lock_metadata = path.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(lock_metadata.st_mode) or not stat.S_ISREG(lock_metadata.st_mode):
        raise SystemExit(f"Unsafe DistillFeed filesystem lock: {path}")
    if lock_metadata.st_mode & 0o077:
        raise SystemExit(f"DistillFeed filesystem lock is not owner-only: {path}")
PY

exec 8<>"$INSTALL_DIR/.distillfeed/launch.lock"
LAUNCH_LOCK_FD=8
if ! python3 - "$LAUNCH_LOCK_FD" "$INSTALL_DIR/.distillfeed/launch.lock" <<'PY'
import fcntl
import os
import stat
import sys
from pathlib import Path

descriptor = int(sys.argv[1])
path = Path(sys.argv[2])
opened = os.fstat(descriptor)
named = path.lstat()
if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
    raise SystemExit("Launcher lock changed while it was being opened")
if opened.st_mode & 0o077:
    raise SystemExit("Launcher lock is not owner-only")
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("DistillFeed is already starting or running from this folder")
PY
then
    fail "could not acquire .distillfeed/launch.lock; stop the active launcher and retry"
fi

exec 9<>"$INSTALL_DIR/.distillfeed/install.lock"
INSTALL_LOCK_FD=9
if ! python3 - "$INSTALL_LOCK_FD" "$INSTALL_DIR/.distillfeed/install.lock" <<'PY'
import fcntl
import os
import stat
import sys
from pathlib import Path

descriptor = int(sys.argv[1])
path = Path(sys.argv[2])
opened = os.fstat(descriptor)
named = path.lstat()
if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
    raise SystemExit("Installer lock changed while it was being opened")
if opened.st_mode & 0o077:
    raise SystemExit("Installer lock is not owner-only")
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("DistillFeed installation is already in progress")
PY
then
    fail "could not acquire .distillfeed/install.lock; wait for installation to finish and retry"
fi

note "Validating and extracting the release archive..."
python3 - "$ARCHIVE" "$EXTRACT_ROOT" "$EXPECTED_VERSION" "${ARCHIVE_SHA256:-}" <<'PY'
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
extract_root = Path(sys.argv[2])
expected_version = sys.argv[3]
expected_digest = sys.argv[4].strip().lower()
maximum_archive_bytes = 128 * 1024 * 1024
maximum_members = 5_000
maximum_member_bytes = 64 * 1024 * 1024
maximum_total_bytes = 256 * 1024 * 1024

if archive.stat().st_size > maximum_archive_bytes:
    raise SystemExit("Release archive exceeds the 128 MiB compressed-size limit")
if expected_digest and not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
    raise SystemExit("ARCHIVE_SHA256 must contain exactly 64 hexadecimal characters")

required_files = {
    "distillfeed/install.sh",
    "distillfeed/launch.sh",
    "distillfeed/pyproject.toml",
    "distillfeed/uv.lock",
    "distillfeed/config.example.toml",
    "distillfeed/rss_reader/__init__.py",
    "distillfeed/rss_reader/cli.py",
    "distillfeed/rss_reader/web.py",
    "distillfeed/rss_reader/ai_engine.py",
    "distillfeed/rss_reader/ai_policy.py",
    "distillfeed/rss_reader/ai_queue.py",
    "distillfeed/rss_reader/generated_feeds.py",
    "distillfeed/rss_reader/launcher.py",
    "distillfeed/rss_reader/ntfy_policy.py",
    "distillfeed/rss_reader/secret_store.py",
    "distillfeed/rss_reader/setup_service.py",
    "distillfeed/rss_reader/setup_state.py",
    "distillfeed/rss_reader/setup_web.py",
    "distillfeed/rss_reader/resources/starter-subscriptions.opml",
    "distillfeed/rss_reader/static/setup.css",
    "distillfeed/rss_reader/static/setup.js",
    "distillfeed/rss_reader/templates/setup.html",
    "distillfeed/distillfeed_arxiv/__init__.py",
    "distillfeed/distillfeed_arxiv/plugin.py",
    "distillfeed/distillfeed_arxiv/resources/arxiv-digest.example.toml",
    "distillfeed/deployment/start.sh",
    "distillfeed/LICENSE",
    "distillfeed/README.md",
    "distillfeed/SECURITY.md",
    "distillfeed/MATURITY_AUDIT_0.22.0.md",
    "distillfeed/docs/CUSTOM_FEEDS.md",
    "distillfeed/tests/test_bundled_arxiv.py",
    "distillfeed/tests/test_launch_lifecycle.py",
    "distillfeed/tests/test_setup_api.py",
    "distillfeed/tests/test_setup_commit.py",
    "distillfeed/tests/test_setup_profiles.py",
    "distillfeed/tests/test_setup_state_model.py",
}
forbidden_parts = {
    ".git", ".venv", ".pytest_cache", "__pycache__", "build", "dist",
    "data", "backups", ".distillfeed",
}
forbidden_names = {
    ".env", "secrets.env", "config.toml", "arxiv-digest.toml",
    ".distillfeed-managed-entries",
}

with archive.open("rb") as raw:
    digest = hashlib.sha256()
    while chunk := raw.read(1024 * 1024):
        digest.update(chunk)
    actual_digest = digest.hexdigest()
    if expected_digest and actual_digest != expected_digest:
        raise SystemExit(
            f"Archive SHA-256 mismatch: expected {expected_digest}, found {actual_digest}"
        )
    raw.seek(0)
    with tarfile.open(fileobj=raw, mode="r:gz") as handle:
        members = handle.getmembers()
        if not members or len(members) > maximum_members:
            raise SystemExit("Release archive has an invalid member count")
        seen: set[str] = set()
        regular_files: set[str] = set()
        directory_names: set[str] = set()
        directories: list[Path] = []
        total_bytes = 0

        for member in members:
            raw_name = member.name
            if (
                not raw_name
                or "\\" in raw_name
                or "\x00" in raw_name
                or any(ord(character) < 32 or ord(character) == 127 for character in raw_name)
            ):
                raise SystemExit(f"Unsafe archive member name: {raw_name!r}")
            canonical_name = raw_name[:-1] if raw_name.endswith("/") else raw_name
            path = PurePosixPath(canonical_name)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != "distillfeed"
                or ".." in path.parts
                or path.as_posix() != canonical_name
            ):
                raise SystemExit(f"Unsafe or non-canonical archive member: {raw_name!r}")
            name = path.as_posix()
            if name in seen:
                raise SystemExit(f"Duplicate archive member: {name!r}")
            seen.add(name)
            mode = member.mode & 0o7777
            if mode & 0o7000:
                raise SystemExit(f"Archive member has special permission bits: {name!r}")
            if member.isdir():
                if mode != 0o755:
                    raise SystemExit(
                        f"Archive directory must have mode 0755, found {mode:04o}: {name!r}"
                    )
                directory_names.add(name)
            elif member.isfile():
                expected_mode = (
                    0o755
                    if name in {"distillfeed/install.sh", "distillfeed/launch.sh"}
                    else 0o644
                )
                if mode != expected_mode:
                    raise SystemExit(
                        f"Archive file must have mode {expected_mode:04o}, "
                        f"found {mode:04o}: {name!r}"
                    )
                if member.size < 0 or member.size > maximum_member_bytes:
                    raise SystemExit(f"Archive member has an invalid size: {name!r}")
                total_bytes += member.size
                regular_files.add(name)
            else:
                raise SystemExit(
                    f"Links, devices, FIFOs, and other special members are forbidden: {name!r}"
                )
            relative_parts = path.parts[1:]
            if any(part in forbidden_parts or part.endswith(".egg-info") for part in relative_parts):
                raise SystemExit(f"Generated or private directory in release archive: {name!r}")
            if relative_parts and relative_parts[-1] in forbidden_names:
                raise SystemExit(f"Private runtime file in release archive: {name!r}")
            if name.endswith((".pyc", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
                raise SystemExit(f"Generated runtime file in release archive: {name!r}")

        if total_bytes > maximum_total_bytes:
            raise SystemExit("Release archive exceeds the 256 MiB expansion limit")
        if "distillfeed" not in directory_names:
            raise SystemExit("Release archive must contain a top-level distillfeed/ directory")
        missing = required_files - regular_files
        if missing:
            raise SystemExit("Release archive is incomplete; missing: " + ", ".join(sorted(missing)))
        for file_name in regular_files:
            file_path = PurePosixPath(file_name)
            if any(parent.as_posix() in regular_files for parent in file_path.parents):
                raise SystemExit(f"Archive has a file/directory prefix collision: {file_name!r}")

        metadata_file = handle.extractfile("distillfeed/pyproject.toml")
        if metadata_file is None:
            raise SystemExit("Cannot read pyproject.toml from release archive")
        metadata = tomllib.loads(metadata_file.read().decode("utf-8"))
        found_version = str(metadata.get("project", {}).get("version", ""))
        if found_version != expected_version:
            raise SystemExit(
                f"Expected DistillFeed {expected_version}, found {found_version or 'no version'}"
            )

        extract_root.mkdir(parents=True, exist_ok=False)
        for member in members:
            canonical_name = member.name[:-1] if member.name.endswith("/") else member.name
            path = PurePosixPath(canonical_name)
            destination = extract_root.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                directories.append(destination)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise SystemExit(f"Cannot read archive member: {canonical_name!r}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags, 0o600)
            try:
                with source, os.fdopen(descriptor, "wb") as output:
                    descriptor = -1
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            # Normalize source permissions instead of preserving arbitrary
            # executable bits: shell entry points are executable, other files
            # are read-only to group/other and writable only by the owner.
            destination.chmod(
                0o755
                if canonical_name in {"distillfeed/install.sh", "distillfeed/launch.sh"}
                else 0o644
            )
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            directory.chmod(0o755)

print(f"Archive SHA-256: {actual_digest}")
print(f"Validated members: {len(members)}; expanded bytes: {total_bytes}")
PY

[[ -d "$RELEASE" ]] || fail "validated archive did not produce distillfeed/"

note "Checking release-version loci and syntax..."
python3 - "$RELEASE" "$EXPECTED_VERSION" <<'PY'
import re
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
expected = sys.argv[2]

def module_version(path: Path) -> str:
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', path.read_text("utf-8"), re.M)
    if not match:
        raise SystemExit(f"Missing __version__ in {path.relative_to(root)}")
    return match.group(1)

checks = {
    "pyproject.toml": tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"],
    "rss_reader/__init__.py": module_version(root / "rss_reader/__init__.py"),
    "distillfeed_arxiv/__init__.py": module_version(root / "distillfeed_arxiv/__init__.py"),
}
lock = tomllib.loads((root / "uv.lock").read_text("utf-8"))
packages = [p for p in lock.get("package", []) if p.get("name") == "distillfeed"]
if len(packages) != 1:
    raise SystemExit("uv.lock must contain exactly one distillfeed package")
checks["uv.lock"] = str(packages[0].get("version", ""))
bad = {name: value for name, value in checks.items() if value != expected}
if bad:
    raise SystemExit("Version mismatch: " + ", ".join(f"{name}={value}" for name, value in bad.items()))
print("Version loci:", ", ".join(f"{name}={value}" for name, value in checks.items()))
PY

bash -n "$SCRIPT_DIR/upd.sh"
while IFS= read -r -d '' shell_file; do
    first_line="$(head -n 1 "$shell_file" 2>/dev/null || true)"
    if [[ "$first_line" == *bash* ]]; then
        bash -n "$shell_file"
    else
        sh -n "$shell_file"
    fi
done < <(find "$RELEASE" -type f -name '*.sh' -print0)
if command -v node >/dev/null 2>&1; then
    while IFS= read -r -d '' javascript_file; do
        node --check "$javascript_file"
    done < <(find "$RELEASE/rss_reader/static" -type f -name '*.js' -print0)
fi

note "Resolving managed source entries and creating an old-release snapshot..."
entry_is_managed() {
    local candidate="$1"
    [[ "$MANAGED_ENTRY_KEYS" == *":$candidate:"* ]]
}

add_managed_entry() {
    if ! entry_is_managed "$1"; then
        ALL_ENTRIES+=("$1")
        MANAGED_ENTRY_KEYS="${MANAGED_ENTRY_KEYS}${1}:"
    fi
}

for entry in "${LEGACY_ENTRIES[@]}"; do
    safe_entry "$entry" || fail "internal unsafe legacy entry: $entry"
    if [[ -e "$INSTALL_DIR/$entry" || -L "$INSTALL_DIR/$entry" ]]; then
        OLD_ENTRIES+=("$entry")
        add_managed_entry "$entry"
    fi
done
if [[ -f "$INSTALL_DIR/.distillfeed-managed-entries" ]]; then
    while IFS= read -r entry || [[ -n "$entry" ]]; do
        [[ -n "$entry" ]] || continue
        safe_entry "$entry" || fail "unsafe entry in .distillfeed-managed-entries: $entry"
        case "$entry" in .venv|.distillfeed|data|backups|config.toml|arxiv-digest.toml) fail "reserved managed entry: $entry" ;; esac
        if [[ -e "$INSTALL_DIR/$entry" || -L "$INSTALL_DIR/$entry" ]]; then
            OLD_ENTRIES+=("$entry")
            add_managed_entry "$entry"
        fi
    done < "$INSTALL_DIR/.distillfeed-managed-entries"
fi
while IFS= read -r -d '' path; do
    entry="$(basename "$path")"
    safe_entry "$entry" || fail "unsafe top-level release entry: $entry"
    case "$entry" in .venv|.distillfeed|data|backups|config.toml|arxiv-digest.toml|.distillfeed-managed-entries)
        fail "release archive contains reserved top-level entry: $entry"
        ;;
    esac
    if ! entry_is_managed "$entry" && [[ -e "$INSTALL_DIR/$entry" || -L "$INSTALL_DIR/$entry" ]]; then
        # A new release-owned name may collide with a previously unmanaged user
        # file. Preserve it in the rollback/source backup before replacement.
        OLD_ENTRIES+=("$entry")
    fi
    NEW_ENTRIES+=("$entry")
    add_managed_entry "$entry"
done < <(top_level_entries "$RELEASE")
SORTED_ENTRIES=()
while IFS= read -r entry; do
    [[ -n "$entry" ]] && SORTED_ENTRIES+=("$entry")
done < <(printf '%s\n' "${ALL_ENTRIES[@]}" | LC_ALL=C sort)
ALL_ENTRIES=("${SORTED_ENTRIES[@]}")

mkdir -p "$OLD_SNAPSHOT"
for entry in "${OLD_ENTRIES[@]}"; do
    [[ -e "$OLD_SNAPSHOT/$entry" || -L "$OLD_SNAPSHOT/$entry" ]] \
        || cp -R -p -P "$INSTALL_DIR/$entry" "$OLD_SNAPSHOT/$entry"
done
[[ -f "$OLD_SNAPSHOT/pyproject.toml" && -d "$OLD_SNAPSHOT/rss_reader" ]] \
    || fail "could not construct a complete old-release snapshot for rollback"

note "Building deterministic old/new rollback wheels without executing project build code..."
mkdir -p "$OLD_WHEEL_DIR" "$NEW_WHEEL_DIR"
python3 - "$OLD_SNAPSHOT" "$OLD_WHEEL_DIR" "$RELEASE" "$NEW_WHEEL_DIR" <<'PY'
from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import sys
import tomllib
import zipfile
from pathlib import Path

SKIP_PARTS = {"__pycache__", ".pytest_cache", "build", "dist"}

def requirement_with_extra(requirement: str, extra: str) -> str:
    if ";" in requirement:
        base, marker = requirement.split(";", 1)
        return f"{base.strip()}; ({marker.strip()}) and extra == '{extra}'"
    return f"{requirement.strip()}; extra == '{extra}'"

def build(root: Path, output: Path) -> Path:
    with (root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    project = document.get("project", {})
    name = str(project.get("name", ""))
    version = str(project.get("version", ""))
    if name != "distillfeed" or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"Cannot build rollback wheel from {root}: invalid name/version")
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    wheel_path = output / f"{distribution}-{version}-py3-none-any.whl"

    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {str(project.get('description', '')).replace(chr(10), ' ').strip()}",
        f"Requires-Python: {project.get('requires-python', '>=3.11')}",
    ]
    license_value = project.get("license")
    if isinstance(license_value, str) and license_value:
        metadata.append(f"License-Expression: {license_value}")
    for dependency in project.get("dependencies", []):
        metadata.append(f"Requires-Dist: {dependency}")
    optional = project.get("optional-dependencies", {})
    for extra in sorted(optional):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(extra)):
            raise SystemExit(f"Invalid optional dependency name in {root}: {extra!r}")
        metadata.append(f"Provides-Extra: {extra}")
        for dependency in optional[extra]:
            metadata.append(f"Requires-Dist: {requirement_with_extra(str(dependency), str(extra))}")
    metadata_bytes = ("\n".join(metadata) + "\n").encode("utf-8")

    scripts = project.get("scripts", {})
    groups = project.get("entry-points", {})
    entry_lines: list[str] = []
    if scripts:
        entry_lines.append("[console_scripts]")
        entry_lines.extend(f"{key} = {scripts[key]}" for key in sorted(scripts))
        entry_lines.append("")
    for group in sorted(groups):
        entry_lines.append(f"[{group}]")
        values = groups[group]
        entry_lines.extend(f"{key} = {values[key]}" for key in sorted(values))
        entry_lines.append("")
    entry_bytes = ("\n".join(entry_lines).rstrip() + "\n").encode("utf-8")
    wheel_bytes = (
        "Wheel-Version: 1.0\n"
        "Generator: DistillFeed offline updater\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")

    payload: dict[str, bytes] = {}
    for package_name in ("rss_reader", "distillfeed_arxiv"):
        package = root / package_name
        if not (package / "__init__.py").is_file():
            raise SystemExit(f"Rollback source is missing package {package_name}: {root}")
        for path in package.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            payload[relative.as_posix()] = path.read_bytes()
    license_path = root / "LICENSE"
    if license_path.is_file():
        payload[f"{dist_info}/licenses/LICENSE"] = license_path.read_bytes()
    payload[f"{dist_info}/METADATA"] = metadata_bytes
    payload[f"{dist_info}/WHEEL"] = wheel_bytes
    payload[f"{dist_info}/entry_points.txt"] = entry_bytes
    payload[f"{dist_info}/top_level.txt"] = b"distillfeed_arxiv\nrss_reader\n"

    records: list[tuple[str, str, str]] = []
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for archive_name in sorted(payload):
            data = payload[archive_name]
            info = zipfile.ZipInfo(archive_name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            records.append((archive_name, f"sha256={digest}", str(len(data))))
        record_name = f"{dist_info}/RECORD"
        record_buffer = io.StringIO(newline="")
        writer = csv.writer(record_buffer, lineterminator="\n")
        writer.writerows([*records, (record_name, "", "")])
        record_data = record_buffer.getvalue().encode("utf-8")
        info = zipfile.ZipInfo(record_name, (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, record_data)
    print("Built offline wheel:", wheel_path)
    return wheel_path

build(Path(sys.argv[1]), Path(sys.argv[2]))
build(Path(sys.argv[3]), Path(sys.argv[4]))
PY

shopt -s nullglob
old_wheels=("$OLD_WHEEL_DIR"/*.whl)
new_wheels=("$NEW_WHEEL_DIR"/*.whl)
shopt -u nullglob
[[ "${#old_wheels[@]}" -eq 1 ]] || fail "old-release wheel build did not produce exactly one wheel"
[[ "${#new_wheels[@]}" -eq 1 ]] || fail "new-release wheel build did not produce exactly one wheel"
OLD_WHEEL="${old_wheels[0]}"
NEW_WHEEL="${new_wheels[0]}"
[[ "$(basename "$NEW_WHEEL")" == "distillfeed-${EXPECTED_VERSION}-"*.whl ]] \
    || fail "new wheel has an unexpected filename: $(basename "$NEW_WHEEL")"
"$PYTHON" -m pip install --dry-run --no-index "$NEW_WHEEL" >/dev/null \
    || fail "the existing virtual environment lacks a dependency required by $EXPECTED_VERSION"

note "Running offline compile, CLI, Flask, and provider-isolation smoke checks before the switch..."
"$PYTHON" -m compileall -q "$RELEASE/rss_reader" "$RELEASE/distillfeed_arxiv"
mkdir -p "$SMOKE_ROOT"
cp "$RELEASE/config.example.toml" "$SMOKE_ROOT/config.toml"
(
    cd "$RELEASE"
    "$PYTHON" -m rss_reader.cli --config "$SMOKE_ROOT/config.toml" init
    "$PYTHON" -m rss_reader.cli --config "$SMOKE_ROOT/config.toml" doctor
    "$PYTHON" - "$SMOKE_ROOT/config.toml" <<'PY'
import os
import sys
from rss_reader.web import create_app

for name in ("OPENAI_API_KEY", "NTFY_TOKEN", "ARXIV_NTFY_TOKEN"):
    if os.environ.get(name):
        raise SystemExit(f"provider secret unexpectedly present during updater smoke: {name}")
app = create_app(sys.argv[1])
client = app.test_client()
for path in ("/", "/ai", "/summaries", "/api/status"):
    response = client.get(path)
    if response.status_code != 200:
        raise SystemExit(f"offline web smoke failed: {path} returned {response.status_code}")
print("Offline staged web smoke: healthy")
PY
)

note "Resolving configured database and OPML paths..."
python3 - "$CONFIG_PATH" "$ARXIV_CONFIG_OVERRIDE" "$INSTALL_DIR" "$RUNTIME_STATE" <<'PY'
import json
import sys
import tomllib
from pathlib import Path

config = Path(sys.argv[1]).resolve(strict=True)
override = sys.argv[2].strip()
install = Path(sys.argv[3]).resolve(strict=True)
destination = Path(sys.argv[4])
with config.open("rb") as handle:
    values = tomllib.load(handle)
app = values.get("app", {})

def resolved(value: object, default: str) -> Path:
    text = str(value if value is not None else default).strip()
    if not text or any(character in text for character in "\r\n\x00"):
        raise SystemExit("Configured runtime paths must be non-empty single-line paths")
    path = Path(text).expanduser()
    return (path if path.is_absolute() else config.parent / path).resolve()

database = resolved(app.get("database_path"), "data/reader.sqlite3")
opml = resolved(app.get("working_opml_path"), "data/subscriptions.opml")
plugin = (
    Path(override).expanduser().resolve()
    if override
    else (config.parent / "arxiv-digest.toml").resolve()
)
managed_instance = install / ".distillfeed" / "instance"
managed_config = managed_instance / "config.toml"
managed_instance_path = None
if managed_config.exists() and config == managed_config.resolve():
    if managed_instance.is_symlink() or not managed_instance.is_dir():
        raise SystemExit(f"Managed instance is unsafe: {managed_instance}")
    managed_instance_path = str(managed_instance)
if len({config, database, opml}) != 3:
    raise SystemExit("config, database, and working OPML must resolve to distinct paths")
state = {
    "install_dir": str(install),
    "config_path": str(config),
    "database_path": str(database),
    "opml_path": str(opml),
    "plugin_config_path": str(plugin),
    "managed_instance_path": managed_instance_path,
}
destination.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
print("Database:", database)
print("Working OPML:", opml)
print("arXiv recipe:", plugin)
PY

note "Checking for running DistillFeed processes..."
python3 - "$INSTALL_DIR" "$ALLOW_ACTIVE" <<'PY'
import os
import sys
from pathlib import Path

install = Path(sys.argv[1]).resolve()
allow = sys.argv[2] == "1"
excluded: set[int] = set()
pid = os.getpid()
while pid > 1 and pid not in excluded:
    excluded.add(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text("utf-8").split()
        pid = int(fields[3])
    except (OSError, ValueError, IndexError):
        break

matches: list[tuple[int, str]] = []
proc = Path("/proc")
if proc.is_dir():
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded:
            continue
        try:
            arguments = [
                part.decode("utf-8", "replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except (OSError, PermissionError):
            continue
        joined = " ".join(arguments)
        executable_names = {Path(argument).name for argument in arguments[:2]}
        under_install = cwd == install or install in cwd.parents or any(
            argument == str(install) or argument.startswith(str(install) + os.sep)
            for argument in arguments
        )
        application = bool(
            executable_names & {"distillfeed", "rssreader", "gunicorn", "launch.sh", "install.sh"}
            or "rss_reader.wsgi" in joined
            or "rss_reader.cli" in joined
            or "rss_reader.launcher" in joined
            or "distillfeed serve" in joined
        )
        if under_install and application:
            matches.append((int(entry.name), joined[:500]))

if matches:
    for process_id, command in matches:
        print(f"Active DistillFeed process {process_id}: {command}", file=sys.stderr)
    if not allow:
        raise SystemExit("Stop active DistillFeed processes or use --allow-active explicitly")
PY

note "Checking application locks and acquiring the maintenance barrier..."
python3 - "$RUNTIME_STATE" "$LOCK_OWNER" "$ALLOW_ACTIVE" <<'PY'
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text("utf-8"))
database = Path(state["database_path"])
owner = sys.argv[2]
allow = sys.argv[3] == "1"
if not database.exists():
    print("Database does not yet exist; no database lock is available")
    raise SystemExit(0)
if not database.is_file():
    raise SystemExit(f"Configured database is not a regular file: {database}")
with sqlite3.connect(database, timeout=10) as connection:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_locks'"
    ).fetchone()
    if not table:
        print("Legacy database has no job_locks table; process check remains the safety barrier")
        raise SystemExit(0)
    now = datetime.now(UTC)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM job_locks WHERE expires_at < ?", (now.isoformat(),))
    active = connection.execute(
        "SELECT name,owner,expires_at FROM job_locks ORDER BY name"
    ).fetchall()
    if active:
        connection.rollback()
        for name, active_owner, expires in active:
            print(f"Active application lock {name!r}, owner={active_owner!r}, expires={expires}", file=sys.stderr)
        if not allow:
            raise SystemExit("Active DistillFeed jobs must finish before an update")
        print("WARNING: proceeding without a maintenance lock because --allow-active was used", file=sys.stderr)
        raise SystemExit(0)
    if allow:
        connection.rollback()
        print("WARNING: --allow-active skips maintenance-lock acquisition", file=sys.stderr)
        raise SystemExit(0)
    expires = now + timedelta(hours=2)
    connection.execute(
        "INSERT INTO job_locks(name,owner,acquired_at,expires_at,cancel_requested) VALUES('maintenance',?,?,?,0)",
        (owner, now.isoformat(), expires.isoformat()),
    )
    connection.commit()
print("Maintenance lock acquired")
PY
if [[ "$ALLOW_ACTIVE" -eq 0 ]]; then
    # Record whether the legacy schema supported the lock. A harmless delete on
    # cleanup is still safe if it did not.
    MAINTENANCE_ACQUIRED=1
fi

# Close the small process-start race after lock acquisition. Conforming jobs now
# see the maintenance lock; this catches nonconforming web/server starts too.
python3 - "$INSTALL_DIR" "$ALLOW_ACTIVE" <<'PY'
import os
import sys
from pathlib import Path

if sys.argv[2] == "1" or not Path("/proc").is_dir():
    raise SystemExit(0)
install = Path(sys.argv[1]).resolve()
ancestors: set[int] = set()
pid = os.getpid()
while pid > 1 and pid not in ancestors:
    ancestors.add(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text("utf-8").split()
        pid = int(fields[3])
    except (OSError, ValueError, IndexError):
        break
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) in ancestors:
        continue
    try:
        args = [p.decode("utf-8", "replace") for p in (entry / "cmdline").read_bytes().split(b"\0") if p]
        cwd = Path(os.readlink(entry / "cwd")).resolve()
    except OSError:
        continue
    joined = " ".join(args)
    under = cwd == install or install in cwd.parents or any(a.startswith(str(install) + os.sep) for a in args)
    if under and (
        "gunicorn" in joined
        or "rss_reader.wsgi" in joined
        or "rss_reader.launcher" in joined
        or "distillfeed serve" in joined
    ):
        raise SystemExit(f"DistillFeed process appeared during update preflight: PID {entry.name}: {joined[:300]}")
PY

note "Creating a durable backup of source, configuration, OPML, and SQLite state..."
mkdir -p "$INSTALL_DIR/backups"
BACKUP_DIR="$(mktemp -d "$INSTALL_DIR/backups/update-$(date '+%Y%m%d-%H%M%S').XXXXXX")"
mkdir -p "$BACKUP_DIR/source" "$BACKUP_DIR/data"
DATA_STATE="$BACKUP_DIR/data/state.json"
ROLLBACK_WHEEL="$BACKUP_DIR/$(basename "$OLD_WHEEL")"
cp "$OLD_WHEEL" "$ROLLBACK_WHEEL"
chmod 0600 "$ROLLBACK_WHEEL"
OLD_WHEEL="$ROLLBACK_WHEEL"
cp "$SCRIPT_DIR/upd.sh" "$BACKUP_DIR/upd.sh"
chmod 0700 "$BACKUP_DIR/upd.sh"

for entry in "${OLD_ENTRIES[@]}"; do
    [[ -e "$BACKUP_DIR/source/$entry" || -L "$BACKUP_DIR/source/$entry" ]] \
        || cp -R -p -P "$INSTALL_DIR/$entry" "$BACKUP_DIR/source/$entry"
done

python3 - "$RUNTIME_STATE" "$DATA_STATE" "$INSTALL_DIR/.distillfeed-managed-entries" <<'PY'
import json
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

runtime = json.loads(Path(sys.argv[1]).read_text("utf-8"))
state_path = Path(sys.argv[2])
manifest = Path(sys.argv[3])
backup_root = state_path.parent
files_root = backup_root / "files"
files_root.mkdir(parents=True, exist_ok=True)

database = Path(runtime["database_path"])
config = Path(runtime["config_path"])
opml = Path(runtime["opml_path"])
plugin = Path(runtime["plugin_config_path"])
install = Path(runtime["install_dir"])
install_marker = install / ".venv" / ".distillfeed-install.json"
managed_text = runtime.get("managed_instance_path")
managed = Path(managed_text) if managed_text else None

def readable_regular(path: Path, *, required: bool = False) -> bool:
    exists = path.exists() or path.is_symlink()
    if not exists:
        if required:
            raise SystemExit(f"Required file disappeared during backup: {path}")
        return False
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"Backup target is not a real regular file: {path}")
    if not os.access(path, os.R_OK):
        raise SystemExit(f"Backup target is not readable: {path}")
    return True

def sync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())

records: list[dict] = []
seen = {database}
protected: set[Path] = {config.absolute(), plugin.absolute()}
candidate_files = [
    config,
    Path(str(config) + ".bak"),
    opml,
    Path(str(opml) + ".bak"),
    plugin,
    Path(str(plugin) + ".bak"),
    manifest,
    install_marker,
]

# A managed instance owns security-sensitive metadata in addition to its
# mutable SQLite database and derived OPML export.  Inventory every existing
# real file in that instance, preserve all non-database/non-OPML bytes, and
# reject symbolic links or special files before any switch starts.  This also
# protects future setup metadata that an older updater does not yet know by
# name.
if managed is not None:
    if managed.is_symlink() or not managed.is_dir():
        raise SystemExit(f"Managed instance is missing or unsafe: {managed}")
    mutable = {
        database.absolute(),
        Path(str(database) + "-wal").absolute(),
        Path(str(database) + "-shm").absolute(),
        opml.absolute(),
        Path(str(opml) + ".bak").absolute(),
    }
    for root, directories, files in os.walk(managed, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *files]:
            path = root_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise SystemExit(f"Symbolic link inside managed instance: {path}")
        for name in files:
            path = (root_path / name).absolute()
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise SystemExit(f"Special file inside managed instance: {path}")
            if path not in mutable:
                candidate_files.append(path)
                protected.add(path)

for path in candidate_files:
    path = path.absolute()
    if path in seen:
        continue
    seen.add(path)
    existed = readable_regular(path, required=(path == config))
    record = {
        "path": str(path),
        "existed": existed,
        "backup": None,
        "mode": None,
        "preserve_bytes": path in protected,
    }
    if existed:
        backup = files_root / f"{len(records):04d}"
        shutil.copy2(path, backup)
        sync_file(backup)
        record["backup"] = str(backup.relative_to(backup_root))
        record["mode"] = path.stat().st_mode & 0o777
    records.append(record)

database_record = {
    "path": str(database), "existed": False, "backup": None, "mode": None,
}
if database.exists() or database.is_symlink():
    readable_regular(database, required=True)
    database_backup = backup_root / "database.sqlite3"
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as incoming:
        check = incoming.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise SystemExit(f"SQLite quick_check failed before update: {check}")
        violations = incoming.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit(f"SQLite has {len(violations)} foreign-key violation(s)")
        with sqlite3.connect(database_backup) as outgoing:
            incoming.backup(outgoing)
    with sqlite3.connect(database_backup) as check_connection:
        if check_connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("SQLite backup failed its integrity check")
    sync_file(database_backup)
    database_record.update({
        "existed": True,
        "backup": str(database_backup.relative_to(backup_root)),
        "mode": database.stat().st_mode & 0o777,
    })

state = {"database": database_record, "files": records}
state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
sync_file(state_path)
print("Backup files:", sum(1 for record in records if record["existed"]))
print("SQLite backup:", database_record["existed"])
PY

cat > "$BACKUP_DIR/RECOVERY.txt" <<EOF
DistillFeed update recovery checkpoint
Target version: $EXPECTED_VERSION
Installation: $INSTALL_DIR
Configuration: $CONFIG_PATH
Archive: $ARCHIVE
Staging directory at update time: $STAGE

The updater restores this checkpoint automatically when a post-switch command
fails. The previous source tree, SQLite/file snapshots, rollback wheel, and a
copy of the updater are stored here. Preserve this directory if recovery is
ever incomplete.
EOF

note "Switching managed source entries; runtime data and .venv remain in place..."
SWITCH_STARTED=1
for entry in "${ALL_ENTRIES[@]}"; do
    safe_entry "$entry" || fail "unsafe managed entry during switch: $entry"
    if [[ -e "$INSTALL_DIR/$entry" || -L "$INSTALL_DIR/$entry" ]]; then
        rm -rf "$INSTALL_DIR/$entry"
    fi
done
for entry in "${NEW_ENTRIES[@]}"; do
    mv "$RELEASE/$entry" "$INSTALL_DIR/$entry"
done

note "Installing the prebuilt wheel without network access and migrating local state..."
(
    cd /
    "$PYTHON" -m pip install --no-index --no-deps --force-reinstall "$NEW_WHEEL"
)
(
    cd "$INSTALL_DIR"
    "$PYTHON" -m rss_reader.cli --config "$CONFIG_PATH" init
    "$PYTHON" -m compileall -q "$INSTALL_DIR/rss_reader" "$INSTALL_DIR/distillfeed_arxiv"
    "$PYTHON" -m rss_reader.cli --config "$CONFIG_PATH" doctor
)

note "Verifying installed metadata and preserved user configuration..."
(
    cd /
    "$PYTHON" - "$EXPECTED_VERSION" <<'PY'
import importlib.metadata
import sys
import distillfeed_arxiv
import rss_reader

expected = sys.argv[1]
versions = {
    "metadata": importlib.metadata.version("distillfeed"),
    "rss_reader": rss_reader.__version__,
    "distillfeed_arxiv": distillfeed_arxiv.__version__,
}
bad = {name: value for name, value in versions.items() if value != expected}
if bad:
    raise SystemExit("Installed version mismatch: " + ", ".join(f"{k}={v}" for k, v in bad.items()))
points = {point.name for point in importlib.metadata.entry_points(group="distillfeed.plugins")}
if "arxiv_digest" not in points:
    raise SystemExit("Bundled arxiv_digest entry point is missing after install")
print("Installed version:", expected)
PY
)

note "Publishing the schema-2 source/install receipt used by launch.sh..."
"$PYTHON" - "$INSTALL_DIR" "$INSTALL_DIR/.venv/.distillfeed-install.json" "$EXPECTED_VERSION" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import tomllib

root = pathlib.Path(sys.argv[1]).resolve()
marker = pathlib.Path(sys.argv[2])
expected = sys.argv[3]
with (root / "pyproject.toml").open("rb") as handle:
    version = str(tomllib.load(handle)["project"]["version"])
if version != expected:
    raise SystemExit(f"Cannot write install receipt for {version}; expected {expected}")
try:
    metadata = marker.lstat()
except FileNotFoundError:
    metadata = None
if metadata is not None and (
    stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
):
    raise SystemExit(f"Install receipt path is unsafe: {marker}")

digest = hashlib.sha256()
paths = [root / "pyproject.toml"]
for package in ("rss_reader", "distillfeed_arxiv"):
    paths.extend(
        path for path in (root / package).rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix in {
            ".py", ".html", ".css", ".js", ".svg", ".webmanifest",
            ".opml", ".toml",
        }
    )
for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix().encode("utf-8")
    content = path.read_bytes()
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
document = {
    "schema": 2,
    "version": version,
    "source_sha256": digest.hexdigest(),
}

descriptor, temporary_name = tempfile.mkstemp(
    prefix=".distillfeed-install-update-", dir=marker.parent
)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(document, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, marker)
    os.chmod(marker, 0o600)
    directory = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except Exception:
    temporary.unlink(missing_ok=True)
    raise
print("Install receipt:", document["source_sha256"])
PY
DISTILLFEED_INSTALL_LOCKED=1 "$INSTALL_DIR/install.sh" --check \
    || fail "the updated source/install receipt failed launch.sh's installation check"

python3 - "$DATA_STATE" "$CONFIG_PATH" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

state_path = Path(sys.argv[1])
config = Path(sys.argv[2]).resolve()
state = json.loads(state_path.read_text("utf-8"))
config_record = next(
    (item for item in state["files"] if Path(item["path"]).resolve() == config),
    None,
)
if not config_record or not config_record["existed"]:
    raise SystemExit("Configuration backup record is missing")
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
checked = 0
for record in state["files"]:
    if not record.get("preserve_bytes"):
        continue
    path = Path(record["path"])
    exists = path.exists() or path.is_symlink()
    if record["existed"]:
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"Protected runtime file became unsafe or disappeared: {path}")
        backup = state_path.parent / record["backup"]
        if digest(path) != digest(backup):
            raise SystemExit(f"Protected runtime file changed during the update: {path}")
        if (path.stat().st_mode & 0o777) != int(record["mode"]):
            raise SystemExit(f"Protected runtime file mode changed during the update: {path}")
    elif exists:
        raise SystemExit(f"Protected runtime file appeared during the update: {path}")
    checked += 1
if not config_record.get("preserve_bytes"):
    raise SystemExit("Configuration was not classified as protected runtime state")
print(f"Protected runtime files preserved byte-for-byte: {checked}")
PY

MANIFEST_TEMP="$INSTALL_DIR/.distillfeed-managed-entries.tmp.$$"
printf '%s\n' "${NEW_ENTRIES[@]}" | LC_ALL=C sort > "$MANIFEST_TEMP"
chmod 0644 "$MANIFEST_TEMP"
mv -f "$MANIFEST_TEMP" "$INSTALL_DIR/.distillfeed-managed-entries"

release_maintenance_lock
COMMITTED=1

note ""
note "DistillFeed $EXPECTED_VERSION update completed successfully."
note "Backup: $BACKUP_DIR"
note "Configuration, configured SQLite/OPML paths, arXiv recipe, and runtime data were preserved."
note "No feed, notification, package-index, or AI-provider network call was made."
note "Restart the DistillFeed service explicitly after reviewing this result."
