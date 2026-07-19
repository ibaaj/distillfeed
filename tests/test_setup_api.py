from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from rss_reader.secret_store import load_secret_store
from rss_reader.setup_service import (
    SECRET_RELATIVE_PATH,
    SetupCommitError,
    SetupCommitter,
    SetupRecoveryRequired,
    SetupSession,
    preset_payload,
    verify_instance,
)
from rss_reader.setup_state import SetupEvent, SetupPhase
from rss_reader.setup_web import (
    CSRF_HEADER,
    MAX_SETUP_BODY_BYTES,
    create_setup_app,
)


HOST = "127.0.0.1:48123"
ORIGIN = f"http://{HOST}"
CAPABILITY = "private-capability-that-never-enters-the-query-string"


@pytest.fixture
def setup_api_factory(tmp_path, monkeypatch):
    monkeypatch.setattr("rss_reader.setup_service._port_available", lambda _port: True)
    count = 0

    def make(
        *,
        committer=None,
        profile="recommended",
        environment=None,
        idle_timeout_seconds=30 * 60,
        absolute_timeout_seconds=60 * 60,
    ):
        nonlocal count
        count += 1
        state_root = tmp_path / f"setup-{count}"
        committer = committer or SetupCommitter(state_root)
        session = SetupSession(committer)
        app, controller = create_setup_app(
            session,
            capability=CAPABILITY,
            expected_host=HOST,
            expected_origin=ORIGIN,
            profile=profile,
            environment={} if environment is None else environment,
            idle_timeout_seconds=idle_timeout_seconds,
            absolute_timeout_seconds=absolute_timeout_seconds,
        )
        return SimpleNamespace(
            app=app,
            client=app.test_client(),
            controller=controller,
            session=session,
            committer=committer,
            state_root=state_root,
            csrf=None,
        )

    return make


def _request(client, method: str, path: str, **kwargs):
    kwargs.setdefault("base_url", ORIGIN)
    return client.open(path, method=method, **kwargs)


def _bootstrap(harness, *, capability=CAPABILITY, client=None, origin=ORIGIN):
    client = client or harness.client
    response = _request(
        client,
        "POST",
        "/api/bootstrap",
        json={"capability": capability},
        headers={"Origin": origin},
    )
    if response.status_code == 200:
        harness.csrf = response.get_json()["csrf_token"]
    return response


def _mutation(
    harness,
    path: str,
    body,
    *,
    client=None,
    csrf=None,
    origin=ORIGIN,
    content_type=None,
):
    client = client or harness.client
    headers = {"Origin": origin, CSRF_HEADER: csrf if csrf is not None else harness.csrf}
    if content_type is None:
        return _request(client, "POST", path, json=body, headers=headers)
    return _request(
        client,
        "POST",
        path,
        data=body,
        content_type=content_type,
        headers=headers,
    )


def _state(harness, *, client=None):
    return _request(client or harness.client, "GET", "/api/state")


def _settings(profile="recommended", **changes):
    result = preset_payload(profile)
    result.update(changes)
    if result["ai_provider"] == "openai" and not (
        result["openai_key"] or result["use_environment_openai_key"]
    ):
        result["openai_key"] = "test-key-never-used"
    return result


def _body_and_headers(response) -> str:
    return response.get_data(as_text=True) + "\n" + "\n".join(
        f"{name}: {value}" for name, value in response.headers.items()
    )


def _assert_security_headers(response):
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "geolocation=()" in response.headers["Permissions-Policy"]
    assert "Access-Control-Allow-Origin" not in response.headers


