import socket

import pytest

from rss_reader.net import read_limited_response, safe_external_url, safe_get, validate_http_url


class Response:
    def __init__(self, status, *, location=None, content=b""):
        self.status_code = status
        self.headers = {} if location is None else {"Location": location}
        self.content = content
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=65536):
        yield self.content


def test_url_validation_rejects_credentials_and_private_destinations(monkeypatch):
    with pytest.raises(ValueError, match="Credentials"):
        validate_http_url("https://name:secret@example.test/feed")
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="Private"):
        validate_http_url("https://example.test/feed")
    with pytest.raises(ValueError, match="4096"):
        validate_http_url("https://example.test/" + "x" * 4096)


def test_every_redirect_is_validated_before_the_next_request(monkeypatch):
    checked = []
    responses = iter([
        Response(302, location="https://second.test/feed"),
        Response(200, content=b"ok"),
    ])
    monkeypatch.setattr("rss_reader.net.validate_http_url", lambda url, allow: checked.append(url))
    monkeypatch.setattr("rss_reader.net.requests.get", lambda *args, **kwargs: next(responses))
    response = safe_get(
        "https://first.test/feed", headers={"User-Agent": "test"}, timeout=5,
        allow_private=False,
    )
    assert response.status_code == 200
    assert checked == ["https://first.test/feed", "https://second.test/feed"]


def test_response_limit_rejects_declared_and_streamed_oversize():
    declared = Response(200, content=b"x")
    declared.headers["Content-Length"] = "11"
    with pytest.raises(ValueError, match="exceeds"):
        read_limited_response(declared, 10)
    streamed = Response(200, content=b"x" * 11)
    with pytest.raises(ValueError, match="exceeds"):
        read_limited_response(streamed, 10)


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "data:text/html,bad", "file:///etc/passwd", "//example.test/x",
     "https://user:secret@example.test/x", "https://example.test:bad/x", "https://example.test/\nX"],
)
def test_unsafe_external_links_are_never_clickable(value):
    assert safe_external_url(value) is None


def test_http_external_links_remain_clickable():
    assert safe_external_url("https://example.test/article?q=1") == "https://example.test/article?q=1"
