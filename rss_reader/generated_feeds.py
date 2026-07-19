from __future__ import annotations

import os
import re
import stat
from urllib.parse import urlsplit

from .config import Config


GENERATED_SCHEME = "generated"
GENERATED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.(?:xml|rss|atom)", re.I)


def is_generated_feed_url(value: str) -> bool:
    return str(value).strip().casefold().startswith(f"{GENERATED_SCHEME}://")


def generated_feed_name(value: str) -> str:
    text = str(value).strip()
    if len(text) > 240 or any(character in text for character in "\r\n\x00"):
        raise ValueError("Generated feed reference is invalid")
    parsed = urlsplit(text)
    if (
        parsed.scheme.casefold() != GENERATED_SCHEME
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not GENERATED_NAME.fullmatch(parsed.netloc)
    ):
        raise ValueError(
            "Generated feed references must look like generated://name.xml "
            "and may not contain paths, queries, or fragments"
        )
    return parsed.netloc


def validate_generated_feed_url(config: Config, value: str) -> str:
    name = generated_feed_name(value)
    if config.generated_feed_directory is None:
        raise ValueError(
            "Generated feeds are disabled; a server administrator must configure "
            "feeds.generated_feed_directory first"
        )
    return name


def read_generated_feed(config: Config, value: str, maximum_bytes: int) -> bytes:
    """Read one regular file from the configured drop directory without links.

    The URL contains a basename only. Directory-relative open plus O_NOFOLLOW
    keeps a replaced file or symlink from escaping the administrator-selected
    directory between validation and read.
    """
    name = validate_generated_feed_url(config, value)
    root = config.generated_feed_directory
    assert root is not None
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, directory_flags)
    except FileNotFoundError as exc:
        raise ValueError(f"Generated feed directory does not exist: {root}") from exc
    except OSError as exc:
        raise ValueError(f"Generated feed directory is not a safe directory: {root}") from exc
    try:
        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, file_flags, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            raise ValueError(f"Generated feed file is not available: {name}") from exc
        except OSError as exc:
            raise ValueError(f"Generated feed file is not a safe regular file: {name}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"Generated feed file is not a regular file: {name}")
            if details.st_size > int(maximum_bytes):
                raise ValueError(f"Generated feed exceeds {int(maximum_bytes)} bytes")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, int(maximum_bytes) + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > int(maximum_bytes):
                    raise ValueError(f"Generated feed exceeds {int(maximum_bytes)} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