def test_page_is_generic_and_every_response_has_private_security_headers(setup_api_factory):
    harness = setup_api_factory(environment={"OPENAI_API_KEY": "environment-secret"})
    page = _request(harness.client, "GET", "/")
    assert page.status_code == 200
    assert b"Set up DistillFeed" in page.data
    assert CAPABILITY.encode() not in page.data
    assert b"environment-secret" not in page.data
    _assert_security_headers(page)

    stylesheet = _request(harness.client, "GET", "/static/setup.css")
    assert stylesheet.status_code == 200
    _assert_security_headers(stylesheet)

    script = _request(harness.client, "GET", "/static/setup.js")
    assert script.status_code == 200
    assert b"window.cancelAnimationFrame(scheduledScreenFocus)" in script.data
    assert b'showScreen("wizard", {focusHeading: false})' in script.data
    assert b"if (!target.hidden) heading.focus" in script.data
    assert b'form.toggleAttribute("inert", busy)' in script.data
    assert b"async function reviewSettings() {\n    if (requestInProgress) return;" in script.data
    _assert_security_headers(script)

    unauthorized = _state(harness)
    assert unauthorized.status_code == 401
    _assert_security_headers(unauthorized)

    wrong_host = harness.client.get("/", base_url="http://attacker.example")
    assert wrong_host.status_code == 400
    _assert_security_headers(wrong_host)


