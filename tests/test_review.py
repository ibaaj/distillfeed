from __future__ import annotations

import json
from pathlib import Path

import pytest

from rss_reader.db import connect, initialize, transaction
from rss_reader.review import (
    _duplicates_source,
    _matches,
    finish_review_day,
    list_review_day_items,
    list_review_days,
    parse_review_filters,
    resolve_review_scope,
    review_item_details,
)

TODAY = "2026-08-18"
YESTERDAY = "2026-08-17"

ARXIV_SCHEMA = """
CREATE TABLE distillfeed_arxiv_papers (
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
"""


def _build_database(path: Path) -> None:
    initialize(path)
    with connect(path) as connection, transaction(connection, immediate=True):
        connection.executescript(ARXIV_SCHEMA)
        connection.execute(
            "INSERT INTO groups(id,parent_id,title,position,created_at) VALUES(1,NULL,'arXiv Digest',0,?)",
            (f"{TODAY}T00:00:00+00:00",),
        )
        feeds = [
            (10, "cs.AI", "plugin://arxiv/cs.AI"),
            (11, "cs.LG", "plugin://arxiv/cs.LG"),
            (12, "cs.LO", "plugin://arxiv/cs.LO"),
        ]
        for feed_id, title, xml_url in feeds:
            connection.execute(
                """INSERT INTO feeds(
                       id,group_id,title,xml_url,enabled,llm_enabled,ai_mode,created_at
                   ) VALUES(?,1,?,?,1,1,'inherit',?)""",
                (feed_id, title, xml_url, f"{TODAY}T00:00:00+00:00"),
            )

        item_rows = []
        arxiv_rows = []
        for identifier in range(1, 845):
            day = TODAY if identifier <= 837 else YESTERDAY
            feed_id = 10 + identifier % 3
            title = (
                f"Causal reasoning paper {identifier}"
                if identifier % 17 == 0
                else f"Reasoning paper {identifier}"
            )
            abstract = f"Abstract for paper {identifier}. Symbolic reasoning and uncertainty."
            published = f"{day}T{identifier % 24:02d}:{identifier % 60:02d}:00+00:00"
            item_rows.append((
                identifier, feed_id, f"stable-{identifier}", title,
                f"https://arxiv.org/abs/2608.{identifier:05d}",
                f"Author {identifier % 9}", published, published, abstract,
                1, int(identifier % 3 == 0), int(identifier % 11 == 0),
                int(identifier % 13 == 0),
            ))
            state = identifier % 4
            local = identifier % 15
            if state == 0:
                score = 90 - identifier % 10
                decision = "keep"
                status = "complete"
            elif state == 1:
                score = 35 + identifier % 25
                decision = "drop"
                status = "complete"
            elif state == 2:
                score = None
                decision = None
                status = "pending"
            else:
                score = None
                decision = "drop"
                status = "screened_out"
            arxiv_rows.append((
                identifier, f"2608.{identifier:05d}", "v1", '["cs.AI"]', "cs.AI",
                f"https://arxiv.org/pdf/2608.{identifier:05d}", "new", "rss", local,
                score, float(local + (score or 0) * 0.35) if score is not None else None,
                decision, f"Relevance rationale for paper {identifier}" if score is not None else "",
                json.dumps(["reasoning", f"tag-{identifier % 5}"]),
                json.dumps([f"local signal {identifier % 3}"]), status,
                published if score is not None else None,
            ))
        connection.executemany(
            """INSERT INTO items(
                   id,feed_id,stable_id,title,url,author,published_at,discovered_at,
                   description_text,summary_eligible,is_read,is_starred,is_read_later
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            item_rows,
        )
        connection.executemany(
            """INSERT INTO distillfeed_arxiv_papers(
                   item_id,arxiv_id,version,categories_json,primary_category,pdf_url,
                   announce_type,source,local_score,llm_score,final_score,decision,why,
                   tags_json,local_reasons_json,evaluation_status,evaluated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            arxiv_rows,
        )

        connection.execute(
            """INSERT INTO llm_runs(
                   id,request_key,started_at,completed_at,status,model,prompt_version,
                   pricing_json,stage
               ) VALUES(1,'daily-brief',?,?, 'success','gpt-test','distillfeed-arxiv-1','{}','summary')""",
            (f"{TODAY}T10:00:00+00:00", f"{TODAY}T10:01:00+00:00"),
        )
        sections = [{
            "heading": "Neuro-symbolic reasoning",
            "body": (
                "- 2608.00004 — *Certified reasoning*: First result. "
                "- 2608.00008 — *Probabilistic logic*: Second result."
            ),
        }]
        connection.execute(
            """INSERT INTO summaries(
                   id,llm_run_id,group_id,scope_kind,scope_id,policy_hash,overview,
                   changes,sections_json,created_at
               ) VALUES(1,1,1,'group',1,'test','**Daily overview** for the selected papers.',
                        '',?,?)""",
            (json.dumps(sections), f"{TODAY}T10:01:00+00:00"),
        )
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description,justification,
                   story_cluster
               ) VALUES(1,4,1,1,90,?,?,?)""",
            (
                "Abstract for paper 4. Symbolic reasoning and uncertainty.",
                "Relevance rationale for paper 4", "Reasoning",
            ),
        )
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description,justification,
                   story_cluster
               ) VALUES(1,8,1,2,88,?,?,?)""",
            ("A distinct AI-written item synopsis.", "Relevance rationale for paper 8", "Logic"),
        )


