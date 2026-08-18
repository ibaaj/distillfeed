from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from distillfeed_arxiv.config import load_plugin_config
from distillfeed_arxiv.llm import LLMUsage
from distillfeed_arxiv.models import LocalScore, Paper
from distillfeed_arxiv.notifications import deliver_arxiv_pushes
from distillfeed_arxiv.plugin import ArxivDigestPlugin
from rss_reader.config import DEFAULTS, Config, load_config, save_config
from rss_reader.db import connect, initialize, utcnow
from rss_reader.plugins import RefreshContext, available_plugin_names, refresh_plugins
from rss_reader.opml import build_tree_from_database, parse_opml_bytes, serialize_opml
from rss_reader.service import run_update_summaries
from rss_reader.web import _group_tree, create_app
import distillfeed_arxiv.llm as llm_module
import distillfeed_arxiv.fetch as fetch_module
import distillfeed_arxiv.notifications as notification_module
import distillfeed_arxiv.plugin as plugin_module


def test_arxiv_api_backfill_encodes_boolean_date_query_as_syntax(monkeypatch):
    requested: list[str] = []
    monkeypatch.setattr(
        fetch_module,
        "_get_text",
        lambda url, **_kwargs: requested.append(url) or (
            '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        ),
    )
    monkeypatch.setattr(fetch_module.time, "sleep", lambda _seconds: None)
    cfg = {
        "app": {"user_agent": "DistillFeed test"},
        "arxiv": {"api_page_size": 100, "api_pause_seconds": 0},
    }

    assert fetch_module.fetch_api_window(
        ["cs.AI"],
        datetime(2026, 7, 12, 10, 11, 12, tzinfo=UTC),
        datetime(2026, 7, 19, 13, 14, 15, tzinfo=UTC),
        cfg,
    ) == []

    query = parse_qs(urlparse(requested[0]).query)["search_query"][0]
    assert query == (
        "cat:cs.AI AND submittedDate:"
        "[202607121011 TO 202607191314]"
    )
    assert "%2BAND%2B" not in requested[0]
    assert re.search(r"submittedDate%3A%5B\d{12}\+TO\+\d{12}%5D", requested[0])


def test_arxiv_model_retries_transient_output_but_not_authentication(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _: None)
    attempts = {"transient": 0, "auth": 0, "missing": 0}

    def transient():
        attempts["transient"] += 1
        if attempts["transient"] < 3:
            raise RuntimeError("incomplete structured output")
        return "validated"

    assert llm_module._with_retries("test ranking", transient) == "validated"
    assert attempts["transient"] == 3

    class AuthenticationError(Exception):
        status_code = 401

    def rejected():
        attempts["auth"] += 1
        raise AuthenticationError("invalid key material must not be retained")

    with pytest.raises(AuthenticationError):
        llm_module._with_retries("test authentication", rejected)
    assert attempts["auth"] == 1

    def missing_key():
        attempts["missing"] += 1
        raise RuntimeError(
            "OPENAI_API_KEY is not available to the DistillFeed server. "
            "Set it in the server environment and restart DistillFeed."
        )

    with pytest.raises(RuntimeError):
        llm_module._with_retries("test missing key", missing_key)
    assert attempts["missing"] == 1


def test_arxiv_reranker_contract_makes_duplicate_ids_unrepresentable(configured):
    cfg = load_plugin_config(configured)
    cfg["llm"]["system_prompt"] += '\nReturn {"items":[{"arxiv_id":"..."}]}'
    selected = [
        (paper("2607.10001"), LocalScore(8, reasons=["topic match"])),
        (paper("cs/9901001"), LocalScore(6, reasons=["author match"])),
    ]
    captured = {}

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps({"results": {
                    "paper_001": {
                        "score": 91, "decision": "keep",
                        "why": "Direct match for the configured reasoning interests",
                        "tags": ["reasoning"],
                    },
                    "paper_002": {
                        "score": 42, "decision": "drop",
                        "why": "Only a weak connection to the configured profile",
                        "tags": [],
                    },
                }}),
                usage=None,
                id="rank-response",
            )

    results, _ = llm_module._rerank_batch(
        selected, cfg, SimpleNamespace(responses=Responses()),
    )

    assert set(results) == {"2607.10001", "cs/9901001"}
    assert results["2607.10001"]["score"] == 91
    request_papers = json.loads(captured["input"])["papers"]
    assert [candidate["response_key"] for candidate in request_papers] == [
        "paper_001", "paper_002",
    ]
    schema = captured["text"]["format"]["schema"]
    assert schema["required"] == ["results"]
    assert schema["properties"]["results"]["required"] == [
        "paper_001", "paper_002",
    ]
    assert schema["properties"]["results"]["additionalProperties"] is False
    assert "authoritative output contract" in captured["instructions"]
    assert captured["instructions"].rfind("authoritative output contract") > captured[
        "instructions"
    ].rfind('"items"')


def test_arxiv_reranker_rejects_legacy_duplicate_array(configured):
    cfg = load_plugin_config(configured)
    selected = [
        (paper("2607.10002"), LocalScore(5)),
        (paper("2607.10003"), LocalScore(4)),
    ]

    class Responses:
        def create(self, **kwargs):
            repeated = {
                "arxiv_id": "2607.10002", "score": 80, "decision": "keep",
                "why": "Repeated identifier", "tags": [],
            }
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps({"items": [repeated, repeated]}),
                usage=None,
                id="legacy-response",
            )

    with pytest.raises(RuntimeError, match="every submitted paper exactly once"):
        llm_module._rerank_batch(
            selected, cfg, SimpleNamespace(responses=Responses()),
        )


def csrf_from(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)">', response.data)
    assert match
    return match.group(1).decode()


def paper(identifier: str = "2607.00001", category: str = "cs.AI") -> Paper:
    now = datetime.now(UTC)
    return Paper(
        arxiv_id=identifier,
        version="v1",
        title="Reliable machine learning systems",
        abstract="A technical study of robust artificial intelligence and software systems.",
        authors=["A. Researcher", "B. Scientist"],
        categories=[category],
        primary_category=category,
        link=f"https://arxiv.org/abs/{identifier}v1",
        pdf_link=f"https://arxiv.org/pdf/{identifier}v1.pdf",
        published=now,
        updated=now,
        source="rss",
        announce_type="new",
        source_categories=[category],
    )


