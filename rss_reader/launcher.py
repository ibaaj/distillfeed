from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import shutil
import socket
import stat
import sys
import threading
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from werkzeug.serving import WSGIRequestHandler, make_server

from . import __version__
from .config import Config, load_config
from .db import connect
from .opml import build_tree_from_database, parse_opml_bytes, write_database_opml
from .secret_store import merged_secret_environment
from .setup_service import (
    MANIFEST_NAME,
    SECRET_RELATIVE_PATH,
    SETUP_SCHEMA_VERSION,
    SetupRecoveryRequired,
)
from .web import create_app


LOGGER = logging.getLogger(__name__)
STATE_DIRECTORY = ".distillfeed"
INSTANCE_DIRECTORY = "instance"
STAGE_PREFIX = ".setup-stage-"
STAGE_MARKER = ".distillfeed-stage"
STAGE_MARKER_CONTENT = b"DistillFeed setup staging directory\n"
LOCK_NAME = "launch.lock"


class LaunchError(RuntimeError):
    """A safe, actionable launcher failure suitable for terminal display."""


class TargetKind(str, Enum):
    FIRST_RUN = "first_run"
    MANAGED = "managed"
    LEGACY = "legacy"
    EXTERNAL = "external"


@dataclass(frozen=True)
class LaunchTarget:
    kind: TargetKind
    config_path: Path | None
    instance_path: Path | None = None


def _resolved_config_path(project_root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _ensure_private_state_root(project_root: Path) -> Path:
    state_root = project_root / STATE_DIRECTORY
    try:
        metadata = state_root.lstat()
    except FileNotFoundError:
        state_root.mkdir(mode=0o700)
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise LaunchError(
                f"Refusing unsafe state path: {state_root}. It must be a real directory."
            )
    os.chmod(state_root, 0o700)
    return state_root


@contextmanager
def launcher_lock(project_root: Path) -> Iterator[Path]:
    """Hold one non-blocking per-project lock through setup and serving."""
    state_root = _ensure_private_state_root(project_root)
    lock_path = state_root / LOCK_NAME
    try:
        metadata = lock_path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
    ):
        raise LaunchError(f"Refusing unsafe launcher lock path: {lock_path}")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LaunchError(f"The launcher lock cannot be opened: {lock_path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise LaunchError(
                    "DistillFeed is already starting or running from this folder. "
                    "Use the browser window that is already open, or stop that process first."
                ) from exc
            raise LaunchError("The DistillFeed launcher lock could not be acquired.") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield state_root
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def clean_stale_setup_stages(state_root: Path) -> list[Path]:
    """Remove only direct children carrying DistillFeed's exact stage marker."""
    removed: list[Path] = []
    for candidate in state_root.iterdir():
        if not candidate.name.startswith(STAGE_PREFIX):
            continue
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(candidate_metadata.st_mode) or not stat.S_ISDIR(
            candidate_metadata.st_mode
        ):
            continue
        marker = candidate / STAGE_MARKER
        try:
            marker_metadata = marker.lstat()
            marked = (
                stat.S_ISREG(marker_metadata.st_mode)
                and not stat.S_ISLNK(marker_metadata.st_mode)
                and marker_metadata.st_size == len(STAGE_MARKER_CONTENT)
                and marker.read_bytes() == STAGE_MARKER_CONTENT
            )
        except OSError:
            marked = False
        if not marked:
            continue
        shutil.rmtree(candidate)
        removed.append(candidate)
    return removed


def classify_target(
    project_root: Path, *, environ: Mapping[str, str] | None = None
) -> LaunchTarget:
    """Choose one existing configuration without creating or overwriting it."""
    source = os.environ if environ is None else environ
    explicit = source.get("RSSREADER_CONFIG")
    if explicit is not None and explicit.strip():
        config_path = _resolved_config_path(project_root, explicit)
        if not config_path.is_file():
            raise LaunchError(
                "RSSREADER_CONFIG points to a configuration that does not exist: "
                f"{config_path}"
            )
        return LaunchTarget(TargetKind.EXTERNAL, config_path)

    legacy_config = project_root / "config.toml"
    instance = project_root / STATE_DIRECTORY / INSTANCE_DIRECTORY
    legacy_exists = legacy_config.exists() or legacy_config.is_symlink()
    instance_exists = instance.exists() or instance.is_symlink()
    if legacy_exists and instance_exists:
        raise LaunchError(
            "Two DistillFeed configurations were found: config.toml and "
            f"{STATE_DIRECTORY}/{INSTANCE_DIRECTORY}/config.toml. No configuration was "
            "chosen or changed. Set RSSREADER_CONFIG to the one you intend to run."
        )
    if instance_exists:
        try:
            metadata = instance.lstat()
        except FileNotFoundError as exc:
            raise LaunchError("The managed instance disappeared while starting.") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise LaunchError(
                f"The managed instance path is unsafe or incomplete: {instance}"
            )
        config_path = instance / "config.toml"
        if config_path.is_symlink() or not config_path.is_file():
            raise LaunchError(
                f"The managed instance is incomplete; its configuration is missing: {config_path}"
            )
        return LaunchTarget(TargetKind.MANAGED, config_path, instance)
    if legacy_exists:
        if not legacy_config.is_file():
            raise LaunchError(f"The existing configuration is not a file: {legacy_config}")
        return LaunchTarget(TargetKind.LEGACY, legacy_config.resolve())
    return LaunchTarget(TargetKind.FIRST_RUN, None)


