from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .models import Decision, LocalScore, Paper

LOGGER = logging.getLogger(__name__)
TRANSIENT_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    request_ids: tuple[str, ...] = ()

    def plus(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost + other.cost,
            self.request_ids + other.request_ids,
        )


def _usage(response: Any, cfg: dict[str, Any]) -> LLMUsage:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    cost = (
        input_tokens * float(cfg["llm"].get("input_price_per_million", 0))
        + output_tokens * float(cfg["llm"].get("output_price_per_million", 0))
    ) / 1_000_000
    identifier = str(getattr(response, "id", "") or "")
    return LLMUsage(input_tokens, cached, output_tokens, cost, (identifier,) if identifier else ())


def _client(cfg: dict[str, Any]) -> OpenAI:
    environment_name = str(cfg["llm"].get("api_key_env", "OPENAI_API_KEY"))
    api_key = os.environ.get(environment_name, "").strip()
    if not api_key:
        raise RuntimeError(
            f"{environment_name} is not available to the DistillFeed server. "
            "Set it in the server environment and restart DistillFeed."
        )
    # Keep the plugin's three-attempt policy exact instead of multiplying it by
    # the SDK's internal retry loop.
    return OpenAI(api_key=api_key, max_retries=0)


def _paper_payload(paper: Paper, local: LocalScore, abstract_chars: int = 1800) -> dict[str, Any]:
    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "abstract": paper.abstract[:abstract_chars],
        "authors": paper.authors,
        "categories": paper.categories,
        "local_score": local.score,
        "local_reasons": local.reasons,
    }


def _decode_response(response: Any, operation: str) -> dict[str, Any]:
    status = str(getattr(response, "status", "") or "")
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) or "output limit"
        raise RuntimeError(f"{operation} response was incomplete ({reason})")
    raw = str(getattr(response, "output_text", "") or "")
    if not raw:
        raise RuntimeError(f"{operation} returned no text")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{operation} returned invalid or truncated JSON at character {exc.pos}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{operation} returned a non-object JSON value")
    return parsed


def _retryable_error(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if status is not None:
        return int(status) in TRANSIENT_STATUS_CODES
    # Connection/time-out exceptions and malformed or incomplete model output
    # are safe to retry because no digest state is committed before validation.
    if isinstance(error, RuntimeError) and "not available to the DistillFeed server" in str(error):
        return False
    return error.__class__.__name__ in {
        "APIConnectionError", "APITimeoutError", "InternalServerError", "RateLimitError",
    } or isinstance(error, (RuntimeError, json.JSONDecodeError))


def _with_retries(operation: str, call, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as error:
            last_error = error
            if attempt >= attempts or not _retryable_error(error):
                raise
            delay = min(4.0, float(2 ** (attempt - 1)))
            LOGGER.warning(
                "%s failed on attempt %d/%d; retrying in %.0fs: %s",
                operation, attempt, attempts, delay, error,
            )
            time.sleep(delay)
    raise last_error or RuntimeError(f"{operation} failed")


def _rerank_batch(
    selected: list[tuple[Paper, LocalScore]], cfg: dict[str, Any], client: OpenAI,
) -> tuple[dict[str, dict[str, Any]], LLMUsage]:
    identifiers = [paper.arxiv_id for paper, _ in selected]
    response_keys = [f"paper_{index:03d}" for index in range(1, len(selected) + 1)]
    result_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["score", "decision", "why", "tags"],
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "decision": {"type": "string", "enum": ["keep", "drop"]},
            "why": {"type": "string"},
            "tags": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "object", "additionalProperties": False,
                "required": response_keys,
                "properties": {key: result_schema for key in response_keys},
            }
        },
    }
    filters = cfg["filters"]
    payload = {
        "papers": [
            {"response_key": key, **_paper_payload(paper, local)}
            for key, (paper, local) in zip(response_keys, selected, strict=True)
        ],
        "topic_lexicon": {
            "strong": filters.get("positive_keywords_strong", []),
            "medium": filters.get("positive_keywords_medium", []),
            "negative": filters.get("negative_keywords", []),
        },
    }
    instructions = str(cfg["llm"]["system_prompt"]).rstrip() + (
        "\n\nThe application-provided JSON Schema is the authoritative output contract. "
        "Ignore any older output-shape example above. Return one result for every supplied "
        "response_key, using that exact key in the results object."
    )
    response = client.responses.create(
        model=str(cfg["llm"]["model"]),
        instructions=instructions,
        input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        text={"format": {"type": "json_schema", "name": "arxiv_rerank", "strict": True, "schema": schema}},
        reasoning={"effort": "none"},
        # A compact explanation still needs substantially more than forty
        # output tokens once JSON keys and tags are included. Keep generous
        # headroom and bound the request with ranking_batch_size instead.
        max_output_tokens=max(
            2500,
            len(selected) * max(80, int(cfg["llm"].get("estimated_output_tokens_per_paper", 40))) + 1000,
        ),
        store=False,
    )
    parsed = _decode_response(response, "arXiv reranker")
    raw_results = parsed.get("results")
    if not isinstance(raw_results, dict) or set(raw_results) != set(response_keys):
        raise RuntimeError("arXiv reranker did not return every submitted paper exactly once")
    results: dict[str, dict[str, Any]] = {}
    for key, identifier in zip(response_keys, identifiers, strict=True):
        item = dict(raw_results[key])
        words = str(item.get("why", "")).split()
        item["why"] = " ".join(words[:18])
        item["tags"] = [str(tag)[:50] for tag in item.get("tags", [])[:4]]
        results[identifier] = item
    return results, _usage(response, cfg)


