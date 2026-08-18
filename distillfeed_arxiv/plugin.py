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
    # Older releases retained only a compact "seen" record for papers that did
    # not reach the LLM shortlist.  Store them again whenever retrieval sees
    # them so the complete announcement becomes browseable.
    return True


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
               pdf_url=?,announce_type=?,source=? WHERE item_id=?""",
            (paper.version, json.dumps(paper.categories, ensure_ascii=False), paper.primary_category,
             paper.pdf_link, paper.announce_type, paper.source, item_id),
        )
        if revised:
            # A revised paper is new evidence. Never display or reuse its previous
            # model result while it is waiting to be ranked again.
            connection.execute(
                """UPDATE distillfeed_arxiv_papers
                      SET local_score=NULL,llm_score=NULL,final_score=NULL,decision=NULL,why=NULL,
                          tags_json='[]',local_reasons_json='[]',evaluation_status='pending',evaluated_at=NULL
                    WHERE item_id=?""",
                (item_id,),
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


def _paper_from_row(row: Any) -> Paper:
    published = datetime.fromisoformat(row["published_at"]) if row["published_at"] else None
    return Paper(
        arxiv_id=row["arxiv_id"], version=row["version"], title=row["title"],
        abstract=row["description_text"],
        authors=[value.strip() for value in str(row["author"] or "").split(",") if value.strip()],
        categories=json.loads(row["categories_json"]), primary_category=row["primary_category"],
        link=row["url"], pdf_link=row["pdf_url"], published=published, updated=published,
        source=row["source"], announce_type=row["announce_type"],
    )


def _paper_day(paper: Paper) -> str:
    stamp = paper.published or paper.updated
    return stamp.astimezone(UTC).date().isoformat() if stamp else "undated"


def _pending(connection: Any, feed_ids: list[int]) -> list[tuple[int, Paper]]:
    marks = ",".join("?" for _ in feed_ids)
    rows = connection.execute(
        f"""SELECT ap.*,i.feed_id,i.title,i.url,i.author,i.published_at,i.description_text
            FROM distillfeed_arxiv_papers ap JOIN items i ON i.id=ap.item_id
            WHERE ap.evaluation_status='pending' AND i.feed_id IN ({marks})
            ORDER BY COALESCE(i.published_at,i.discovered_at),i.id""", feed_ids,
    ).fetchall()
    return [(int(row["item_id"]), _paper_from_row(row)) for row in rows]


def _stored_day_evaluations(
    connection: Any, feed_ids: list[int], day: str, broad_threshold: int,
) -> list[tuple[int, Paper, LocalScore, Decision]]:
    """Load already AI-ranked papers for one day without re-querying the model."""
    marks = ",".join("?" for _ in feed_ids)
    rows = connection.execute(
        f"""SELECT ap.*,i.feed_id,i.title,i.url,i.author,i.published_at,i.description_text
              FROM distillfeed_arxiv_papers ap JOIN items i ON i.id=ap.item_id
             WHERE i.feed_id IN ({marks})
               AND substr(COALESCE(i.published_at,i.discovered_at),1,10)=?
               AND ap.evaluation_status='complete'
               AND ap.llm_score IS NOT NULL
               AND ap.local_score>=?
             ORDER BY ap.llm_score DESC,ap.local_score DESC,i.id""",
        [*feed_ids, day, broad_threshold],
    ).fetchall()
    result: list[tuple[int, Paper, LocalScore, Decision]] = []
    for row in rows:
        reasons = json.loads(row["local_reasons_json"] or "[]")
        tags = json.loads(row["tags_json"] or "[]")
        local = LocalScore(score=int(row["local_score"]), reasons=[str(value) for value in reasons])
        decision = Decision(
            local_score=int(row["local_score"]), llm_score=int(row["llm_score"]),
            final_score=float(row["final_score"] if row["final_score"] is not None else row["local_score"]),
            decision=str(row["decision"] or "drop"), why=str(row["why"] or ""),
            tags=[str(value) for value in tags], local_reasons=[str(value) for value in reasons],
        )
        result.append((int(row["item_id"]), _paper_from_row(row), local, decision))
    return result


def _group_scored_by_day(
    scored: list[tuple[int, Paper, LocalScore]],
) -> dict[str, list[tuple[int, Paper, LocalScore]]]:
    grouped: dict[str, list[tuple[int, Paper, LocalScore]]] = {}
    for entry in scored:
        grouped.setdefault(_paper_day(entry[1]), []).append(entry)
    return grouped


def _dedicated_digest_days(connection: Any, group_id: int, feed_ids: list[int]) -> set[str]:
    """Return days that already have a successful single-day arXiv summary.

    Older releases could write one summary containing several announcement days.
    Such a summary is intentionally *not* considered a dedicated daily digest so
    a one-time manual recovery can rebuild those dates independently.
    """
    marks = ",".join("?" for _ in feed_ids)
    rows = connection.execute(
        f"""SELECT s.id,
                    MIN(substr(COALESCE(i.published_at,i.discovered_at),1,10)) AS first_day,
                    MAX(substr(COALESCE(i.published_at,i.discovered_at),1,10)) AS last_day
               FROM summaries s
               JOIN llm_runs lr ON lr.id=s.llm_run_id
               JOIN summary_items si ON si.summary_id=s.id
               JOIN items i ON i.id=si.item_id
              WHERE lr.status='success'
                AND lr.prompt_version LIKE 'distillfeed-arxiv-%'
                AND CASE WHEN s.scope_id IS NOT NULL THEN s.scope_kind
                         WHEN s.scope_feed_id IS NOT NULL THEN 'feed' ELSE 'group' END='group'
                AND COALESCE(s.scope_id,s.group_id)=?
                AND i.feed_id IN ({marks})
              GROUP BY s.id
             HAVING first_day=last_day""",
        [group_id, *feed_ids],
    ).fetchall()
    return {str(row["first_day"]) for row in rows if row["first_day"]}


def _missing_daily_digest_days(
    connection: Any, group_id: int, feed_ids: list[int], broad_threshold: int,
) -> list[str]:
    """Find historical AI-scored days that never received a true daily digest."""
    dedicated = _dedicated_digest_days(connection, group_id, feed_ids)
    marks = ",".join("?" for _ in feed_ids)
    rows = connection.execute(
        f"""SELECT DISTINCT substr(COALESCE(i.published_at,i.discovered_at),1,10) AS digest_day
              FROM distillfeed_arxiv_papers ap
              JOIN items i ON i.id=ap.item_id
             WHERE i.feed_id IN ({marks})
               AND ap.evaluation_status='complete'
               AND ap.llm_score IS NOT NULL
               AND ap.local_score>=?
             ORDER BY digest_day""",
        [*feed_ids, broad_threshold],
    ).fetchall()
    return [str(row["digest_day"]) for row in rows if row["digest_day"] and str(row["digest_day"]) not in dedicated]


def _evidence_fingerprint(
    evidence: list[tuple[int, Paper]],
    cfg: dict[str, Any],
    categories: list[str],
    language: str,
) -> str:
    """Identify one day's paper evidence and ranking/digest policy."""
    days = {_paper_day(paper) for _, paper in evidence}
    if len(days) > 1:
        raise ValueError("A daily arXiv fingerprint cannot span multiple publication days")
    llm = cfg["llm"]
    material = {
        "announcement": next(iter(days), None),
        "papers": sorted(
            [str(paper.arxiv_id), str(paper.version or "")]
            for _, paper in evidence
        ),
        "categories": sorted(str(value) for value in categories),
        "language": str(language),
        "filters": cfg["filters"],
        "llm": {
            key: llm.get(key)
            for key in (
                "model", "ranking_batch_size",
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


def _requeue_ai_eligible_without_score(connection: Any, cfg: dict[str, Any]) -> int:
    """Restore the invariant that every locally eligible paper receives AI scoring.

    Releases through 0.23.7 could mark above-threshold papers ``screened_out``
    solely because the announcement exceeded ``llm.max_candidates``. Those rows
    were then invisible to retry because only ``pending`` rows are evaluated.
    Requeue terminal rows whose stored local score meets the current AI threshold.
    """
    threshold = int(cfg["filters"].get("broad_candidate_threshold", 0))
    cursor = connection.execute(
        """UPDATE distillfeed_arxiv_papers
              SET llm_score=NULL,final_score=NULL,decision=NULL,why=NULL,tags_json='[]',
                  evaluation_status='pending',evaluated_at=NULL
            WHERE llm_score IS NULL
              AND local_score IS NOT NULL
              AND local_score>=?
              AND evaluation_status IN ('screened_out','complete')""",
        (threshold,),
    )
    return max(int(cursor.rowcount), 0)


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
    ranked = sorted(
        evaluated,
        key=lambda entry: (
            entry[3].decision != "keep",
            -(entry[3].llm_score if entry[3].llm_score is not None else -1),
            -entry[3].local_score,
            -entry[3].final_score,
        ),
    )
    for rank, (item_id, paper, local, decision) in enumerate(ranked, 1):
        if decision.llm_score is None:
            raise RuntimeError("Cannot publish an arXiv daily digest item without an AI score")
        importance = int(decision.llm_score)
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
        restored = _requeue_ai_eligible_without_score(connection, cfg)
        if restored:
            LOGGER.info(
                "Requeued %d arXiv paper(s) that were locally eligible but had no AI score",
                restored,
            )
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
        """Process all remaining arXiv work while preserving one digest per day.

        The local threshold defines AI eligibility. ``ranking_batch_size`` only
        bounds provider requests; it never truncates the day's eligible set.
        Historical terminal rows that should have received an AI score are
        requeued during plugin initialization. In addition, a one-time recovery
        creates a dedicated daily digest for older AI-scored days that were
        previously merged into a multi-day summary.
        """
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

        feed_ids = [feeds[category] for category in categories]
        pending = _pending(context.connection, feed_ids)
        broad = int(cfg["filters"].get("broad_candidate_threshold", 0))
        scored = [(item_id, paper, compute_local_score(paper, cfg)) for item_id, paper in pending]
        shortlisted = sorted(
            [entry for entry in scored if entry[2].score >= broad],
            key=lambda entry: (_paper_day(entry[1]), -entry[2].score, entry[0]),
        )
        shortlisted_ids = {item_id for item_id, _, _ in shortlisted}
        retained_ids = {item_id for item_id, _, _ in scored}
        if scored:
            with transaction(context.connection, immediate=True):
                for item_id, paper, local in scored:
                    selected = item_id in shortlisted_ids
                    _record_seen(context.connection, paper, local_score=local.score, selected=selected)
                    if selected:
                        context.connection.execute(
                            """UPDATE distillfeed_arxiv_papers
                                  SET local_score=?,local_reasons_json=?,evaluation_status='pending'
                                WHERE item_id=?""",
                            (local.score, json.dumps(local.reasons, ensure_ascii=False), item_id),
                        )
                        continue
                    context.connection.execute(
                        """UPDATE distillfeed_arxiv_papers SET local_score=?,local_reasons_json=?,
                           llm_score=NULL,final_score=NULL,decision='drop',
                           why='Screened out below the local AI threshold',tags_json='[]',
                           evaluation_status='screened_out',evaluated_at=? WHERE item_id=?""",
                        (local.score, json.dumps(local.reasons, ensure_ascii=False), utcnow(), item_id),
                    )

        stats["screened_locally"] = len(scored) - len(shortlisted)
        stats["selected_for_llm"] = len(shortlisted)
        stats["new_items"] = len(created_item_ids.intersection(retained_ids))

        pending_by_day = _group_scored_by_day(shortlisted)
        missing_digest_days = _missing_daily_digest_days(
            context.connection, group_id, feed_ids, broad,
        )
        work_days = sorted(set(pending_by_day).union(missing_digest_days))
        stats["backlog_days"] = len(work_days)
        stats["announcements"] = work_days
        stats["missing_daily_digests"] = len(missing_digest_days)

        if not work_days:
            if advance_watermark and not _retrieval_degraded(stats):
                _set_state(context.connection, "last_complete_at", utcnow())
            stats["status"] = "partial" if _retrieval_degraded(stats) else (
                "unchanged" if stats.get("new_items", 0) == 0 else "success"
            )
            stats["evaluated_items"] = 0
            stats["kept_items"] = 0
            stats["daily_digests"] = 0
            if stats["status"] == "unchanged":
                stats["message"] = "The daily arXiv digest is already up to date"
            return stats

        latest_day = work_days[-1]
        stats["announcement"] = latest_day
        _set_state(context.connection, "pending_announcement", latest_day)

        if not allow_model:
            stats["status"] = "waiting-for-digest"
            stats["evaluated_items"] = 0
            stats["kept_items"] = 0
            stats["daily_digests"] = 0
            stats["message"] = (
                f"{len(work_days)} arXiv day{'s are' if len(work_days) != 1 else ' is'} waiting for AI completion"
            )
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
        language = str(context.config.get("app", "summary_language", "English"))
        batch_size = max(1, int(cfg["llm"].get("ranking_batch_size", 20)))
        total_evaluated = 0
        total_kept = 0
        total_calls = 0
        run_ids: list[int] = []
        completed_days: list[str] = []
        notification_results: list[dict[str, Any]] = []

        for day in work_days:
            if getattr(context, "cancel_requested", lambda: False)():
                stats.update({
                    "status": "cancelled", "cancelled": True,
                    "message": "The remaining arXiv days are still waiting for a later update",
                })
                break

            day_scored = pending_by_day.get(day, [])
            existing = _stored_day_evaluations(context.connection, feed_ids, day, broad)
            evidence_map: dict[int, tuple[int, Paper]] = {
                item_id: (item_id, paper) for item_id, paper, _local, _decision in existing
            }
            evidence_map.update({item_id: (item_id, paper) for item_id, paper, _local in day_scored})
            evidence = list(evidence_map.values())
            if not evidence:
                # Defensive: a day can disappear only if rows were concurrently
                # removed. The job lock should make this unreachable, but do not
                # create an empty provider request if it ever occurs.
                continue
            evidence_fingerprint = _evidence_fingerprint(evidence, cfg, categories, language)
            _set_state(context.connection, "pending_announcement", day)
            _set_state(context.connection, "pending_evidence_fingerprint", evidence_fingerprint)
            run_id = _start_run(
                context.connection,
                [item_id for item_id, _, _ in day_scored],
                cfg, evidence_fingerprint,
            )
            run_ids.append(run_id)
            rerank_usage = LLMUsage()
            try:
                newly_evaluated: list[tuple[int, Paper, LocalScore, Decision]] = []
                if day_scored:
                    candidates = [(paper, local) for _, paper, local in day_scored]
                    total_calls += (len(candidates) + batch_size - 1) // batch_size
                    reranked, rerank_usage = rerank(
                        candidates, cfg,
                        cancel_requested=getattr(context, "cancel_requested", lambda: False),
                    )
                    expected_ids = {paper.arxiv_id for _, paper, _ in day_scored}
                    if set(reranked) != expected_ids:
                        raise RuntimeError("arXiv reranker did not return every pending paper exactly once")
                    newly_evaluated = [
                        (item_id, paper, local, decide(local, reranked[paper.arxiv_id], cfg))
                        for item_id, paper, local in day_scored
                    ]
                    if any(decision.llm_score is None for _, _, _, decision in newly_evaluated):
                        raise RuntimeError("A locally eligible arXiv paper completed without an AI score")

                merged: dict[int, tuple[int, Paper, LocalScore, Decision]] = {
                    entry[0]: entry for entry in existing
                }
                merged.update({entry[0]: entry for entry in newly_evaluated})
                day_evaluated = sorted(
                    merged.values(),
                    key=lambda entry: (
                        entry[3].decision != "keep",
                        -(entry[3].llm_score if entry[3].llm_score is not None else -1),
                        -entry[3].local_score,
                        entry[0],
                    ),
                )
                if not day_evaluated:
                    raise RuntimeError(f"No AI-scored arXiv papers are available for {day}")
                digest_input = [entry for entry in day_evaluated if entry[3].decision == "keep"]
                if not digest_input:
                    digest_input = day_evaluated[: min(25, len(day_evaluated))]
                if getattr(context, "cancel_requested", lambda: False)():
                    raise InterruptedError("arXiv update stopped before daily digest composition")
                total_calls += 1
                digest, digest_usage = daily_digest(
                    [(paper, local, decision) for _, paper, local, decision in digest_input],
                    cfg, language,
                )
                if getattr(context, "cancel_requested", lambda: False)():
                    raise InterruptedError("arXiv update stopped after daily digest composition")
                with transaction(context.connection, immediate=True):
                    _complete_run(
                        context.connection, run_id, group_id, day_evaluated, digest,
                        rerank_usage.plus(digest_usage),
                    )
                    _set_state(context.connection, "last_digest_announcement", day)
                    _set_state(context.connection, "last_digest_fingerprint", evidence_fingerprint)
                    _set_state(context.connection, "pending_announcement", "")
                    _set_state(context.connection, "pending_evidence_fingerprint", "")
                completed_days.append(day)
                total_evaluated += len(newly_evaluated)
                total_kept += sum(
                    decision.decision == "keep"
                    for _, _, _, decision in newly_evaluated
                )
                if newly_evaluated:
                    try:
                        notification_results.append(deliver_arxiv_pushes(
                            context.connection, cfg,
                            [item_id for item_id, _, _, _ in newly_evaluated],
                            automatic=context.automatic,
                        ))
                    except Exception as exc:
                        LOGGER.exception("arXiv ntfy device-alert processing failed")
                        notification_results.append({"status": "failed", "error": str(exc)[:2000]})
            except InterruptedError as exc:
                context.connection.execute(
                    "UPDATE llm_runs SET completed_at=?,status='cancelled',error=? WHERE id=?",
                    (utcnow(), str(exc)[:2000], run_id),
                )
                stats.update({
                    "status": "cancelled", "cancelled": True,
                    "message": "The remaining arXiv days are still waiting for a later update",
                })
                break
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
                stats["failed_announcement"] = day
                break

        stats["llm_calls"] = total_calls
        stats["summary_run_ids"] = run_ids
        if run_ids:
            stats["summary_run_id"] = run_ids[-1]
        stats["evaluated_items"] = total_evaluated
        stats["kept_items"] = total_kept
        stats["daily_digests"] = len(completed_days)
        stats["completed_announcements"] = completed_days
        if notification_results:
            stats["arxiv_notifications"] = notification_results[-1]
            stats["arxiv_notification_runs"] = notification_results

        if stats.get("status") in {"cancelled", "llm-failed"}:
            return stats
        if advance_watermark and not _retrieval_degraded(stats):
            _set_state(context.connection, "last_complete_at", utcnow())
        stats["status"] = "partial" if _retrieval_degraded(stats) else "success"
        stats["message"] = (
            f"Completed {len(completed_days)} arXiv daily update"
            f"{'s' if len(completed_days) != 1 else ''}"
        )
        return stats

    def summarize(self, context: Any) -> dict[str, Any]:
        """Complete every waiting arXiv day as an independent daily digest."""
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
            # "AI relevance" must mean an actual model score.  Older reader
            # decoration mapped local_score * 5 into the same numeric channel,
            # which interleaved papers that had never been AI-ranked with real
            # AI results.  Keep local score as a secondary ordering signal only.
            display_score = int(llm_score) if llm_score is not None else None
            ai_display: int | str
            if llm_score is not None:
                ai_display = int(llm_score)
            elif row["evaluation_status"] == "screened_out":
                ai_display = "not sent"
            else:
                ai_display = "pending"
            score_parts = [f"<span><strong>Local</strong> {escape(row['local_score'] if row['local_score'] is not None else 'pending')}</span>"]
            score_parts.append(f"<span><strong>AI</strong> {escape(ai_display)}</span>")
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
            item["display_local_relevance"] = int(row["local_score"]) if row["local_score"] is not None else None


plugin = ArxivDigestPlugin()
