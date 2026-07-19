from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import Decision, LocalScore, Paper


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", value).strip()


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    normalized = normalize_text(text)
    return unique([keyword for keyword in keywords if normalize_text(keyword) in normalized])


def match_authors(authors: list[str], seeds: list[str]) -> list[str]:
    normalized_authors = [normalize_text(author) for author in authors]
    matches: list[str] = []
    for seed in seeds:
        normalized_seed = normalize_text(seed)
        if normalized_seed and any(
            normalized_seed == author or normalized_seed in author or author in normalized_seed
            for author in normalized_authors
        ):
            matches.append(seed)
    return unique(matches)


def compute_local_score(paper: Paper, cfg: dict[str, Any]) -> LocalScore:
    filters = cfg["filters"]
    author_hits = match_authors(paper.authors, filters.get("preferred_authors", []))
    blocked_hits = match_authors(paper.authors, filters.get("blocked_authors_exact", []))
    if blocked_hits:
        return LocalScore(-999, author_hits=blocked_hits, reasons=[f"blocked author match: {', '.join(blocked_hits[:3])}"])
    strong = unique(match_keywords(paper.title, filters.get("positive_keywords_strong", [])) + match_keywords(paper.abstract, filters.get("positive_keywords_strong", [])))
    medium = unique(match_keywords(paper.title, filters.get("positive_keywords_medium", [])) + match_keywords(paper.abstract, filters.get("positive_keywords_medium", [])))
    weak = unique(match_keywords(paper.title, filters.get("positive_keywords_weak", [])) + match_keywords(paper.abstract, filters.get("positive_keywords_weak", [])))
    negative = unique(match_keywords(paper.title, filters.get("negative_keywords", [])) + match_keywords(paper.abstract, filters.get("negative_keywords", [])))
    score = 0
    reasons: list[str] = []
    if author_hits:
        bonus = 6 * len(author_hits); score += bonus; reasons.append(f"preferred author match (+{bonus})")
    if strong:
        bonus = 4 * len(strong); score += bonus; reasons.append(f"strong topic hits (+{bonus})")
    if medium:
        bonus = 2 * len(medium); score += bonus; reasons.append(f"medium topic hits (+{bonus})")
    if weak:
        bonus = min(len(weak), 4); score += bonus; reasons.append(f"weak bridge hits (+{bonus})")
    categories = {category.casefold() for category in paper.categories}
    category_bonus = str(filters.get("category_bonus", "")).strip()
    normalized_bonus = category_bonus.casefold()
    category_points = int(filters.get("category_bonus_points", 0))
    if normalized_bonus and normalized_bonus in categories and category_points:
        score += category_points; reasons.append(f"{category_bonus} bonus ({category_points:+d})")
    pair_points = int(filters.get("cross_category_bonus_points", 0))
    for configured_pair in filters.get("cross_category_bonuses", []):
        pair = {part.strip().casefold() for part in str(configured_pair).split("+") if part.strip()}
        if len(pair) >= 2 and pair <= categories and pair_points:
            score += pair_points; reasons.append(f"{configured_pair} bonus ({pair_points:+d})")
    no_signal_penalty = int(filters.get("no_signal_penalty", 0))
    if not strong and not medium and not author_hits and (
        not normalized_bonus or normalized_bonus not in categories
    ) and no_signal_penalty:
        score += no_signal_penalty; reasons.append(f"no strong topic signal ({no_signal_penalty:+d})")
    if negative:
        penalty = 4 * len(negative); score -= penalty; reasons.append(f"out-of-scope hits (-{penalty})")
    bridge_bonus = int(filters.get("category_bridge_bonus", 0))
    if not strong and not medium and normalized_bonus and normalized_bonus in categories and bridge_bonus:
        score += bridge_bonus; reasons.append(f"category bridge ({bridge_bonus:+d})")
    return LocalScore(score, author_hits, strong, medium, weak, negative, reasons)


def compact_local_why(local: LocalScore) -> str:
    parts: list[str] = []
    if local.author_hits:
        parts.append(f"author match: {', '.join(local.author_hits[:2])}")
    if local.keyword_hits_strong:
        parts.append(f"strong: {', '.join(local.keyword_hits_strong[:3])}")
    elif local.keyword_hits_medium:
        parts.append(f"medium: {', '.join(local.keyword_hits_medium[:3])}")
    if local.negative_hits:
        parts.append(f"negative: {', '.join(local.negative_hits[:2])}")
    return "; ".join(parts[:3]) if parts else "local topic filter"


def decide(local: LocalScore, llm_result: dict[str, Any] | None, cfg: dict[str, Any]) -> Decision:
    filters = cfg["filters"]
    if local.score <= -999:
        return Decision(local.score, None, float(local.score), "drop", local.reasons[0], ["blocked"], local.reasons)
    if llm_result:
        llm_score = max(0, min(100, int(llm_result.get("score", 0))))
        final_score = float(filters.get("local_weight", 1.0)) * local.score + float(filters.get("llm_weight", .35)) * llm_score
        keep = llm_result.get("decision") == "keep" and final_score >= float(filters.get("final_keep_threshold", 25))
        if not keep and local.score >= int(filters.get("local_keep_threshold", 6)) + 8 and llm_score >= 40:
            keep = True
        return Decision(local.score, llm_score, final_score, "keep" if keep else "drop", str(llm_result.get("why") or compact_local_why(local)), [str(tag)[:50] for tag in llm_result.get("tags", [])[:4]], local.reasons)
    keep = local.score >= int(filters.get("local_keep_threshold", 6))
    return Decision(local.score, None, float(local.score), "keep" if keep else "drop", compact_local_why(local), [], local.reasons)
