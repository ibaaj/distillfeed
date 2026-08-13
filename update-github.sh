#!/usr/bin/env bash

if ((BASH_VERSINFO[0] < 3 || (BASH_VERSINFO[0] == 3 && BASH_VERSINFO[1] < 2))); then
    printf 'Error: DistillFeed requires Bash 3.2 or newer.\n' >&2
    exit 1
fi

set -Eeuo pipefail
umask 077

EXPECTED_VERSION="0.23.7"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ARCHIVE="$SCRIPT_DIR/distillfeed-update-$EXPECTED_VERSION.zip"
BRANCH="main"
MESSAGE="Release DistillFeed $EXPECTED_VERSION"
PUSH=0
REPOSITORY_ARGUMENT=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] GITHUB_REPOSITORY

Apply DistillFeed $EXPECTED_VERSION code to a clean Git checkout after first
fast-forwarding it from origin. The repository's README.md, CITATION.cff state,
and UPDATE-GITHUB.md state are preserved exactly.

Options:
  --archive PATH    Release .tar.gz or update-bundle .zip
                    (default: $ARCHIVE)
  --branch NAME     Branch to update (default: main)
  --message TEXT    Commit message (default: $MESSAGE)
  --push            Commit the staged update and push it to origin
  -h, --help        Show this help