def verify_managed_instance(instance: Path) -> Config:
    """Verify durable runtime invariants without rejecting legitimate later use."""
    if instance.is_symlink() or not instance.is_dir():
        raise LaunchError("The managed DistillFeed instance is missing or unsafe.")
    manifest_path = instance / MANIFEST_NAME
    config_path = instance / "config.toml"
    for path, label in ((manifest_path, "completion manifest"), (config_path, "configuration")):
        if path.is_symlink() or not path.is_file():
            raise LaunchError(f"The managed {label} is missing or unsafe: {path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchError("The managed setup completion manifest is unreadable.") from exc
    if not isinstance(manifest, dict) or manifest.get("state") != "ready":
        raise LaunchError("The managed setup did not reach its durable ready state.")
    if manifest.get("schema") != SETUP_SCHEMA_VERSION:
        raise LaunchError(
            "This managed setup uses an unsupported setup schema. Keep the instance intact "
            "and use the matching DistillFeed release to migrate it."
        )
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise LaunchError("The managed setup manifest has no release version.")
    try:
        uuid.UUID(str(manifest.get("installation_id", "")))
    except (ValueError, AttributeError) as exc:
        raise LaunchError("The managed setup manifest has an invalid installation ID.") from exc

    try:
        config = load_config(config_path)
    except Exception as exc:
        raise LaunchError(f"The managed configuration is invalid: {exc}") from exc
    try:
        config.database_path.relative_to(instance)
        config.working_opml_path.relative_to(instance)
    except ValueError as exc:
        raise LaunchError(
            "Managed database and OPML paths must remain inside the managed instance."
        ) from exc
    if config.database_path.is_symlink() or not config.database_path.is_file():
        raise LaunchError(
            f"The managed database is missing or unsafe: {config.database_path}"
        )
    if config.working_opml_path.is_symlink():
        raise LaunchError(
            f"The managed subscription OPML is an unsafe symbolic link: {config.working_opml_path}"
        )
    arxiv_config = instance / "arxiv-digest.toml"
    arxiv_config_present = arxiv_config.exists() or arxiv_config.is_symlink()
    if arxiv_config_present and (arxiv_config.is_symlink() or not arxiv_config.is_file()):
        raise LaunchError(
            f"The managed arXiv settings path is unsafe: {arxiv_config}"
        )
    if bool(config.get("plugins", "arxiv_digest_enabled", False)):
        if not arxiv_config_present:
            raise LaunchError(
                "The bundled arXiv workflow is enabled, but its managed settings "
                f"are missing or unsafe: {arxiv_config}"
            )
    try:
        with connect(config.database_path) as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            expected_opml = build_tree_from_database(connection)
            try:
                opml_matches = (
                    parse_opml_bytes(config.working_opml_path.read_bytes())
                    == expected_opml
                )
            except (OSError, ValueError, ParseError, DefusedXmlException):
                opml_matches = False
            if not opml_matches and quick_check == "ok" and not foreign_keys:
                # The database is authoritative. A crash can leave its exported
                # OPML behind without making the reader itself unrecoverable.
                write_database_opml(connection, config.working_opml_path)
                opml_matches = (
                    parse_opml_bytes(config.working_opml_path.read_bytes())
                    == build_tree_from_database(connection)
                )
                LOGGER.warning(
                    "Rebuilt the managed subscription OPML from the verified database."
                )
    except Exception as exc:
        raise LaunchError(f"The managed database could not be verified: {exc}") from exc
    if quick_check != "ok" or foreign_keys:
        raise LaunchError(
            "The managed database integrity check failed. No server was started."
        )
    if not opml_matches:
        raise LaunchError(
            "The managed subscription file and database disagree. No server was started."
        )
    return config


def _apply_managed_secrets(
    instance: Path, *, one_launch: Mapping[str, str] | None = None
) -> None:
    # A managed installation owns its plugin recipe even when the plugin is
    # still disabled. Do not let a leftover expert-shell override make the
    # Settings page read or write an unrelated file.
    managed_arxiv_config = str(instance / "arxiv-digest.toml")
    secret_path = instance / SECRET_RELATIVE_PATH
    private_directory = secret_path.parent
    try:
        private_metadata = private_directory.lstat()
    except FileNotFoundError:
        private_metadata = None
    if private_metadata is not None and (
        stat.S_ISLNK(private_metadata.st_mode)
        or not stat.S_ISDIR(private_metadata.st_mode)
    ):
        raise LaunchError(
            f"The private DistillFeed secret directory is unsafe: {private_directory}"
        )
    try:
        values = merged_secret_environment(secret_path, os.environ)
    except Exception as exc:
        raise LaunchError(f"The private DistillFeed secret store is unsafe or invalid: {exc}") from exc
    if one_launch:
        values.update(one_launch)
    # Apply only after every filesystem/schema check has succeeded, so a
    # rejected secret store cannot leave a half-selected managed environment.
    os.environ["DISTILLFEED_ARXIV_CONFIG"] = managed_arxiv_config
    for name, value in values.items():
        os.environ[name] = value


def _open_browser(url: str) -> None:
    def open_url() -> None:
        try:
            if not webbrowser.open(url, new=2):
                LOGGER.warning("A browser could not be opened automatically. Visit %s", url)
        except Exception:
            LOGGER.warning("A browser could not be opened automatically. Visit %s", url)

    threading.Thread(target=open_url, name="distillfeed-browser", daemon=True).start()


class _LocalRequestHandler(WSGIRequestHandler):
    """Keep a personal-reader terminal quiet unless a request fails."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            failed = int(code) >= 400
        except (TypeError, ValueError):
            failed = True
        if failed:
            super().log_request(code, size)


def _pending_application(environ, start_response):
    del environ
    body = b"DistillFeed is starting.\n"
    start_response(
        "503 Service Unavailable",
        [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
    )
    return [body]


def serve_reader(config_path: Path, *, open_browser: bool = True) -> int:
    try:
        config = load_config(config_path)
        port = int(config.get("app", "port"))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            listener.set_inheritable(False)
            server = make_server(
                "127.0.0.1",
                port,
                _pending_application,
                threaded=True,
                request_handler=_LocalRequestHandler,
                fd=listener.fileno(),
            )
        finally:
            listener.close()
    except OSError as exc:
        raise LaunchError(
            f"Port {locals().get('port', 'configured')} is unavailable on 127.0.0.1. "
            "DistillFeed did not reinstall or change your setup. Close the program using "
            "that port, or choose a different app.port in the configuration."
        ) from exc
    except (SystemExit, Exception) as exc:
        if isinstance(exc, LaunchError):
            raise
        raise LaunchError(f"DistillFeed could not start: {exc}") from exc

    try:
        app = create_app(str(config_path))
        server.app = app
    except Exception as exc:
        server.server_close()
        raise LaunchError(f"DistillFeed could not start: {exc}") from exc

    url = f"http://127.0.0.1:{port}/"
    print(f"DistillFeed {__version__} is ready at {url}", flush=True)
    print("Press Control-C to stop it.", flush=True)
    if open_browser:
        _open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDistillFeed stopped.", flush=True)
    finally:
        server.server_close()
        scheduler = app.extensions.get("distillfeed_scheduler")
        if scheduler is not None:
            scheduler.stop(wait=True)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="launch.sh",
        description="Install, configure, and run a local DistillFeed reader.",
    )
    result.add_argument(
        "--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS
    )
    result.add_argument(
        "--demo",
        action="store_true",
        help="Use the 8081, arXiv, 40/100, seven-day demo preset during first setup.",
    )
    result.add_argument(
        "--no-browser",
        action="store_true",
        help="Print local links instead of opening a browser.",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    os.umask(0o077)
    project_root = args.root.expanduser().resolve()
    if not project_root.is_dir():
        print(f"error: project folder does not exist: {project_root}", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        with launcher_lock(project_root) as state_root:
            clean_stale_setup_stages(state_root)
            target = classify_target(project_root)
            one_launch_secrets: Mapping[str, str] | None = None
            if target.kind == TargetKind.FIRST_RUN:
                from .setup_web import run_setup

                profile = "demo" if args.demo else "recommended"
                result = run_setup(
                    state_root,
                    profile=profile,
                    open_browser=not args.no_browser,
                )
                if result is None:
                    print("Setup ended without changes. Run ./launch.sh when you are ready.")
                    return 0
                one_launch_secrets = result.environment
                target = classify_target(project_root)
                if target.kind != TargetKind.MANAGED:
                    raise LaunchError(
                        "Setup returned without a complete managed instance. No reader was started."
                    )

            assert target.config_path is not None
            if target.kind == TargetKind.MANAGED:
                assert target.instance_path is not None
                config = verify_managed_instance(target.instance_path)
                _apply_managed_secrets(
                    target.instance_path, one_launch=one_launch_secrets
                )
                config_path = config.path
            else:
                config_path = target.config_path
            return serve_reader(config_path, open_browser=not args.no_browser)
    except LaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SetupRecoveryRequired as exc:
        # The setup page has already stopped without retrying ambiguous state.
        # Keep the terminal equally explicit and avoid an internal traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDistillFeed stopped.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
