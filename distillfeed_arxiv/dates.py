"""One canonical announcement-day policy shared by planning, execution and UI.

Submission dates never masquerade as announcement dates. Legacy records with no
recoverable announcement evidence remain undated; no weekday is guessed.
"""
from __future__ import annotations
from datetime import UTC, datetime
from typing import Any


def parse_stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def day_of(value: Any) -> str:
    stamp = parse_stamp(value)
    return stamp.date().isoformat() if stamp else 'undated'


def ensure_date_schema(connection) -> None:
    cols = {r['name'] for r in connection.execute('PRAGMA table_info(distillfeed_arxiv_papers)')}
    if not cols:
        return
    additions = {
        'submitted_at': 'TEXT', 'updated_at': 'TEXT', 'announced_at': 'TEXT',
        'announcement_day': 'TEXT', 'announcement_source': 'TEXT',
        'llm_model': 'TEXT', 'llm_rubric_version': 'TEXT', 'llm_scored_at': 'TEXT',
        'llm_input_fingerprint': 'TEXT', 'llm_batch_id': 'TEXT',
    }
    for name, kind in additions.items():
        if name not in cols:
            connection.execute(f'ALTER TABLE distillfeed_arxiv_papers ADD COLUMN {name} {kind}')
    # Never overwrite announcement evidence already supplied by a later release.
    connection.execute('''UPDATE distillfeed_arxiv_papers
        SET announced_at=(SELECT i.published_at FROM items i WHERE i.id=item_id),
            announcement_day=(SELECT date(i.published_at) FROM items i WHERE i.id=item_id),
            announcement_source='rss-legacy'
        WHERE source='rss' AND announcement_day IS NULL
          AND COALESCE(announcement_source,'')='' ''')
    connection.execute('''UPDATE distillfeed_arxiv_papers
        SET submitted_at=COALESCE(submitted_at,(SELECT i.published_at FROM items i WHERE i.id=item_id)),
            announcement_source=CASE WHEN announcement_day IS NOT NULL THEN 'legacy' ELSE 'unknown' END
        WHERE COALESCE(announcement_source,'')='' ''')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_arxiv_announcement_day ON distillfeed_arxiv_papers(announcement_day,evaluation_status)')
    # Derived identity is separate from the source dates; unknown is explicit.
    connection.execute('''CREATE TABLE IF NOT EXISTS distillfeed_arxiv_daily_briefs(
        day TEXT PRIMARY KEY, summary_id INTEGER NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL, completed_at TEXT NOT NULL)''')


def canonical_day_sql(connection, item='i', paper='ap') -> str:
    cols = {r['name'] for r in connection.execute('PRAGMA table_info(distillfeed_arxiv_papers)')}
    ordinary = f"COALESCE(date({item}.published_at),date({item}.discovered_at),'undated')"
    if 'announcement_day' not in cols:
        return ordinary
    return (f"CASE WHEN {paper}.item_id IS NOT NULL THEN "
            f"COALESCE(date({paper}.announced_at),date({paper}.announcement_day),'undated') "
            f"ELSE {ordinary} END")