Without --push, the script validates and stages the update for your review.
It never force-pushes and refuses dirty, detached, diverged, or in-progress
Git states.
EOF
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --archive)
            (($# >= 2)) || fail "--archive requires a path"
            ARCHIVE="$2"
            shift 2
            ;;
        --branch)
            (($# >= 2)) || fail "--branch requires a name"
            BRANCH="$2"
            shift 2
            ;;
        --message)
            (($# >= 2)) || fail "--message requires text"
            MESSAGE="$2"
            shift 2
            ;;
        --push)
            PUSH=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            (($# == 1)) || fail "provide exactly one GITHUB_REPOSITORY"
            REPOSITORY_ARGUMENT="$1"
            shift
            ;;
        -*)
            fail "unknown option: $1"
            ;;
        *)
            [[ -z "$REPOSITORY_ARGUMENT" ]] \
                || fail "provide exactly one GITHUB_REPOSITORY"
            REPOSITORY_ARGUMENT="$1"
            shift
            ;;
    esac
done

[[ -n "$REPOSITORY_ARGUMENT" ]] || { usage >&2; fail "GITHUB_REPOSITORY is required"; }
for command in git python3 mktemp; do
    command -v "$command" >/dev/null 2>&1 \
        || fail "required command is unavailable: $command"
done
python3 - <<'PY' || fail "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

[[ -d "$REPOSITORY_ARGUMENT" ]] \
    || fail "repository directory not found: $REPOSITORY_ARGUMENT"
REPOSITORY="$(cd -- "$REPOSITORY_ARGUMENT" && pwd -P)"
[[ "$REPOSITORY" != "/" ]] || fail "refusing to update the filesystem root"
git -C "$REPOSITORY" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "not a Git worktree: $REPOSITORY"
[[ "$(git -C "$REPOSITORY" rev-parse --show-toplevel)" == "$REPOSITORY" ]] \
    || fail "GITHUB_REPOSITORY must be the Git worktree root"

GIT_DIR="$(git -C "$REPOSITORY" rev-parse --git-dir)"
if [[ "$GIT_DIR" != /* ]]; then
    GIT_DIR="$REPOSITORY/$GIT_DIR"
fi
for marker in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
    [[ ! -e "$GIT_DIR/$marker" ]] \
        || fail "Git has an unfinished operation ($marker); finish or abort it first"
done
[[ -z "$(git -C "$REPOSITORY" status --porcelain --untracked-files=normal)" ]] \
    || fail "repository has tracked or untracked changes; commit, move, or stash them first"
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] \
    || fail "release archive is missing or is a symbolic link: $ARCHIVE"
ARCHIVE="$(python3 - "$ARCHIVE" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve(strict=True))
PY
)"

git -C "$REPOSITORY" check-ref-format --branch "$BRANCH" >/dev/null \
    || fail "invalid branch name: $BRANCH"
git -C "$REPOSITORY" remote get-url origin >/dev/null 2>&1 \
    || fail "the repository has no origin remote"

printf 'Fetching origin/%s...\n' "$BRANCH"
git -C "$REPOSITORY" fetch --prune origin "$BRANCH"
git -C "$REPOSITORY" show-ref --verify --quiet "refs/heads/$BRANCH" \
    || fail "local branch does not exist: $BRANCH"
git -C "$REPOSITORY" switch "$BRANCH"
[[ "$(git -C "$REPOSITORY" symbolic-ref --short HEAD)" == "$BRANCH" ]] \
    || fail "could not attach HEAD to $BRANCH"
git -C "$REPOSITORY" merge --ff-only "origin/$BRANCH"
[[ -z "$(git -C "$REPOSITORY" status --porcelain --untracked-files=normal)" ]] \
    || fail "fast-forward left unexpected worktree changes"

PROTECTED=(README.md CITATION.cff UPDATE-GITHUB.md)
PROTECTED_STATE="$(python3 - "$REPOSITORY" "${PROTECTED[@]}" <<'PY'
from __future__ import annotations
import hashlib
import json
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
state = {}
for name in sys.argv[2:]:
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        state[name] = {"kind": "absent"}
        continue
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"Protected repository path must be absent or a regular file: {name}")
    state[name] = {
        "kind": "file",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": stat.S_IMODE(metadata.st_mode),
    }
print(json.dumps(state, sort_keys=True, separators=(",", ":")))
PY
)" || fail "could not record protected repository files"

REPOSITORY_PARENT="$(dirname -- "$REPOSITORY")"
STAGE="$(mktemp -d "$REPOSITORY_PARENT/.distillfeed-github-$EXPECTED_VERSION.XXXXXX")"
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    case "$STAGE" in
        "$REPOSITORY_PARENT"/.distillfeed-github-"$EXPECTED_VERSION".*)
            rm -rf -- "$STAGE"
            ;;
        *)
            printf 'Refusing unsafe staging cleanup: %s\n' "$STAGE" >&2
            status=2
            ;;
    esac
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Validating release %s and preparing its code overlay...\n' "$EXPECTED_VERSION"
python3 - "$ARCHIVE" "$STAGE" "$EXPECTED_VERSION" <<'PY'
from __future__ import annotations

import shutil
import stat
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

source = Path(sys.argv[1])
stage = Path(sys.argv[2])
version = sys.argv[3]
maximum_archive_bytes = 128 * 1024 * 1024
maximum_member_bytes = 64 * 1024 * 1024
maximum_total_bytes = 256 * 1024 * 1024
maximum_members = 5_000

if source.stat().st_size > maximum_archive_bytes:
    raise SystemExit("Release input exceeds the 128 MiB limit")
payload = source
if zipfile.is_zipfile(source):
    expected = f"distillfeed-{version}.tar.gz"
    with zipfile.ZipFile(source) as bundle:
        matches = [item for item in bundle.infolist() if item.filename == expected]
        if len(matches) != 1:
            raise SystemExit(f"Update bundle must contain exactly one {expected}")
        member = matches[0]
        if member.is_dir() or member.flag_bits & 0x1 or member.file_size > maximum_archive_bytes:
            raise SystemExit("Update bundle payload is invalid")
        payload = stage / expected
        with bundle.open(member) as input_handle, payload.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)

extract_root = stage / "extracted"
extract_root.mkdir(mode=0o700)
regular_files: set[str] = set()
total_bytes = 0
with tarfile.open(payload, mode="r:gz") as archive:
    members = archive.getmembers()
    if not members or len(members) > maximum_members:
        raise SystemExit("Release archive has an invalid member count")
    seen: set[str] = set()
    for member in members:
        raw_name = member.name
        canonical = raw_name[:-1] if raw_name.endswith("/") else raw_name
        path = PurePosixPath(canonical)
        if (
            not canonical or "\\" in canonical or "\x00" in canonical
            or any(ord(character) < 32 or ord(character) == 127 for character in canonical)
            or path.is_absolute() or not path.parts or path.parts[0] != "distillfeed"
            or ".." in path.parts or path.as_posix() != canonical or canonical in seen
        ):
            raise SystemExit(f"Unsafe release member: {raw_name!r}")
        seen.add(canonical)
        mode = member.mode & 0o7777
        if mode & 0o7000:
            raise SystemExit(f"Special permission bits are forbidden: {canonical}")
        if member.isdir():
            if mode != 0o755:
                raise SystemExit(f"Release directory is not mode 0755: {canonical}")
            destination = extract_root.joinpath(*path.parts)
            destination.mkdir(parents=True, exist_ok=True)
            destination.chmod(0o755)
            continue
        if not member.isfile():
            raise SystemExit(f"Links and special files are forbidden: {canonical}")
        if member.size < 0 or member.size > maximum_member_bytes:
            raise SystemExit(f"Release member has an invalid size: {canonical}")
        total_bytes += member.size
        if total_bytes > maximum_total_bytes:
            raise SystemExit("Release expansion exceeds 256 MiB")
        destination = extract_root.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        input_handle = archive.extractfile(member)
        if input_handle is None:
            raise SystemExit(f"Cannot read release member: {canonical}")
        with input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
        destination.chmod(mode)
        regular_files.add(canonical)

required = {
    "distillfeed/pyproject.toml", "distillfeed/rss_reader/web.py",
    "distillfeed/rss_reader/static/app.js", "distillfeed/update-github.sh",
    "distillfeed/upd.sh", "distillfeed/test-distillfeed.sh",
}
missing = required - regular_files
if missing:
    raise SystemExit("Release is incomplete; missing: " + ", ".join(sorted(missing)))
metadata = tomllib.loads((extract_root / "distillfeed" / "pyproject.toml").read_text("utf-8"))
actual = str(metadata.get("project", {}).get("version", ""))
if actual != version:
    raise SystemExit(f"Release version mismatch: expected {version}, found {actual}")
PY

printf 'Applying the validated code while preserving repository-only files...\n'
python3 - "$STAGE/extracted/distillfeed" "$REPOSITORY" "${PROTECTED[@]}" <<'PY'
from __future__ import annotations
import shutil
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
protected = set(sys.argv[3:])
for path in sorted(source.rglob("*")):
    relative = path.relative_to(source)
    if not relative.parts or relative.parts[0] in protected:
        continue
    target = destination / relative
    for parent in (destination, *target.parents):
        if parent == destination.parent:
            break
        if parent.is_symlink():
            raise SystemExit(f"Refusing repository symlink in destination path: {parent}")
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        if target.exists() and not target.is_dir():
            raise SystemExit(f"Repository path conflicts with release directory: {relative}")
        target.mkdir(parents=True, exist_ok=True)
        continue
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"Unexpected release path type: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_dir():
        raise SystemExit(f"Repository directory conflicts with release file: {relative}")
    shutil.copy2(path, target)
PY

CURRENT_PROTECTED_STATE="$(python3 - "$REPOSITORY" "${PROTECTED[@]}" <<'PY'
from __future__ import annotations
import hashlib
import json
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
state = {}
for name in sys.argv[2:]:
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        state[name] = {"kind": "absent"}
        continue
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"Protected repository path changed type: {name}")
    state[name] = {
        "kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": stat.S_IMODE(metadata.st_mode),
    }
print(json.dumps(state, sort_keys=True, separators=(",", ":")))
PY
)" || fail "could not verify protected repository files"
[[ "$CURRENT_PROTECTED_STATE" == "$PROTECTED_STATE" ]] \
    || fail "README.md, CITATION.cff, or UPDATE-GITHUB.md changed unexpectedly"

git -C "$REPOSITORY" diff --check
git -C "$REPOSITORY" add -A
git -C "$REPOSITORY" diff --cached --check
git -C "$REPOSITORY" diff --cached --quiet -- README.md CITATION.cff UPDATE-GITHUB.md \
    || fail "a protected repository file was staged unexpectedly"

if git -C "$REPOSITORY" diff --cached --quiet; then
    printf 'Repository is already at DistillFeed %s; no commit is needed.\n' "$EXPECTED_VERSION"
    exit 0
fi

printf '\nStaged DistillFeed %s update:\n' "$EXPECTED_VERSION"
git -C "$REPOSITORY" diff --cached --stat
if [[ "$PUSH" -eq 0 ]]; then
    printf '\nReview with:\n  git -C %q diff --cached\n' "$REPOSITORY"
    printf 'Then publish with:\n  git -C %q commit -m %q\n  git -C %q push origin %q\n' \
        "$REPOSITORY" "$MESSAGE" "$REPOSITORY" "$BRANCH"
    exit 0
fi

git -C "$REPOSITORY" commit -m "$MESSAGE"
git -C "$REPOSITORY" push origin "$BRANCH"
printf 'Published DistillFeed %s to origin/%s.\n' "$EXPECTED_VERSION" "$BRANCH"