def context(config: Config, connection, *, automatic: bool = True) -> RefreshContext:
    return RefreshContext(connection, config, None, None, False, automatic, lambda _: {})


def successful_llm(monkeypatch: pytest.MonkeyPatch, score: int = 92) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        plugin_module,
        "rerank",
        lambda candidates, cfg, **kwargs: (
            {
                candidate.arxiv_id: {
                    "score": score,
                    "decision": "keep",
                    "why": "Strong match for the configured research profile",
                    "tags": ["machine-learning"],
                }
                for candidate, _ in candidates
            },
            LLMUsage(100, 0, 40, 0.001, ("rank-1",)),
        ),
    )
    monkeypatch.setattr(
        plugin_module,
        "daily_digest",
        lambda papers, cfg, language: (
            {"overview": "A focused daily research digest.", "sections": []},
            LLMUsage(80, 0, 30, 0.001, ("digest-1",)),
        ),
    )
    monkeypatch.setattr(
        plugin_module,
        "deliver_arxiv_pushes",
        lambda *args, **kwargs: {"status": "disabled"},
    )


def test_neutral_plugin_is_installed_but_disabled_and_discloses_no_profile(configured):
    assert "arxiv_digest" in available_plugin_names()
    assert configured.get("plugins", "arxiv_digest_enabled") is False
    assert not (configured.path.parent / "arxiv-digest.toml").exists()

    client = create_app(str(configured.path)).test_client()
    page = client.get("/?settings=arxiv")
    assert page.status_code == 200
    assert b'data-config-path="plugins.arxiv_digest_enabled"' in page.data
    assert b'data-config-path="plugin.arxiv_digest.arxiv.categories"' in page.data
    assert b"cs.AI" in page.data
    assert b"machine learning\nprogram analysis" not in page.data
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM feeds WHERE xml_url LIKE 'plugin://arxiv/%'"
        ).fetchone()[0] == 0

    neutral = load_plugin_config(configured)
    assert neutral["arxiv"]["categories"] == ["cs.AI"]
    assert neutral["filters"]["preferred_authors"] == []
    assert neutral["notifications"]["ntfy"]["enabled"] is False
    assert Path(neutral["_path"]) == configured.path.parent / "arxiv-digest.toml"