@pytest.mark.parametrize(
    "bad_base_url",
    [
        "http://localhost:48123",
        "http://127.0.0.1:48124",
        "http://127.0.0.1",
        "http://attacker.example",
    ],
)
def test_host_must_match_the_bound_loopback_endpoint_exactly(setup_api_factory, bad_base_url):
    harness = setup_api_factory()
    response = harness.client.post(
        "/api/bootstrap",
        base_url=bad_base_url,
        json={"capability": CAPABILITY},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 400
    assert harness.session.phase is SetupPhase.LISTENING
    assert harness.controller.capability == CAPABILITY
    assert CAPABILITY not in _body_and_headers(response)


@pytest.mark.parametrize(
    "bad_origin",
    [None, "null", "https://127.0.0.1:48123", "http://localhost:48123", "http://attacker.example"],
)
def test_origin_must_match_exactly_and_failure_does_not_consume_capability(
    setup_api_factory, bad_origin,
):
    harness = setup_api_factory()
    headers = {} if bad_origin is None else {"Origin": bad_origin}
    response = _request(
        harness.client,
        "POST",
        "/api/bootstrap",
        json={"capability": CAPABILITY},
        headers=headers,
    )
    assert response.status_code == 403
    assert harness.session.phase is SetupPhase.LISTENING
    assert harness.controller.capability == CAPABILITY


def test_bad_missing_and_replayed_capabilities_have_one_way_semantics(setup_api_factory):
    harness = setup_api_factory()

    bad = _bootstrap(harness, capability="wrong-private-link")
    assert bad.status_code == 409
    assert harness.session.phase is SetupPhase.LISTENING
    assert harness.controller.capability == CAPABILITY
    assert "wrong-private-link" not in _body_and_headers(bad)

    malformed = _request(
        harness.client,
        "POST",
        "/api/bootstrap",
        json={"capability": CAPABILITY, "extra": True},
        headers={"Origin": ORIGIN},
    )
    assert malformed.status_code == 400
    assert harness.controller.capability == CAPABILITY

    accepted = _bootstrap(harness)
    assert accepted.status_code == 200
    assert accepted.get_json()["state"]["phase"] == "editing"
    assert CAPABILITY not in _body_and_headers(accepted)
    cookie = accepted.headers["Set-Cookie"]
    assert harness.controller.cookie_name in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert harness.controller.capability is None

    replay = _bootstrap(harness)
    assert replay.status_code == 409
    assert "already used" in replay.get_json()["message"]
    assert harness.session.phase is SetupPhase.EDITING


def test_concurrent_capability_exchange_authorizes_exactly_one_browser(setup_api_factory):
    harness = setup_api_factory()
    clients = (harness.app.test_client(), harness.app.test_client())

    def exchange(client):
        return _request(
            client,
            "POST",
            "/api/bootstrap",
            json={"capability": CAPABILITY},
            headers={"Origin": ORIGIN},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result(timeout=5) for future in [
            pool.submit(exchange, clients[0]),
            pool.submit(exchange, clients[1]),
        ]]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert harness.session.phase is SetupPhase.EDITING
    assert harness.session.history == [(SetupEvent.BOOTSTRAP.value, SetupPhase.EDITING.value)]
    assert harness.controller.capability is None


def test_api_requires_randomized_cookie_but_page_and_static_assets_do_not(setup_api_factory):
    harness = setup_api_factory()
    assert _request(harness.client, "GET", "/").status_code == 200
    assert _request(harness.client, "GET", "/static/setup.js").status_code == 200

    unauthenticated = harness.app.test_client()
    for method, path, kwargs in (
        ("GET", "/api/state", {}),
        ("GET", "/api/preset/recommended", {}),
        (
            "POST",
            "/api/validate",
            {"json": {"settings": _settings()}, "headers": {"Origin": ORIGIN, CSRF_HEADER: "x"}},
        ),
    ):
        response = _request(unauthenticated, method, path, **kwargs)
        assert response.status_code == 401

    assert _bootstrap(harness).status_code == 200
    assert _state(harness).status_code == 200
    wrong_cookie = harness.app.test_client()
    wrong_cookie.set_cookie(
        harness.controller.cookie_name,
        "wrong-browser-session",
        domain="127.0.0.1",
    )
    assert _request(wrong_cookie, "GET", "/api/state").status_code == 401


def test_reload_state_returns_csrf_but_blanks_every_secret_field(setup_api_factory):
    openai_secret = "sk-RELOAD-SECRET-123456"
    ntfy_secret = "ntfy-reload-private-value"
    harness = setup_api_factory()
    boot = _bootstrap(harness)
    assert boot.status_code == 200
    assert boot.get_json()["csrf_token"] == harness.csrf

    settings = _settings(
        "demo",
        openai_key=openai_secret,
        auto_summarize=True,
        ntfy_enabled=True,
        ntfy_topic="reload-private",
        ntfy_token=ntfy_secret,
    )
    reviewed = _mutation(harness, "/api/validate", {"settings": settings})
    assert reviewed.status_code == 200
    assert reviewed.get_json()["state"]["settings"]["openai_key"] == ""
    assert reviewed.get_json()["state"]["settings"]["ntfy_token"] == ""
    assert openai_secret not in _body_and_headers(reviewed)
    assert ntfy_secret not in _body_and_headers(reviewed)

    reload_response = _state(harness)
    state = reload_response.get_json()
    assert state["csrf_token"] == harness.csrf
    assert state["state"]["phase"] == "reviewed"
    assert state["state"]["settings"]["openai_key"] == ""
    assert state["state"]["settings"]["ntfy_token"] == ""
    assert openai_secret not in _body_and_headers(reload_response)
    assert ntfy_secret not in _body_and_headers(reload_response)

    edited = _mutation(harness, "/api/edit", {})
    assert edited.status_code == 200
    assert edited.get_json()["state"]["phase"] == "editing"
    assert edited.get_json()["state"]["settings"]["openai_key"] == ""
    assert edited.get_json()["state"]["settings"]["ntfy_token"] == ""


def test_csrf_and_origin_fail_closed_before_any_state_transition(setup_api_factory):
    harness = setup_api_factory()
    _bootstrap(harness)
    assert harness.session.phase is SetupPhase.EDITING

    for csrf, origin in (
        ("", ORIGIN),
        ("wrong-csrf", ORIGIN),
        (harness.csrf, "http://attacker.example"),
        (harness.csrf, "null"),
    ):
        response = _mutation(
            harness,
            "/api/validate",
            {"settings": _settings()},
            csrf=csrf,
            origin=origin,
        )
        assert response.status_code == 403
        assert harness.session.phase is SetupPhase.EDITING
        assert harness.session.history == [("bootstrap", "editing")]

    accepted = _mutation(harness, "/api/validate", {"settings": _settings()})
    assert accepted.status_code == 200
    assert harness.session.phase is SetupPhase.REVIEWED


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "application/merge-patch+json"])
def test_mutations_accept_only_json_mime_without_changing_state(
    setup_api_factory, content_type,
):
    harness = setup_api_factory()
    _bootstrap(harness)
    response = _mutation(
        harness,
        "/api/validate",
        json.dumps({"settings": _settings()}).encode(),
        content_type=content_type,
    )
    assert response.status_code == 415
    assert harness.session.phase is SetupPhase.EDITING


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"", 422),
        (b"null", 422),
        (b"[]", 422),
        (b"not-json", 422),
    ],
)
def test_invalid_json_shapes_are_field_errors_not_server_errors(
    setup_api_factory, body, status,
):
    harness = setup_api_factory()
    _bootstrap(harness)
    response = _mutation(
        harness,
        "/api/validate",
        body,
        content_type="application/json",
    )
    assert response.status_code == status
    assert harness.session.phase is SetupPhase.EDITING


