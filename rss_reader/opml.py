from __future__ import annotations

import logging
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from defusedxml import ElementTree as SafeET

from .db import ensure_ungrouped, transaction, utcnow

LOGGER = logging.getLogger(__name__)
MAX_OPML_OUTLINES = 10_000
MAX_OPML_DEPTH = 50


@dataclass
class FeedOutline:
    title: str
    xml_url: str
    html_url: str | None = None
    llm_enabled: bool | None = None
    position: int = 0
    ai_mode: str | None = None


@dataclass
class GroupOutline:
    title: str
    groups: list["GroupOutline"] = field(default_factory=list)
    feeds: list[FeedOutline] = field(default_factory=list)
    llm_enabled: bool | None = None
    position: int = 0
    ai_priority: str | None = None
    summary_interval_hours: int | None = None
    summary_item_budget: int | None = None
    ai_mode: str | None = None


def _attr(element: ET.Element, name: str) -> str | None:
    target = name.casefold()
    for key, value in element.attrib.items():
        if key.casefold() == target and value.strip():
            return value.strip()
    return None


def parse_opml_bytes(content: bytes) -> list[GroupOutline]:
    root = SafeET.fromstring(content)
    if root.tag.rsplit("}", 1)[-1].casefold() != "opml":
        raise ValueError("The document root is not OPML")
    body = next((node for node in root if node.tag.rsplit("}", 1)[-1].casefold() == "body"), None)
    if body is None:
        raise ValueError("The OPML document has no body")

    top_groups: list[GroupOutline] = []
    loose = GroupOutline("Ungrouped")
    outline_count = 0

    def visit(
        node: ET.Element, destination: GroupOutline | None, depth: int = 1, position: int = 0
    ) -> None:
        nonlocal outline_count
        outline_count += 1
        if outline_count > MAX_OPML_OUTLINES:
            raise ValueError(f"OPML contains more than {MAX_OPML_OUTLINES} outlines")
        if depth > MAX_OPML_DEPTH:
            raise ValueError(f"OPML nesting exceeds {MAX_OPML_DEPTH} levels")
        xml_url = _attr(node, "xmlUrl") or _attr(node, "url") if (_attr(node, "type") or "").casefold() in {"rss", "atom"} else _attr(node, "xmlUrl")
        title = _attr(node, "text") or _attr(node, "title") or xml_url or "Untitled"
        llm_attribute = _attr(node, "llmEnabled")
        llm_enabled = None if llm_attribute is None else llm_attribute.casefold() not in {"false", "0", "no"}
        stored_position = _attr(node, "distillfeedPosition")
        if stored_position is not None:
            try:
                position = max(0, int(stored_position))
            except ValueError as exc:
                raise ValueError("An OPML subscription position is invalid") from exc
        if xml_url:
            if len(xml_url) > 4096:
                raise ValueError("An OPML feed URL exceeds the 4096-character limit")
            if len(title) > 300:
                raise ValueError("An OPML feed title exceeds the 300-character limit")
            feed_mode = _attr(node, "distillfeedAIMode")
            if feed_mode is not None:
                feed_mode = feed_mode.casefold()
                if feed_mode not in {"inherit", "automatic", "manual", "off"}:
                    raise ValueError("An OPML feed AI mode is invalid")
            (destination or loose).feeds.append(
                FeedOutline(
                    title, xml_url, _attr(node, "htmlUrl"), llm_enabled, position, feed_mode,
                )
            )
            return
        if len(title) > 200:
            raise ValueError("An OPML group title exceeds the 200-character limit")
        priority = _attr(node, "distillfeedAIPriority")
        if priority is not None:
            priority = priority.casefold()
            if priority not in {"high", "normal", "low", "manual", "off"}:
                raise ValueError("An OPML AI group priority is invalid")
        group_mode = _attr(node, "distillfeedAIMode")
        if group_mode is not None:
            group_mode = group_mode.casefold()
            if group_mode not in {"automatic", "manual", "off"}:
                raise ValueError("An OPML group AI mode is invalid")
        try:
            interval = int(_attr(node, "distillfeedSummaryIntervalHours")) if _attr(node, "distillfeedSummaryIntervalHours") is not None else None
            budget = int(_attr(node, "distillfeedSummaryItemBudget")) if _attr(node, "distillfeedSummaryItemBudget") is not None else None
        except ValueError as exc:
            raise ValueError("An OPML AI group limit is invalid") from exc
        if interval is not None and not 0 <= interval <= 8760:
            raise ValueError("An OPML summary interval is invalid")
        if budget is not None and not 0 <= budget <= 1000:
            raise ValueError("An OPML summary item budget is invalid")
        group = GroupOutline(
            title, llm_enabled=llm_enabled, position=position, ai_priority=priority,
            summary_interval_hours=interval, summary_item_budget=budget,
            ai_mode=group_mode,
        )
        (destination.groups if destination else top_groups).append(group)
        child_position = 0
        for child in node:
            if child.tag.rsplit("}", 1)[-1].casefold() == "outline":
                visit(child, group, depth + 1, child_position)
                child_position += 1

    position = 0
    for outline in body:
        if outline.tag.rsplit("}", 1)[-1].casefold() == "outline":
            visit(outline, None, position=position)
            position += 1
    if loose.feeds:
        top_groups.insert(0, loose)
    return top_groups