def test_settings_enable_configure_disable_and_reenable_preserves_history(configured):
    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    headers = {"X-CSRF-Token": csrf}

    enabled = client.post(
        "/api/config",
        json={"values": {"plugins.arxiv_digest_enabled": True}},
        headers=headers,
    )
    assert enabled.status_code == 200
    assert enabled.get_json()["plugin_state_changed"] is True
    effective = load_config(configured.path)
    assert effective.get("plugins", "arxiv_digest_enabled") is True
    assert "arxiv_digest" not in effective.get("plugins", "enabled")
    assert not (configured.path.parent / "arxiv-digest.toml").exists()
    with connect(configured.database_path) as connection:
        feed_id = int(connection.execute(
            "SELECT id FROM feeds WHERE xml_url='plugin://arxiv/cs.AI' AND enabled=1"
        ).fetchone()[0])
        item_id = int(connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,discovered_at,summary_eligible)
               VALUES(?, 'preserved-paper', 'Preserved paper', datetime('now'), 0)""",
            (feed_id,),
        ).lastrowid)
    assert b'data-config-path="plugin.arxiv_digest.arxiv.categories"' in client.get("/?settings=arxiv").data
    with connect(configured.database_path) as connection:
        portable = serialize_opml(build_tree_from_database(connection))
    exported = parse_opml_bytes(portable)
    assert all(group.title != "arXiv Digest" for group in exported)
    assert b"plugin://" not in portable

    configured_categories = client.post(
        "/api/config",
        json={"values": {
            "plugins.arxiv_digest_enabled": True,
            "plugin.arxiv_digest.arxiv.categories": "cs.LG, cs.CL",
            "plugin.arxiv_digest.filters.positive_keywords_strong": "machine learning\nprogram analysis",
        }},
        headers=headers,
    )
    assert configured_categories.status_code == 200
    plugin_file = configured.path.parent / "arxiv-digest.toml"
    before_invalid = plugin_file.read_bytes()
    assert load_plugin_config(load_config(configured.path))["arxiv"]["categories"] == ["cs.LG", "cs.CL"]
    invalid = client.post(
        "/api/config",
        json={"values": {"plugin.arxiv_digest.arxiv.categories": "not-a-category"}},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert plugin_file.read_bytes() == before_invalid

    disabled = client.post(
        "/api/config",
        json={"values": {"plugins.arxiv_digest_enabled": False}},
        headers=headers,
    )
    assert disabled.status_code == 200
    with connect(configured.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM feeds WHERE xml_url LIKE 'plugin://arxiv/%' AND enabled=1"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
        assert all(node["title"] != "arXiv Digest" for node in _group_tree(connection))
        assert refresh_plugins(connection, load_config(configured.path))["plugins"] == []
    disabled_page = client.get("/?settings=arxiv")
    assert b'data-config-path="plugins.arxiv_digest_enabled"' in disabled_page.data
    assert b'data-config-path="plugin.arxiv_digest.arxiv.categories"' in disabled_page.data

    reenabled = client.post(
        "/api/config",
        json={"values": {"plugins.arxiv_digest_enabled": True}},
        headers=headers,
    )
    assert reenabled.status_code == 200
    with connect(configured.database_path) as connection:
        active = {
            row[0] for row in connection.execute(
                "SELECT xml_url FROM feeds WHERE xml_url LIKE 'plugin://arxiv/%' AND enabled=1"
            )
        }
        assert active == {"plugin://arxiv/cs.LG", "plugin://arxiv/cs.CL"}
        assert connection.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
        assert any(node["title"] == "arXiv Digest" for node in _group_tree(connection))


def test_legacy_entry_point_switch_migrates_to_the_named_checkbox(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[plugins]\nenabled = \"arxiv_digest\"\n\n[app]\n"
        "database_path = \"reader.sqlite3\"\nworking_opml_path = \"subscriptions.opml\"\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.get("plugins", "arxiv_digest_enabled") is True


def test_large_announcement_scores_every_locally_eligible_paper_and_repeat_is_model_free(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    announcement = [paper(f"2607.{index:05d}") for index in range(120)]
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: announcement)
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = {"rank": 0, "digest": 0}

    def rank(candidates, cfg, **kwargs):
        calls["rank"] += 1
        assert len(candidates) == 120
        return ({
            candidate.arxiv_id: {
                "score": 90, "decision": "keep", "why": "Configured topic match", "tags": [],
            }
            for candidate, _ in candidates
        }, LLMUsage())

    def digest(papers, cfg, language):
        calls["digest"] += 1
        assert len(papers) == 120
        return ({"overview": "Focused digest", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "rerank", rank)
    monkeypatch.setattr(plugin_module, "daily_digest", digest)
    monkeypatch.setattr(plugin_module, "deliver_arxiv_pushes", lambda *args, **kwargs: {"status": "disabled"})
    first = plugin.refresh(context(configured, connection))
    assert first["fetched_items"] == 120
    assert first["selected_for_llm"] == 120
    assert first["screened_locally"] == 0
    assert first["new_items"] == 120
    assert first["status"] == "waiting-for-digest"
    assert calls == {"rank": 0, "digest": 0}
    assert {
        row["evaluation_status"]: int(row["count"])
        for row in connection.execute(
            """SELECT evaluation_status,COUNT(*) AS count
                 FROM distillfeed_arxiv_papers GROUP BY evaluation_status"""
        ).fetchall()
    } == {"pending": 120}
    digested = plugin.summarize(context(configured, connection))
    assert digested["status"] == "success"
    assert calls == {"rank": 1, "digest": 1}
    assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 120
    assert connection.execute(
        "SELECT COUNT(*) FROM distillfeed_arxiv_papers WHERE evaluation_status='screened_out'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM distillfeed_arxiv_papers WHERE evaluation_status='complete' AND llm_score IS NOT NULL"
    ).fetchone()[0] == 120
    assert connection.execute("SELECT COUNT(*) FROM distillfeed_arxiv_seen").fetchone()[0] == 120

    second = plugin.refresh(context(configured, connection))
    assert second["new_items"] == 0
    repeated = plugin.summarize(context(configured, connection))
    assert repeated["status"] == "unchanged"
    assert repeated["message"] == "The daily arXiv digest is already up to date"
    assert calls == {"rank": 1, "digest": 1}
    connection.close()


def test_initialize_requeues_above_threshold_rows_that_never_received_ai_score(configured):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    cfg = load_plugin_config(configured)
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    feed_id = int(connection.execute(
        "SELECT id FROM feeds WHERE group_id=? ORDER BY id LIMIT 1", (group_id,)
    ).fetchone()[0])
    threshold = int(cfg["filters"]["broad_candidate_threshold"])

    def insert_row(stable_id: str, score: int) -> int:
        item_id = int(connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,discovered_at,summary_eligible)
               VALUES(?,?,?,?,0)""",
            (feed_id, stable_id, stable_id, utcnow()),
        ).lastrowid)
        connection.execute(
            """INSERT INTO distillfeed_arxiv_papers(
                   item_id,arxiv_id,categories_json,source,local_score,decision,why,evaluation_status,evaluated_at
               ) VALUES(?,?,?,'rss',?,'drop','Screened out before AI reranking','screened_out',?)""",
            (item_id, stable_id, '["cs.AI"]', score, utcnow()),
        )
        return item_id

    eligible = insert_row("2607.88001", threshold + 1)
    ineligible = insert_row("2607.88002", threshold - 1)
    plugin.initialize(connection, configured)

    rows = {
        int(row["item_id"]): row["evaluation_status"]
        for row in connection.execute(
            "SELECT item_id,evaluation_status FROM distillfeed_arxiv_papers WHERE item_id IN (?,?)",
            (eligible, ineligible),
        ).fetchall()
    }
    assert rows == {eligible: "pending", ineligible: "screened_out"}
    connection.close()



def test_initialize_repairs_large_historical_stranded_population(configured):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    cfg = load_plugin_config(configured)
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    feed_id = int(connection.execute(
        "SELECT id FROM feeds WHERE group_id=? ORDER BY id LIMIT 1", (group_id,)
    ).fetchone()[0])
    threshold = int(cfg["filters"]["broad_candidate_threshold"])

    for index in range(303):
        stable_id = f"2608.{30000 + index:05d}"
        item_id = int(connection.execute(
            """INSERT INTO items(feed_id,stable_id,title,discovered_at,summary_eligible)
               VALUES(?,?,?,?,0)""",
            (feed_id, stable_id, stable_id, utcnow()),
        ).lastrowid)
        connection.execute(
            """INSERT INTO distillfeed_arxiv_papers(
                   item_id,arxiv_id,categories_json,source,local_score,decision,why,
                   evaluation_status,evaluated_at
               ) VALUES(?,?,?,'rss',?,'drop','Screened out before AI reranking',
                        'screened_out',?)""",
            (item_id, stable_id, '["cs.AI"]', threshold, utcnow()),
        )
    below_id = int(connection.execute(
        """INSERT INTO items(feed_id,stable_id,title,discovered_at,summary_eligible)
           VALUES(?,?,?,?,0)""",
        (feed_id, "2608.99999", "below", utcnow()),
    ).lastrowid)
    connection.execute(
        """INSERT INTO distillfeed_arxiv_papers(
               item_id,arxiv_id,categories_json,source,local_score,decision,why,
               evaluation_status,evaluated_at
           ) VALUES(?,?,?,'rss',?,'drop','Screened out below threshold',
                    'screened_out',?)""",
        (below_id, "2608.99999", '["cs.AI"]', threshold - 1, utcnow()),
    )

    plugin.initialize(connection, configured)

    assert connection.execute(
        """SELECT COUNT(*) FROM distillfeed_arxiv_papers
             WHERE local_score>=? AND llm_score IS NULL AND evaluation_status='pending'""",
        (threshold,),
    ).fetchone()[0] == 303
    assert connection.execute(
        "SELECT evaluation_status FROM distillfeed_arxiv_papers WHERE item_id=?", (below_id,)
    ).fetchone()[0] == "screened_out"
    connection.close()


