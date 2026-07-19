from __future__ import annotations

from typing import Any


def _usage_value(usage: Any, name: str, default: int = 0) -> int:
    value = getattr(usage, name, default)
    return int(value or 0)


def usage_and_cost(
    response: Any, pricing: dict[str, Any],
) -> tuple[int, int, int, float]:
    """Normalize Responses/Chat usage and estimate provider cost."""
    usage = getattr(response, "usage", None)
    input_tokens = _usage_value(usage, "input_tokens") if usage else 0
    output_tokens = _usage_value(usage, "output_tokens") if usage else 0
    input_details = getattr(usage, "input_tokens_details", None) if usage else None
    if usage and not input_tokens:
        input_tokens = _usage_value(usage, "prompt_tokens")
        output_tokens = _usage_value(usage, "completion_tokens")
        input_details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = _usage_value(input_details, "cached_tokens") if input_details else 0
    cost = (
        max(0, input_tokens - cached_tokens) * float(pricing["input"])
        + cached_tokens * float(pricing["cached_input"])
        + output_tokens * float(pricing["output"])
    ) / 1_000_000
    return input_tokens, cached_tokens, output_tokens, cost
