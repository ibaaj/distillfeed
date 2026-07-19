from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .db import utcnow


SCOPE_MODE_KEY = "ntfy_scope_mode"
VALID_SCOPE_MODES = {"all", "selected"}
VALID_SCOPE_KINDS = {"group", "feed"}


@dataclass(frozen=True)
class NtfyMatch:
    minimum_relevance: int
    scope_kind: str
    scope_id: int | None
    label: str


@dataclass(frozen=True)
class NtfyScopePolicy:
    mode: str
    global_threshold: int
    group_parents: dict[int, int | None]
    group_titles: dict[int, str]
    feed_groups: dict[int, int]
    feed_titles: dict[int, str]
    group_thresholds: dict[int, int]
    feed_thresholds: dict[int, int]

    @property
    def rule_count(self) -> int:
        return len(self.group_thresholds) + len(self.feed_thresholds)

    def match(self, feed_id: int) -> NtfyMatch | None:
        """Return the effective rule for a feed, or None when it is excluded.

        A feed rule is most specific. Otherwise, the first configured group
        encountered while walking towards the root wins. Selected-sources mode
        deliberately fails closed when no rule matches.
        """
        identifier = int(feed_id)
        # The ordinary device-alert policy deliberately has no implicit rule
        # for plugin-owned sources. Plugins such as the bundled arXiv digest
        # own their delivery policy and history independently.
        if identifier not in self.feed_groups:
            return None
        if identifier in self.feed_thresholds:
            return NtfyMatch(
                self.feed_thresholds[identifier], "feed", identifier,
                self.feed_titles.get(identifier, f"Feed {identifier}"),
            )
        group_id = self.feed_groups.get(identifier)
        visited: set[int] = set()
        while group_id is not None and group_id not in visited:
            visited.add(group_id)
            if group_id in self.group_thresholds:
                return NtfyMatch(
                    self.group_thresholds[group_id], "group", group_id,
                    self.group_titles.get(group_id, f"Group {group_id}"),
                )
            group_id = self.group_parents.get(group_id)
        if self.mode == "all":
            return NtfyMatch(self.global_threshold, "global", None, "All feeds")
        return None


def _scope_mode(connection: Any) -> str:
    row = connection.execute(
        "SELECT value FROM settings WHERE key=?", (SCOPE_MODE_KEY,)
    ).fetchone()
    mode = str(row[0]) if row else "all"
    return mode if mode in VALID_SCOPE_MODES else "all"


def _group_path(
    group_id: int, parents: dict[int, int | None], titles: dict[int, str],
) -> str:
    parts: list[str] = []
    current: int | None = group_id
    visited: set[int] = set()
    while current is not None and current not in visited:
        visited.add(current)
        parts.append(titles.get(current, f"Group {current}"))
        current = parents.get(current)
    return " › ".join(reversed(parts))


def load_ntfy_scope_policy(
    connection: Any, global_threshold: int,
) -> NtfyScopePolicy:
    groups = connection.execute("SELECT id,parent_id,title FROM groups").fetchall()
    parents = {
        int(row["id"]): int(row["parent_id"]) if row["parent_id"] is not None else None
        for row in groups
    }
    titles = {int(row["id"]): str(row["title"]) for row in groups}
    feeds = connection.execute(
        "SELECT id,group_id,title FROM feeds WHERE xml_url NOT LIKE 'plugin://%'"
    ).fetchall()
    feed_groups = {int(row["id"]): int(row["group_id"]) for row in feeds}
    feed_titles = {int(row["id"]): str(row["title"]) for row in feeds}
    group_thresholds: dict[int, int] = {}
    feed_thresholds: dict[int, int] = {}
    for row in connection.execute(
        "SELECT group_id,feed_id,minimum_relevance FROM ntfy_scope_rules"
    ).fetchall():
        threshold = int(row["minimum_relevance"])
        if row["feed_id"] is not None:
            feed_thresholds[int(row["feed_id"])] = threshold
        elif row["group_id"] is not None:
            group_thresholds[int(row["group_id"])] = threshold
    return NtfyScopePolicy(
        mode=_scope_mode(connection),
        global_threshold=max(0, min(100, int(global_threshold))),
        group_parents=parents,
        group_titles={
            identifier: _group_path(identifier, parents, titles) for identifier in titles
        },
        feed_groups=feed_groups,
        feed_titles=feed_titles,
        group_thresholds=group_thresholds,
        feed_thresholds=feed_thresholds,
    )


