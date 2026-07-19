from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests


def safe_external_url(url: str | None) -> str | None:
    """Return a clickable HTTP(S) URL, rejecting active or malformed schemes."""
    value = str(url or "").strip()
    if not value or len(value) > 8192 or any(character in value for character in "\r\n\x00"):
        return None
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return value


def validate_http_url(url: str, allow_private: bool = False) -> None:
    if len(url) > 4096:
        raise ValueError("URL exceeds the 4096-character limit")
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP and HTTPS URLs are accepted")
    if parsed.username or parsed.password:
        raise ValueError("Credentials embedded in URLs are not accepted")
    if allow_private:
        return
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, and reserved feed addresses are disabled")


def read_limited_response(response, maximum_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > maximum_bytes:
        raise ValueError(f"Response exceeds {maximum_bytes} bytes")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        size += len(chunk)
        if size > maximum_bytes:
            raise ValueError(f"Response exceeds {maximum_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def safe_get(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    allow_private: bool,
    maximum_redirects: int = 5,
):
    """GET a URL while validating every redirect before connecting to it."""
    current = url
    for redirect in range(maximum_redirects + 1):
        validate_http_url(current, allow_private)
        response = requests.get(current, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ValueError("Redirect response has no Location header")
        current = urljoin(current, location)
    raise ValueError(f"Too many redirects (maximum {maximum_redirects})")