def test_json_body_limit_is_exactly_64_kib_with_or_without_valid_json(setup_api_factory):
    harness = setup_api_factory()
    _bootstrap(harness)
    exact = b"[" + b" " * (MAX_SETUP_BODY_BYTES - 2) + b"]"
    assert len(exact) == MAX_SETUP_BODY_BYTES
    exact_response = _mutation(
        harness,
        "/api/validate",
        exact,
        content_type="application/json",
    )
    assert exact_response.status_code == 422

    oversized = b"[" + b" " * (MAX_SETUP_BODY_BYTES - 1) + b"]"
    assert len(oversized) == MAX_SETUP_BODY_BYTES + 1
    large_response = _mutation(
        harness,
        "/api/validate",
        oversized,
        content_type="application/json",
    )
    assert large_response.status_code == 413
    assert "64 KiB" in large_response.get_json()["message"]
    assert harness.session.phase is SetupPhase.EDITING

    exact_unknown_length = _request(
        harness.client,
        "POST",
        "/api/validate",
        data=exact,
        content_type="application/json",
        headers={"Origin": ORIGIN, CSRF_HEADER: harness.csrf},
        environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
    )
    assert exact_unknown_length.status_code == 422

    unknown_length = _request(
        harness.client,
        "POST",
        "/api/validate",
        data=oversized,
        content_type="application/json",
        headers={"Origin": ORIGIN, CSRF_HEADER: harness.csrf},
        environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
    )
    assert unknown_length.status_code == 413
    assert harness.session.phase is SetupPhase.EDITING


def test_unknown_wrapper_and_setup_fields_are_rejected_without_mutation(setup_api_factory):
    harness = setup_api_factory()
    _bootstrap(harness)
    wrapper = _mutation(
        harness,
        "/api/validate",
        {"settings": _settings(), "unexpected": True},
    )
    assert wrapper.status_code == 422
    assert harness.session.phase is SetupPhase.EDITING

    settings = _settings() | {"run_shell_after_setup": "yes"}
    unknown = _mutation(harness, "/api/validate", {"settings": settings})
    assert unknown.status_code == 422
    assert "Unknown setup field" in unknown.get_json()["errors"]["form"]
    assert harness.session.phase is SetupPhase.EDITING

    cancellation = _mutation(harness, "/api/cancel", {"unexpected": True})
    assert cancellation.status_code == 400
    assert harness.session.phase is SetupPhase.EDITING


