from types import SimpleNamespace

from rss_reader.ai_usage import usage_and_cost
from rss_reader.baseline import baseline_backlog
from rss_reader.db import connect, utcnow


def test_usage_normalizes_responses_and_chat_token_shapes():
    pricing = {"input": 1.0, "cached_input": 0.25, "output": 4.0}
    responses = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=100, output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=40),
    ))
    chat = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=0, output_tokens=0, prompt_tokens=50, completion_tokens=10,
        prompt_tokens_details=SimpleNamespace(cached_tokens=20),
    ))
    assert usage_and_cost(responses, pricing) == (100, 40, 20, 0.00015)
    assert usage_and_cost(chat, pricing) == (50, 20, 10, 0.000075)


def test_historical_baseline_is_fair_durable_and_dry_run_safe(configured):
    with connect(configured.database_path) as connection:
        item_ids = []
        for group_number, item_count in ((1, 5), (2, 2)):
            group = connection.execute(
                "INSERT INTO groups(title,position,created_at) VALUES(?,?,?)",
                (f"Group {group_number}", group_number, utcnow()),
            ).lastrowid
            feed = connection.execute(
                "INSERT INTO feeds(group_id,title,xml_url,created_at) VALUES(?,?,?,?)",
                (group, f"Feed {group_number}", f"https://example.test/{group_number}", utcnow()),
            ).lastrowid
            for item_number in range(item_count):
                item_ids.append(int(connection.execute(
                    """INSERT INTO items(feed_id,stable_id,title,discovered_at)
                       VALUES(?,?,?,?)""",
                    (
                        feed, f"{group_number}-{item_number}",
                        f"Item {group_number}-{item_number}", utcnow(),
                    ),
                ).lastrowid))

        preview = baseline_backlog(
            connection, configured, max_items=3, max_per_feed=2, dry_run=True,
        )
        assert preview == {"eligible": 3, "baselined": 4, "examined": 7}
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE summary_eligible=0"
        ).fetchone()[0] == 0

        applied = baseline_backlog(
            connection, configured, max_items=3, max_per_feed=2,
        )
        assert applied == preview
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE summary_eligible=1"
        ).fetchone()[0] == 3
        archived = connection.execute(
            "SELECT COUNT(*) FROM ai_review_queue WHERE state='archived'"
        ).fetchone()[0]
        assert archived == 4
        assert len(item_ids) == 7