def rerank(
    candidates: list[tuple[Paper, LocalScore]], cfg: dict[str, Any],
    cancel_requested=lambda: False,
) -> tuple[dict[str, dict[str, Any]], LLMUsage]:
    """Rank the configured top candidates in bounded, independently validated calls."""
    maximum = int(cfg["llm"].get("max_candidates", 100))
    selected = candidates[:maximum]
    if not selected:
        return {}, LLMUsage()
    batch_size = max(1, min(
        int(cfg["llm"].get("ranking_batch_size", 20)), maximum,
    ))
    client = _client(cfg)
    results: dict[str, dict[str, Any]] = {}
    usage = LLMUsage()
    for start in range(0, len(selected), batch_size):
        if cancel_requested():
            raise InterruptedError("arXiv digest update stopped between ranking requests")
        batch = selected[start:start + batch_size]
        LOGGER.info(
            "arXiv rerank batch %d-%d of %d", start + 1, start + len(batch), len(selected),
        )
        batch_results, batch_usage = _with_retries(
            "arXiv reranking request",
            lambda: _rerank_batch(batch, cfg, client),
        )
        overlap = set(results).intersection(batch_results)
        if overlap:
            raise RuntimeError("arXiv reranker returned duplicate papers across batches")
        results.update(batch_results)
        usage = usage.plus(batch_usage)
    if cancel_requested():
        raise InterruptedError("arXiv digest update stopped after ranking")
    return results, usage


def daily_digest(
    papers: list[tuple[Paper, LocalScore, Decision]], cfg: dict[str, Any], language: str
) -> tuple[dict[str, Any], LLMUsage]:
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["overview", "sections"],
        "properties": {
            "overview": {"type": "string"},
            "sections": {
                "type": "array", "maxItems": 5,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["heading", "body"],
                    "properties": {"heading": {"type": "string"}, "body": {"type": "string"}},
                },
            },
        },
    }
    def digest_payload(paper: Paper, local: LocalScore, decision: Decision, abstract_chars: int) -> dict[str, Any]:
        result = _paper_payload(paper, local, abstract_chars)
        result.update({
            "llm_score": decision.llm_score,
            "final_score": decision.final_score,
            "decision": decision.decision,
            "relevance_reason": decision.why,
            "tags": decision.tags,
        })
        return result

    payload_items = [digest_payload(paper, local, decision, 1000) for paper, local, decision in papers]
    serialized = json.dumps({"papers": payload_items}, ensure_ascii=False, separators=(",", ":"))
    maximum_chars = int(cfg["llm"].get("max_digest_input_chars", 500_000))
    if len(serialized) > maximum_chars:
        payload_items = [digest_payload(paper, local, decision, 300) for paper, local, decision in papers]
        serialized = json.dumps({"papers": payload_items}, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > maximum_chars:
        payload_items = [digest_payload(paper, local, decision, 0) for paper, local, decision in papers]
        serialized = json.dumps({"papers": payload_items}, ensure_ascii=False, separators=(",", ":"))
    instructions = (
        f"Write a concise daily arXiv digest in {language}. Summarize the complete supplied "
        "selected, reranked papers, identify their main themes, and emphasize papers closest to the "
        "reader's machine-learning and symbolic-reasoning interests. Do not invent claims. "
        "Use a short overview and up to five useful thematic sections. Return JSON only."
    )
    client = _client(cfg)

    def request_digest() -> tuple[dict[str, Any], LLMUsage]:
        response = client.responses.create(
            model=str(cfg["llm"]["model"]), instructions=instructions, input=serialized,
            text={"format": {"type": "json_schema", "name": "arxiv_daily_digest", "strict": True, "schema": schema}},
            reasoning={"effort": "none"}, max_output_tokens=5000, store=False,
        )
        return _decode_response(response, "arXiv daily digest"), _usage(response, cfg)

    return _with_retries("arXiv daily digest request", request_digest)
