#!/usr/bin/env bash

if ((BASH_VERSINFO[0] < 3 || (BASH_VERSINFO[0] == 3 && BASH_VERSINFO[1] < 2))); then
    printf 'Error: DistillFeed release verification requires Bash 3.2 or newer.\n' >&2
    exit 1
fi

set -Eeuo pipefail

# Deterministic release verifier for DistillFeed. External feed access and paid
# AI calls are separate, explicit opt-ins; the default run only uses mocked
# network responses from the test suite and loopback HTTP health requests.

EXPECTED_VERSION="0.22.0"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ARCHIVE="${ARCHIVE:-$SCRIPT_DIR/distillfeed-$EXPECTED_VERSION.tar.gz}"
TEST_TMPDIR="${TEST_TMPDIR:-${TMPDIR:-/tmp}}"
REQUESTED_TEST_ROOT="${TEST_ROOT:-}"
KEEP_TEST_ROOT="${KEEP_TEST_ROOT:-0}"
REQUIRE_NODE="${REQUIRE_NODE:-0}"
RUN_LIVE_REFRESH="${RUN_LIVE_REFRESH:-0}"
RUN_PAID_AI="${RUN_PAID_AI:-0}"
CONFIRM_PAID_AI="${CONFIRM_PAID_AI:-}"
EXPECTED_ARCHIVE_SHA256="${ARCHIVE_SHA256:-}"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '%s\n' "$*"
}

case "$KEEP_TEST_ROOT" in 0|1) ;; *) fail "KEEP_TEST_ROOT must be 0 or 1" ;; esac
case "$REQUIRE_NODE" in 0|1) ;; *) fail "REQUIRE_NODE must be 0 or 1" ;; esac
case "$RUN_LIVE_REFRESH" in 0|1) ;; *) fail "RUN_LIVE_REFRESH must be 0 or 1" ;; esac
case "$RUN_PAID_AI" in 0|1) ;; *) fail "RUN_PAID_AI must be 0 or 1" ;; esac

for command in python3 find; do
    command -v "$command" >/dev/null 2>&1 || fail "required command is unavailable: $command"
done
if [[ "$REQUIRE_NODE" -eq 1 ]]; then
    command -v node >/dev/null 2>&1 \
        || fail "REQUIRE_NODE=1 was requested, but Node.js is unavailable"
fi

python3 - <<'PY' || fail "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

[[ -f "$ARCHIVE" ]] || fail "release archive not found: $ARCHIVE"
[[ ! -L "$ARCHIVE" ]] || fail "release archive must not be a symbolic link: $ARCHIVE"
[[ -r "$ARCHIVE" ]] || fail "release archive is not readable: $ARCHIVE"

# Capture a possible live key before clearing the application environment. It
# is restored for exactly one command only after the double opt-in below.
LIVE_OPENAI_API_KEY="${OPENAI_API_KEY:-}"
unset RSSREADER_CONFIG DISTILLFEED_MODE DISTILLFEED_ARXIV_CONFIG
unset OPENAI_API_KEY RSSREADER_PASSWORD NTFY_TOKEN ARXIV_NTFY_TOKEN
unset OPENAI_BASE_URL OPENAI_ORGANIZATION OPENAI_PROJECT
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV FLASK_APP FLASK_ENV FLASK_DEBUG
unset PYTEST_ADDOPTS PYTEST_PLUGINS COVERAGE_PROCESS_START

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PYTHONUNBUFFERED=1
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"

AUTO_TEST_ROOT=0
if [[ -n "$REQUESTED_TEST_ROOT" ]]; then
    [[ "$REQUESTED_TEST_ROOT" != "/" ]] || fail "TEST_ROOT cannot be the filesystem root"
    [[ ! -e "$REQUESTED_TEST_ROOT" && ! -L "$REQUESTED_TEST_ROOT" ]] \
        || fail "TEST_ROOT already exists: $REQUESTED_TEST_ROOT"
    mkdir -m 0700 -- "$REQUESTED_TEST_ROOT"
    TEST_ROOT="$(cd -- "$REQUESTED_TEST_ROOT" && pwd -P)"
else
    [[ -d "$TEST_TMPDIR" && -w "$TEST_TMPDIR" ]] \
        || fail "TEST_TMPDIR must be an existing writable directory: $TEST_TMPDIR"
    TEST_ROOT="$(mktemp -d "$TEST_TMPDIR/distillfeed-$EXPECTED_VERSION-test.XXXXXX")"
    AUTO_TEST_ROOT=1
fi
PROJECT="$TEST_ROOT/source/distillfeed"
INSTALL_PARENT="$TEST_ROOT/installer project with spaces"
INSTALL_PROJECT="$INSTALL_PARENT/distillfeed release"
BUILD_VENV="$TEST_ROOT/build-venv"
RUNTIME_VENV="$TEST_ROOT/runtime-venv"
WHEELHOUSE="$TEST_ROOT/wheelhouse"
SMOKE_ROOT="$TEST_ROOT/smoke"
CONSTRAINTS="$TEST_ROOT/locked-constraints.txt"
PIP_CACHE_DIR="$TEST_ROOT/pip-cache"
export PIP_CACHE_DIR

