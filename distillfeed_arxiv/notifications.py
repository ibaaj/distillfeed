from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}


def _options(cfg: dict[str, Any]) -> dict[str, Any]:
    notifications = cfg.get("notifications", {})
    return notifications.get("ntfy", {}) if isinstance(notifications, dict) else {}


def send_arxiv_test(cfg: dict[str, Any]) -> dict[str, Any]:
    options = _options(cfg)
    if not options.get("enabled", False):
        raise ValueError("Enable arXiv ntfy and save Settings before sending a test device alert")
    server_url = str(options.get("server_url", "https://ntfy.sh")).strip().rstrip("/")
    topic = str(options.get("topic", "")).strip()
    if not topic:
        raise ValueError("Save an arXiv ntfy topic before testing")
    token = os.environ.get(str(options.get("token_env", "ARXIV_NTFY_TOKEN")), "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = requests.post(
        server_url,
        json={
            "topic": topic, "title": "DistillFeed arXiv test",
            "message": "High-score arXiv device alerts are configured correctly.",
            "priority": PRIORITIES.get(str(options.get("priority", "high")), 4),
            "tags": ["white_check_mark", "mortar_board"],
        },
        headers=headers, timeout=int(options.get("timeout_seconds", 10)),
    )
    response.raise_for_status()
    return {"status": "delivered", "message": "Test arXiv device alert sent through ntfy."}


def deliver_arxiv_pushes(connection: Any, cfg: dict[str, Any], item_ids: list[int], *, automatic: bool) -> dict[str, Any]:
    options = _options(cfg)
    if not options.get("enabled", False):
        return {"status": "disabled", "eligible": 0, "delivered": 0, "failed": 0}
    if not automatic and not options.get("send_on_manual_refresh", False):
        return {"status": "manual-suppressed", "eligible": 0, "delivered": 0, "failed": 0}
    server_url = str(options.get("server_url", "https://ntfy.sh")).strip().rstrip("/")
    topic = str(options.get("topic", "")).strip()
    if not topic:
        return {"status": "misconfigured", "message": "arXiv ntfy topic is empty", "eligible": 0, "delivered": 0, "failed": 0}
    threshold = int(options.get("minimum_llm_score", 88))
    limit = int(options.get("max_items_per_digest", 5))
    if not item_ids:
        return {"status": "empty", "eligible": 0, "delivered": 0, "failed": 0}
    marks = ",".join("?" for _ in item_ids)
    rows = connection.execute(
        f"""SELECT ap.item_id, ap.arxiv_id, ap.llm_score, ap.why, ap.final_score,
                   i.title, i.url, i.author
            FROM distillfeed_arxiv_papers ap JOIN items i ON i.id=ap.item_id
            WHERE ap.item_id IN ({marks}) AND ap.decision='keep' AND ap.llm_score>=?
            ORDER BY ap.llm_score DESC, ap.final_score DESC LIMIT ?""",
        [*item_ids, threshold, limit],
    ).fetchall()
    destination = hashlib.sha256(f"{server_url.casefold()}\n{topic}".encode()).hexdigest()
    token = os.environ.get(str(options.get("token_env", "ARXIV_NTFY_TOKEN")), "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    delivered = failed = duplicates = 0
    for row in rows:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO distillfeed_arxiv_notifications(
                   destination_key,item_id,llm_score,status,attempted_at
               ) VALUES(?,?,?,'sending',datetime('now'))""",
            (destination, int(row["item_id"]), int(row["llm_score"])),
        )
        if not cursor.rowcount:
            duplicates += 1
            continue
        delivery_id = int(cursor.lastrowid)
        payload = {
            "topic": topic,
            "title": str(row["title"])[:250],
            "message": (
                f"{row['why']}\n\nLLM {int(row['llm_score'])}/100 · "
                f"final {float(row['final_score']):.1f}\n{row['author'] or 'Unknown authors'}"
            )[:4000],
            "priority": PRIORITIES.get(str(options.get("priority", "high")), 4),
            "tags": ["mortar_board", "distillfeed"],
            "click": str(row["url"]),
        }
        try:
            response = requests.post(server_url, json=payload, headers=headers, timeout=int(options.get("timeout_seconds", 10)))
            response.raise_for_status()
            try:
                provider_id = str(response.json().get("id") or "")[:300] or None
            except (TypeError, ValueError):
                provider_id = None
            connection.execute(
                """UPDATE distillfeed_arxiv_notifications SET status='delivered',
                   delivered_at=datetime('now'),provider_message_id=?,error=NULL WHERE id=?""",
                (provider_id, delivery_id),
            )
            delivered += 1
        except Exception as exc:
            connection.execute(
                "UPDATE distillfeed_arxiv_notifications SET status='failed',error=? WHERE id=?",
                (str(exc)[:2000], delivery_id),
            )
            failed += 1
            LOGGER.warning("arXiv ntfy delivery failed for item %s: %s", row["item_id"], exc)
    return {
        "status": "success" if not failed else "partial", "eligible": len(rows),
        "delivered": delivered, "failed": failed, "duplicates": duplicates,
        "minimum_llm_score": threshold,
    }
