from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AIError:
    code: str
    message: str
    retryable: bool


def classify_ai_error(error: BaseException | str) -> AIError:
    """Map provider/library failures to stable, credential-safe UI states."""
    text = str(error or "").strip()
    lowered = text.casefold()
    status: Any = getattr(error, "status_code", None) if not isinstance(error, str) else None
    if "is not available to the distillfeed server" in lowered or "api key" in lowered and "missing" in lowered:
        return AIError(
            "API_KEY_MISSING",
            "The configured API key is not available to the DistillFeed server. Set it in the server environment and restart DistillFeed.",
            False,
        )
    if status == 401 or "invalid_api_key" in lowered or "incorrect api key" in lowered:
        return AIError(
            "AUTH_REJECTED",
            "OpenAI rejected the configured API key (401). Replace the server environment value and restart DistillFeed.",
            False,
        )
    if status == 403:
        return AIError(
            "MODEL_ACCESS_DENIED",
            "The provider refused this model request (403). Check the API project's model permissions.",
            False,
        )
    if status == 404 or "model_not_found" in lowered:
        return AIError(
            "MODEL_UNAVAILABLE",
            "The configured model is not available to this API project. Choose an accessible model in AI settings.",
            False,
        )
    if status == 429 and any(marker in lowered for marker in ("insufficient_quota", "quota", "billing")):
        return AIError(
            "INSUFFICIENT_QUOTA",
            "The provider reported insufficient quota or credit (429). DistillFeed cannot read the account balance; review provider billing before retrying.",
            False,
        )
    if status == 429 or "rate limit" in lowered:
        return AIError(
            "RATE_LIMITED",
            "The provider rate limit was reached (429). Waiting entries were retained and can be retried later.",
            True,
        )
    if isinstance(error, (json.JSONDecodeError, ValueError)) or any(
        marker in lowered for marker in ("json", "did not contain", "response was incomplete")
    ):
        return AIError(
            "INVALID_PROVIDER_RESPONSE",
            "The provider returned an incomplete or invalid structured response. Existing summaries were kept and the work can be retried.",
            True,
        )
    try:
        server_error = status is not None and int(status) >= 500
    except (TypeError, ValueError):
        server_error = False
    if server_error or any(
        marker in lowered for marker in ("connection", "timed out", "timeout", "temporarily unavailable")
    ):
        return AIError(
            "PROVIDER_UNREACHABLE",
            "The AI provider was temporarily unavailable. Waiting entries and existing summaries were retained for retry.",
            True,
        )
    safe = re.sub(r"\bsk-[A-Za-z0-9_.*-]{8,}", "[redacted OpenAI key]", text)[:1200]
    safe = safe or "The AI provider request failed"
    return AIError("AI_REQUEST_FAILED", safe, True)


def stored_ai_error(error: BaseException | str) -> str:
    failure = classify_ai_error(error)
    return f"{failure.code}: {failure.message}"
