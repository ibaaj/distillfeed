from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from markupsafe import Markup, escape

from rss_reader.db import transaction, utcnow

from .config import load_plugin_config, settings_fields, update_settings
from .fetch import fetch_api_window, fetch_rss, merge_papers
from .llm import LLMUsage, daily_digest, rerank
from .models import Decision, LocalScore, Paper
from .notifications import deliver_arxiv_pushes, send_arxiv_test
from .scoring import compute_local_score, decide

LOGGER = logging.getLogger(__name__)
GROUP_TITLE = "arXiv Digest"
PROMPT_VERSION = "distillfeed-arxiv-1"
FEED_TITLES = {
    "cs.AI": "Artificial Intelligence (cs.AI)",
    "cs.LG": "Machine Learning (cs.LG)",
    "cs.LO": "Logic in Computer Science (cs.LO)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS distillfeed_arxiv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS distillfeed_arxiv_papers (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    arxiv_id TEXT NOT NULL UNIQUE,
    version TEXT,
    categories_json TEXT NOT NULL,
    primary_category TEXT,
    pdf_url TEXT,
    announce_type TEXT,
    source TEXT NOT NULL,
    local_score INTEGER,
    llm_score INTEGER,
    final_score REAL,
    decision TEXT,
    why TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    local_reasons_json TEXT NOT NULL DEFAULT '[]',
    evaluation_status TEXT NOT NULL DEFAULT 'pending',
    evaluated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_distillfeed_arxiv_pending
ON distillfeed_arxiv_papers(evaluation_status, item_id);
CREATE TABLE IF NOT EXISTS distillfeed_arxiv_seen (
    arxiv_id TEXT PRIMARY KEY,
    version TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    local_score INTEGER,
    selected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS distillfeed_arxiv_notifications (
    id INTEGER PRIMARY KEY,
    destination_key TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    llm_score INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    delivered_at TEXT,
    provider_message_id TEXT,
    error TEXT,
    UNIQUE(destination_key, item_id)
);
"""


def _state(connection: Any, key: str) -> str | None:
    row = connection.execute("SELECT value FROM distillfeed_arxiv_state WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_state(connection: Any, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO distillfeed_arxiv_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _model_error(error: Exception, cfg: dict[str, Any]) -> str:
    status = getattr(error, "status_code", None)
    environment_name = str(cfg["llm"].get("api_key_env", "OPENAI_API_KEY"))
    model = str(cfg["llm"].get("model", "the configured model"))
    if status == 401:
        return (
            f"OpenAI rejected {environment_name} (401). Replace the key in the "
            "server environment and restart DistillFeed."
        )
    if status == 403:
        return f"OpenAI refused access to {model} (403). Check the API project's model permissions."
    if status == 404:
        return f"OpenAI could not find {model} (404). Choose an available arXiv AI model."
    if status == 429:
        return "OpenAI rate or usage limit reached (429). The announcement remains ready to retry."
    return str(error)[:2000]


def _retrieval_degraded(stats: dict[str, Any]) -> bool:
    return bool(
        stats.get("failed", 0)
        or stats.get("backfill_degraded")
        or stats.get("retrieval_degraded")
    )


def _record_seen(
    connection: Any, paper: Paper, *, local_score: int | None = None,
    selected: bool | None = None,
) -> None:
    now = utcnow()
    connection.execute(
        """INSERT INTO distillfeed_arxiv_seen(
               arxiv_id,version,first_seen_at,last_seen_at,local_score,selected
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(arxiv_id) DO UPDATE SET
               version=COALESCE(excluded.version,distillfeed_arxiv_seen.version),
               last_seen_at=excluded.last_seen_at,
               local_score=COALESCE(excluded.local_score,distillfeed_arxiv_seen.local_score),
               selected=CASE WHEN ? IS NULL THEN distillfeed_arxiv_seen.selected ELSE excluded.selected END""",
        (
            paper.arxiv_id, paper.version, now, now, local_score, int(bool(selected)),
            None if selected is None else int(bool(selected)),
        ),
    )


def _needs_storage(connection: Any, paper: Paper) -> bool:
    if connection.execute(
        "SELECT 1 FROM distillfeed_arxiv_papers WHERE arxiv_id=?", (paper.arxiv_id,),
    ).fetchone():
        return True
    row = connection.execute(
        "SELECT version FROM distillfeed_arxiv_seen WHERE arxiv_id=?", (paper.arxiv_id,),
    ).fetchone()
    if not row:
        return True
    return bool(paper.version and row["version"] and paper.version != row["version"])


def _ensure_sources(connection: Any, cfg: dict[str, Any]) -> tuple[int, dict[str, int]]:
    group_id: int | None = None
    stored = _state(connection, "group_id")
    if stored and stored.isdecimal():
        row = connection.execute("SELECT id FROM groups WHERE id=?", (int(stored),)).fetchone()
        group_id = int(row["id"]) if row else None
    if group_id is None:
        row = connection.execute(
            "SELECT id FROM groups WHERE parent_id IS NULL AND title=? ORDER BY id LIMIT 1",
            (GROUP_TITLE,),
        ).fetchone()
        if row:
            group_id = int(row["id"])
        else:
            position = int(connection.execute("SELECT COALESCE(MAX(position),-1)+1 FROM groups WHERE parent_id IS NULL").fetchone()[0])
            group_id = int(connection.execute(
                """INSERT INTO groups(parent_id,title,position,llm_enabled,created_at)
                   VALUES(NULL,?,?,1,?)""", (GROUP_TITLE, position, utcnow()),
            ).lastrowid)
        _set_state(connection, "group_id", str(group_id))
    feeds: dict[str, int] = {}
    for position, category in enumerate(cfg["arxiv"]["categories"]):
        url = f"plugin://arxiv/{category}"
        row = connection.execute("SELECT id FROM feeds WHERE xml_url=?", (url,)).fetchone()
        if row:
            identifier = int(row["id"])
            connection.execute("UPDATE feeds SET group_id=?,enabled=1,llm_enabled=1 WHERE id=?", (group_id, identifier))
        else:
            identifier = int(connection.execute(
                """INSERT INTO feeds(group_id,title,title_locked,position,xml_url,html_url,
                       enabled,llm_enabled,created_at) VALUES(?,?,1,?,?,?,1,1,?)""",
                (group_id, FEED_TITLES.get(category, category), position, url, f"https://arxiv.org/list/{category}/new", utcnow()),
            ).lastrowid)
        feeds[category] = identifier
    if feeds:
        marks = ",".join("?" for _ in feeds)
        connection.execute(
            f"""UPDATE feeds SET enabled=0 WHERE xml_url LIKE 'plugin://arxiv/%'
                AND xml_url NOT IN ({marks})""",
            [f"plugin://arxiv/{category}" for category in feeds],
        )
    return group_id, feeds


def _selected_categories(context: Any, group_id: int, feeds: dict[str, int]) -> list[str]:
    if context.feed_id is not None:
        return [category for category, identifier in feeds.items() if identifier == context.feed_id]
    if context.group_id is not None:
        return list(feeds) if context.group_id == group_id else []
    return list(feeds)


def _paper_feed(paper: Paper, selected: list[str], feeds: dict[str, int]) -> int:
    choices = [paper.primary_category, *paper.source_categories, *paper.categories]
    category = next((value for value in choices if value in selected and value in feeds), selected[0])
    return feeds[category]


def _store_paper(connection: Any, paper: Paper, feed_id: int) -> tuple[int, bool, bool]:
    existing = connection.execute(
        "SELECT item_id,version FROM distillfeed_arxiv_papers WHERE arxiv_id=?", (paper.arxiv_id,)
    ).fetchone()
    published = (paper.published or paper.updated or datetime.now(UTC)).isoformat(timespec="seconds")
    if existing:
        item_id = int(existing["item_id"])
        revised = bool(paper.version and existing["version"] and paper.version != existing["version"])
        connection.execute(
            """UPDATE items SET title=?,url=?,author=?,published_at=?,description_text=?
               WHERE id=?""",
            (paper.title, paper.link, ", ".join(paper.authors), published, paper.abstract, item_id),
        )
        connection.execute(
            """UPDATE distillfeed_arxiv_papers SET version=?,categories_json=?,primary_category=?,
               pdf_url=?,announce_type=?,source=?,
               evaluation_status=CASE WHEN ? THEN 'pending' ELSE evaluation_status END,
               evaluated_at=CASE WHEN ? THEN NULL ELSE evaluated_at END WHERE item_id=?""",
            (paper.version, json.dumps(paper.categories, ensure_ascii=False), paper.primary_category,
             paper.pdf_link, paper.announce_type, paper.source, int(revised), int(revised), item_id),
        )
        return item_id, False, revised
    item_id = int(connection.execute(
        """INSERT INTO items(feed_id,stable_id,title,url,author,published_at,discovered_at,
               description_text,summary_eligible) VALUES(?,?,?,?,?,?,?,?,0)""",
        (feed_id, paper.arxiv_id, paper.title, paper.link, ", ".join(paper.authors),
         published, utcnow(), paper.abstract),
    ).lastrowid)
    connection.execute(
        """INSERT INTO distillfeed_arxiv_papers(item_id,arxiv_id,version,categories_json,
               primary_category,pdf_url,announce_type,source) VALUES(?,?,?,?,?,?,?,?)""",
        (item_id, paper.arxiv_id, paper.version, json.dumps(paper.categories, ensure_ascii=False),
         paper.primary_category, paper.pdf_link, paper.announce_type, paper.source),
    )
    return item_id, True, True


def _pending(connection: Any, feed_ids: list[int]) -> list[tuple[int, Paper]]:
    marks = ",".join("?" for _ in feed_ids)
    rows = connection.execute(
        f"""SELECT ap.*,i.feed_id,i.title,i.url,i.author,i.published_at,i.description_text
            FROM distillfeed_arxiv_papers ap JOIN items i ON i.id=ap.item_id
            WHERE ap.evaluation_status='pending' AND i.feed_id IN ({marks})
            ORDER BY COALESCE(i.published_at,i.discovered_at),i.id""", feed_ids,
    ).fetchall()
    result: list[tuple[int, Paper]] = []
    for row in rows:
        published = datetime.fromisoformat(row["published_at"]) if row["published_at"] else None
        result.append((int(row["item_id"]), Paper(
            arxiv_id=row["arxiv_id"], version=row["version"], title=row["title"],
            abstract=row["description_text"], authors=[value.strip() for value in str(row["author"] or "").split(",") if value.strip()],
            categories=json.loads(row["categories_json"]), primary_category=row["primary_category"],
            link=row["url"], pdf_link=row["pdf_url"], published=published, updated=published,
            source=row["source"], announce_type=row["announce_type"],
        )))
    return result


def _announcement_key(pending: list[tuple[int, Paper]]) -> str | None:
    """Return the arXiv announcement day represented by the pending set."""
    dates = [
        (paper.published or paper.updated).astimezone(UTC).date().isoformat()
        for _, paper in pending if paper.published or paper.updated
    ]
    return max(dates) if dates else None


def _evidence_fingerprint(
    connection: Any,
    pending: list[tuple[int, Paper]],
    cfg: dict[str, Any],
    categories: list[str],
    language: str,
) -> str:
    """Identify the evidence and policy for one digest revision.

    The announcement date is useful presentation metadata, but it is not an
    idempotency key: categories can recover and revisions can arrive later on
    the same date. Include every retained paper from the represented date as
    well as every pending paper, so later evidence necessarily creates a new
    fingerprint.
    """
    announcement = _announcement_key(pending)
    papers = {
        (str(paper.arxiv_id), str(paper.version or ""))
        for _, paper in pending
    }
    if announcement:
        papers.update(
            (str(row["arxiv_id"]), str(row["version"] or ""))
            for row in connection.execute(
                """SELECT ap.arxiv_id,ap.version
                     FROM distillfeed_arxiv_papers ap
                     JOIN items i ON i.id=ap.item_id
                    WHERE substr(COALESCE(i.published_at,i.discovered_at),1,10)=?
                      AND ap.evaluation_status IN ('pending','complete')""",
                (announcement,),
            ).fetchall()
        )
    llm = cfg["llm"]
    material = {
        "announcement": announcement,
        "papers": [list(value) for value in sorted(papers)],
        "categories": sorted(str(value) for value in categories),
        "language": str(language),
        "filters": cfg["filters"],
        "llm": {
            key: llm.get(key)
            for key in (
                "model", "max_candidates", "ranking_batch_size",
                "estimated_output_tokens_per_paper", "max_digest_input_chars",
                "system_prompt",
            )
        },
        "prompt_version": PROMPT_VERSION,
    }
    return hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _model_blocker(cfg: dict[str, Any]) -> tuple[str, str] | None:
    if not cfg["llm"].get("enabled", True):
        return (
            "ai-disabled",
            "The arXiv ranking model is disabled. Enable it before updating the daily digest.",
        )
    environment_name = str(cfg["llm"].get("api_key_env", "OPENAI_API_KEY"))
    if not os.environ.get(environment_name, "").strip():
        return (
            "api-key-missing",
            f"{environment_name} is not available to the DistillFeed server. "
            "Set it in the server environment and restart DistillFeed.",
        )
    return None


def _start_run(
    connection: Any,
    item_ids: list[int],
    cfg: dict[str, Any],
    evidence_fingerprint: str,
) -> int:
    # Each provider attempt is immutable history. In particular, retrying the
    # same evidence must not reset a failed run or delete its last good digest.
    request_key = f"arxiv:{evidence_fingerprint}:attempt:{uuid.uuid4().hex}"
    pricing = {
        "input": float(cfg["llm"].get("input_price_per_million", 0)),
        "cached_input": float(cfg["llm"].get("input_price_per_million", 0)),
        "output": float(cfg["llm"].get("output_price_per_million", 0)),
    }
    return int(connection.execute(
        """INSERT INTO llm_runs(request_key,started_at,status,model,prompt_version,
               submitted_items,deferred_items,pricing_json) VALUES(?,?,'running',?,?,?,0,?)""",
        (request_key, utcnow(), cfg["llm"]["model"], PROMPT_VERSION, len(item_ids), json.dumps(pricing)),
    ).lastrowid)


def _complete_run(
    connection: Any, run_id: int, group_id: int,
    evaluated: list[tuple[int, Paper, LocalScore, Decision]], digest: dict[str, Any], usage: LLMUsage,
) -> None:
    summary_id = int(connection.execute(
        """INSERT INTO summaries(
               llm_run_id,group_id,scope_kind,scope_id,policy_hash,overview,changes,sections_json,created_at
           ) VALUES(?,?,'group',?,? ,?,'',?,?)""",
        (run_id, group_id, group_id, PROMPT_VERSION, str(digest.get("overview", ""))[:8000],
         json.dumps(digest.get("sections", []), ensure_ascii=False), utcnow()),
    ).lastrowid)
    ranked = sorted(evaluated, key=lambda entry: (entry[3].decision != "keep", -(entry[3].llm_score or -1), -entry[3].local_score, -entry[3].final_score))
    for rank, (item_id, paper, local, decision) in enumerate(ranked, 1):
        importance = decision.llm_score if decision.llm_score is not None else max(0, min(100, decision.local_score * 5))
        connection.execute(
            """INSERT INTO summary_items(summary_id,item_id,included,rank,importance,description,
                   justification,story_cluster) VALUES(?,?,?,?,?,?,?,?)""",
            (summary_id, item_id, int(decision.decision == "keep"), rank, importance,
             paper.abstract[:1000], decision.why, decision.tags[0] if decision.tags else "arXiv"),
        )
        connection.execute(
            """UPDATE distillfeed_arxiv_papers SET local_score=?,llm_score=?,final_score=?,decision=?,
               why=?,tags_json=?,local_reasons_json=?,evaluation_status='complete',evaluated_at=?
               WHERE item_id=?""",
            (decision.local_score, decision.llm_score, decision.final_score, decision.decision,
             decision.why, json.dumps(decision.tags, ensure_ascii=False),
             json.dumps(local.reasons, ensure_ascii=False), utcnow(), item_id),
        )
    connection.execute(
        """UPDATE llm_runs SET completed_at=?,status='success',input_tokens=?,cached_input_tokens=?,
           output_tokens=?,estimated_cost_usd=?,provider_request_id=?,error=NULL WHERE id=?""",
        (utcnow(), usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.cost,
         ",".join(usage.request_ids)[:1000] or None, run_id),
    )


class ArxivDigestPlugin:
    name = "arxiv_digest"

    def disable(self, connection: Any, main_config: Any) -> None:
        """Hide virtual sources while retaining papers, digests, and plugin state."""
        connection.execute(
            "UPDATE feeds SET enabled=0 WHERE xml_url LIKE 'plugin://arxiv/%'"
        )

    def settings_fields(self, main_config: Any) -> list[dict[str, Any]]:
        return settings_fields(main_config)

    def update_settings(self, main_config: Any, values: dict[str, Any]) -> None:
        update_settings(main_config, values)

    def settings_actions(self, main_config: Any) -> list[dict[str, Any]]:
        return [{
            "action": "test-ntfy", "category": "arXiv digest",
            "label": "Send test arXiv device alert",
            "help": "Save the arXiv ntfy settings before testing.",
        }]

    def run_settings_action(self, main_config: Any, action: str) -> dict[str, Any]:
        if action != "test-ntfy":
            raise ValueError(f"Unknown arXiv plugin action: {action}")
        return send_arxiv_test(load_plugin_config(main_config))

    def initialize(self, connection: Any, main_config: Any) -> None:
        cfg = load_plugin_config(main_config)
        connection.executescript(SCHEMA)
        connection.execute(
            """INSERT OR IGNORE INTO distillfeed_arxiv_seen(
                   arxiv_id,version,first_seen_at,last_seen_at,local_score,selected
               )
               SELECT ap.arxiv_id,ap.version,i.discovered_at,i.discovered_at,
                      ap.local_score,1
               FROM distillfeed_arxiv_papers ap JOIN items i ON i.id=ap.item_id"""
        )
        _ensure_sources(connection, cfg)

    def _evaluate_pending(
        self,
        context: Any,
        cfg: dict[str, Any],
        group_id: int,
        feeds: dict[str, int],
        categories: list[str],
        stats: dict[str, Any],
        *,
        advance_watermark: bool,
        allow_model: bool = True,
    ) -> dict[str, Any]:
        created_item_ids = set(int(value) for value in stats.pop("_created_item_ids", []))
        if allow_model:
            stats["retrieval_degraded"] = (
                _state(context.connection, "pending_retrieval_degraded") == "1"
            )
        else:
            _set_state(
                context.connection,
                "pending_retrieval_degraded",
                "1" if _retrieval_degraded(stats) else "0",
            )
        if getattr(context, "cancel_requested", lambda: False)():
            stats["status"] = "cancelled"
            return stats
        pending = _pending(context.connection, [feeds[category] for category in categories])
        if not pending:
            if advance_watermark and not _retrieval_degraded(stats):
                _set_state(context.connection, "last_complete_at", utcnow())
            stats["status"] = "partial" if _retrieval_degraded(stats) else (
                "unchanged" if stats.get("new_items", 0) == 0 else "success"
            )
            return stats
        scored = [(item_id, paper, compute_local_score(paper, cfg)) for item_id, paper in pending]
        broad = int(cfg["filters"].get("broad_candidate_threshold", 0))
        maximum = int(cfg["llm"].get("max_candidates", 100))
        shortlisted = sorted(
            [entry for entry in scored if entry[2].score >= broad],
            key=lambda entry: entry[2].score, reverse=True,
        )[:maximum]
        shortlisted_ids = {item_id for item_id, _, _ in shortlisted}
        retained_ids = set(shortlisted_ids)
        with transaction(context.connection, immediate=True):
            for item_id, paper, local in scored:
                selected = item_id in shortlisted_ids
                _record_seen(context.connection, paper, local_score=local.score, selected=selected)
                if selected:
                    context.connection.execute(
                        """UPDATE distillfeed_arxiv_papers SET local_score=?,local_reasons_json=?
                           WHERE item_id=?""",
                        (local.score, json.dumps(local.reasons, ensure_ascii=False), item_id),
                    )
                    continue
                saved = context.connection.execute(
                    """SELECT i.is_starred OR i.is_read_later OR EXISTS(
                               SELECT 1 FROM item_tags it WHERE it.item_id=i.id
                           ) FROM items i WHERE i.id=?""",
                    (item_id,),
                ).fetchone()
                if saved and bool(saved[0]):
                    retained_ids.add(item_id)
                    context.connection.execute(
                        """UPDATE distillfeed_arxiv_papers SET local_score=?,local_reasons_json=?,
                           decision='drop',why='Screened out before LLM reranking',
                           evaluation_status='screened_out',evaluated_at=? WHERE item_id=?""",
                        (local.score, json.dumps(local.reasons, ensure_ascii=False), utcnow(), item_id),
                    )
                else:
                    context.connection.execute("DELETE FROM items WHERE id=?", (item_id,))
        stats["screened_locally"] = len(scored) - len(shortlisted)
        stats["selected_for_llm"] = len(shortlisted)
        stats["new_items"] = len(created_item_ids.intersection(retained_ids))
        scored = shortlisted
        if not scored:
            if advance_watermark and not _retrieval_degraded(stats):
                _set_state(context.connection, "last_complete_at", utcnow())
            stats["status"] = "partial" if _retrieval_degraded(stats) else "success"
            stats["evaluated_items"] = 0
            stats["kept_items"] = 0
            return stats
        announcement_key = _announcement_key([(item_id, paper) for item_id, paper, _ in scored])
        language = str(context.config.get("app", "summary_language", "English"))
        evidence_fingerprint = _evidence_fingerprint(
            context.connection,
            [(item_id, paper) for item_id, paper, _ in scored],
            cfg,
            categories,
            language,
        )
        stats["evidence_fingerprint"] = evidence_fingerprint
        if announcement_key:
            stats["announcement"] = announcement_key
            _set_state(context.connection, "pending_announcement", announcement_key)
        _set_state(context.connection, "pending_evidence_fingerprint", evidence_fingerprint)
        if not allow_model:
            stats["status"] = "waiting-for-digest"
            stats["evaluated_items"] = 0
            stats["kept_items"] = 0
            return stats
        if _state(context.connection, "last_digest_fingerprint") == evidence_fingerprint:
            stats["status"] = "unchanged"
            stats["message"] = "A digest for this exact arXiv evidence and policy already exists"
            return stats
        blocker = _model_blocker(cfg)
        if blocker:
            reason, message = blocker
            stats.update({
                "status": "blocked", "blocked_reason": reason,
                "message": message, "retryable": False,
            })
            _set_state(context.connection, "blocked_reason", reason)
            _set_state(context.connection, "blocked_message", message)
            return stats
        if getattr(context, "cancel_requested", lambda: False)():
            stats["status"] = "cancelled"
            return stats
        _set_state(context.connection, "blocked_reason", "")
        _set_state(context.connection, "blocked_message", "")
        run_id = _start_run(
            context.connection,
            [item_id for item_id, _, _ in scored],
            cfg,
            evidence_fingerprint,
        )
        try:
            candidates = [(paper, local) for _, paper, local in scored]
            ranked_count = len(candidates)
            batch_size = max(1, int(cfg["llm"].get("ranking_batch_size", 20)))
            stats["llm_calls"] = (
                ((ranked_count + batch_size - 1) // batch_size) if ranked_count else 0
            ) + 1  # the daily digest call
            reranked, rerank_usage = rerank(
                candidates, cfg,
                cancel_requested=getattr(context, "cancel_requested", lambda: False),
            )
            evaluated: list[tuple[int, Paper, LocalScore, Decision]] = []
            for item_id, paper, local in scored:
                evaluated.append((item_id, paper, local, decide(local, reranked.get(paper.arxiv_id), cfg)))
            digest_input = [entry for entry in evaluated if entry[3].decision == "keep"]
            if not digest_input:
                digest_input = evaluated[: min(25, len(evaluated))]
            if getattr(context, "cancel_requested", lambda: False)():
                raise InterruptedError("arXiv digest update stopped before digest composition")
            digest, digest_usage = daily_digest(
                [(paper, local, decision) for _, paper, local, decision in digest_input], cfg,
                language,
            )
            if getattr(context, "cancel_requested", lambda: False)():
                raise InterruptedError("arXiv digest update stopped after digest composition")
            with transaction(context.connection, immediate=True):
                _complete_run(context.connection, run_id, group_id, evaluated, digest, rerank_usage.plus(digest_usage))
                # A degraded API backfill must retry its old window next time;
                # advancing here could permanently skip papers absent from RSS.
                if advance_watermark and not _retrieval_degraded(stats):
                    _set_state(context.connection, "last_complete_at", utcnow())
                if announcement_key:
                    _set_state(context.connection, "last_digest_announcement", announcement_key)
                    _set_state(context.connection, "pending_announcement", "")
                _set_state(context.connection, "last_digest_fingerprint", evidence_fingerprint)
                _set_state(context.connection, "pending_evidence_fingerprint", "")
            stats["summary_run_id"] = run_id
            stats["evaluated_items"] = len(evaluated)
            stats["kept_items"] = sum(decision.decision == "keep" for _, _, _, decision in evaluated)
            if _retrieval_degraded(stats):
                stats["status"] = "partial"
            else:
                stats["status"] = "success"
            try:
                stats["arxiv_notifications"] = deliver_arxiv_pushes(
                    context.connection, cfg, [item_id for item_id, _, _, _ in evaluated], automatic=context.automatic,
                )
            except Exception as exc:
                LOGGER.exception("arXiv ntfy device-alert processing failed")
                stats["arxiv_notifications"] = {"status": "failed", "error": str(exc)[:2000]}
        except InterruptedError as exc:
            context.connection.execute(
                "UPDATE llm_runs SET completed_at=?,status='cancelled',error=? WHERE id=?",
                (utcnow(), str(exc)[:2000], run_id),
            )
            stats["status"] = "cancelled"
            stats["cancelled"] = True
            stats["message"] = "The arXiv announcement remains waiting for a later digest"
        except Exception as exc:
            message = _model_error(exc, cfg)
            context.connection.execute(
                "UPDATE llm_runs SET completed_at=?,status='failed',error=? WHERE id=?",
                (utcnow(), message, run_id),
            )
            stats["attempted"] = int(stats.get("attempted", 0)) + 1
            stats["failed"] = int(stats.get("failed", 0)) + 1
            stats["status"] = "llm-failed"
            stats["llm_error"] = message
        return stats

    def summarize(self, context: Any) -> dict[str, Any]:
        """Create one combined digest for a new daily arXiv announcement."""
        cfg = load_plugin_config(context.config)
        group_id, feeds = _ensure_sources(context.connection, cfg)
        selected = _selected_categories(context, group_id, feeds)
        # The specialist feature is deliberately one digest across every
        # configured category. A feed-scoped request may activate it, but it
        # must not create three separate daily digests.
        categories = list(feeds) if selected else []
        if not categories:
            return {"status": "out-of-scope", "attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0}
        stats: dict[str, Any] = {
            "status": "success", "attempted": 0, "succeeded": 0,
            "failed": 0, "new_items": 0, "categories": {}, "llm_calls": 0,
        }
        if getattr(context, "cancel_requested", lambda: False)():
            stats["status"] = "cancelled"
            return stats
        return self._evaluate_pending(
            context, cfg, group_id, feeds, categories, stats, advance_watermark=False,
            allow_model=True,
        )

    def refresh(self, context: Any) -> dict[str, Any]:
        cfg = load_plugin_config(context.config)
        group_id, feeds = _ensure_sources(context.connection, cfg)
        categories = _selected_categories(context, group_id, feeds)
        if not categories:
            return {"status": "out-of-scope", "attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0}
        stats: dict[str, Any] = {"status": "success", "attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0, "categories": {}, "llm_calls": 0}
        rss_papers: list[Paper] = []
        for index, category in enumerate(categories):
            if getattr(context, "cancel_requested", lambda: False)():
                stats["status"] = "cancelled"
                return stats
            stats["attempted"] += 1
            try:
                papers = fetch_rss(category, cfg)
                rss_papers.extend(papers)
                stats["succeeded"] += 1
                stats["categories"][category] = {"status": "success", "items": len(papers)}
                context.connection.execute(
                    """UPDATE feeds SET last_attempt_at=?,last_success_at=?,last_http_status=200,
                       last_error=NULL,consecutive_failures=0,next_retry_at=NULL WHERE id=?""",
                    (utcnow(), utcnow(), feeds[category]),
                )
            except Exception as exc:
                stats["failed"] += 1
                stats["categories"][category] = {"status": "failed", "error": str(exc)[:1000]}
                context.connection.execute(
                    """UPDATE feeds SET last_attempt_at=?,last_error=?,consecutive_failures=consecutive_failures+1
                       WHERE id=?""", (utcnow(), str(exc)[:1000], feeds[category]),
                )
            if index + 1 < len(categories):
                time.sleep(float(cfg["arxiv"].get("rss_pause_seconds", 0)))
        unseen_rss = any(not context.connection.execute(
            "SELECT 1 FROM distillfeed_arxiv_papers WHERE arxiv_id=?", (paper.arxiv_id,)
        ).fetchone() for paper in rss_papers)
        api_papers: list[Paper] = []
        api_interval = int(cfg["arxiv"].get("api_interval_hours", 20))
        last_api = _state(context.connection, "last_api_success_at")
        api_due = not last_api or datetime.fromisoformat(last_api) < datetime.now(UTC) - timedelta(hours=api_interval)
        if getattr(context, "cancel_requested", lambda: False)():
            stats["status"] = "cancelled"
            return stats
        if cfg["arxiv"].get("api_backfill_enabled", True) and (unseen_rss or api_due):
            last_complete = _state(context.connection, "last_complete_at")
            if last_complete:
                since = datetime.fromisoformat(last_complete) - timedelta(minutes=int(cfg["arxiv"].get("resume_overlap_minutes", 90)))
            else:
                since = datetime.now(UTC) - timedelta(days=int(cfg["arxiv"].get("initial_lookback_days", 3)))
            try:
                api_papers = fetch_api_window(categories, since, datetime.now(UTC), cfg)
                _set_state(context.connection, "last_api_success_at", utcnow())
                _set_state(context.connection, "last_api_error", "")
                stats["api_backfill"] = {"status": "success", "items": len(api_papers)}
            except Exception as exc:
                stats["backfill_degraded"] = True
                _set_state(context.connection, "last_api_error", str(exc)[:1000])
                stats["api_backfill"] = {"status": "degraded", "error": str(exc)[:1000]}
        merged = merge_papers(rss_papers, api_papers)
        stats["fetched_items"] = len(merged)
        created_item_ids: list[int] = []
        with transaction(context.connection, immediate=True):
            for paper in merged:
                needs_storage = _needs_storage(context.connection, paper)
                _record_seen(context.connection, paper)
                if not needs_storage:
                    stats["already_screened"] = int(stats.get("already_screened", 0)) + 1
                    continue
                item_id, created, needs_evaluation = _store_paper(
                    context.connection, paper, _paper_feed(paper, categories, feeds)
                )
                stats["new_items"] += int(created)
                if created:
                    created_item_ids.append(item_id)
                if needs_evaluation and not created:
                    stats["revised_items"] = int(stats.get("revised_items", 0)) + 1
        stats["_created_item_ids"] = created_item_ids
        return self._evaluate_pending(
            context, cfg, group_id, feeds, categories, stats, advance_watermark=True,
            allow_model=False,
        )

    def decorate_page(self, connection: Any, main_config: Any, data: dict[str, Any]) -> None:
        if not data.get("items"):
            return
        identifiers = [int(item["id"]) for item in data["items"]]
        marks = ",".join("?" for _ in identifiers)
        rows = connection.execute(
            f"SELECT * FROM distillfeed_arxiv_papers WHERE item_id IN ({marks})", identifiers,
        ).fetchall()
        metadata = {int(row["item_id"]): row for row in rows}
        if not metadata:
            return
        group_id, feeds = _ensure_sources(connection, load_plugin_config(main_config))
        if data.get("selected_group_id") == group_id or data.get("selected_feed_id") in set(feeds.values()):
            data["item_sort_profile"] = "relevance"
        for item in data["items"]:
            row = metadata.get(int(item["id"]))
            if not row:
                continue
            categories = json.loads(row["categories_json"] or "[]")
            tags = json.loads(row["tags_json"] or "[]")
            reasons = json.loads(row["local_reasons_json"] or "[]")
            llm_score = row["llm_score"]
            display_score = int(llm_score) if llm_score is not None else max(-1, min(100, int(row["local_score"] or -1) * 5))
            score_parts = [f"<span><strong>Local</strong> {escape(row['local_score'] if row['local_score'] is not None else 'pending')}</span>"]
            score_parts.append(f"<span><strong>AI</strong> {escape(llm_score if llm_score is not None else 'pending')}</span>")
            final_display = f"{float(row['final_score']):.1f}" if row["final_score"] is not None else "pending"
            score_parts.append(f"<span><strong>Final</strong> {escape(final_display)}</span>")
            score_parts.append(f"<span><strong>Decision</strong> {escape(row['decision'] or 'pending')}</span>")
            tags_html = " ".join(f"<span class=\"tag\">{escape(tag)}</span>" for tag in tags[:4])
            pdf = f" · <a href=\"{escape(row['pdf_url'])}\" target=\"_blank\" rel=\"noopener noreferrer\">PDF</a>" if row["pdf_url"] else ""
            item["plugin_html"] = Markup(
                f"<div class=\"plugin-card\">"
                f"<a class=\"plugin-title item-title\" href=\"{escape(item['url'])}\" target=\"_blank\" rel=\"noopener noreferrer\">{escape(item['title'])}</a>"
                f"<div class=\"plugin-meta\"><strong>Authors:</strong> {escape(item['author'] or 'Unknown authors')}</div>"
                f"<div class=\"plugin-meta\"><strong>Categories:</strong> {escape(', '.join(categories))} · <strong>arXiv:</strong> {escape(row['arxiv_id'])}{pdf}</div>"
                f"<div class=\"plugin-scoreline\">{''.join(score_parts)}</div>"
                f"<div class=\"plugin-reason\"><strong>Why relevant:</strong> {escape(row['why'] or 'Waiting for evaluation')}</div>"
                f"<div class=\"plugin-reason\"><strong>Local rationale:</strong> {escape('; '.join(reasons[:5]) or 'Waiting for local scoring')}</div>"
                f"<div>{tags_html}</div>"
                f"<details class=\"plugin-details\"><summary>Abstract</summary><div class=\"plugin-details-body\">{escape(item['description_text'] or 'No abstract available.')}</div></details>"
                f"</div>"
            )
            item["display_relevance"] = display_score


plugin = ArxivDigestPlugin()
