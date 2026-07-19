from __future__ import annotations

import json
import secrets
from typing import Any

from .db import transaction, utcnow


ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {
    "success", "partial", "empty", "blocked", "failed", "cancelled",
}
ALLOWED_TRANSITIONS = {
    "queued": {"running", "blocked", "failed", "cancelled"},
    "running": TERMINAL_STATES,
}


def create_operation(
    connection,
    *,
    kind: str,
    trigger: str,
    lock_name: str,
    lock_owner: str,
    scope_kind: str = "all",
    scope_id: int | None = None,
) -> dict[str, Any]:
    """Create the durable identity returned to an operation's initiating client."""
    if kind not in {"refresh", "summary", "arxiv"}:
        raise ValueError(f"Unknown operation kind: {kind}")
    if trigger not in {"browser", "automatic", "cli"}:
        raise ValueError(f"Unknown operation trigger: {trigger}")
    operation_key = secrets.token_urlsafe(18)
    identifier = int(connection.execute(
        """INSERT INTO app_operations(
               operation_key,kind,trigger,scope_kind,scope_id,lock_name,lock_owner,
               state,phase,message,created_at
           ) VALUES(?,?,?,?,?,?,?,'queued','queued','Waiting for the worker to start',?)""",
        (
            operation_key, kind, trigger, scope_kind, scope_id, lock_name,
            lock_owner, utcnow(),
        ),
    ).lastrowid)
    return {"id": identifier, "operation_id": operation_key}


def _transition(
    connection,
    operation_key: str,
    target: str,
    *,
    phase: str,
    message: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    row = connection.execute(
        "SELECT state FROM app_operations WHERE operation_key=?", (operation_key,),
    ).fetchone()
    if not row:
        raise LookupError("Operation not found")
    current = str(row["state"])
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise RuntimeError(f"Invalid operation transition: {current} -> {target}")
    terminal = target in TERMINAL_STATES
    now = utcnow()
    connection.execute(
        """UPDATE app_operations SET state=?,phase=?,message=?,result_json=?,error=?,
                  started_at=CASE WHEN started_at IS NULL THEN ? ELSE started_at END,
                  completed_at=CASE WHEN ? THEN ? ELSE completed_at END
           WHERE operation_key=?""",
        (
            target, phase, str(message)[:2000],
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)[:100_000]
            if result is not None else None,
            str(error)[:4000] if error else None,
            now, int(terminal), now if terminal else None, operation_key,
        ),
    )


def start_operation(connection, operation_key: str, message: str) -> None:
    with transaction(connection, immediate=True):
        _transition(
            connection, operation_key, "running", phase="starting", message=message,
        )


def set_operation_phase(
    connection, operation_key: str | None, phase: str, message: str,
) -> None:
    if not operation_key:
        return
    cursor = connection.execute(
        """UPDATE app_operations SET phase=?,message=?
           WHERE operation_key=? AND state='running'""",
        (str(phase)[:80], str(message)[:2000], operation_key),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Cannot update an operation that is not running")


def _terminal_state(status: Any) -> str:
    value = str(status or "failed").casefold()
    if value in {"success", "fresh", "unchanged"}:
        return "success"
    if value == "partial":
        return "partial"
    if value == "empty":
        return "empty"
    if value == "cancelled":
        return "cancelled"
    if value in {
        "blocked", "disabled", "cooldown", "pending-llm-disabled",
        "budget-blocked", "missing-credential",
    }:
        return "blocked"
    return "failed"


def _refresh_message(result: dict[str, Any]) -> str:
    status = str(result.get("status", "failed"))
    if result.get("message"):
        return str(result["message"])
    attempted = int(result.get("attempted", 0))
    succeeded = int(result.get("succeeded", 0))
    added = int(result.get("new_items", 0))
    if status == "partial":
        return (
            f"Checked {attempted} feeds; {succeeded} succeeded and {added} new "
            "entries were stored. Failed feeds kept their previous content."
        )
    if status == "failed":
        return str(result.get("error") or "The feed check failed")
    return f"Checked {attempted} feeds and stored {added} new entries"


def operation_message(kind: str, result: dict[str, Any]) -> str:
    if result.get("message"):
        return str(result["message"])
    if kind == "refresh":
        return _refresh_message(result)
    nested = result.get("summary")
    if isinstance(nested, dict) and nested.get("message"):
        return str(nested["message"])
    return "The update completed" if _terminal_state(result.get("status")) == "success" else (
        "The update did not complete"
    )


def finish_operation(
    connection, operation_key: str, kind: str, result: dict[str, Any],
) -> None:
    state = _terminal_state(result.get("status"))
    message = operation_message(kind, result)
    error = message if state == "failed" else None
    with transaction(connection, immediate=True):
        _transition(
            connection, operation_key, state, phase="complete", message=message,
            result=result, error=error,
        )


def fail_operation(connection, operation_key: str, error: BaseException | str) -> None:
    message = str(error).strip() or "The operation failed before returning a result"
    with transaction(connection, immediate=True):
        row = connection.execute(
            "SELECT state FROM app_operations WHERE operation_key=?", (operation_key,),
        ).fetchone()
        if not row or str(row["state"]) in TERMINAL_STATES:
            return
        _transition(
            connection, operation_key, "failed", phase="complete", message=message,
            result={"status": "failed", "message": message}, error=message,
        )


def operation_for_display(connection, operation_key: str | None) -> dict[str, Any] | None:
    if not operation_key:
        return None
    row = connection.execute(
        "SELECT * FROM app_operations WHERE operation_key=?", (operation_key,),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        decoded = json.loads(str(result.pop("result_json") or "null"))
    except json.JSONDecodeError:
        decoded = None
    result["result"] = decoded if isinstance(decoded, dict) else None
    result["active"] = str(result["state"]) in ACTIVE_STATES
    return result