def import_groups(connection, groups: Iterable[GroupOutline]) -> tuple[int, int]:
    group_count = 0
    feed_count = 0

    def upsert_group(group: GroupOutline, parent_id: int | None, position: int) -> None:
        nonlocal group_count, feed_count
        if parent_id is None:
            row = connection.execute(
                "SELECT id FROM groups WHERE parent_id IS NULL AND title = ?", (group.title,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id FROM groups WHERE parent_id = ? AND title = ?", (parent_id, group.title)
            ).fetchone()
        if row:
            group_id = int(row["id"])
            connection.execute(
                """UPDATE groups SET position = ?,
                   llm_enabled = CASE WHEN ? IS NULL THEN llm_enabled ELSE ? END,
                   ai_mode = CASE WHEN ? IS NULL THEN ai_mode ELSE ? END,
                   ai_priority = CASE WHEN ? IS NULL THEN ai_priority ELSE ? END,
                   summary_interval_hours = CASE WHEN ? IS NULL THEN summary_interval_hours ELSE ? END,
                   summary_item_budget = CASE WHEN ? IS NULL THEN summary_item_budget ELSE ? END
                   WHERE id = ?""",
                (
                    position, group.llm_enabled,
                    int(group.llm_enabled) if group.llm_enabled is not None else None,
                    group.ai_mode, group.ai_mode,
                    group.ai_priority, group.ai_priority,
                    group.summary_interval_hours, group.summary_interval_hours,
                    group.summary_item_budget, group.summary_item_budget, group_id,
                ),
            )
        else:
            group_id = int(
                connection.execute(
                    """INSERT INTO groups(
                           parent_id,title,position,llm_enabled,ai_mode,ai_priority,
                           summary_interval_hours,summary_item_budget,created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        parent_id, group.title, position,
                        int(group.llm_enabled if group.llm_enabled is not None else True),
                        group.ai_mode or (
                            group.ai_priority if group.ai_priority in {"manual", "off"} else "automatic"
                        ),
                        group.ai_priority or "normal", group.summary_interval_hours or 0,
                        group.summary_item_budget or 0, utcnow(),
                    ),
                ).lastrowid
            )
        group_count += 1
        for index, feed in enumerate(group.feeds):
            feed_position = int(feed.position if feed.position is not None else index)
            existing = connection.execute("SELECT id FROM feeds WHERE xml_url = ?", (feed.xml_url,)).fetchone()
            if existing:
                connection.execute(
                    """UPDATE feeds SET group_id = ?, position=?, title=?, title_locked=1,
                       html_url = COALESCE(?, html_url),
                       llm_enabled = CASE WHEN ? IS NULL THEN llm_enabled ELSE ? END,
                       ai_mode = CASE WHEN ? IS NULL THEN ai_mode ELSE ? END WHERE id = ?""",
                    (
                        group_id, feed_position, feed.title, feed.html_url, feed.llm_enabled,
                        int(feed.llm_enabled) if feed.llm_enabled is not None else None,
                        feed.ai_mode, feed.ai_mode, existing["id"],
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO feeds(group_id, title, title_locked, position, xml_url, html_url,
                           llm_enabled,ai_mode,created_at)
                       VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                    (
                        group_id, feed.title, feed_position, feed.xml_url, feed.html_url,
                        int(feed.llm_enabled if feed.llm_enabled is not None else True),
                        feed.ai_mode or ("off" if feed.llm_enabled is False else "inherit"), utcnow(),
                    ),
                )
            feed_count += 1
        for index, child in enumerate(group.groups):
            upsert_group(child, group_id, int(child.position if child.position is not None else index))

    with transaction(connection, immediate=True):
        for index, group in enumerate(groups):
            upsert_group(group, None, int(group.position if group.position is not None else index))
        ensure_ungrouped(connection)
    return group_count, feed_count


def build_tree_from_database(connection) -> list[GroupOutline]:
    # Plugin-owned virtual feeds are configured by their plugin, not by OPML.
    # Keep them out of portable subscription exports and omit a group that is
    # used exclusively as a virtual-source container.
    rows = connection.execute(
        """SELECT g.* FROM groups g
           WHERE NOT (
               EXISTS (
                   SELECT 1 FROM feeds plugin_feed
                   WHERE plugin_feed.group_id=g.id AND plugin_feed.xml_url LIKE 'plugin://%'
               ) AND NOT EXISTS (
                   SELECT 1 FROM feeds regular_feed
                   WHERE regular_feed.group_id=g.id AND regular_feed.xml_url NOT LIKE 'plugin://%'
               )
           )
           ORDER BY g.position, g.title COLLATE NOCASE"""
    ).fetchall()
    groups = {
        int(row["id"]): GroupOutline(
            row["title"],
            llm_enabled=None if row["parent_id"] is None and row["title"] == "Ungrouped"
            else bool(row["llm_enabled"]),
            position=int(row["position"]),
            ai_priority=None if row["parent_id"] is None and row["title"] == "Ungrouped"
            else str(row["ai_priority"]),
            summary_interval_hours=None if row["parent_id"] is None and row["title"] == "Ungrouped"
            else int(row["summary_interval_hours"]),
            summary_item_budget=None if row["parent_id"] is None and row["title"] == "Ungrouped"
            else int(row["summary_item_budget"]),
            ai_mode=None if row["parent_id"] is None and row["title"] == "Ungrouped"
            else str(row["ai_mode"]),
        )
        for row in rows
    }
    roots: list[GroupOutline] = []
    for row in rows:
        group = groups[int(row["id"])]
        if row["parent_id"] is None:
            roots.append(group)
        elif int(row["parent_id"]) in groups:
            groups[int(row["parent_id"])].groups.append(group)
    feeds = connection.execute(
        """SELECT * FROM feeds WHERE xml_url NOT LIKE 'plugin://%'
           ORDER BY position, title COLLATE NOCASE"""
    ).fetchall()
    for feed in feeds:
        group = groups.get(int(feed["group_id"]))
        if group:
            group.feeds.append(
                FeedOutline(
                    feed["title"], feed["xml_url"], feed["html_url"],
                    bool(feed["llm_enabled"]), int(feed["position"]), str(feed["ai_mode"]),
                )
            )
    loose = next((group for group in roots if group.title == "Ungrouped"), None)
    visible_roots = [group for group in roots if group is not loose]
    return ([loose] if loose and (loose.feeds or loose.groups) else []) + visible_roots


def serialize_opml(groups: Iterable[GroupOutline]) -> bytes:
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "DistillFeed subscriptions"
    body = ET.SubElement(root, "body")

    def append_feed(parent: ET.Element, feed: FeedOutline) -> None:
        attrs = {
            "type": "rss", "text": feed.title, "title": feed.title, "xmlUrl": feed.xml_url,
            "distillfeedPosition": str(max(0, int(feed.position))),
        }
        if feed.html_url:
            attrs["htmlUrl"] = feed.html_url
        attrs["llmEnabled"] = str(
            True if feed.llm_enabled is None else bool(feed.llm_enabled)
        ).lower()
        attrs["distillfeedAIMode"] = feed.ai_mode or (
            "off" if feed.llm_enabled is False else "inherit"
        )
        ET.SubElement(parent, "outline", attrs)

    def ordered(group: GroupOutline):
        entries = [(feed.position, 1, feed.title.casefold(), feed) for feed in group.feeds]
        entries.extend((child.position, 0, child.title.casefold(), child) for child in group.groups)
        return [entry[-1] for entry in sorted(entries, key=lambda entry: entry[:-1])]

    def append_group(parent: ET.Element, group: GroupOutline) -> None:
        element = ET.SubElement(
            parent,
            "outline",
            {
                "text": group.title, "title": group.title,
                "llmEnabled": str(True if group.llm_enabled is None else bool(group.llm_enabled)).lower(),
                "distillfeedPosition": str(max(0, int(group.position))),
                "distillfeedAIPriority": group.ai_priority or "normal",
                "distillfeedAIMode": group.ai_mode or (
                    group.ai_priority if group.ai_priority in {"manual", "off"} else "automatic"
                ),
                "distillfeedSummaryIntervalHours": str(max(0, int(group.summary_interval_hours or 0))),
                "distillfeedSummaryItemBudget": str(max(0, int(group.summary_item_budget or 0))),
            },
        )
        for entry in ordered(group):
            append_feed(element, entry) if isinstance(entry, FeedOutline) else append_group(element, entry)

    roots = list(groups)
    loose = next((group for group in roots if group.title == "Ungrouped"), None)
    root_entries = [(group.position, 0, group.title.casefold(), group) for group in roots if group is not loose]
    if loose:
        root_entries.extend((feed.position, 1, feed.title.casefold(), feed) for feed in loose.feeds)
    for *_, entry in sorted(root_entries, key=lambda value: value[:-1]):
        append_feed(body, entry) if isinstance(entry, FeedOutline) else append_group(body, entry)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_database_opml(connection, path: Path) -> None:
    atomic_write(path, serialize_opml(build_tree_from_database(connection)))
    LOGGER.info("Wrote OPML working copy to %s (backup: %s)", path, path.with_suffix(path.suffix + ".bak"))


def is_remote_source(source: str) -> bool:
    return urlparse(source).scheme.casefold() in {"http", "https"}
