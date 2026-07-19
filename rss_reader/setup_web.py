from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.serving import WSGIRequestHandler, make_server

from .setup_service import (
    CommitResult,
    SetupCommitError,
    SetupCommitter,
    SetupRecoveryRequired,
    SetupSession,
    SetupValidationError,
    preset_payload,
)
from .setup_state import SetupPhase, TransitionError


MAX_SETUP_BODY_BYTES = 64 * 1024
SETUP_COOKIE = "distillfeed_setup"
CSRF_HEADER = "X-DistillFeed-Setup-CSRF"
TERMINAL_PHASES = {
    SetupPhase.COMPLETE,
    SetupPhase.RECOVERY_REQUIRED,
    SetupPhase.CANCELLED,
    SetupPhase.TIMED_OUT,
}


class _QuietRequestHandler(WSGIRequestHandler):
    """The private setup server must not put requests or capabilities in logs."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return None

    def log_error(self, format: str, *args: Any) -> None:
        return None

    def log_message(self, format: str, *args: Any) -> None:
        return None


def _same_secret(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _safe_error(value: Any) -> str:
    """Keep operational errors useful without reflecting credential-shaped text."""

    text = " ".join(str(value or "").split())
    text = re.sub(r"\bsk-[A-Za-z0-9_.-]{6,}", "[redacted API key]", text)
    text = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", text, flags=re.IGNORECASE)
    return text[:600] or "Setup could not finish. Review the settings and try again."


@dataclass
class SetupWebController:
    """Security boundary and terminal result for one browser setup session."""

    session: SetupSession
    profile: str
    environment: Mapping[str, str] = field(repr=False)
    capability: str | None = field(repr=False)
    cookie_name: str = field(
        default_factory=lambda: f"{SETUP_COOKIE}_{secrets.token_hex(8)}",
        repr=False,
    )
    expected_host: str = ""
    expected_origin: str = ""
    idle_timeout_seconds: float = 30 * 60
    absolute_timeout_seconds: float = 60 * 60
    browser_session: str | None = field(default=None, repr=False)
    csrf_token: str | None = field(default=None, repr=False)
    review_token: str | None = field(default=None, repr=False)
    review: dict[str, Any] | None = None
    editable_settings: dict[str, Any] | None = field(default=None, repr=False)
    result: CommitResult | None = field(default=None, repr=False)
    started_at: float = field(default_factory=time.monotonic, repr=False)
    last_activity_at: float = field(default_factory=time.monotonic, repr=False)
    terminal: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    mutation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def set_endpoint(self, host: str, port: int) -> None:
        with self._lock:
            self.expected_host = f"{host}:{port}"
            self.expected_origin = f"http://{host}:{port}"

    def touch(self) -> None:
        with self._lock:
            self.last_activity_at = time.monotonic()

    def is_authenticated(self, cookie: str | None) -> bool:
        with self._lock:
            return _same_secret(cookie, self.browser_session)

    def csrf_is_valid(self, token: str | None) -> bool:
        with self._lock:
            return _same_secret(token, self.csrf_token)

    def exchange_capability(self, candidate: str | None) -> tuple[str, str]:
        with self._lock:
            if self.capability is None:
                raise SetupCommitError(
                    "This private setup link was already used. Relaunch DistillFeed to open a new one."
                )
            if not _same_secret(candidate, self.capability):
                raise SetupCommitError("This is not the private setup link for this launch.")
            # Invalidate before advancing state so even an exceptional response cannot replay it.
            self.capability = None
            self.browser_session = secrets.token_urlsafe(32)
            self.csrf_token = secrets.token_urlsafe(32)
            self.session.bootstrap()
            self.last_activity_at = time.monotonic()
            return self.browser_session, self.csrf_token

    def public_state(self) -> dict[str, Any]:
        state = self.session.public_state()
        with self._lock:
            state["profile"] = self.profile
            if self.review is not None and state["phase"] in {
                SetupPhase.REVIEWED.value,
                SetupPhase.FAILED.value,
            }:
                state["review"] = self.review
                state["review_token"] = self.review_token
            if self.editable_settings is not None and state["phase"] in {
                SetupPhase.EDITING.value,
                SetupPhase.REVIEWED.value,
                SetupPhase.FAILED.value,
            }:
                # Secret fields are blanked before this copy reaches the controller.
                state["settings"] = dict(self.editable_settings)
            if self.result is not None and state["phase"] == SetupPhase.COMPLETE.value:
                state["result"] = self.result.public()
            return state

    def mark_terminal(self, result: CommitResult | None = None) -> None:
        with self._lock:
            if result is not None:
                self.result = result
            self.terminal.set()

    def expire_if_due(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if self.terminal.is_set():
                return False
            due = (
                now - self.started_at >= self.absolute_timeout_seconds
                or now - self.last_activity_at >= self.idle_timeout_seconds
            )
            if not due:
                return False
        self.session.timeout()
        self.mark_terminal()
        return True


def create_setup_app(
    session: SetupSession,
    *,
    capability: str,
    expected_host: str = "",
    expected_origin: str = "",
    profile: str = "recommended",
    environment: Mapping[str, str] | None = None,
    idle_timeout_seconds: float = 30 * 60,
    absolute_timeout_seconds: float = 60 * 60,
) -> tuple[Flask, SetupWebController]:
    """Build the loopback-only setup application.

    ``expected_host`` and ``expected_origin`` may be filled after an ephemeral
    port is bound by calling ``controller.set_endpoint``. Tests can pass exact
    values up front and exercise the same checks as the real server.
    """

    # Validate here instead of letting an unknown value select an accidental default.
    preset_payload(profile)
    environment = dict(os.environ if environment is None else environment)
    controller = SetupWebController(
        session=session,
        profile=profile,
        environment=environment,
        capability=capability,
        expected_host=expected_host,
        expected_origin=expected_origin,
        idle_timeout_seconds=idle_timeout_seconds,
        absolute_timeout_seconds=absolute_timeout_seconds,
    )
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        # Permit one sentinel byte at the WSGI layer so json_object can detect
        # an oversized streaming request whose Content-Length is unavailable.
        # The public limit remains exactly MAX_SETUP_BODY_BYTES.
        MAX_CONTENT_LENGTH=MAX_SETUP_BODY_BYTES + 1,
        PROPAGATE_EXCEPTIONS=False,
        TRAP_HTTP_EXCEPTIONS=False,
    )
    app.logger.disabled = True

    def api_error(
        message: str,
        status: int,
        *,
        errors: dict[str, str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> tuple[Response, int]:
        payload: dict[str, Any] = {"ok": False, "message": message}
        if errors:
            payload["errors"] = errors
        if state is not None:
            payload["state"] = state
        return jsonify(payload), status

    @app.before_request
    def protect_loopback_setup() -> Response | tuple[Response, int] | None:
        if not controller.expected_host or request.host != controller.expected_host:
            return api_error("This setup page is available only through its private loopback address.", 400)
        if (
            request.path.startswith("/api/")
            and request.headers.get("Origin") is not None
            and request.headers.get("Origin") != controller.expected_origin
        ):
            return api_error("The setup request did not come from this setup page.", 403)
        if request.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("Origin") is None:
                return api_error("The setup request did not come from this setup page.", 403)
            if request.mimetype != "application/json":
                return api_error("Send this setup request as JSON.", 415)
            content_length = request.content_length
            if content_length is not None and content_length > MAX_SETUP_BODY_BYTES:
                raise RequestEntityTooLarge()
        if request.path.startswith("/api/") and request.endpoint != "setup_bootstrap":
            if not controller.is_authenticated(request.cookies.get(controller.cookie_name)):
                return api_error(
                    "This setup session is not authorized. Relaunch DistillFeed to open a new private link.",
                    401,
                )
            controller.touch()
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not controller.csrf_is_valid(
                request.headers.get(CSRF_HEADER)
            ):
                return api_error("The setup page security token is missing or expired.", 403)
        return None

    @app.after_request
    def setup_security_headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
            "object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error: RequestEntityTooLarge) -> tuple[Response, int]:
        return api_error("The setup request is larger than 64 KiB.", 413)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int] | HTTPException:
        if request.path.startswith("/api/"):
            return api_error("The setup request could not be processed.", int(error.code or 500))
        return error

    @app.errorhandler(Exception)
    def unexpected_error(_error: Exception) -> tuple[Response, int]:
        return api_error("Setup encountered an internal error. Relaunch DistillFeed and try again.", 500)

    def json_object(*, allowed: set[str] | None = None) -> dict[str, Any]:
        # get_data applies Flask's maximum body limit even when Content-Length is absent.
        raw = request.get_data(cache=True)
        if len(raw) > MAX_SETUP_BODY_BYTES:
            raise RequestEntityTooLarge()
        if request.content_length is None and len(raw) == MAX_SETUP_BODY_BYTES:
            # Werkzeug bounds an unknown-length/chunked WSGI stream at
            # MAX_CONTENT_LENGTH, so get_data() cannot itself distinguish an
            # exact-size body from a silently truncated oversized one. Probe
            # only the next byte of the already bounded input stream.
            stream = request.environ.get("wsgi.input")
            if stream is not None and stream.read(1):
                raise RequestEntityTooLarge()
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise SetupValidationError({"form": "The setup request must be a JSON object."})
        if allowed is not None and set(value) - allowed:
            raise SetupValidationError({"form": "The setup request contains an unknown field."})
        return value

    def serialized_mutation(function: Any) -> Any:
        @wraps(function)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            # State check + transition + associated controller data form one
            # browser-level action even when the WSGI server is threaded.
            with controller.mutation_lock:
                return function(*args, **kwargs)

        return guarded

    @app.get("/")
    def setup_page() -> str:
        return render_template("setup.html", default_profile=profile)

    @app.post("/api/bootstrap")
    def setup_bootstrap() -> tuple[Response, int] | Response:
        try:
            payload = json_object(allowed={"capability"})
            if set(payload) != {"capability"} or not isinstance(payload["capability"], str):
                raise SetupValidationError({"form": "The private setup capability is missing."})
            browser_session, csrf = controller.exchange_capability(payload["capability"])
        except SetupValidationError as exc:
            return api_error("The private setup link is incomplete.", 400, errors=exc.errors)
        except (SetupCommitError, TransitionError) as exc:
            return api_error(_safe_error(exc), 409)
        response = jsonify(
            {
                "ok": True,
                "csrf_token": csrf,
                "state": controller.public_state(),
                "preset": preset_payload(profile),
                "environment_openai_key_available": bool(environment.get("OPENAI_API_KEY", "").strip()),
            }
        )
        response.set_cookie(
            controller.cookie_name,
            browser_session,
            httponly=True,
            secure=False,
            samesite="Strict",
            path="/",
        )
        return response

    @app.get("/api/state")
    def setup_state() -> Response:
        # The authenticated page needs a fresh in-memory CSRF value after reload.
        # SameSite=Strict + exact Host/Origin checks still protect mutations.
        return jsonify(
            {
                "ok": True,
                "state": controller.public_state(),
                "csrf_token": controller.csrf_token,
                "environment_openai_key_available": bool(environment.get("OPENAI_API_KEY", "").strip()),
            }
        )

    @app.get("/api/preset/<selected_profile>")
    def setup_preset(selected_profile: str) -> tuple[Response, int] | Response:
        try:
            preset = preset_payload(selected_profile)
        except ValueError:
            return api_error("That setup path is not available.", 404)
        return jsonify(
            {
                "ok": True,
                "preset": preset,
                "environment_openai_key_available": bool(environment.get("OPENAI_API_KEY", "").strip()),
            }
        )

    @app.post("/api/validate")
    @serialized_mutation
    def setup_validate() -> tuple[Response, int] | Response:
        try:
            payload = json_object(allowed={"settings"})
            if set(payload) != {"settings"}:
                raise SetupValidationError({"form": "The setup settings are missing."})
            if session.phase not in {SetupPhase.EDITING, SetupPhase.REVIEWED, SetupPhase.FAILED}:
                return api_error("These settings cannot be reviewed in the current setup state.", 409)
            review_token, review = session.validate(payload["settings"], environment=environment)
        except SetupValidationError as exc:
            return api_error("Some settings need attention.", 422, errors=exc.errors)
        except TransitionError:
            return api_error("These settings cannot be reviewed in the current setup state.", 409)
        editable_settings = dict(payload["settings"])
        # The server-side SetupSession retains the real values for completion,
        # while an authenticated reload receives only a safe editable copy.
        editable_settings["openai_key"] = ""
        editable_settings["ntfy_token"] = ""
        with controller._lock:
            controller.review_token = review_token
            controller.review = review
            controller.editable_settings = editable_settings
        return jsonify(
            {
                "ok": True,
                "state": controller.public_state(),
                "review_token": review_token,
                "review": review,
            }
        )

    @app.post("/api/edit")
    @serialized_mutation
    def setup_edit() -> tuple[Response, int] | Response:
        try:
            payload = json_object(allowed=set())
            if payload:
                raise SetupValidationError({"form": "This request does not accept settings."})
            session.edit()
        except SetupValidationError as exc:
            return api_error("The edit request is invalid.", 400, errors=exc.errors)
        except TransitionError:
            return api_error("Settings can be edited only after reviewing them.", 409)
        with controller._lock:
            controller.review_token = None
            controller.review = None
        return jsonify({"ok": True, "state": controller.public_state()})

    @app.post("/api/complete")
    @serialized_mutation
    def setup_complete() -> tuple[Response, int] | Response:
        try:
            payload = json_object(allowed={"review_token"})
            if set(payload) != {"review_token"} or not isinstance(payload["review_token"], str):
                raise SetupValidationError({"form": "Review the current settings before applying them."})
            if session.phase not in {SetupPhase.REVIEWED, SetupPhase.COMPLETE}:
                return api_error("Setup cannot be completed from its current state.", 409)
            result = session.complete(payload["review_token"])
        except SetupValidationError as exc:
            return api_error("The completion request is invalid.", 400, errors=exc.errors)
        except TransitionError:
            return api_error("Setup cannot be completed from its current state.", 409)
        except SetupRecoveryRequired as exc:
            # Rollback could not be proved. This is deliberately terminal: a
            # second Apply in the same process must not touch ambiguous durable
            # state. The launcher will verify it afresh on the next invocation.
            with controller._lock:
                controller.review_token = None
                controller.review = None
                controller.editable_settings = None
            controller.mark_terminal()
            return api_error(
                _safe_error(session.sanitize_error(exc)),
                500,
                state=controller.public_state(),
            )
        except SetupCommitError as exc:
            if session.phase != SetupPhase.FAILED:
                return api_error(_safe_error(session.sanitize_error(exc)), 409)
            # A real commit failure has already moved the session to FAILED and
            # SetupCommitter has rolled back its marked stage.
            return api_error(
                "Setup did not change the active reader. "
                + _safe_error(session.sanitize_error(exc)),
                500,
            )
        except Exception as exc:
            # SetupCommitter has already rolled back any marked stage at this point.
            return api_error(
                "Setup did not change the active reader. "
                + _safe_error(session.sanitize_error(exc)),
                500,
            )
        controller.mark_terminal(result)
        return jsonify({"ok": True, "state": controller.public_state(), "result": result.public()})

    @app.post("/api/cancel")
    @serialized_mutation
    def setup_cancel() -> tuple[Response, int] | Response:
        try:
            payload = json_object(allowed=set())
            if payload:
                raise SetupValidationError({"form": "This request does not accept settings."})
            session.cancel()
        except SetupValidationError as exc:
            return api_error("The cancellation request is invalid.", 400, errors=exc.errors)
        except TransitionError:
            return api_error("Setup can no longer be cancelled from its current state.", 409)
        controller.mark_terminal()
        return jsonify(
            {
                "ok": True,
                "state": controller.public_state(),
                "message": "Setup was cancelled. No reader was created.",
            }
        )

    return app, controller


def run_setup(
    state_root: Path,
    *,
    profile: str = "recommended",
    open_browser: bool = True,
) -> CommitResult | None:
    """Run one blocking, loopback-only first-use session.

    The HTTP server owns no application state after this function returns. A
    successful result contains launch-only secrets internally; browser responses
    expose only ``CommitResult.public()``.
    """

    preset_payload(profile)
    capability = secrets.token_urlsafe(32)  # 32 random bytes: a 256-bit capability.
    session = SetupSession(SetupCommitter(Path(state_root)))
    app, controller = create_setup_app(
        session,
        capability=capability,
        profile=profile,
    )
    server = make_server(
        "127.0.0.1",
        0,
        app,
        threaded=True,
        request_handler=_QuietRequestHandler,
    )
    port = int(server.server_port)
    controller.set_endpoint("127.0.0.1", port)
    private_url = f"{controller.expected_origin}/#capability={capability}"

    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(private_url, new=1, autoraise=True))
        except (OSError, webbrowser.Error):
            opened = False
    if opened:
        print("DistillFeed setup opened in your browser.", flush=True)
        print(
            f"Setup address: {controller.expected_origin}/ (reopen it in the same browser if the tab closes).",
            flush=True,
        )
        print(
            "If no browser window appeared, press Ctrl+C and run ./launch.sh --no-browser.",
            flush=True,
        )
    else:
        print("Open this private setup link in a browser on this computer:", flush=True)
        print(private_url, flush=True)
        print("Do not share this one-time link.", flush=True)

    def stop_on_terminal_or_timeout() -> None:
        while not controller.terminal.wait(timeout=0.5):
            if controller.expire_if_due():
                break
        # Let the terminal JSON response flush before closing the listener.
        time.sleep(0.25)
        # BaseServer.shutdown must run from a thread other than serve_forever.
        server.shutdown()

    watcher = threading.Thread(
        target=stop_on_terminal_or_timeout,
        name="distillfeed-setup-lifecycle",
        daemon=True,
    )
    watcher.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        try:
            if session.phase not in TERMINAL_PHASES:
                session.cancel()
        except TransitionError:
            pass
        controller.mark_terminal()
    finally:
        controller.terminal.set()
        server.server_close()
        watcher.join(timeout=2)
    if session.phase == SetupPhase.RECOVERY_REQUIRED:
        raise SetupRecoveryRequired(
            session.last_error
            or (
                "Setup could not confirm a safe rollback. Preserve the private "
                f"state in {state_root} and relaunch DistillFeed for verification."
            )
        )
    return controller.result


__all__ = ["SetupWebController", "create_setup_app", "run_setup"]