def filter_ntfy_candidates(
    policy: NtfyScopePolicy, candidates: Iterable[Any], limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        values = dict(candidate)
        match = policy.match(int(values["feed_id"]))
        try:
            relevance = int(values["relevance"])
        except (TypeError, ValueError):
            continue
        if match is None or relevance < match.minimum_relevance:
            continue
        values["minimum_relevance"] = match.minimum_relevance
        values["policy_scope_kind"] = match.scope_kind
        values["policy_scope_id"] = match.scope_id
        values["policy_label"] = match.label
        selected.append(values)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def ntfy_scope_settings(connection: Any, global_threshold: int) -> dict[str, Any]:
    policy = load_ntfy_scope_policy(connection, global_threshold)
    hidden_root_ids = {
        int(row["id"])
        for row in connection.execute(
            """SELECT id FROM groups
               WHERE parent_id IS NULL AND title='Ungrouped'"""
        ).fetchall()
    }
    ordinary_group_ids: set[int] = set()
    for group_id in policy.feed_groups.values():
        current: int | None = group_id
        visited: set[int] = set()
        while current is not None and current not in visited:
            visited.add(current)
            ordinary_group_ids.add(current)
            current = policy.group_parents.get(current)
    groups = [
        {
            "id": identifier,
            "title": policy.group_titles[identifier],
            "selected": identifier in policy.group_thresholds,
            "minimum_relevance": policy.group_thresholds.get(
                identifier, policy.global_threshold,
            ),
        }
        for identifier in ordinary_group_ids
        if identifier not in hidden_root_ids
    ]
    groups.sort(key=lambda row: (str(row["title"]).casefold(), int(row["id"])))
    feeds = [
        {
            "id": identifier,
            "title": title,
            "group_title": (
                "Top level" if policy.feed_groups[identifier] in hidden_root_ids
                else policy.group_titles.get(policy.feed_groups[identifier], "Top level")
            ),
            "selected": identifier in policy.feed_thresholds,
            "minimum_relevance": policy.feed_thresholds.get(
                identifier, policy.global_threshold,
            ),
        }
        for identifier, title in policy.feed_titles.items()
    ]
    feeds.sort(key=lambda row: (
        str(row["group_title"]).casefold(), str(row["title"]).casefold(), int(row["id"]),
    ))
    return {
        "mode": policy.mode,
        "global_threshold": policy.global_threshold,
        "rule_count": policy.rule_count,
        "groups": groups,
        "feeds": feeds,
    }


def replace_ntfy_scope_policy(
    connection: Any, mode: str, rules: list[dict[str, Any]],
) -> None:
    """Replace a validated policy inside the caller's database transaction."""
    normalized_mode = str(mode).strip().casefold()
    if normalized_mode not in VALID_SCOPE_MODES:
        raise ValueError("ntfy source mode must be all or selected")
    if not isinstance(rules, list) or len(rules) > 5000:
        raise ValueError("ntfy source rules must be a list of at most 5000 entries")

    ordinary_feeds = {
        int(row["id"]): int(row["group_id"])
        for row in connection.execute(
            "SELECT id,group_id FROM feeds WHERE xml_url NOT LIKE 'plugin://%'"
        ).fetchall()
    }
    group_rows = connection.execute("SELECT id,parent_id,title FROM groups").fetchall()
    parents = {
        int(row["id"]): int(row["parent_id"]) if row["parent_id"] is not None else None
        for row in group_rows
    }
    hidden_root_ids = {
        int(row["id"])
        for row in group_rows
        if row["parent_id"] is None and str(row["title"]) == "Ungrouped"
    }
    ordinary_groups: set[int] = set()
    for group_id in ordinary_feeds.values():
        current: int | None = group_id
        visited: set[int] = set()
        while current is not None and current not in visited:
            visited.add(current)
            ordinary_groups.add(current)
            current = parents.get(current)
    ordinary_groups.difference_update(hidden_root_ids)

    normalized: list[tuple[str, int, int]] = []
    seen: set[tuple[str, int]] = set()
    for raw in rules:
        if not isinstance(raw, dict):
            raise ValueError("Each ntfy source rule must be an object")
        kind = str(raw.get("scope_kind", "")).strip().casefold()
        if kind not in VALID_SCOPE_KINDS:
            raise ValueError("ntfy source rule kind must be group or feed")
        if isinstance(raw.get("scope_id"), bool):
            raise ValueError("ntfy source rule identifiers must be integers")
        try:
            scope_id = int(raw.get("scope_id"))
            threshold = int(raw.get("minimum_relevance"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ntfy source rule identifiers and thresholds must be integers") from exc
        if isinstance(raw.get("minimum_relevance"), bool) or not 0 <= threshold <= 100:
            raise ValueError("ntfy source rule threshold must be between 0 and 100")
        key = (kind, scope_id)
        if key in seen:
            raise ValueError("Duplicate ntfy source rule")
        seen.add(key)
        if kind == "feed" and scope_id not in ordinary_feeds:
            raise ValueError(f"Feed {scope_id} is not an ordinary RSS or Atom source")
        if kind == "group" and scope_id not in ordinary_groups:
            raise ValueError(f"Group {scope_id} does not contain an ordinary RSS or Atom source")
        normalized.append((kind, scope_id, threshold))

    connection.execute("DELETE FROM ntfy_scope_rules")
    now = utcnow()
    for kind, scope_id, threshold in normalized:
        connection.execute(
            """INSERT INTO ntfy_scope_rules(
                   group_id,feed_id,minimum_relevance,created_at,updated_at
               ) VALUES(?,?,?,?,?)""",
            (
                scope_id if kind == "group" else None,
                scope_id if kind == "feed" else None,
                threshold, now, now,
            ),
        )
    connection.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (SCOPE_MODE_KEY, normalized_mode),
    )