def test_wrong_and_stale_completion_tokens_never_publish_or_change_review(setup_api_factory):
    harness = setup_api_factory()
    _bootstrap(harness)
    first_review = _mutation(harness, "/api/validate", {"settings": _settings()})
    first_token = first_review.get_json()["review_token"]

    malformed = _mutation(harness, "/api/complete", {"review_token": 123})
    assert malformed.status_code == 400
    assert harness.session.phase is SetupPhase.REVIEWED
    assert not harness.committer.instance.exists()

    wrong = _mutation(harness, "/api/complete", {"review_token": "wrong-token"})
    assert wrong.status_code == 409
    assert harness.session.phase is SetupPhase.REVIEWED
    assert harness.session.review_token == first_token
    assert not harness.committer.instance.exists()

    second_review = _mutation(harness, "/api/validate", {"settings": _settings(port=8082)})
    second_token = second_review.get_json()["review_token"]
    assert second_token != first_token
    stale = _mutation(harness, "/api/complete", {"review_token": first_token})
    assert stale.status_code == 409
    assert harness.session.phase is SetupPhase.REVIEWED
    assert not harness.committer.instance.exists()

    completed = _mutation(harness, "/api/complete", {"review_token": second_token})
    assert completed.status_code == 200
    assert harness.session.phase is SetupPhase.COMPLETE
    verify_instance(harness.committer.instance)


def test_real_commit_fault_returns_failed_then_revalidates_and_retries_cleanly(
    setup_api_factory, tmp_path,
):
    should_fail = True

    def fault(step):
        nonlocal should_fail
        if should_fail and step == "stage-verified":
            should_fail = False
            raise SetupCommitError("injected transient storage fault")

    committer = SetupCommitter(tmp_path / "fault-state", fault=fault)
    harness = setup_api_factory(committer=committer)
    _bootstrap(harness)
    settings = _settings(subscriptions="empty")
    reviewed = _mutation(harness, "/api/validate", {"settings": settings})
    first_token = reviewed.get_json()["review_token"]

    failed = _mutation(harness, "/api/complete", {"review_token": first_token})
    assert failed.status_code == 500
    assert "did not change the active reader" in failed.get_json()["message"]
    assert harness.session.phase is SetupPhase.FAILED
    assert not committer.instance.exists()
    assert list(committer.state_root.glob(".setup-stage-*")) == []
    assert harness.controller.terminal.is_set() is False
    failed_state = _state(harness).get_json()["state"]
    assert failed_state["phase"] == "failed"
    assert failed_state["settings"]["openai_key"] == ""

    retried = _mutation(harness, "/api/validate", {"settings": settings})
    second_token = retried.get_json()["review_token"]
    assert second_token != first_token
    assert harness.session.phase is SetupPhase.REVIEWED
    completed = _mutation(harness, "/api/complete", {"review_token": second_token})
    assert completed.status_code == 200
    assert harness.session.phase is SetupPhase.COMPLETE
    assert harness.controller.terminal.is_set()
    verify_instance(committer.instance)
    assert (SetupEvent.RETRY.value, SetupPhase.EDITING.value) in harness.session.history


class _UnprovableRollbackCommitter:
    def __init__(self, root: Path):
        self.state_root = root
        self.instance = root / "instance"
        self.calls = 0

    def commit(self, _draft):
        self.calls += 1
        self.instance.mkdir(parents=True, exist_ok=True)
        (self.instance / "uncertain-state").write_text("preserve", encoding="utf-8")
        raise SetupRecoveryRequired(
            "Setup could not confirm a safe rollback. A managed instance or staging "
            f"files may remain in {self.state_root}; preserve that folder and relaunch."
        )