def test_legacy_multi_day_summary_is_rebuilt_daily_without_reranking(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    feed_id = int(connection.execute(
        "SELECT id FROM feeds WHERE group_id=? ORDER BY id LIMIT 1", (group_id,)
    ).fetchone()[0])
    item_ids: list[int] = []
    for arxiv_id, day in (("2608.21001", 10), ("2608.22001", 11)):
        value = paper(arxiv_id)
        value.published = value.updated = datetime(2026, 8, day, 12, tzinfo=UTC)
        item_id, _created, _revised = plugin_module._store_paper(connection, value, feed_id)
        item_ids.append(item_id)
        connection.execute(
            """UPDATE distillfeed_arxiv_papers
                  SET local_score=2,llm_score=91,final_score=33.85,decision='keep',
                      why='Already ranked',evaluation_status='complete'
                WHERE item_id=?""",
            (item_id,),
        )
    run_id = int(connection.execute(
        """INSERT INTO llm_runs(
               request_key,started_at,completed_at,status,model,prompt_version,
               submitted_items,deferred_items,pricing_json
           ) VALUES('legacy-mixed',?,?,'success','gpt-5.4-nano',?,2,0,'{}')""",
        (utcnow(), utcnow(), plugin_module.PROMPT_VERSION),
    ).lastrowid)
    summary_id = int(connection.execute(
        """INSERT INTO summaries(
               llm_run_id,group_id,scope_kind,scope_id,policy_hash,overview,changes,
               sections_json,created_at
           ) VALUES(?,?,'group',?,?,'Legacy mixed digest','','[]',?)""",
        (run_id, group_id, group_id, plugin_module.PROMPT_VERSION, utcnow()),
    ).lastrowid)
    for rank, item_id in enumerate(item_ids, 1):
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description,justification,story_cluster
               ) VALUES(?,?,1,?,91,'Abstract','Already ranked','arXiv')""",
            (summary_id, item_id, rank),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    rerank_calls: list[int] = []
    digest_days: list[str] = []

    def rank(candidates, cfg, **kwargs):
        rerank_calls.append(len(candidates))
        return {}, LLMUsage()

    def digest(values, cfg, language):
        digest_days.append(values[0][0].published.date().isoformat())
        return ({"overview": "Rebuilt daily digest", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "rerank", rank)
    monkeypatch.setattr(plugin_module, "daily_digest", digest)
    monkeypatch.setattr(
        plugin_module, "deliver_arxiv_pushes", lambda *args, **kwargs: {"status": "disabled"},
    )

    result = plugin.summarize(context(configured, connection, automatic=False))

    assert result["status"] == "success"
    assert result["completed_announcements"] == ["2026-08-10", "2026-08-11"]
    assert rerank_calls == []
    assert digest_days == ["2026-08-10", "2026-08-11"]
    dedicated = connection.execute(
        """SELECT COUNT(*) FROM summaries s JOIN llm_runs lr ON lr.id=s.llm_run_id
             WHERE lr.status='success' AND lr.prompt_version LIKE 'distillfeed-arxiv-%'"""
    ).fetchone()[0]
    assert dedicated == 3  # one legacy multi-day record plus two daily revisions
    repeated = plugin.summarize(context(configured, connection, automatic=False))
    assert repeated["status"] == "unchanged"
    connection.close()


def test_revised_paper_clears_stale_ai_result_before_rerank(configured):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    feed_id = int(connection.execute(
        "SELECT id FROM feeds WHERE group_id=? ORDER BY id LIMIT 1", (group_id,)
    ).fetchone()[0])
    original = paper("2608.23001")
    original.version = "v1"
    item_id, _created, _revised = plugin_module._store_paper(connection, original, feed_id)
    connection.execute(
        """UPDATE distillfeed_arxiv_papers
              SET local_score=5,llm_score=97,final_score=38.95,decision='keep',
                  why='Old model result',evaluation_status='complete',evaluated_at=?
            WHERE item_id=?""",
        (utcnow(), item_id),
    )
    revised = paper("2608.23001")
    revised.version = "v2"

    same_item, created, needs_evaluation = plugin_module._store_paper(connection, revised, feed_id)
    row = connection.execute(
        """SELECT version,local_score,llm_score,final_score,decision,why,
                  evaluation_status,evaluated_at
             FROM distillfeed_arxiv_papers WHERE item_id=?""",
        (item_id,),
    ).fetchone()

    assert same_item == item_id
    assert created is False and needs_evaluation is True
    assert row["version"] == "v2"
    assert row["local_score"] is None and row["llm_score"] is None and row["final_score"] is None
    assert row["decision"] is None and row["why"] is None
    assert row["evaluation_status"] == "pending" and row["evaluated_at"] is None
    connection.close()


def test_previously_seen_only_paper_is_restored_to_browseable_archive(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    candidate = paper("2607.77777")
    connection.execute(
        """INSERT INTO distillfeed_arxiv_seen(
               arxiv_id,version,first_seen_at,last_seen_at,local_score,selected
           ) VALUES(?,?,?,?,?,0)""",
        (candidate.arxiv_id, candidate.version, utcnow(), utcnow(), -3),
    )
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: [candidate])
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)

    result = plugin.refresh(context(configured, connection))

    assert result["new_items"] == 1
    restored = connection.execute(
        """SELECT ap.evaluation_status,i.title FROM distillfeed_arxiv_papers ap
             JOIN items i ON i.id=ap.item_id WHERE ap.arxiv_id=?""",
        (candidate.arxiv_id,),
    ).fetchone()
    assert restored["title"] == candidate.title
    assert restored["evaluation_status"] in {"pending", "screened_out"}
    connection.close()


def test_arxiv_archive_and_rankings_follow_configured_category_scope(configured):
    configured.data["plugins"]["arxiv_digest_enabled"] = True
    save_config(configured)
    with connect(configured.database_path) as connection:
        plugin = ArxivDigestPlugin()
        plugin.initialize(connection, configured)
        feed_id = int(connection.execute(
            "SELECT id FROM feeds WHERE xml_url='plugin://arxiv/cs.AI'"
        ).fetchone()[0])
        group_id = int(connection.execute(
            "SELECT group_id FROM feeds WHERE id=?", (feed_id,),
        ).fetchone()[0])
        archive_item_ids: dict[int, int] = {}
        states = (
            ("pending", None, None),
            ("screened_out", "drop", None),
            ("complete", "keep", 96),
            ("complete", "drop", 31),
        )
        for index, (evaluation_status, decision, llm_score) in enumerate(states):
            item_id = int(connection.execute(
                """INSERT INTO items(feed_id,stable_id,title,url,author,published_at,
                           discovered_at,description_text,summary_eligible)
                   VALUES(?,?,?,?,?,?,?,?,0)""",
                (
                    feed_id, f"archive-{index}", f"Archive paper {index}",
                    f"https://arxiv.org/abs/2608.1000{index}",
                    "Ada Logician" if index == 1 else "Researcher",
                    f"2026-08-10T0{index}:00:00+00:00", utcnow(),
                    "Unique proof-search abstract" if index == 1 else "Abstract",
                ),
            ).lastrowid)
            archive_item_ids[index] = item_id
            category = "cs.LO" if index == 1 else "cs.AI"
            connection.execute(
                """INSERT INTO distillfeed_arxiv_papers(
                       item_id,arxiv_id,categories_json,primary_category,source,
                       local_score,llm_score,final_score,decision,why,
                       local_reasons_json,evaluation_status)
                   VALUES(?,?,?,?, 'rss',?,?,?,?,?,?,?)""",
                (
                    item_id, f"2608.1000{index}", f'["{category}"]', category,
                    index, llm_score, (index * 10) if llm_score is not None else None,
                    decision, "Unique rationale for logic" if index == 1 else "General rationale",
                    '["keyword match"]', evaluation_status,
                ),
            )
        run_id = int(connection.execute(
            """INSERT INTO llm_runs(
                   request_key,started_at,completed_at,status,model,prompt_version,pricing_json
               ) VALUES('arxiv-score-order',?,?,'success','model','distillfeed-arxiv-test','{}')""",
            (utcnow(), utcnow()),
        ).lastrowid)
        summary_id = int(connection.execute(
            """INSERT INTO summaries(
                   llm_run_id,group_id,scope_kind,scope_id,overview,created_at
               ) VALUES(?,?,'group',?,'Digest',?)""",
            (run_id, group_id, group_id, utcnow()),
        ).lastrowid)
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description
               ) VALUES(?,?,1,2,31,'Lower-scored arXiv paper')""",
            (summary_id, archive_item_ids[3]),
        )
        connection.execute(
            """INSERT INTO summary_items(
                   summary_id,item_id,included,rank,importance,description
               ) VALUES(?,?,1,1,96,'Higher-scored arXiv paper')""",
            (summary_id, archive_item_ids[2]),
        )

    client = create_app(str(configured.path)).test_client()
    reader = client.get(f"/?feed={feed_id}")
    assert reader.status_code == 200
    assert b'id="review-app"' in reader.data
    assert b'id="review-page-size"' in reader.data
    ranked = client.get(
        f"/api/review/days/2026-08-10/items?feed_id={feed_id}&preset=everything&sort=ai&page_size=25"
    )
    assert ranked.status_code == 200
    ranked_items = ranked.get_json()["items"]
    assert [item["title"] for item in ranked_items] == [
        "Archive paper 2", "Archive paper 3", "Archive paper 0", "Archive paper 1",
    ]
    assert [item["ai_state"] for item in ranked_items] == [
        "scored", "scored", "pending", "not-sent",
    ]
    all_page = client.get("/arxiv")
    assert all_page.status_code == 200
    assert b"All fetched papers" in all_page.data
    assert b"Locally screened out" in all_page.data
    assert all_page.data.count(b'class="item-row saved-row"') == 3
    assert b"All configured categories" in all_page.data
    assert b"cs.AI" in all_page.data
    assert b"cs.LO" not in all_page.data
    kept = client.get("/arxiv?status=kept")
    assert kept.data.count(b'class="item-row saved-row"') == 1
    assert b"AI kept" in kept.data and b"96" in kept.data
    searched = client.get("/arxiv?q=2608.10002")
    assert searched.data.count(b'class="item-row saved-row"') == 1
    assert client.get("/arxiv?q=proof-search").data.count(b'class="item-row saved-row"') == 0
    assert client.get("/arxiv?q=Unique+rationale").data.count(b'class="item-row saved-row"') == 0
    logic = client.get("/arxiv?category=cs.LO")
    assert logic.data.count(b'class="item-row saved-row"') == 3
    assert b"Ada Logician" not in logic.data and b"Unique proof-search abstract" not in logic.data
    sorted_ai = client.get("/arxiv?sort=ai-high")
    assert sorted_ai.data.index(b"Archive paper 2") < sorted_ai.data.index(b"Archive paper 3")
    assert b"Combined score" in sorted_ai.data
    assert b"Abstract, authors, categories, and ranking rationale" in sorted_ai.data
    top_ranked = client.get("/ranked/arxiv")
    assert top_ranked.status_code == 200
    assert b"Top ArXiv ranked" in top_ranked.data
    assert b"Archive paper 2" in top_ranked.data
    assert b"Archive paper 1" not in top_ranked.data
    stylesheet = client.get("/static/app.css").data
    assert b".arxiv-browser-controls .arxiv-apply-button" in stylesheet
    assert b"min-height: 30px" in stylesheet


