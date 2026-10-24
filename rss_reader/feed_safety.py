"""Respectful source-protection detection. No challenge is followed or executed."""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

class ProviderChallenge(ValueError):
    def __init__(self, status: int, content_type: str, retry_at: datetime):
        self.status_code = status
        self.retry_at = retry_at
        self.content_type = content_type
        super().__init__(f'provider_challenge: source returned an anti-bot page (HTTP {status}, {content_type}); ordinary RSS retrieval deferred until {retry_at.isoformat()}')


def retry_deadline(value: str, now: datetime) -> datetime:
    default = now + timedelta(hours=12)
    try:
        seconds = int(str(value).strip())
        parsed = now + timedelta(seconds=max(0, min(seconds, 7*86400)))
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = parsedate_to_datetime(str(value))
            parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (TypeError, ValueError, OverflowError):
            return default
    return max(default, min(parsed, now+timedelta(days=7)))


def check_provider_response(content: bytes, status: int, headers) -> None:
    head = content[:32768].decode('utf-8','replace').casefold()
    content_type = str(headers.get('Content-Type', headers.get('content-type', ''))).split(';')[0].strip().lower()
    html = content_type == 'text/html' or '<!doctype html' in head or '<html' in head
    challenge = any(value in head for value in ('/.within.website/x/', 'anubis', 'making sure you', 'verify you are human', 'checking your browser'))
    if html and challenge:
        raise ProviderChallenge(status, content_type or 'text/html', retry_deadline(headers.get('Retry-After',''), datetime.now(UTC)))