def test_unprovable_rollback_stops_on_a_distinct_terminal_without_offering_retry(
    setup_api_factory, tmp_path,
):
    committer = _UnprovableRollbackCommitter(tmp_path / "recovery-state")
    harness = setup_api_factory(committer=committer)
    _bootstrap(harness)
    reviewed = _mutation(
        harness,
        "/api/validate",
        {"settings": _settings(subscriptions="empty")},
    )

    response = _mutation(
        harness,
        "/api/complete",
        {"review_token": reviewed.get_json()["review_token"]},
    )

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["state"]["phase"] == "recovery_required"
    assert payload["state"]["recovery_required"] is True
    assert payload["state"]["recovery_path"] == str(committer.state_root)
    assert "could not confirm a safe rollback" in payload["message"]
    assert "did not change the active reader" not in payload["message"]
    assert "settings" not in payload["state"]
    assert "review_token" not in payload["state"]
    assert harness.session.review_token is None
    assert harness.session.draft is None
    assert harness.controller.review_token is None
    assert harness.controller.review is None
    assert harness.controller.editable_settings is None
    assert harness.controller.terminal.is_set()
    assert harness.session.phase is SetupPhase.RECOVERY_REQUIRED
    assert committer.calls == 1
    assert (committer.instance / "uncertain-state").read_text() == "preserve"

    # Even if a test client reaches the app before the real server shuts down,
    # no browser mutation can turn ambiguous durable state into a second apply.
    for path, body in (
        ("/api/validate", {"settings": _settings(subscriptions="empty")}),
        ("/api/edit", {}),
        ("/api/complete", {"review_token": reviewed.get_json()["review_token"]}),
        ("/api/cancel", {}),
    ):
        assert _mutation(harness, path, body).status_code == 409
    assert committer.calls == 1

    page = _request(harness.client, "GET", "/").get_data(as_text=True)
    script = _request(harness.client, "GET", "/static/setup.js").get_data(as_text=True)
    assert 'data-screen="recovery"' in page
    assert "Run <code>./launch.sh</code> again" in page
    assert 'state.phase === "recovery_required"' in script


class _CountingCommitter(SetupCommitter):
    def __init__(self, state_root):
        super().__init__(state_root)
        self.calls = 0
        self.counter_lock = threading.Lock()

    def commit(self, draft):
        with self.counter_lock:
            self.calls += 1
        return super().commit(draft)


def _authenticated_client(harness):
    client = harness.app.test_client()
    client.set_cookie(
        harness.controller.cookie_name,
        harness.controller.browser_session,
        domain="127.0.0.1",
    )
    return client


def test_concurrent_duplicate_complete_is_one_commit_and_two_idempotent_successes(
    setup_api_factory, tmp_path,
):
    committer = _CountingCommitter(tmp_path / "concurrent-state")
    harness = setup_api_factory(committer=committer)
    _bootstrap(harness)
    reviewed = _mutation(
        harness,
        "/api/validate",
        {"settings": _settings(subscriptions="empty")},
    )
    token = reviewed.get_json()["review_token"]
    clients = (_authenticated_client(harness), _authenticated_client(harness))

    def complete(client):
        return _mutation(
            harness,
            "/api/complete",
            {"review_token": token},
            client=client,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete, client) for client in clients]
        responses = [future.result(timeout=15) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].get_json()["result"] == responses[1].get_json()["result"]
    assert committer.calls == 1
    assert harness.session.phase is SetupPhase.COMPLETE
    assert harness.session.history[-1] == (SetupEvent.REPLAY.value, SetupPhase.COMPLETE.value)
    verify_instance(committer.instance)


def test_cancel_is_terminal_idempotent_at_state_level_and_never_creates_reader(setup_api_factory):
    harness = setup_api_factory()
    _bootstrap(harness)
    invalid = _mutation(harness, "/api/cancel", {"unknown": True})
    assert invalid.status_code == 400
    assert harness.session.phase is SetupPhase.EDITING

    cancelled = _mutation(harness, "/api/cancel", {})
    assert cancelled.status_code == 200
    assert cancelled.get_json()["state"]["phase"] == "cancelled"
    assert harness.controller.terminal.is_set()
    assert not harness.committer.instance.exists()
    assert _state(harness).get_json()["state"]["phase"] == "cancelled"

    for path, body in (
        ("/api/cancel", {}),
        ("/api/validate", {"settings": _settings()}),
        ("/api/edit", {}),
        ("/api/complete", {"review_token": "unused"}),
    ):
        response = _mutation(harness, path, body)
        assert response.status_code == 409
        assert harness.session.phase is SetupPhase.CANCELLED
        assert not harness.committer.instance.exists()