def test_daily_digest_button_checks_then_summarizes_and_repeats_as_up_to_date(
    configured, monkeypatch,
):
    configured.data["plugins"]["arxiv_digest_enabled"] = True
    save_config(configured)
    with connect(configured.database_path) as connection:
        plugin = ArxivDigestPlugin()
        plugin.initialize(connection, configured)
        group_id = int(connection.execute(
            "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
        ).fetchone()[0])

    candidate = paper("2608.42424")
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: [candidate])
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        "rss_reader.service.refresh_all",
        lambda *args, **kwargs: {
            "attempted": 0, "succeeded": 0, "failed": 0, "new_items": 0,
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model_calls = {"ranking": 0, "digest": 0}

    def rank(candidates, cfg, **kwargs):
        model_calls["ranking"] += 1
        return ({
            entry.arxiv_id: {
                "score": 93, "decision": "keep", "why": "Direct match", "tags": ["AI"],
            }
            for entry, _local in candidates
        }, LLMUsage())

    def digest(papers, cfg, language):
        model_calls["digest"] += 1
        return ({"overview": "Current daily digest", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "rerank", rank)
    monkeypatch.setattr(plugin_module, "daily_digest", digest)
    monkeypatch.setattr(
        plugin_module, "deliver_arxiv_pushes",
        lambda *args, **kwargs: {"status": "disabled"},
    )

    first = run_update_summaries(
        configured, group_id=group_id, include_plugins=True, include_generic=False,
    )
    assert first["status"] == "success"
    assert first["refresh"]["plugins"][0]["status"] == "waiting-for-digest"
    assert first["summary"]["plugins"][0]["status"] == "success"
    assert model_calls == {"ranking": 1, "digest": 1}

    repeated = run_update_summaries(
        configured, group_id=group_id, include_plugins=True, include_generic=False,
    )
    assert repeated["status"] == "unchanged"
    assert repeated["message"] == "The daily arXiv digest is already up to date"
    assert repeated["refresh"]["plugins"][0]["status"] == "unchanged"
    assert repeated["summary"]["plugins"][0]["status"] == "unchanged"
    assert model_calls == {"ranking": 1, "digest": 1}


def test_same_day_late_evidence_creates_append_only_digest_revision(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    stamp = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    first_paper = paper("2607.90001")
    late_paper = paper("2607.90002")
    first_paper.published = first_paper.updated = stamp
    late_paper.published = late_paper.updated = stamp
    announcements = iter(([first_paper], [first_paper, late_paper]))
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: next(announcements))
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = {"rank": 0, "digest": 0}

    def rank(candidates, cfg, **kwargs):
        calls["rank"] += 1
        return ({
            candidate.arxiv_id: {
                "score": 92, "decision": "keep", "why": "Strong topic match", "tags": [],
            }
            for candidate, _ in candidates
        }, LLMUsage())

    def digest(papers, cfg, language):
        calls["digest"] += 1
        if calls["digest"] == 2:
            raise RuntimeError("composition unavailable")
        return ({"overview": f"Digest revision {calls['digest']}", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "rerank", rank)
    monkeypatch.setattr(plugin_module, "daily_digest", digest)
    monkeypatch.setattr(
        plugin_module, "deliver_arxiv_pushes", lambda *args, **kwargs: {"status": "disabled"},
    )

    assert plugin.refresh(context(configured, connection))["status"] == "waiting-for-digest"
    first = plugin.summarize(context(configured, connection))
    assert first["status"] == "success"
    first_fingerprint = connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='last_digest_fingerprint'"
    ).fetchone()[0]
    first_summary_id = int(connection.execute("SELECT id FROM summaries").fetchone()[0])

    refreshed = plugin.refresh(context(configured, connection))
    assert refreshed["status"] == "waiting-for-digest"
    assert connection.execute(
        "SELECT evaluation_status FROM distillfeed_arxiv_papers WHERE arxiv_id='2607.90002'"
    ).fetchone()[0] == "pending"
    assert connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='pending_announcement'"
    ).fetchone()[0] == "2026-07-16"
    assert connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='last_digest_announcement'"
    ).fetchone()[0] == "2026-07-16"

    failed_revision = plugin.summarize(context(configured, connection))
    assert failed_revision["status"] == "llm-failed"
    assert connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1
    assert [row[0] for row in connection.execute(
        "SELECT status FROM llm_runs ORDER BY id"
    ).fetchall()] == ["success", "failed"]
    assert connection.execute(
        "SELECT 1 FROM summaries WHERE id=?", (first_summary_id,)
    ).fetchone()

    replacement = plugin.summarize(context(configured, connection))
    assert replacement["status"] == "success"
    assert connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 2
    assert [row[0] for row in connection.execute(
        "SELECT status FROM llm_runs ORDER BY id"
    ).fetchall()] == ["success", "failed", "success"]
    assert connection.execute(
        "SELECT COUNT(DISTINCT request_key) FROM llm_runs"
    ).fetchone()[0] == 3
    assert connection.execute(
        "SELECT evaluation_status FROM distillfeed_arxiv_papers WHERE arxiv_id='2607.90002'"
    ).fetchone()[0] == "complete"
    second_fingerprint = connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='last_digest_fingerprint'"
    ).fetchone()[0]
    assert second_fingerprint != first_fingerprint
    assert calls == {"rank": 3, "digest": 3}
    connection.close()