TEST_ROOT_MARKER="$TEST_ROOT/.distillfeed-release-test-root"
: > "$TEST_ROOT_MARKER"
SERVER_PID=""

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [[ "$status" -eq 0 && "$AUTO_TEST_ROOT" -eq 1 && "$KEEP_TEST_ROOT" -eq 0 && -f "$TEST_ROOT_MARKER" ]]; then
        rm -rf "$TEST_ROOT"
        note "Temporary test directory removed."
    else
        printf 'Test directory retained: %s\n' "$TEST_ROOT" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

note "DistillFeed $EXPECTED_VERSION release verification"
note "Archive: $ARCHIVE"
note "Test directory: $TEST_ROOT"
note "Validating and extracting the archive without trusting tar paths or owners..."

python3 - "$ARCHIVE" "$TEST_ROOT/source" "$EXPECTED_VERSION" "$EXPECTED_ARCHIVE_SHA256" <<'PY'
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
    "distillfeed/rss_reader/static/app.js",
    "distillfeed/rss_reader/static/ai.js",
    "distillfeed/rss_reader/static/summary.js",
    "distillfeed/rss_reader/static/setup.css",
    "distillfeed/rss_reader/static/setup.js",
    "distillfeed/rss_reader/templates/setup.html",
    "distillfeed/distillfeed_arxiv/__init__.py",
    "distillfeed/distillfeed_arxiv/plugin.py",
    "distillfeed/distillfeed_arxiv/resources/arxiv-digest.example.toml",
    "distillfeed/tests/conftest.py",
    "distillfeed/tests/test_launch_lifecycle.py",
    "distillfeed/tests/test_secret_store.py",
    "distillfeed/tests/test_setup_api.py",
    "distillfeed/tests/test_setup_commit.py",
    "distillfeed/tests/test_setup_profiles.py",
    "distillfeed/tests/test_setup_state_model.py",
    "distillfeed/deployment/start.sh",
    "distillfeed/AUDIT.md",
    "distillfeed/CHANGELOG.md",
    "distillfeed/MATURITY_AUDIT_0.22.0.md",
    "distillfeed/QUALITY.md",
    "distillfeed/README.md",
    "distillfeed/SECURITY.md",
    "distillfeed/docs/CUSTOM_FEEDS.md",
}
executable_files = {
    "distillfeed/install.sh",
    "distillfeed/launch.sh",
}
forbidden_parts = {
    ".git", ".venv", ".pytest_cache", "__pycache__", "build", "dist",
    ".distillfeed", "data", "backups",
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
        directories: list[tuple[Path, int]] = []
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
                expected_mode = 0o755 if name in executable_files else 0o644
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
        # Extraction is manual: link members were rejected, owner metadata is
        # ignored, files use exclusive creation, and validated modes are applied.
        for member in members:
            canonical_name = member.name[:-1] if member.name.endswith("/") else member.name
            path = PurePosixPath(canonical_name)
            destination = extract_root.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                directories.append((destination, member.mode & 0o777))
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
            destination.chmod(member.mode & 0o777)
        for directory, mode in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
            directory.chmod(mode)

print(f"Archive SHA-256: {actual_digest}")
print(f"Validated members: {len(members)}; expanded bytes: {total_bytes}")
PY

[[ -d "$PROJECT" ]] || fail "validated archive did not produce the expected project directory"

python3 - "$PROJECT" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = {
    "install.sh": 0o755,
    "launch.sh": 0o755,
    "rss_reader/resources/starter-subscriptions.opml": 0o644,
    "rss_reader/static/setup.css": 0o644,
    "rss_reader/static/setup.js": 0o644,
    "rss_reader/templates/setup.html": 0o644,
    "distillfeed_arxiv/resources/arxiv-digest.example.toml": 0o644,
}
for relative, wanted in expected.items():
    path = root / relative
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != wanted:
        raise SystemExit(
            f"Extracted {relative} must have mode {wanted:04o}, found {actual:04o}"
        )
for relative in ("install.sh", "launch.sh"):
    if not os.access(root / relative, os.X_OK):
        raise SystemExit(f"Extracted {relative} is not executable")
print("Release script and setup-asset modes: correct")
PY

note "Deriving exact runtime/test constraints from the frozen lock file..."
python3 - "$PROJECT/uv.lock" "$CONSTRAINTS" <<'PY'
import sys
import tomllib
from pathlib import Path

lock_path = Path(sys.argv[1])
destination = Path(sys.argv[2])
lock = tomllib.loads(lock_path.read_text("utf-8"))
versions: dict[str, str] = {}
for package in lock.get("package", []):
    source = package.get("source", {})
    if not isinstance(source, dict) or "registry" not in source:
        continue
    name = str(package.get("name", "")).strip()
    version = str(package.get("version", "")).strip()
    if not name or not version:
        raise SystemExit("uv.lock contains an incomplete registry package")
    previous = versions.setdefault(name, version)
    if previous != version:
        raise SystemExit(f"uv.lock contains multiple versions of {name}: {previous}, {version}")
if not versions:
    raise SystemExit("uv.lock contains no registry dependency pins")
destination.write_text(
    "# Generated from the release's frozen uv.lock\n"
    + "".join(f"{name}=={versions[name]}\n" for name in sorted(versions)),
    encoding="utf-8",
)
print(f"Locked registry packages: {len(versions)}")
PY

note "Checking release-version consistency..."
python3 - "$PROJECT" "$EXPECTED_VERSION" <<'PY'
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
distillfeed_packages = [p for p in lock.get("package", []) if p.get("name") == "distillfeed"]
if len(distillfeed_packages) != 1:
    raise SystemExit("uv.lock must contain exactly one distillfeed package")
checks["uv.lock"] = str(distillfeed_packages[0].get("version", ""))

citation = (root / "CITATION.cff").read_text("utf-8")
match = re.search(r"^version:\s*[\"']?([^\s\"']+)", citation, re.M)
if not match:
    raise SystemExit("CITATION.cff has no version")
checks["CITATION.cff"] = match.group(1)

bad = {name: value for name, value in checks.items() if value != expected}
if bad:
    raise SystemExit("Version mismatch: " + ", ".join(f"{name}={value}" for name, value in bad.items()))
print("Version loci:", ", ".join(f"{name}={value}" for name, value in checks.items()))
PY

note "Checking every shipped shell file..."
bash -n "$SCRIPT_DIR/test-distillfeed.sh"
while IFS= read -r -d '' shell_file; do
    first_line="$(head -n 1 "$shell_file" 2>/dev/null || true)"
    if [[ "$first_line" == *bash* ]]; then
        bash -n "$shell_file"
    else
        sh -n "$shell_file"
    fi
done < <(find "$PROJECT" -type f -name '*.sh' -print0)
if command -v node >/dev/null 2>&1; then
    note "Node.js found; checking shipped JavaScript syntax as an additional developer test..."
    while IFS= read -r -d '' javascript_file; do
        node --check "$javascript_file"
    done < <(find "$PROJECT/rss_reader/static" -type f -name '*.js' -print0)
else
    note "Node.js is absent; JavaScript syntax check skipped. DistillFeed users do not need Node.js."
fi

note "Building the release wheel in a clean copied-interpreter environment..."
python3 -m venv --copies "$BUILD_VENV"
mkdir -p "$WHEELHOUSE"
"$BUILD_VENV/bin/python" -m pip wheel --no-deps --wheel-dir "$WHEELHOUSE" "$PROJECT"

shopt -s nullglob
wheels=("$WHEELHOUSE"/*.whl)
shopt -u nullglob
[[ "${#wheels[@]}" -eq 1 ]] || fail "wheel build produced ${#wheels[@]} wheel files instead of one"
WHEEL="${wheels[0]}"
[[ "$(basename "$WHEEL")" == "distillfeed-${EXPECTED_VERSION}-"*.whl ]] \
    || fail "wheel filename does not identify DistillFeed $EXPECTED_VERSION: $(basename "$WHEEL")"

note "Verifying first-run resources are present and byte-identical in the wheel..."
python3 - "$WHEEL" "$PROJECT" <<'PY'
import csv
import io
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])
source = Path(sys.argv[2])
expected = {
    "rss_reader/resources/starter-subscriptions.opml": source / "rss_reader/resources/starter-subscriptions.opml",
    "rss_reader/static/setup.css": source / "rss_reader/static/setup.css",
    "rss_reader/static/setup.js": source / "rss_reader/static/setup.js",
    "rss_reader/templates/setup.html": source / "rss_reader/templates/setup.html",
    "distillfeed_arxiv/resources/arxiv-digest.example.toml": source / "distillfeed_arxiv/resources/arxiv-digest.example.toml",
}
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    missing = set(expected) - names
    if missing:
        raise SystemExit("Wheel package data is incomplete: " + ", ".join(sorted(missing)))
    for name, path in expected.items():
        if archive.read(name) != path.read_bytes():
            raise SystemExit(f"Wheel package data differs from source: {name}")
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise SystemExit("Wheel must contain exactly one RECORD")
    recorded = {
        row[0]
        for row in csv.reader(io.StringIO(archive.read(record_names[0]).decode("utf-8")))
        if row
    }
    absent_from_record = set(expected) - recorded
    if absent_from_record:
        raise SystemExit(
            "Wheel RECORD omits setup package data: " + ", ".join(sorted(absent_from_record))
        )
print("Wheel setup resources: complete")
PY

note "Installing the wheel with test and server extras in a second clean environment..."
python3 -m venv --copies "$RUNTIME_VENV"
"$RUNTIME_VENV/bin/python" -m pip install --constraint "$CONSTRAINTS" "${WHEEL}[server,test]"
"$RUNTIME_VENV/bin/python" -m pip check
"$RUNTIME_VENV/bin/python" - <<'PY'
from importlib.resources import files

expected = {
    "rss_reader": (
        "resources/starter-subscriptions.opml",
        "static/setup.css",
        "static/setup.js",
        "templates/setup.html",
    ),
    "distillfeed_arxiv": ("resources/arxiv-digest.example.toml",),
}
for package, resources in expected.items():
    root = files(package)
    for relative in resources:
        resource = root.joinpath(*relative.split("/"))
        if not resource.is_file() or not resource.read_bytes():
            raise SystemExit(f"Installed package resource is missing or empty: {package}/{relative}")
print("Installed setup resources: accessible")
PY

note "Independently verifying the complete setup and commit transition tables..."
"$RUNTIME_VENV/bin/python" - <<'PY'
from itertools import product

from rss_reader.setup_state import (
    COMMIT_TRANSITIONS,
    SETUP_TRANSITIONS,
    CommitEvent,
    CommitPhase,
    SetupEvent,
    SetupPhase,
    TransitionError,
    commit_transition,
    setup_transition,
)

expected_setup = {
    (SetupPhase.LISTENING, SetupEvent.BOOTSTRAP): SetupPhase.EDITING,
    (SetupPhase.LISTENING, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.LISTENING, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.EDITING, SetupEvent.INVALID): SetupPhase.EDITING,
    (SetupPhase.EDITING, SetupEvent.VALIDATE): SetupPhase.REVIEWED,
    (SetupPhase.EDITING, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.EDITING, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.REVIEWED, SetupEvent.EDIT): SetupPhase.EDITING,
    (SetupPhase.REVIEWED, SetupEvent.APPLY): SetupPhase.COMMITTING,
    (SetupPhase.REVIEWED, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.REVIEWED, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.COMMITTING, SetupEvent.SUCCEED): SetupPhase.COMPLETE,
    (SetupPhase.COMMITTING, SetupEvent.FAIL): SetupPhase.FAILED,
    (SetupPhase.COMMITTING, SetupEvent.REQUIRE_RECOVERY): SetupPhase.RECOVERY_REQUIRED,
    (SetupPhase.FAILED, SetupEvent.RETRY): SetupPhase.EDITING,
    (SetupPhase.FAILED, SetupEvent.VALIDATE): SetupPhase.REVIEWED,
    (SetupPhase.FAILED, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.FAILED, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.COMPLETE, SetupEvent.REPLAY): SetupPhase.COMPLETE,
}
expected_commit = {
    (CommitPhase.IDLE, CommitEvent.BEGIN): CommitPhase.STAGING_FILES,
    (CommitPhase.STAGING_FILES, CommitEvent.FILES_STAGED): CommitPhase.STAGING_DATABASE,
    (CommitPhase.STAGING_DATABASE, CommitEvent.DATABASE_STAGED): CommitPhase.VERIFYING_STAGE,
    (CommitPhase.VERIFYING_STAGE, CommitEvent.VERIFIED): CommitPhase.MARKING_STAGE,
    (CommitPhase.MARKING_STAGE, CommitEvent.MARKED): CommitPhase.PUBLISHING,
    (CommitPhase.PUBLISHING, CommitEvent.PUBLISHED): CommitPhase.POSTCHECK,
    (CommitPhase.POSTCHECK, CommitEvent.POSTCHECKED): CommitPhase.COMPLETE,
}
for phase in (
    CommitPhase.STAGING_FILES,
    CommitPhase.STAGING_DATABASE,
    CommitPhase.VERIFYING_STAGE,
    CommitPhase.MARKING_STAGE,
    CommitPhase.PUBLISHING,
    CommitPhase.POSTCHECK,
):
    expected_commit[(phase, CommitEvent.ROLLBACK)] = CommitPhase.ROLLED_BACK
    expected_commit[(phase, CommitEvent.ROLLBACK_FAILED)] = CommitPhase.RECOVERY_REQUIRED

if SETUP_TRANSITIONS != expected_setup:
    raise SystemExit("Setup transition table differs from the audited release contract")
if COMMIT_TRANSITIONS != expected_commit:
    raise SystemExit("Commit transition table differs from the audited release contract")

for phase, event in product(SetupPhase, SetupEvent):
    pair = (phase, event)
    if pair in expected_setup:
        if setup_transition(phase, event) != expected_setup[pair]:
            raise SystemExit(f"Wrong setup transition for {phase.value}/{event.value}")
    else:
        try:
            setup_transition(phase, event)
        except TransitionError:
            pass
        else:
            raise SystemExit(f"Forbidden setup edge accepted: {phase.value}/{event.value}")
for phase, event in product(CommitPhase, CommitEvent):
    pair = (phase, event)
    if pair in expected_commit:
        if commit_transition(phase, event) != expected_commit[pair]:
            raise SystemExit(f"Wrong commit transition for {phase.value}/{event.value}")
    else:
        try:
            commit_transition(phase, event)
        except TransitionError:
            pass
        else:
            raise SystemExit(f"Forbidden commit edge accepted: {phase.value}/{event.value}")

def reachable(start, transitions):
    found = {start}
    changed = True
    while changed:
        changed = False
        for (source, _event), target in transitions.items():
            if source in found and target not in found:
                found.add(target)
                changed = True
    return found

if reachable(SetupPhase.LISTENING, expected_setup) != set(SetupPhase):
    raise SystemExit("One or more setup states are unreachable")
if reachable(CommitPhase.IDLE, expected_commit) != set(CommitPhase):
    raise SystemExit("One or more commit states are unreachable")
print(
    f"Transition contract: {len(expected_setup)} setup edges and "
    f"{len(expected_commit)} commit edges; all other pairs rejected"
)
PY

note "Compiling Python and running the full deterministic test suite..."
"$RUNTIME_VENV/bin/python" -m compileall -q "$PROJECT/rss_reader" "$PROJECT/distillfeed_arxiv"
(
    cd "$PROJECT"
    "$RUNTIME_VENV/bin/python" -m pytest
)

note "Exercising the no-Node installer from a project path containing spaces..."
mkdir -p "$INSTALL_PARENT"
python3 - "$PROJECT" "$INSTALL_PROJECT" <<'PY'
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
shutil.copytree(
    source,
    destination,
    symlinks=False,
    ignore=shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "*.pyc", "*.pyo", "*.egg-info"
    ),
)
PY
(
    cd "$INSTALL_PROJECT"
    PIP_CONSTRAINT="$CONSTRAINTS" ./install.sh
)

[[ ! -e "$INSTALL_PROJECT/.distillfeed/instance" ]] \
    || fail "install.sh created an application instance before browser setup"
[[ ! -e "$INSTALL_PROJECT/config.toml" ]] \
    || fail "install.sh created or changed application settings"

INSTALL_MARKER_STATE_BEFORE="$(python3 - "$INSTALL_PROJECT" "$EXPECTED_VERSION" <<'PY'
import hashlib
import json
import stat
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
expected_version = sys.argv[2]
state = root / ".distillfeed"
marker = root / ".venv/.distillfeed-install.json"
lock = state / "install.lock"

if stat.S_IMODE(state.stat().st_mode) != 0o700:
    raise SystemExit("Installer state directory must have mode 0700")
if sorted(path.name for path in state.iterdir()) != ["install.lock"]:
    raise SystemExit("Installer created unexpected runtime state before setup")
if not lock.is_file() or lock.is_symlink() or stat.S_IMODE(lock.stat().st_mode) != 0o600:
    raise SystemExit("Installer lock must be a regular owner-only file")
if not marker.is_file() or marker.is_symlink() or stat.S_IMODE(marker.stat().st_mode) != 0o600:
    raise SystemExit("Installer completion marker must be a regular owner-only file")

with (root / "pyproject.toml").open("rb") as handle:
    source_version = str(tomllib.load(handle)["project"]["version"])
if source_version != expected_version:
    raise SystemExit("Copied installer source has the wrong version")

digest = hashlib.sha256()
paths = [root / "pyproject.toml"]
for package in ("rss_reader", "distillfeed_arxiv"):
    paths.extend(
        path for path in (root / package).rglob("*")
        if path.is_file() and not path.is_symlink()
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
expected = {
    "schema": 2,
    "version": expected_version,
    "source_sha256": digest.hexdigest(),
}
document = json.loads(marker.read_text(encoding="utf-8"))
if document != expected:
    raise SystemExit("Installer completion marker does not match the installed source")

for relative in (
    "data", "config.toml", "rssreader.sqlite3", "working-subscriptions.opml",
):
    if (root / relative).exists() or (root / relative).is_symlink():
        raise SystemExit(f"Installer created forbidden application runtime path: {relative}")

print(f"{marker.stat().st_mtime_ns}:{hashlib.sha256(marker.read_bytes()).hexdigest()}")
PY
)"

(
    cd "$INSTALL_PROJECT"
    ./install.sh --check
)
INSTALL_MARKER_STATE_AFTER_CHECK="$(python3 - "$INSTALL_PROJECT/.venv/.distillfeed-install.json" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
print(f"{path.stat().st_mtime_ns}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
PY
)"
[[ "$INSTALL_MARKER_STATE_BEFORE" == "$INSTALL_MARKER_STATE_AFTER_CHECK" ]] \
    || fail "install.sh --check rewrote its verified completion marker"

INSTALL_SECOND_OUTPUT="$(
    cd "$INSTALL_PROJECT"
    PIP_CONSTRAINT="$CONSTRAINTS" ./install.sh
)"
[[ "$INSTALL_SECOND_OUTPUT" == *"already installed for this release"* ]] \
    || fail "a second install.sh run did not take its idempotent fast path"
(
    cd "$INSTALL_PROJECT"
    ./install.sh --check
)
INSTALL_MARKER_STATE_AFTER="$(python3 - "$INSTALL_PROJECT/.venv/.distillfeed-install.json" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
print(f"{path.stat().st_mtime_ns}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
PY
)"
[[ "$INSTALL_MARKER_STATE_BEFORE" == "$INSTALL_MARKER_STATE_AFTER" ]] \
    || fail "idempotent install rewrote its verified completion marker"
note "Installer marker and no-runtime-before-setup invariants: correct"

note "Creating the recommended managed instance through the setup state machine..."
MANAGED_PORT="$(python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"
"$INSTALL_PROJECT/.venv/bin/python" - "$INSTALL_PROJECT" "$MANAGED_PORT" <<'PY'
import json
import sys
from pathlib import Path

from rss_reader.config import load_config
from rss_reader.db import connect
from rss_reader.launcher import TargetKind, classify_target, verify_managed_instance
from rss_reader.setup_service import SetupCommitter, SetupSession, preset_payload
from rss_reader.setup_state import CommitPhase, SetupPhase

root = Path(sys.argv[1])
port = int(sys.argv[2])
if classify_target(root, environ={}).kind != TargetKind.FIRST_RUN:
    raise SystemExit("Installer did not leave the project in first-run state")

session = SetupSession(SetupCommitter(root / ".distillfeed"))
session.bootstrap()
if session.phase != SetupPhase.EDITING:
    raise SystemExit("Setup did not enter editing after bootstrap")
payload = preset_payload("recommended")
payload["port"] = port
token, review = session.validate(payload, environment={})
if session.phase != SetupPhase.REVIEWED or review["reader_url"] != f"http://127.0.0.1:{port}/":
    raise SystemExit("Recommended setup did not enter an exact reviewed state")
result = session.complete(token)
if session.phase != SetupPhase.COMPLETE or session.committer.commit_phase != CommitPhase.COMPLETE:
    raise SystemExit("Recommended setup did not reach both durable complete states")
if session.complete(token) != result or session.phase != SetupPhase.COMPLETE:
    raise SystemExit("Completion replay was not idempotent")

target = classify_target(root, environ={})
if target.kind != TargetKind.MANAGED or target.instance_path != result.instance_path:
    raise SystemExit("Completed setup was not classified as one managed instance")
config = verify_managed_instance(result.instance_path)
if config.path != result.config_path or int(config.get("app", "port")) != port:
    raise SystemExit("Managed configuration does not match the reviewed port")
expected_safe = {
    ("app", "mode"): "local",
    ("app", "host"): "127.0.0.1",
    ("app", "background_scheduler_enabled"): False,
    ("app", "auto_summarize_after_refresh"): False,
    ("llm", "enabled"): False,
    ("weather", "enabled"): False,
    ("plugins", "arxiv_digest_enabled"): False,
    ("notifications", "ntfy", "enabled"): False,
}
for path, expected in expected_safe.items():
    actual = config.data
    for part in path:
        actual = actual[part]
    if actual != expected:
        raise SystemExit(f"Recommended setup is unsafe at {'.'.join(path)}")
manifest = json.loads((result.instance_path / "setup.json").read_text(encoding="utf-8"))
if manifest.get("profile") != "recommended" or manifest.get("state") != "ready":
    raise SystemExit("Recommended setup manifest is incorrect")
with connect(config.database_path) as connection:
    counts = {
        "refresh": connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0],
        "ai": connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0],
        "ntfy": connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0],
    }
if any(counts.values()):
    raise SystemExit(f"Setup performed external or paid work: {counts}")
print("Recommended setup state machine and pristine managed instance: correct")
PY

wait_for_managed_health() {
    "$INSTALL_PROJECT/.venv/bin/python" - "$MANAGED_PORT" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{int(sys.argv[1])}/api/status"
last_error = None
for _ in range(80):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            payload = json.load(response)
            if response.status == 200 and isinstance(payload, dict):
                print("Managed launcher health: ready")
                break
    except (OSError, ValueError, urllib.error.URLError) as exc:
        last_error = exc
    time.sleep(0.2)
else:
    raise SystemExit(f"Managed launcher did not become healthy: {last_error}")
PY
}

start_managed_server() {
    local log_path=$1
    (
        cd "$INSTALL_PROJECT"
        exec ./launch.sh --no-browser
    ) >"$log_path" 2>&1 &
    SERVER_PID=$!
}

note "Checking managed launch health, duplicate-launch rejection, and relaunch..."
start_managed_server "$TEST_ROOT/managed-first-launch.log"
wait_for_managed_health
set +e
DUPLICATE_OUTPUT="$(
    cd "$INSTALL_PROJECT"
    ./launch.sh --no-browser 2>&1
)"
DUPLICATE_STATUS=$?
set -e
[[ "$DUPLICATE_STATUS" -eq 1 ]] \
    || fail "a duplicate managed launcher returned $DUPLICATE_STATUS instead of failing fast"
[[ "$DUPLICATE_OUTPUT" == *"already starting or running"* ]] \
    || fail "duplicate managed launch did not provide its actionable lock message"
kill -0 "$SERVER_PID" 2>/dev/null \
    || fail "duplicate launch disturbed the already-running reader"
wait_for_managed_health
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""

start_managed_server "$TEST_ROOT/managed-relaunch.log"
wait_for_managed_health
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""

"$INSTALL_PROJECT/.venv/bin/python" - "$INSTALL_PROJECT" <<'PY'
import sys
from pathlib import Path

from rss_reader.config import load_config
from rss_reader.db import connect
from rss_reader.launcher import verify_managed_instance

instance = Path(sys.argv[1]) / ".distillfeed/instance"
config = verify_managed_instance(instance)
with connect(config.database_path) as connection:
    counts = (
        connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0],
    )
if any(counts):
    raise SystemExit(f"Health-only launches unexpectedly performed external work: {counts}")
print("Managed launch/relaunch remained pristine and durable")
PY

note "Verifying and committing the exact 0.22 demo contract without external calls..."
DEMO_STATE="$TEST_ROOT/demo contract/.distillfeed"
"$INSTALL_PROJECT/.venv/bin/python" - "$DEMO_STATE" <<'PY'
import json
import sys
import tomllib
from pathlib import Path

from rss_reader.config import load_config
from rss_reader.db import connect
from rss_reader.secret_store import load_secret_store
from rss_reader.setup_service import SetupCommitter, SetupSession, preset_payload
from rss_reader.setup_state import CommitPhase, SetupPhase

state_root = Path(sys.argv[1])
payload = preset_payload("demo")
contract = {
    "profile": "demo",
    "port": 8081,
    "subscriptions": "starter",
    "ai_provider": "openai",
    "summary_threshold": 40,
    "summary_window_days": 7,
    "candidate_age_days": 30,
    "review_workload": "balanced",
    "monthly_budget_usd": 2.0,
    "arxiv_enabled": True,
    "arxiv_categories": "cs.AI",
    "arxiv_lookback_days": 7,
    "arxiv_final_threshold": 25,
    "weather_enabled": False,
    "background_updates": False,
    "auto_summarize": False,
    "ntfy_enabled": False,
}
for name, expected in contract.items():
    if payload.get(name) != expected:
        raise SystemExit(f"Demo preset contract mismatch: {name}={payload.get(name)!r}")

# A syntactically private placeholder satisfies validation. Setup must store it
# locally but must never use it or contact a provider.
placeholder = "distillfeed-release-verifier-never-send"
payload["openai_key"] = placeholder
session = SetupSession(SetupCommitter(state_root))
session.bootstrap()
token, review = session.validate(payload, environment={})
if review["ordinary_threshold"] != "40/100":
    raise SystemExit("Demo review changed the ordinary summary threshold")
if review["ordinary_evidence_window"] != "Previous 7 day(s)":
    raise SystemExit("Demo review changed the ordinary evidence window")
if review["candidate_age_limit"] != "30 day(s)":
    raise SystemExit("Demo review changed the candidate age limit")
if "first retrieval looks back 7 day(s)" not in review["arxiv"]:
    raise SystemExit("Demo review changed the arXiv first-retrieval window")
result = session.complete(token)
if session.phase != SetupPhase.COMPLETE or session.committer.commit_phase != CommitPhase.COMPLETE:
    raise SystemExit("Demo setup did not reach durable completion")

config = load_config(result.config_path)
configured = {
    ("app", "port"): 8081,
    ("llm", "enabled"): True,
    ("llm", "minimum_relevance"): 40,
    ("llm", "rolling_digest_hours"): 168,
    ("llm", "candidate_max_age_days"): 30,
    ("plugins", "arxiv_digest_enabled"): True,
    ("app", "background_scheduler_enabled"): False,
    ("app", "auto_summarize_after_refresh"): False,
    ("weather", "enabled"): False,
    ("notifications", "ntfy", "enabled"): False,
}
for path, expected in configured.items():
    actual = config.data
    for part in path:
        actual = actual[part]
    if actual != expected:
        raise SystemExit(f"Committed demo contract mismatch: {'.'.join(path)}")
with (result.instance_path / "arxiv-digest.toml").open("rb") as handle:
    arxiv = tomllib.load(handle)
if arxiv["arxiv"]["categories"] != ["cs.AI"]:
    raise SystemExit("Committed demo arXiv categories are incorrect")
if arxiv["arxiv"]["initial_lookback_days"] != 7:
    raise SystemExit("Committed demo arXiv lookback is not seven days")
if arxiv["filters"]["final_keep_threshold"] != 25:
    raise SystemExit("Committed demo arXiv keep threshold is incorrect")
if load_secret_store(result.instance_path / "private/secrets.json") != {
    "OPENAI_API_KEY": placeholder
}:
    raise SystemExit("Demo setup did not use the strict private secret store")
manifest = json.loads((result.instance_path / "setup.json").read_text(encoding="utf-8"))
if manifest.get("profile") != "demo" or manifest.get("state") != "ready":
    raise SystemExit("Demo completion manifest is incorrect")
with connect(config.database_path) as connection:
    counts = {
        "refresh": connection.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0],
        "ai": connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0],
        "ntfy": connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0],
    }
    watermark = connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='last_complete_at'"
    ).fetchone()
if any(counts.values()) or watermark is not None:
    raise SystemExit(f"Demo setup performed external or paid work: {counts}, watermark={watermark}")
print("Exact demo setup contract and no-external-work invariant: correct")
PY

mkdir -p "$SMOKE_ROOT"
CONFIG="$SMOKE_ROOT/config.toml"
DISTILLFEED="$RUNTIME_VENV/bin/distillfeed"

note "Running isolated installed-wheel CLI smoke checks..."
(
    cd "$SMOKE_ROOT"
    "$DISTILLFEED" --config "$CONFIG" init
    "$DISTILLFEED" --config "$CONFIG" language-profile English
    "$DISTILLFEED" --config "$CONFIG" doctor
)

PLUGIN_STATE="$("$DISTILLFEED" --config "$CONFIG" plugin list)"
"$RUNTIME_VENV/bin/python" - "$PLUGIN_STATE" "$EXPECTED_VERSION" "$PROJECT" <<'PY'
import importlib.metadata
import json
import sys
from pathlib import Path

state = json.loads(sys.argv[1])
expected = sys.argv[2]
source = Path(sys.argv[3]).resolve()
if "arxiv_digest" not in state.get("installed", []):
    raise SystemExit("Bundled arxiv_digest entry point is not installed")
if "arxiv_digest" in state.get("enabled", []):
    raise SystemExit("Bundled arxiv_digest must be disabled in a fresh public installation")
if importlib.metadata.version("distillfeed") != expected:
    raise SystemExit("Installed wheel metadata has the wrong version")
import rss_reader
installed_path = Path(rss_reader.__file__).resolve()
if installed_path.is_relative_to(source):
    raise SystemExit("Smoke checks imported the extracted source instead of the installed wheel")
print("Installed package:", installed_path)
PY

note "Running installed-wheel Flask smoke checks..."
(
    cd "$SMOKE_ROOT"
    "$RUNTIME_VENV/bin/python" - "$CONFIG" <<'PY'
import json
import sys
from rss_reader.web import create_app

app = create_app(sys.argv[1])
client = app.test_client()
for path in ("/", "/ai", "/summaries", "/notifications", "/costs", "/api/status"):
    response = client.get(path)
    if response.status_code != 200:
        raise SystemExit(f"Installed-wheel web smoke failed: {path} returned {response.status_code}")
status = client.get("/api/status").get_json()
if not isinstance(status, dict):
    raise SystemExit("/api/status did not return a JSON object")
print("Flask routes: healthy")
PY
)

note "Starting installed gunicorn and checking local HTTP routes..."
PORT="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
export RSSREADER_CONFIG="$CONFIG"
export DISTILLFEED_MODE="development"
(
    cd "$SMOKE_ROOT"
    exec "$RUNTIME_VENV/bin/gunicorn" --workers 1 --threads 2 --timeout 30 \
        --bind "127.0.0.1:$PORT" --access-logfile - --error-logfile - rss_reader.wsgi:app
) >"$TEST_ROOT/gunicorn.log" 2>&1 &
SERVER_PID=$!

"$RUNTIME_VENV/bin/python" - "$PORT" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

port = int(sys.argv[1])
base = f"http://127.0.0.1:{port}"
last_error = None
for _ in range(60):
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=2) as response:
            if response.status == 200:
                payload = json.load(response)
                if not isinstance(payload, dict):
                    raise RuntimeError("status response is not a JSON object")
                break
    except (OSError, ValueError, urllib.error.URLError) as exc:
        last_error = exc
    time.sleep(0.2)
else:
    raise SystemExit(f"gunicorn did not become healthy: {last_error}")

for path in ("/", "/ai", "/summaries"):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        if response.status != 200:
            raise SystemExit(f"gunicorn smoke failed: {path} returned {response.status}")
print("Gunicorn routes: healthy")
PY

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
unset RSSREADER_CONFIG DISTILLFEED_MODE

if [[ "$RUN_LIVE_REFRESH" -eq 1 ]]; then
    printf '\nWARNING: RUN_LIVE_REFRESH=1 permits outbound requests to every starter feed.\n' >&2
    printf '         Results depend on third-party availability and are not a release criterion.\n\n' >&2
    "$DISTILLFEED" --config "$CONFIG" refresh
fi

if [[ "$RUN_PAID_AI" -eq 1 ]]; then
    [[ "$RUN_LIVE_REFRESH" -eq 1 ]] \
        || fail "RUN_PAID_AI=1 also requires RUN_LIVE_REFRESH=1 so the live scope is explicit"
    [[ "$CONFIRM_PAID_AI" == "I_ACCEPT_PROVIDER_CHARGES" ]] \
        || fail "paid AI requires CONFIRM_PAID_AI=I_ACCEPT_PROVIDER_CHARGES"
    [[ -n "$LIVE_OPENAI_API_KEY" ]] \
        || fail "paid AI was requested but OPENAI_API_KEY was not present when the script started"
    printf '\nWARNING: PAID AI TEST ENABLED. THIS SENDS FEED CONTENT TO OPENAI AND INCURS CHARGES.\n' >&2
    printf '         The script cannot guarantee a provider-side spending ceiling.\n' >&2
    printf '         The exact confirmation token supplied above acknowledges this risk.\n\n' >&2
    OPENAI_API_KEY="$LIVE_OPENAI_API_KEY" "$DISTILLFEED" --config "$CONFIG" summarize
fi

note ""
note "DistillFeed $EXPECTED_VERSION release verification completed successfully."
note "Archive, package data, tests, no-Node install, setup transitions, launch lifecycle, CLI, Flask, and gunicorn checks passed."
if [[ "$KEEP_TEST_ROOT" -eq 1 || "$AUTO_TEST_ROOT" -eq 0 ]]; then
    note "Retained test installation: $TEST_ROOT"
fi