def test_timeout_is_terminal_and_cannot_be_revived_by_late_browser_actions(setup_api_factory):
    harness = setup_api_factory(idle_timeout_seconds=-1)
    _bootstrap(harness)
    assert harness.controller.expire_if_due() is True
    assert harness.session.phase is SetupPhase.TIMED_OUT
    assert harness.controller.terminal.is_set()
    assert harness.controller.expire_if_due() is False
    assert _state(harness).get_json()["state"]["phase"] == "timed_out"
    assert _mutation(harness, "/api/cancel", {}).status_code == 409
    assert _mutation(
        harness, "/api/validate", {"settings": _settings()}
    ).status_code == 409
    assert not harness.committer.instance.exists()


def test_environment_and_entered_secrets_never_appear_in_any_success_or_error_response(
    setup_api_factory,
):
    environment_secret = "sk-ENVIRONMENT-PRIVATE-123456"
    entered_but_ignored = "sk-ENTERED-IGNORED-987654"
    ntfy_secret = "NTFY_PRIVATE_SENTINEL_f04a718b"
    harness = setup_api_factory(environment={"OPENAI_API_KEY": environment_secret})
    responses = [_request(harness.client, "GET", "/"), _bootstrap(harness)]
    assert responses[-1].get_json()["environment_openai_key_available"] is True

    settings = _settings(
        "demo",
        use_environment_openai_key=True,
        openai_key=entered_but_ignored,
        auto_summarize=True,
        ntfy_enabled=True,
        ntfy_topic="private-topic",
        ntfy_token=ntfy_secret,
    )
    invalid_settings = dict(settings)
    invalid_settings["port"] = 1
    invalid = _mutation(harness, "/api/validate", {"settings": invalid_settings})
    assert invalid.status_code == 422
    responses.append(invalid)
    assert harness.session.phase is SetupPhase.EDITING
    reviewed = _mutation(harness, "/api/validate", {"settings": settings})
    responses.extend([reviewed, _state(harness)])
    token = reviewed.get_json()["review_token"]
    responses.append(_mutation(harness, "/api/complete", {"review_token": "wrong"}))
    completed = _mutation(harness, "/api/complete", {"review_token": token})
    responses.extend([completed, _state(harness)])
    replayed = _mutation(harness, "/api/complete", {"review_token": token})
    responses.append(replayed)

    for response in responses:
        body = _body_and_headers(response)
        assert environment_secret not in body
        assert entered_but_ignored not in body
        assert ntfy_secret not in body

    stored = load_secret_store(harness.committer.instance / SECRET_RELATIVE_PATH)
    assert stored == {
        "OPENAI_API_KEY": environment_secret,
        "NTFY_TOKEN": ntfy_secret,
    }


class _SecretEchoingFailureCommitter:
    def __init__(self, root: Path):
        self.instance = root / "instance"

    def commit(self, draft):
        raise SetupCommitError(
            f"provider rejected {draft.openai_key}; ntfy rejected {draft.ntfy_token}"
        )


def test_commit_exception_and_failed_state_redact_exact_draft_secrets(setup_api_factory, tmp_path):
    openai_secret = "sk-FAULT-PRIVATE-123456"
    ntfy_secret = "NTFY_FAULT_PRIVATE_97c3a43f"
    harness = setup_api_factory(
        committer=_SecretEchoingFailureCommitter(tmp_path / "echo-fault")
    )
    responses = [_bootstrap(harness)]
    settings = _settings(
        "demo",
        openai_key=openai_secret,
        auto_summarize=True,
        ntfy_enabled=True,
        ntfy_topic="fault-topic",
        ntfy_token=ntfy_secret,
    )
    reviewed = _mutation(harness, "/api/validate", {"settings": settings})
    responses.append(reviewed)
    failed = _mutation(
        harness,
        "/api/complete",
        {"review_token": reviewed.get_json()["review_token"]},
    )
    responses.extend([failed, _state(harness)])
    assert failed.status_code == 500
    assert harness.session.phase is SetupPhase.FAILED
    for response in responses:
        body = _body_and_headers(response)
        assert openai_secret not in body
        assert ntfy_secret not in body
