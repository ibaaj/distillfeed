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
from rss_reader.db import connect, initialize
from rss_reader.plugins import RefreshContext, available_plugin_names, refresh_plugins
from rss_reader.opml import build_tree_from_database, parse_opml_bytes, serialize_opml
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
    page = client.get("/ai")
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
    assert b'data-config-path="plugin.arxiv_digest.arxiv.categories"' in client.get("/ai").data
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
    disabled_page = client.get("/ai")
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


def test_large_announcement_shortlists_once_and_repeat_is_model_free(configured, monkeypatch):
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
        assert len(candidates) == 100
        return ({
            candidate.arxiv_id: {
                "score": 90, "decision": "keep", "why": "Configured topic match", "tags": [],
            }
            for candidate, _ in candidates
        }, LLMUsage())

    def digest(papers, cfg, language):
        calls["digest"] += 1
        assert len(papers) == 100
        return ({"overview": "Focused digest", "sections": []}, LLMUsage())

    monkeypatch.setattr(plugin_module, "rerank", rank)
    monkeypatch.setattr(plugin_module, "daily_digest", digest)
    monkeypatch.setattr(plugin_module, "deliver_arxiv_pushes", lambda *args, **kwargs: {"status": "disabled"})
    first = plugin.refresh(context(configured, connection))
    assert first["fetched_items"] == 120
    assert first["selected_for_llm"] == 100
    assert first["screened_locally"] == 20
    assert first["new_items"] == 100
    assert first["status"] == "waiting-for-digest"
    assert calls == {"rank": 0, "digest": 0}
    digested = plugin.summarize(context(configured, connection))
    assert digested["status"] == "success"
    assert calls == {"rank": 1, "digest": 1}
    assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 100
    assert connection.execute("SELECT COUNT(*) FROM distillfeed_arxiv_seen").fetchone()[0] == 120

    second = plugin.refresh(context(configured, connection))
    assert second["new_items"] == 0
    repeated = plugin.summarize(context(configured, connection))
    assert repeated["status"] == "unchanged"
    assert calls == {"rank": 1, "digest": 1}
    connection.close()


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
    ai_page = client.get("/ai").data
    assert b"The last arXiv AI update failed" in ai_page
    assert b"private-key-material" not in ai_page
    reader = client.get(f"/?group_id={group_id}").data
    assert b"arXiv AI update failed" in reader
    assert b"Retry daily digest" in reader
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