def test_historical_recovery_is_partitioned_by_day_and_reuses_existing_ai_scores(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    feed_id = int(connection.execute(
        "SELECT id FROM feeds WHERE group_id=? ORDER BY id LIMIT 1", (group_id,)
    ).fetchone()[0])
    stamps = {
        "2608.10001": datetime(2026, 8, 10, 12, tzinfo=UTC),
        "2608.10002": datetime(2026, 8, 10, 13, tzinfo=UTC),
        "2608.11001": datetime(2026, 8, 11, 12, tzinfo=UTC),
    }
    item_ids: dict[str, int] = {}
    papers: dict[str, Paper] = {}
    for arxiv_id, stamp in stamps.items():
        value = paper(arxiv_id)
        value.published = value.updated = stamp
        papers[arxiv_id] = value
        item_id, _created, _revised = plugin_module._store_paper(connection, value, feed_id)
        item_ids[arxiv_id] = item_id
    connection.execute(
        """UPDATE distillfeed_arxiv_papers
              SET local_score=5,llm_score=91,final_score=36.85,decision='keep',
                  why='Already ranked',evaluation_status='complete'
            WHERE item_id=?""",
        (item_ids["2608.10001"],),
    )
    for arxiv_id in ("2608.10002", "2608.11001"):
        connection.execute(
            """UPDATE distillfeed_arxiv_papers
                  SET local_score=2,llm_score=NULL,final_score=NULL,decision='drop',
                      why='Screened out before AI reranking',evaluation_status='screened_out'
                WHERE item_id=?""",
            (item_ids[arxiv_id],),
        )
    plugin.initialize(connection, configured)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        plugin_module, "compute_local_score",
        lambda value, cfg: LocalScore(5 if value.arxiv_id == "2608.10001" else 2, ["stored"]),
    )
    calls: list[tuple[str, list[str]]] = []

    def rank(candidates, cfg, **kwargs):
        identifiers = [value.arxiv_id for value, _local in candidates]
        calls.append(("rank", identifiers))
        return ({
            value.arxiv_id: {
                "score": 88, "decision": "keep", "why": "Recovered", "tags": [],
            }
            for value, _local in candidates
        }, LLMUsage())

    def digest(values, cfg, language):
        identifiers = [value.arxiv_id for value, _local, _decision in values]
        calls.append(("digest", identifiers))
        return ({"overview": "Recovered day", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "rerank", rank)
    monkeypatch.setattr(plugin_module, "daily_digest", digest)
    monkeypatch.setattr(
        plugin_module, "deliver_arxiv_pushes", lambda *args, **kwargs: {"status": "disabled"},
    )
    result = plugin.summarize(context(configured, connection, automatic=False))

    assert result["status"] == "success"
    assert result["completed_announcements"] == ["2026-08-10", "2026-08-11"]
    assert calls[0] == ("rank", ["2608.10002"])
    assert calls[1][0] == "digest"
    assert set(calls[1][1]) == {"2608.10001", "2608.10002"}
    assert calls[2:] == [("rank", ["2608.11001"]), ("digest", ["2608.11001"])]
    assert connection.execute(
        "SELECT COUNT(*) FROM distillfeed_arxiv_papers WHERE local_score>=0 AND llm_score IS NULL"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 2
    connection.close()


def test_historical_recovery_commits_completed_days_and_resumes_after_failure(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    feed_id = int(connection.execute(
        "SELECT id FROM feeds WHERE group_id=? ORDER BY id LIMIT 1", (group_id,)
    ).fetchone()[0])
    for arxiv_id, day in (("2608.13001", 13), ("2608.14001", 14)):
        value = paper(arxiv_id)
        value.published = value.updated = datetime(2026, 8, day, 12, tzinfo=UTC)
        item_id, _created, _revised = plugin_module._store_paper(connection, value, feed_id)
        connection.execute(
            """UPDATE distillfeed_arxiv_papers
                  SET local_score=2,decision='drop',why='Screened out before AI reranking',
                      evaluation_status='screened_out'
                WHERE item_id=?""",
            (item_id,),
        )
    plugin.initialize(connection, configured)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(plugin_module, "compute_local_score", lambda value, cfg: LocalScore(2, ["stored"]))
    monkeypatch.setattr(
        plugin_module, "rerank",
        lambda candidates, cfg, **kwargs: ({
            value.arxiv_id: {"score": 90, "decision": "keep", "why": "Recovered", "tags": []}
            for value, _local in candidates
        }, LLMUsage()),
    )
    attempts = {"digest": 0}

    def flaky_digest(values, cfg, language):
        attempts["digest"] += 1
        if attempts["digest"] == 2:
            raise RuntimeError("second day unavailable")
        return ({"overview": "Recovered", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "daily_digest", flaky_digest)
    monkeypatch.setattr(
        plugin_module, "deliver_arxiv_pushes", lambda *args, **kwargs: {"status": "disabled"},
    )
    first = plugin.summarize(context(configured, connection, automatic=False))
    assert first["status"] == "llm-failed"
    assert first["completed_announcements"] == ["2026-08-13"]
    assert first["failed_announcement"] == "2026-08-14"
    states = {
        row["arxiv_id"]: row["evaluation_status"]
        for row in connection.execute(
            "SELECT arxiv_id,evaluation_status FROM distillfeed_arxiv_papers"
        )
    }
    assert states == {"2608.13001": "complete", "2608.14001": "pending"}

    monkeypatch.setattr(
        plugin_module, "daily_digest",
        lambda values, cfg, language: ({"overview": "Retry", "sections": []}, LLMUsage()),
    )
    second = plugin.summarize(context(configured, connection, automatic=False))
    assert second["status"] == "success"
    assert second["completed_announcements"] == ["2026-08-14"]
    assert [row[0] for row in connection.execute(
        "SELECT status FROM llm_runs ORDER BY id"
    ).fetchall()] == ["success", "failed", "success"]
    connection.close()


@pytest.mark.parametrize(
    ("disabled", "reason"),
    ((True, "ai-disabled"), (False, "api-key-missing")),
)
def test_arxiv_preflight_blockers_are_explicit_and_preserve_pending(
    configured, monkeypatch, disabled, reason,
):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    cfg = copy.deepcopy(load_plugin_config(configured))
    cfg["llm"]["enabled"] = not disabled
    monkeypatch.setattr(plugin_module, "load_plugin_config", lambda _: cfg)
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, values: [paper("2607.90003")])
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert plugin.refresh(context(configured, connection))["status"] == "waiting-for-digest"
    result = plugin.summarize(context(configured, connection))

    assert result["status"] == "blocked"
    assert result["blocked_reason"] == reason
    assert result["retryable"] is False
    assert connection.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0
    assert connection.execute(
        "SELECT evaluation_status FROM distillfeed_arxiv_papers"
    ).fetchone()[0] == "pending"
    assert connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='blocked_reason'"
    ).fetchone()[0] == reason
    connection.close()


def test_failed_digest_stays_pending_then_summarize_retries_without_refetch(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: [paper("2607.99991")])
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    successful_llm(monkeypatch)
    good_digest = plugin_module.daily_digest
    attempts = {"digest": 0}

    def flaky(*args, **kwargs):
        attempts["digest"] += 1
        if attempts["digest"] == 1:
            raise RuntimeError("temporary model failure")
        return good_digest(*args, **kwargs)

    monkeypatch.setattr(plugin_module, "daily_digest", flaky)
    first = plugin.refresh(context(configured, connection))
    assert first["status"] == "waiting-for-digest"
    assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert connection.execute(
        "SELECT evaluation_status FROM distillfeed_arxiv_papers"
    ).fetchone()[0] == "pending"
    failed = plugin.summarize(context(configured, connection))
    assert failed["status"] == "llm-failed"
    monkeypatch.setattr(
        plugin_module,
        "fetch_rss",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("summarize refetched arXiv")),
    )
    second = plugin.summarize(SimpleNamespace(
        connection=connection, config=configured, feed_id=None, group_id=None, automatic=False,
    ))
    assert second["status"] == "success"
    assert [row[0] for row in connection.execute(
        "SELECT status FROM llm_runs ORDER BY id"
    ).fetchall()] == ["failed", "success"]
    assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert connection.execute(
        "SELECT evaluation_status FROM distillfeed_arxiv_papers"
    ).fetchone()[0] == "complete"
    connection.close()


def test_invalid_arxiv_key_is_actionable_persistent_and_never_reported_as_success(configured, monkeypatch):
    configured.data["plugins"]["arxiv_digest_enabled"] = True
    save_config(configured)
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: [paper("2607.99993")])
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    monkeypatch.setenv("OPENAI_API_KEY", "rejected-test-key")

    class InvalidKeyError(Exception):
        status_code = 401

    monkeypatch.setattr(
        plugin_module,
        "rerank",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            InvalidKeyError("Incorrect API key provided: private-key-material")
        ),
    )
    checked = plugin.refresh(context(configured, connection))
    assert checked["status"] == "waiting-for-digest"
    failed = plugin.summarize(context(configured, connection))
    assert failed["status"] == "llm-failed"
    assert "OpenAI rejected OPENAI_API_KEY (401)" in failed["llm_error"]
    assert "private-key-material" not in failed["llm_error"]
    group_id = int(connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='group_id'"
    ).fetchone()[0])
    connection.execute(
        """UPDATE llm_runs SET error=?
           WHERE prompt_version LIKE 'distillfeed-arxiv-%'""",
        ("Error code: 401 · invalid_api_key · " + "sk-" + "proj-private-key-material",),
    )
    connection.close()

    client = create_app(str(configured.path)).test_client()
    status = client.get("/api/status").get_json()
    assert status["arxiv_run"]["status"] == "failed"
    assert "Set a valid OPENAI_API_KEY" in status["arxiv_run"]["error"]
    assert "private-key-material" not in status["arxiv_run"]["error"]
    assert status["arxiv"]["pending_items"] == 1
    # ArXiv has its own module and reader state; ordinary AI settings must not
    # reintroduce an unrelated specialist submenu.
    ai_page = client.get("/ai").data
    assert b"arXiv AI update failed" not in ai_page
    assert b"private-key-material" not in ai_page
    reader = client.get(f"/?group={group_id}").data
    assert b"arXiv AI update failed" in reader
    assert b"Retry daily digests" in reader
    assert b"private-key-material" not in reader
    script = client.get("/static/app.js").data
    assert b"arXiv AI update failed" in script
    assert b"Daily arXiv digest is up to date" not in script


