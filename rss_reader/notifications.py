from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Callable

import requests

from .config import Config
from .db import transaction, utcnow
from .net import safe_external_url
from .ntfy_policy import filter_ntfy_candidates, load_ntfy_scope_policy

LOGGER = logging.getLogger(__name__)
NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}


def _destination_key(server_url: str, topic: str) -> str:
    value = f"{server_url.rstrip('/').casefold()}\n{topic}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raise_for_ntfy_status(response: Any) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = str(getattr(response, "text", "") or "").strip()[:500]
        message = f"ntfy rejected the notification: {detail}" if detail else str(exc)
        raise RuntimeError(message) from exc


def send_ntfy_test(
    config: Config, *, post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    options = config.section("notifications")["ntfy"]
    if not options["enabled"]:
        raise ValueError("Enable ntfy and save Settings before sending a test device alert")
    token = os.environ.get(str(options["token_env"]), "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = post(
        str(options["server_url"]).strip().rstrip("/"),
        json={
            "topic": str(options["topic"]).strip(),
            "title": "DistillFeed test",
            "message": "Delivery to other devices is configured correctly.",
            "priority": NTFY_PRIORITY[str(options["priority"])],
            "tags": ["white_check_mark", "distillfeed"],
        },
        headers=headers,
        timeout=int(options["timeout_seconds"]),
    )
    _raise_for_ntfy_status(response)
    try:
        identifier = str(response.json().get("id") or "")[:300] or None
    except (TypeError, ValueError):
        identifier = None
    return {"status": "delivered", "provider_message_id": identifier}


def deliver_ntfy_for_run(
    connection,
    config: Config,
    llm_run_id: int,
    *,
    post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    """Publish high-relevance items once, without changing summary success state."""
    options = config.section("notifications")["ntfy"]
    if not options["enabled"]:
        return {"status": "disabled", "eligible": 0, "delivered": 0, "failed": 0}

    server_url = str(options["server_url"]).strip().rstrip("/")
    topic = str(options["topic"]).strip()
    limit = int(options["max_items_per_summary"])
    destination_key = _destination_key(server_url, topic)
    policy = load_ntfy_scope_policy(connection, int(options["minimum_relevance"]))
    candidates = filter_ntfy_candidates(policy, connection.execute(
        """SELECT i.id AS item_id, i.title, i.url, f.id AS feed_id,
                  f.group_id, f.title AS feed_title,
                  si.importance AS relevance, si.description
           FROM summaries s JOIN summary_items si ON si.summary_id=s.id
           JOIN items i ON i.id=si.item_id JOIN feeds f ON f.id=i.feed_id
           WHERE s.llm_run_id=? AND si.included=1 AND f.enabled=1
             AND f.xml_url NOT LIKE 'plugin://%'
           ORDER BY si.importance DESC, i.id""",
        (llm_run_id,),
    ).fetchall(), limit)

    claimed: list[tuple[int, Any]] = []
    with transaction(connection, immediate=True):
        for candidate in candidates:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO notification_deliveries(
                       channel,destination_key,llm_run_id,item_id,relevance,
                       minimum_relevance,policy_scope_kind,policy_scope_id,policy_label,
                       status,attempted_at
                   ) VALUES('ntfy',?,?,?,?,?,?,?,?, 'sending', ?)""",
                (
                    destination_key, llm_run_id, int(candidate["item_id"]),
                    int(candidate["relevance"]), int(candidate["minimum_relevance"]),
                    str(candidate["policy_scope_kind"]), candidate["policy_scope_id"],
                    str(candidate["policy_label"])[:300], utcnow(),
                ),
            )
            if cursor.rowcount:
                claimed.append((int(cursor.lastrowid), candidate))

    token = os.environ.get(str(options["token_env"]), "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    delivered = 0
    failed = 0
    for delivery_id, candidate in claimed:
        click = safe_external_url(candidate["url"])
        message = str(candidate["description"] or candidate["title"]).strip()
        message = (
            f"{message[:3200]}\n\n{candidate['feed_title']} · relevance "
            f"{int(candidate['relevance'])}/100 · {candidate['policy_label']} alert rule "
            f"≥{int(candidate['minimum_relevance'])}/100"
        )
        payload: dict[str, Any] = {
            "topic": topic,
            "title": str(candidate["title"])[:250],
            "message": message[:4000],
            "priority": NTFY_PRIORITY[str(options["priority"])],
            "tags": ["newspaper", "distillfeed"],
        }
        if click:
            payload["click"] = click
        try:
            response = post(
                server_url,
                json=payload,
                headers=headers,
                timeout=int(options["timeout_seconds"]),
            )
            _raise_for_ntfy_status(response)
            try:
                provider_message_id = str(response.json().get("id") or "")[:300] or None
            except (TypeError, ValueError):
                provider_message_id = None
            connection.execute(
                """UPDATE notification_deliveries SET status='delivered',delivered_at=?,
                   provider_message_id=?,error=NULL WHERE id=?""",
                (utcnow(), provider_message_id, delivery_id),
            )
            delivered += 1
        except Exception as exc:  # A push failure must never roll back a successful digest.
            connection.execute(
                "UPDATE notification_deliveries SET status='failed',error=? WHERE id=?",
                (str(exc)[:2000], delivery_id),
            )
            failed += 1
            LOGGER.warning("ntfy delivery id=%s failed: %s", delivery_id, exc)

    return {
        "status": "success" if not failed else "partial",
        "eligible": len(candidates),
        "claimed": len(claimed),
        "duplicates": len(candidates) - len(claimed),
        "delivered": delivered,
        "failed": failed,
    }


def deliver_ntfy_for_job(
    connection,
    config: Config,
    ai_job_id: int,
    *,
    post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    """Send one duplicate-safe, job-wide mobile notification selection."""
    options = config.section("notifications")["ntfy"]
    if not options["enabled"]:
        return {"status": "disabled", "eligible": 0, "delivered": 0, "failed": 0}
    server_url = str(options["server_url"]).strip().rstrip("/")
    topic = str(options["topic"]).strip()
    limit = int(options["max_items_per_summary"])
    destination_key = _destination_key(server_url, topic)
    policy = load_ntfy_scope_policy(connection, int(options["minimum_relevance"]))
    candidates = filter_ntfy_candidates(policy, connection.execute(
        """SELECT * FROM (
               SELECT i.id AS item_id,i.title,i.url,f.id AS feed_id,f.group_id,
                      f.title AS feed_title,
                      si.importance AS relevance,si.description,s.llm_run_id,
                      ROW_NUMBER() OVER (
                        PARTITION BY i.id ORDER BY si.importance DESC,s.llm_run_id DESC
                      ) AS position
                 FROM summaries s JOIN summary_items si ON si.summary_id=s.id
                 JOIN items i ON i.id=si.item_id JOIN feeds f ON f.id=i.feed_id
                 WHERE s.ai_job_id=? AND si.included=1 AND f.enabled=1
                   AND f.xml_url NOT LIKE 'plugin://%'
           ) WHERE position=1 ORDER BY relevance DESC,item_id""",
        (ai_job_id,),
    ).fetchall(), limit)
    claimed: list[tuple[int, Any]] = []
    with transaction(connection, immediate=True):
        for candidate in candidates:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO notification_deliveries(
                       channel,destination_key,llm_run_id,item_id,relevance,
                       minimum_relevance,policy_scope_kind,policy_scope_id,policy_label,
                       status,attempted_at
                   ) VALUES('ntfy',?,?,?,?,?,?,?,?, 'sending', ?)""",
                (
                    destination_key, int(candidate["llm_run_id"]), int(candidate["item_id"]),
                    int(candidate["relevance"]), int(candidate["minimum_relevance"]),
                    str(candidate["policy_scope_kind"]), candidate["policy_scope_id"],
                    str(candidate["policy_label"])[:300], utcnow(),
                ),
            )
            if cursor.rowcount:
                claimed.append((int(cursor.lastrowid), candidate))
    token = os.environ.get(str(options["token_env"]), "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    delivered = failed = 0
    for delivery_id, candidate in claimed:
        message = str(candidate["description"] or candidate["title"]).strip()
        message = (
            f"{message[:3200]}\n\n{candidate['feed_title']} · relevance "
            f"{int(candidate['relevance'])}/100 · {candidate['policy_label']} alert rule "
            f"≥{int(candidate['minimum_relevance'])}/100"
        )
        payload: dict[str, Any] = {
            "topic": topic, "title": str(candidate["title"])[:250],
            "message": message[:4000],
            "priority": NTFY_PRIORITY[str(options["priority"])],
            "tags": ["newspaper", "distillfeed"],
        }
        click = safe_external_url(candidate["url"])
        if click:
            payload["click"] = click
        try:
            response = post(
                server_url, json=payload, headers=headers,
                timeout=int(options["timeout_seconds"]),
            )
            _raise_for_ntfy_status(response)
            try:
                provider_message_id = str(response.json().get("id") or "")[:300] or None
            except (TypeError, ValueError):
                provider_message_id = None
            connection.execute(
                """UPDATE notification_deliveries SET status='delivered',delivered_at=?,
                   provider_message_id=?,error=NULL WHERE id=?""",
                (utcnow(), provider_message_id, delivery_id),
            )
            delivered += 1
        except Exception as exc:
            connection.execute(
                "UPDATE notification_deliveries SET status='failed',error=? WHERE id=?",
                (str(exc)[:2000], delivery_id),
            )
            failed += 1
    return {
        "status": "success" if not failed else "partial", "eligible": len(candidates),
        "claimed": len(claimed), "duplicates": len(candidates) - len(claimed),
        "delivered": delivered, "failed": failed,
    }
