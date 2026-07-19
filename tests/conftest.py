from pathlib import Path

import pytest

from rss_reader.config import load_config
from rss_reader.db import initialize


@pytest.fixture
def configured(tmp_path: Path, monkeypatch):
    # Provider calls are mocked in tests.  Keep ordinary readiness representative
    # without depending on a developer machine's credentials; tests covering the
    # missing-key transition remove this value explicitly.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used-for-network-calls")
    path = tmp_path / "config.toml"
    path.write_text(
        """[app]
database_path = "reader.sqlite3"
working_opml_path = "subscriptions.opml"
auto_refresh_on_load = false
summary_language = "English"

[feeds]
allow_private_urls = true

[llm]
model = "gpt-5.4-mini"
max_entries_total = 3
max_entries_per_feed = 2
max_description_chars = 30
max_input_chars = 10000
""",
        encoding="utf-8",
    )
    config = load_config(path)
    initialize(config.database_path)
    return config