def test_optional_api_backfill_warning_does_not_inflate_feed_counts(configured, monkeypatch):
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: [paper("2607.99994")])
    monkeypatch.setattr(
        plugin_module, "fetch_api_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary API outage")),
    )
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)

    result = plugin.refresh(context(configured, connection))

    assert result["attempted"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert result["backfill_degraded"] is True
    assert result["api_backfill"]["status"] == "degraded"
    assert connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='last_api_error'"
    ).fetchone()[0] == "temporary API outage"
    assert connection.execute(
        "SELECT value FROM distillfeed_arxiv_state WHERE key='last_complete_at'"
    ).fetchone() is None
    connection.close()


def test_arxiv_items_keep_common_states_and_pushes_are_duplicate_safe(configured, monkeypatch):
    configured.data["plugins"]["arxiv_digest_enabled"] = True
    save_config(configured)
    connection = connect(configured.database_path)
    plugin = ArxivDigestPlugin()
    plugin.initialize(connection, configured)
    monkeypatch.setattr(plugin_module, "fetch_rss", lambda category, cfg: [paper("2607.99992")])
    monkeypatch.setattr(plugin_module, "fetch_api_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _: None)
    successful_llm(monkeypatch)
    plugin.refresh(context(configured, connection, automatic=False))
    plugin.summarize(context(configured, connection, automatic=False))
    item_id = int(connection.execute("SELECT item_id FROM distillfeed_arxiv_papers").fetchone()[0])
    connection.close()

    client = create_app(str(configured.path)).test_client()
    csrf = csrf_from(client.get("/"))
    headers = {"X-CSRF-Token": csrf}
    for is_read, is_starred, is_read_later in product((False, True), repeat=3):
        assert client.post(f"/api/items/{item_id}/read", json={"read": is_read}, headers=headers).status_code == 200
        assert client.post(f"/api/items/{item_id}/star", json={"starred": is_starred}, headers=headers).status_code == 200
        assert client.post(f"/api/items/{item_id}/read-later", json={"read_later": is_read_later}, headers=headers).status_code == 200
        with connect(configured.database_path) as current:
            state = current.execute(
                "SELECT is_read,is_starred,is_read_later FROM items WHERE id=?", (item_id,),
            ).fetchone()
            assert tuple(state) == tuple(map(int, (is_read, is_starred, is_read_later)))
            assert current.execute(
                "SELECT evaluation_status FROM distillfeed_arxiv_papers WHERE item_id=?", (item_id,),
            ).fetchone()[0] == "complete"

    cfg = load_plugin_config(load_config(configured.path))
    cfg["notifications"]["ntfy"].update({
        "enabled": True,
        "topic": "neutral-test-topic",
        "minimum_llm_score": 90,
        "send_on_manual_refresh": True,
    })
    sent = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "push-1"}

    monkeypatch.setattr(
        notification_module.requests,
        "post",
        lambda url, **kwargs: sent.append(kwargs["json"]) or Response(),
    )
    with connect(configured.database_path) as current:
        first = deliver_arxiv_pushes(current, cfg, [item_id], automatic=True)
        second = deliver_arxiv_pushes(current, cfg, [item_id], automatic=True)
    assert first["delivered"] == 1
    assert second["duplicates"] == 1
    assert len(sent) == 1
