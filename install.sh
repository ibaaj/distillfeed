#!/bin/sh

set -eu
umask 077

ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
VENV="$ROOT/.venv"
VENV_PYTHON="$VENV/bin/python"
MARKER="$VENV/.distillfeed-install.json"
PYTHON_BIN=${DISTILLFEED_PYTHON:-python3}

usage() {
    printf '%s\n' "Usage: ./install.sh [--check]"
    printf '%s\n' "Creates the private Python environment used by ./launch.sh. Node.js is not required."
}

case "${1-}" in
    "") MODE=install ;;
    --check) MODE=check ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf '%s\n' "error: Python 3.11 or newer is required. Install Python, then run ./launch.sh again." >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    printf '%s\n' "error: Python 3.11 or newer is required. The selected Python is too old." >&2
    exit 1
fi

if [ "${DISTILLFEED_INSTALL_LOCKED-}" != "1" ]; then
    exec "$PYTHON_BIN" -c '
import fcntl
import os
import pathlib
import stat
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
state = root / ".distillfeed"
try:
    metadata = state.lstat()
except FileNotFoundError:
    state.mkdir(mode=0o700)
else:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        print(f"error: refusing unsafe installer state path: {state}", file=sys.stderr)
        raise SystemExit(1)
os.chmod(state, 0o700)
lock_path = state / "install.lock"
try:
    lock_metadata = lock_path.lstat()
except FileNotFoundError:
    lock_metadata = None
if lock_metadata is not None and (
    stat.S_ISLNK(lock_metadata.st_mode) or not stat.S_ISREG(lock_metadata.st_mode)
):
    print(f"error: refusing unsafe installer lock path: {lock_path}", file=sys.stderr)
    raise SystemExit(1)
flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(lock_path, flags, 0o600)
except OSError as exc:
    print(f"error: the installer lock cannot be opened: {exc}", file=sys.stderr)
    raise SystemExit(1)
try:
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    environment = dict(os.environ)
    environment["DISTILLFEED_INSTALL_LOCKED"] = "1"
    completed = subprocess.run(
        [str(root / "install.sh"), *sys.argv[2:]], cwd=root, env=environment
    )
    raise SystemExit(completed.returncode)
finally:
    os.close(descriptor)
' "$ROOT" "${1-}"
fi

if [ -L "$VENV" ] || { [ -e "$VENV" ] && [ ! -d "$VENV" ]; }; then
    printf '%s\n' "error: refusing unsafe Python environment path: $VENV" >&2
    exit 1
fi

verify_install() {
    require_marker=$1
    "$VENV_PYTHON" -c '
import importlib.metadata
import hashlib
import json
import os
import pathlib
import stat
import sys
import tomllib

root = pathlib.Path(sys.argv[1]).resolve()
marker = pathlib.Path(sys.argv[2])
require_marker = sys.argv[3] == "yes"
if sys.version_info < (3, 11):
    raise SystemExit(1)
with (root / "pyproject.toml").open("rb") as handle:
    source_version = str(tomllib.load(handle)["project"]["version"])
def source_fingerprint():
    digest = hashlib.sha256()
    paths = [root / "pyproject.toml"]
    for package in ("rss_reader", "distillfeed_arxiv"):
        paths.extend(
            path for path in (root / package).rglob("*")
            if path.is_file() and not path.is_symlink()
            and path.suffix in {".py", ".html", ".css", ".js", ".svg", ".webmanifest", ".opml", ".toml"}
        )
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
installed_version = importlib.metadata.version("distillfeed")
import rss_reader
import rss_reader.launcher
import rss_reader.setup_web
entries = tuple(importlib.metadata.entry_points(
    group="distillfeed.plugins", name="arxiv_digest"
))
if (
    installed_version != source_version
    or rss_reader.__version__ != source_version
    or len(entries) != 1
):
    raise SystemExit(1)
entries[0].load()
if require_marker:
    try:
        metadata = marker.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise OSError("unsafe marker metadata")
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SystemExit(1)
    if document != {
        "schema": 2,
        "version": source_version,
        "source_sha256": source_fingerprint(),
    }:
        raise SystemExit(1)
' "$ROOT" "$MARKER" "$require_marker"
}

if [ "$MODE" = check ]; then
    [ -x "$VENV_PYTHON" ] && [ -f "$MARKER" ] && verify_install yes
    exit $?
fi

# A second launcher may have completed installation while this process waited
# for the installer lock. Avoid a redundant or concurrent package installation.
if [ -x "$VENV_PYTHON" ] && [ -f "$MARKER" ] && verify_install yes >/dev/null 2>&1; then
    printf '%s\n' "DistillFeed is already installed for this release."
    exit 0
fi

printf '%s\n' "Preparing DistillFeed's private Python environment..."
printf '%s\n' "Node.js is not required. No application settings are changed during this step."
if [ ! -x "$VENV_PYTHON" ]; then
    "$PYTHON_BIN" -m venv "$VENV"
fi

(
    cd "$ROOT"
    "$VENV_PYTHON" -m pip install --disable-pip-version-check --no-input '.[server]'
)

"$VENV_PYTHON" -m pip check
verify_install no
"$VENV_PYTHON" -c '
import json
import hashlib
import os
import pathlib
import sys
import tempfile
import tomllib

root = pathlib.Path(sys.argv[1]).resolve()
marker = pathlib.Path(sys.argv[2])
with (root / "pyproject.toml").open("rb") as handle:
    version = str(tomllib.load(handle)["project"]["version"])
digest = hashlib.sha256()
paths = [root / "pyproject.toml"]
for package in ("rss_reader", "distillfeed_arxiv"):
    paths.extend(
        path for path in (root / package).rglob("*")
        if path.is_file() and not path.is_symlink()
        and path.suffix in {".py", ".html", ".css", ".js", ".svg", ".webmanifest", ".opml", ".toml"}
    )
for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix().encode("utf-8")
    content = path.read_bytes()
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
source_sha256 = digest.hexdigest()
descriptor, temporary_name = tempfile.mkstemp(prefix=".distillfeed-install-", dir=marker.parent)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(
            {"schema": 2, "version": version, "source_sha256": source_sha256},
            output,
            sort_keys=True,
        )
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
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    raise
' "$ROOT" "$MARKER"

if [ "${DISTILLFEED_FROM_LAUNCH-}" = "1" ]; then
    printf '%s\n' "DistillFeed is installed. Opening the browser setup now..."
else
    printf '%s\n' "DistillFeed is installed. Run ./launch.sh to open the browser setup."
fi
