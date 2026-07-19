from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Paper:
    arxiv_id: str
    version: str | None
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    primary_category: str | None
    link: str
    pdf_link: str | None
    published: datetime | None
    updated: datetime | None
    source: str
    announce_type: str | None = None
    source_categories: list[str] = field(default_factory=list)


@dataclass
class LocalScore:
    score: int
    author_hits: list[str] = field(default_factory=list)
    keyword_hits_strong: list[str] = field(default_factory=list)
    keyword_hits_medium: list[str] = field(default_factory=list)
    keyword_hits_weak: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class Decision:
    local_score: int
    llm_score: int | None
    final_score: float
    decision: str
    why: str
    tags: list[str]
    local_reasons: list[str]
