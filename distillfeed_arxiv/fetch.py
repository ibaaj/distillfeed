from __future__ import annotations

import html
import logging
import random
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import requests
from defusedxml import ElementTree as ET

from .models import Paper

LOGGER = logging.getLogger(__name__)
ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
DC = "http://purl.org/dc/elements/1.1/"
TRANSIENT = {429, 500, 502, 503, 504}


def clean_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def parse_arxiv_id(url: str) -> tuple[str, str | None]:
    value = url.rstrip("/").split("/abs/")[-1].split("/pdf/")[-1].removesuffix(".pdf")
    match = re.search(r"((?:\d{4}\.\d{4,5})|(?:[A-Za-z.-]+/\d{7}))(v\d+)?$", value)
    if not match:
        raise ValueError(f"Not an arXiv paper URL: {url}")
    return match.group(1), match.group(2)


def _get_text(url: str, *, user_agent: str, timeout: int = 45, attempts: int = 4) -> str:
    headers = {"User-Agent": user_agent, "Accept": "application/xml, text/xml;q=0.9"}
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code in TRANSIENT and attempt + 1 < attempts:
                retry = response.headers.get("Retry-After", "").strip()
                delay = float(retry) if retry.isdecimal() else min(30.0, 2.0 ** attempt + random.random())
                time.sleep(delay)
                continue
            response.raise_for_status()
            if len(response.content) > 20 * 1024 * 1024:
                raise RuntimeError("arXiv response exceeded 20 MiB")
            return response.content.decode(response.encoding or "utf-8", errors="replace")
        except requests.RequestException:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(30.0, 2.0 ** attempt + random.random()))
    raise RuntimeError("arXiv request failed")


def _rss_description(value: str) -> tuple[str | None, str]:
    text = html.unescape(re.sub(r"<[^>]+>", "\n", value or ""))
    announce = re.search(r"Announce Type:\s*([^\n]+)", text, flags=re.I)
    abstract = re.search(r"Abstract:\s*(.*)", text, flags=re.I | re.S)
    return (announce.group(1).strip() if announce else None, clean_text(abstract.group(1) if abstract else text))


def parse_rss(xml_text: str, category: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        try:
            arxiv_id, version = parse_arxiv_id(link)
        except ValueError:
            continue
        announce, abstract = _rss_description(item.findtext("description") or "")
        creators = [node.text.strip() for node in item.findall(f"{{{DC}}}creator") if node.text]
        authors = [part.strip() for part in creators[0].split(",") if part.strip()] if len(creators) == 1 and "," in creators[0] else creators
        categories = [node.text.strip() for node in item.findall("category") if node.text] or [category]
        published_text = item.findtext("pubDate")
        published = parsedate_to_datetime(published_text).astimezone(UTC) if published_text else None
        papers.append(Paper(
            arxiv_id=arxiv_id, version=version, title=title, abstract=abstract,
            authors=authors, categories=list(dict.fromkeys(categories)), primary_category=category,
            link=link, pdf_link=link.replace("/abs/", "/pdf/") + ".pdf",
            published=published, updated=published, source="rss", announce_type=announce,
            source_categories=[category],
        ))
    return papers


def fetch_rss(category: str, cfg: dict[str, Any]) -> list[Paper]:
    user_agent = str(cfg["app"]["user_agent"])
    url = f"https://rss.arxiv.org/rss/{category}"
    return parse_rss(_get_text(url, user_agent=user_agent), category)


def parse_atom(xml_text: str, selected_categories: list[str]) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    namespaces = {"atom": ATOM, "arxiv": ARXIV}
    for entry in root.findall("atom:entry", namespaces):
        title = clean_text(entry.findtext("atom:title", default="", namespaces=namespaces))
        link = entry.findtext("atom:id", default="", namespaces=namespaces).strip()
        if not title or not link:
            continue
        arxiv_id, version = parse_arxiv_id(link)
        authors = [clean_text(author.findtext("atom:name", default="", namespaces=namespaces)) for author in entry.findall("atom:author", namespaces)]
        authors = [author for author in authors if author]
        categories = [node.attrib.get("term", "").strip() for node in entry.findall("atom:category", namespaces)]
        categories = [category for category in categories if category]
        primary_node = entry.find("arxiv:primary_category", namespaces)
        primary = primary_node.attrib.get("term") if primary_node is not None else None
        pdf_url = next((node.attrib.get("href") for node in entry.findall("atom:link", namespaces) if node.attrib.get("title") == "pdf"), None)
        published_text = entry.findtext("atom:published", default="", namespaces=namespaces).strip()
        updated_text = entry.findtext("atom:updated", default="", namespaces=namespaces).strip()
        published = datetime.fromisoformat(published_text).astimezone(UTC) if published_text else None
        updated = datetime.fromisoformat(updated_text).astimezone(UTC) if updated_text else None
        sources = [category for category in selected_categories if category in categories]
        papers.append(Paper(
            arxiv_id, version, title,
            clean_text(entry.findtext("atom:summary", default="", namespaces=namespaces)),
            authors, categories, primary, link, pdf_url, published, updated, "api",
            source_categories=sources,
        ))
    return papers


def fetch_api_window(categories: list[str], since: datetime, until: datetime, cfg: dict[str, Any]) -> list[Paper]:
    page_size = int(cfg["arxiv"].get("api_page_size", 100))
    pause = float(cfg["arxiv"].get("api_pause_seconds", 5.0))
    user_agent = str(cfg["app"]["user_agent"])
    papers: list[Paper] = []
    for category in categories:
        start = 0
        while True:
            # urlencode owns escaping. Literal '+' characters here were escaped
            # as %2B, turning arXiv's boolean/date expression into literal text
            # and making the initial weekend backfill appear empty. arXiv's
            # documented submittedDate range is UTC to minute precision.
            query = (
                f"cat:{category} AND submittedDate:"
                f"[{since.astimezone(UTC).strftime('%Y%m%d%H%M')} TO "
                f"{until.astimezone(UTC).strftime('%Y%m%d%H%M')}]"
            )
            url = "https://export.arxiv.org/api/query?" + urlencode({
                "search_query": query, "start": start, "max_results": page_size,
                "sortBy": "submittedDate", "sortOrder": "ascending",
            })
            batch = parse_atom(_get_text(url, user_agent=user_agent, timeout=60), categories)
            papers.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
            time.sleep(pause)
        time.sleep(pause)
    return papers


def merge_papers(*collections: list[Paper]) -> list[Paper]:
    merged: dict[str, Paper] = {}
    for collection in collections:
        for paper in collection:
            current = merged.get(paper.arxiv_id)
            if current is None:
                merged[paper.arxiv_id] = paper
                continue
            prefer = paper if paper.source == "api" else current
            fallback = current if prefer is paper else paper
            prefer.categories = list(dict.fromkeys(prefer.categories + fallback.categories))
            prefer.source_categories = list(dict.fromkeys(prefer.source_categories + fallback.source_categories))
            prefer.announce_type = prefer.announce_type or fallback.announce_type
            prefer.source = "api+rss"
            merged[paper.arxiv_id] = prefer
    return list(merged.values())