@pytest.fixture()
def review_db(tmp_path: Path) -> Path:
    path = tmp_path / "review.sqlite3"
    _build_database(path)
    return path


def _all_day_items(connection, scope, day: str, filters: dict) -> list[dict]:
    result: list[dict] = []
    cursor = ""
    while True:
        page = list_review_day_items(
            connection, scope, day, filters,
            minimum_relevance=70, cursor=cursor,
        )
        result.extend(page["items"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        assert cursor
    return result


def test_markdown_brief_and_duplicate_abstract_are_rendered_correctly(review_db: Path):
    with connect(review_db) as connection:
        scope = resolve_review_scope(connection, group_id=1)
        filters = parse_review_filters(
            {"preset": "everything", "from": TODAY, "to": TODAY},
            today=TODAY,
        )
        payload = list_review_days(connection, scope, filters, minimum_relevance=70)
        brief = payload["days"][0]["brief"]
        assert brief["selected_count"] == 2
        assert "<strong>Daily overview</strong>" in brief["html"]
        assert "<h4>Neuro-symbolic reasoning</h4>" in brief["html"]
        assert brief["html"].count("<li>") == 2
        assert "<em>Certified reasoning</em>" in brief["html"]

        duplicate = review_item_details(connection, scope, 4, minimum_relevance=70)
        assert duplicate is not None
        assert duplicate["summary_html"] == ""
        assert duplicate["source_label"] == "Paper abstract"
        assert "Abstract for paper 4" in duplicate["source_html"]

        distinct = review_item_details(connection, scope, 8, minimum_relevance=70)
        assert distinct is not None
        assert "distinct AI-written item synopsis" in distinct["summary_html"]
        assert "Abstract for paper 8" in distinct["source_html"]


def test_duplicate_source_detection_handles_legacy_truncated_abstracts_without_hiding_real_summaries():
    source = " ".join(["A long source abstract about symbolic reasoning and calibrated uncertainty."] * 8)
    truncated = source[:260]
    assert _duplicates_source(source, source)
    assert _duplicates_source(truncated, source)
    assert not _duplicates_source(
        "A concise synthesis emphasizing the paper's main causal contribution.",
        source,
    )


def test_date_filters_exclude_undated_items_instead_of_comparing_the_label_lexically():
    item = {
        "search_text": "", "is_read": False, "is_starred": False,
        "is_read_later": False, "ai_state": "not-sent", "decision": "",
        "feed_id": 10, "ai_score": None, "day": "undated",
    }
    filters = parse_review_filters(
        {"preset": "everything", "from": "2026-08-01"}, today=TODAY,
    )
    assert not _matches(item, filters)


def test_cursor_walk_returns_837_unique_stably_ordered_items(review_db: Path):
    with connect(review_db) as connection:
        scope = resolve_review_scope(connection, group_id=1)
        filters = parse_review_filters(
            {"preset": "everything", "from": TODAY, "to": TODAY, "page_size": 50, "sort": "ai"},
            today=TODAY,
        )
        items = _all_day_items(connection, scope, TODAY, filters)
        assert len(items) == 837
        assert len({item["id"] for item in items}) == 837
        cohorts = [item["cohort"] for item in items]
        assert cohorts == sorted(cohorts)
        from rss_reader.review import _sort_key
        assert [_sort_key(item, "ai") for item in items] == sorted(
            _sort_key(item, "ai") for item in items
        )
        assert all(item["ai_score"] is not None for item in items if item["cohort"] <= 2)
        assert all(item["ai_score"] is None for item in items if item["cohort"] >= 3)
        assert all(item["decision"] == "" for item in items if item["cohort"] >= 3)


def test_every_filter_and_preset_matches_its_contract(review_db: Path):
    with connect(review_db) as connection:
        scope = resolve_review_scope(connection, group_id=1)

        cases = {
            "best-unread": lambda item: (
                not item["is_read"] and item["ai_state"] == "scored"
                and item["decision"] == "keep" and item["ai_score"] >= 70
            ),
            "catch-up": lambda item: not item["is_read"],
            "today": lambda item: item["day"] == TODAY,
            "awaiting-ai": lambda item: item["ai_state"] == "pending",
            "starred": lambda item: item["is_starred"],
            "everything": lambda item: True,
        }
        base_all = list_review_days(
            connection, scope,
            parse_review_filters({"preset": "everything"}, today=TODAY),
            minimum_relevance=70,
        )["counts"]["total"]
        assert base_all == 844

        # Presets are checked against the full authoritative count.
        from rss_reader.review import _review_rows  # private helper is intentional for the oracle
        authoritative = _review_rows(connection, scope, minimum_relevance=70)
        for preset, predicate in cases.items():
            filters = parse_review_filters({"preset": preset}, default_min_ai=70, today=TODAY)
            payload = list_review_days(connection, scope, filters, minimum_relevance=70)
            assert payload["counts"]["matching"] == sum(predicate(item) for item in authoritative), preset

        advanced = [
            ({"q": "causal"}, lambda item: "causal" in item["search_text"]),
            ({"read": "read"}, lambda item: item["is_read"]),
            ({"saved": "read-later"}, lambda item: item["is_read_later"]),
            ({"ai": "not-sent"}, lambda item: item["ai_state"] == "not-sent"),
            ({"decision": "drop"}, lambda item: item["decision"] == "drop"),
            ({"min_ai": 80}, lambda item: item["ai_score"] is not None and item["ai_score"] >= 80),
            ({"source": 10}, lambda item: item["feed_id"] == 10),
            ({"from": YESTERDAY, "to": YESTERDAY}, lambda item: item["day"] == YESTERDAY),
        ]
        for values, predicate in advanced:
            filters = parse_review_filters({"preset": "everything", **values}, today=TODAY)
            payload = list_review_days(connection, scope, filters, minimum_relevance=70)
            assert payload["counts"]["matching"] == sum(predicate(item) for item in authoritative), values

        for mode in ("ai", "date", "local"):
            filters = parse_review_filters(
                {"preset": "everything", "from": TODAY, "to": TODAY, "sort": mode, "page_size": 25},
                today=TODAY,
            )
            first = list_review_day_items(
                connection, scope, TODAY, filters, minimum_relevance=70,
            )
            assert first["items"]
            assert len(first["items"]) == 25

        with pytest.raises(ValueError, match="outside this review scope"):
            list_review_days(
                connection, scope,
                parse_review_filters({"preset": "everything", "source": 999}, today=TODAY),
                minimum_relevance=70,
            )

        conflicting = parse_review_filters({
            "preset": "custom", "ai": "pending", "min_ai": 90,
            "decision": "drop",
        }, today=TODAY)
        assert conflicting["ai"] == "pending"
        assert conflicting["min_ai"] == 0
        assert conflicting["decision"] == "all"


def test_finish_day_is_atomic_and_idempotent(review_db: Path):
    with connect(review_db) as connection:
        scope = resolve_review_scope(connection, group_id=1)
        before = connection.execute(
            "SELECT COUNT(*) FROM items WHERE substr(published_at,1,10)=? AND is_read=0",
            (TODAY,),
        ).fetchone()[0]
        with transaction(connection, immediate=True):
            first = finish_review_day(connection, scope, TODAY)
        assert first["matched"] == 837
        assert first["changed"] == before
        assert first["unread"] == 0
        with transaction(connection, immediate=True):
            second = finish_review_day(connection, scope, TODAY)
        assert second == {"status": "ok", "day": TODAY, "matched": 837, "changed": 0, "unread": 0}
        # A neighboring day is untouched.
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE substr(published_at,1,10)=? AND is_read=0",
            (YESTERDAY,),
        ).fetchone()[0] > 0


def test_review_display_mode_is_group_scoped_and_inherited_by_feeds(review_db: Path):
    with connect(review_db) as connection:
        connection.execute(
            "UPDATE groups SET review_display_mode='direct' WHERE id=1"
        )
        group_scope = resolve_review_scope(connection, group_id=1)
        feed_scope = resolve_review_scope(connection, feed_id=10)
        assert group_scope.preference_group_id == 1
        assert feed_scope.preference_group_id == 1
        assert group_scope.review_display_mode == "direct"
        assert feed_scope.review_display_mode == "direct"

        filters = parse_review_filters({"preset": "catch-up"}, today=TODAY)
        group_payload = list_review_days(
            connection, group_scope, filters, minimum_relevance=70,
        )
        feed_payload = list_review_days(
            connection, feed_scope, filters, minimum_relevance=70,
        )
        assert group_payload["scope"]["preference_group_id"] == 1
        assert feed_payload["scope"]["preference_group_id"] == 1
        assert group_payload["scope"]["review_display_mode"] == "direct"
        assert feed_payload["scope"]["review_display_mode"] == "direct"
