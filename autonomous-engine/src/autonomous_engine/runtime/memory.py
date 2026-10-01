"""Project memory: typed, retrievable, consolidated (CLAUDE.md/auto-memory pattern).

Coding CLIs converged on the same realisation: a context window is not a
memory. Claude Code keeps a per-project auto-memory directory (`MEMORY.md`
index plus topic files, first 200 lines / 25 KB loaded per session); Gemini
CLI persists facts via a save-memory tool; Aider keeps a conventions file.
What all of them need is the same three things this module provides:

1. **A typed store.** Memories have a kind — fact, decision, lesson,
   incident, preference — plus a source, a confidence, tags and timestamps.
   "PostgreSQL is the production database" (fact) is different from "library
   X breaks on Node 24" (lesson) and from "always run ruff" (preference).
2. **Recall, not dumping.** Retrieval is relevance-ranked against the current
   task (keyword scoring, deterministic) under a character budget, with
   pinned items always loaded. The whole store never enters a prompt.
3. **Consolidation.** Episodes (what actually happened: task outcomes, run
   digests) are written by the orchestration loop and rolled into a bounded,
   human-readable `MEMORY.md` index. Duplicate memories merge instead of
   accumulating; the store has hard caps.

`facts.json` from earlier versions is migrated on first load.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

MEMORY_FILENAME = "memory.json"
EPISODES_FILENAME = "episodes.jsonl"
INDEX_FILENAME = "MEMORY.md"
LEGACY_FACTS = "facts.json"

MemoryKind = Literal["fact", "decision", "lesson", "incident", "preference"]
KINDS: tuple[str, ...] = ("fact", "decision", "lesson", "incident", "preference")

MAX_ITEMS = 400
MAX_EPISODES_READ = 500
MAX_TEXT_CHARS = 800
DEFAULT_RECALL_BUDGET = 2500

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "should",
    "must",
    "when",
    "then",
    "than",
    "have",
    "has",
    "are",
    "was",
    "were",
    "not",
    "but",
    "its",
    "it's",
    "our",
    "you",
    "your",
}


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^A-Za-z0-9_]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


@dataclass
class MemoryItem:
    id: str
    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    source: str = "system"
    confidence: float = 0.8
    pinned: bool = False
    supersedes: str = ""
    hits: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "tags": self.tags,
            "source": self.source,
            "confidence": self.confidence,
            "pinned": self.pinned,
            "supersedes": self.supersedes,
            "hits": self.hits,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MemoryItem:
        kind = str(payload.get("kind", "fact"))
        if kind not in KINDS:
            kind = "fact"
        return cls(
            id=str(payload.get("id") or ""),
            kind=kind,
            text=str(payload.get("text", ""))[:MAX_TEXT_CHARS],
            tags=[str(t) for t in payload.get("tags", []) or []][:12],
            source=str(payload.get("source", "system")),
            confidence=float(payload.get("confidence", 0.8)),
            pinned=bool(payload.get("pinned", False)),
            supersedes=str(payload.get("supersedes", "")),
            hits=int(payload.get("hits", 0) or 0),
            created_at=str(payload.get("created_at") or _now()),
            updated_at=str(payload.get("updated_at") or _now()),
        )


class MemoryStore:
    """One project's memory: typed items plus episodic records."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.memory_path = self.state_dir / "memory" / MEMORY_FILENAME
        self.episodes_path = self.state_dir / "memory" / EPISODES_FILENAME
        self.index_path = self.state_dir / "memory" / INDEX_FILENAME
        self._items: list[MemoryItem] | None = None

    # ---- load / save ----

    def _load(self) -> list[MemoryItem]:
        if self._items is not None:
            return self._items
        items: list[MemoryItem] = []
        if self.memory_path.is_file():
            try:
                payload = json.loads(self.memory_path.read_text(encoding="utf-8"))
                for raw in payload.get("items", []) if isinstance(payload, dict) else []:
                    if isinstance(raw, dict) and str(raw.get("text", "")).strip():
                        items.append(MemoryItem.from_dict(raw))
            except (json.JSONDecodeError, OSError):
                # Quarantine the unreadable store instead of overwriting it on
                # the next save() — corruption must not cause a silent,
                # total loss of project memory.
                quarantine = self.memory_path.with_suffix(
                    f".corrupt.{int(time.time())}.json"
                )
                with contextlib.suppress(OSError):
                    self.memory_path.replace(quarantine)
                items = []
        elif (self.state_dir / "memory" / LEGACY_FACTS).is_file():
            items = self._migrate_legacy_facts()
        self._items = items
        return items

    def _migrate_legacy_facts(self) -> list[MemoryItem]:
        """Absorb the old facts.json format once, so nothing is lost."""
        legacy = self.state_dir / "memory" / LEGACY_FACTS
        migrated: list[MemoryItem] = []
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return migrated
        for raw in payload if isinstance(payload, list) else []:
            if not isinstance(raw, dict):
                continue
            statement = str(raw.get("statement", "")).strip()
            if statement:
                migrated.append(
                    MemoryItem(
                        id=_new_id(),
                        kind="fact",
                        text=statement,
                        source=str(raw.get("source", "legacy")),
                        created_at=str(raw.get("created_at") or _now()),
                    )
                )
        return migrated

    def save(self) -> None:
        items = self._load()
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": _now(), "items": [i.as_dict() for i in items]}
        # A unique tmp name per writer: two processes sharing one ".tmp" file
        # clobber each other's replace() on Windows (PermissionError) and can
        # interleave content.
        tmp = self.memory_path.with_suffix(f".{os.getpid()}.{uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.memory_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        self._write_index()

    # ---- mutation ----

    def add(
        self,
        kind: str,
        text: str,
        *,
        source: str = "system",
        tags: list[str] | None = None,
        confidence: float = 0.8,
        pinned: bool = False,
        supersedes: str = "",
    ) -> MemoryItem:
        """Add a memory; identical text merges (confidence up, kind preserved)."""
        if kind not in KINDS:
            kind = "fact"
        text = " ".join(text.split())[:MAX_TEXT_CHARS]
        if not text:
            raise ValueError("memory text must not be empty")
        items = self._load()
        fingerprint = text.lower().rstrip(".")
        for item in items:
            if item.text.lower().rstrip(".") == fingerprint:
                item.confidence = min(1.0, max(item.confidence, confidence) + 0.05)
                item.updated_at = _now()
                if tags:
                    item.tags = sorted(set(item.tags) | {str(t) for t in tags})[:12]
                if pinned:
                    item.pinned = True
                self.save()
                return item
        item = MemoryItem(
            id=_new_id(),
            kind=kind,
            text=text,
            tags=[str(t) for t in (tags or [])][:12],
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            pinned=pinned,
            supersedes=supersedes,
        )
        items.append(item)
        self.save()
        return item

    def prune(self, *, max_items: int = MAX_ITEMS) -> int:
        """Drop the weakest memories when the store overflows.

        Priority kept: pinned > kind (preference/decision > fact > lesson >
        incident) > confidence > recency. Removed count is returned.
        """
        items = self._load()
        if len(items) <= max_items:
            return 0
        kind_rank = {"preference": 3, "decision": 2, "fact": 1, "lesson": 0, "incident": 0}
        ranked = sorted(
            items,
            key=lambda i: (
                i.pinned,
                kind_rank.get(i.kind, 0),
                i.confidence,
                i.updated_at,
            ),
            reverse=True,
        )
        keep = {item.id for item in ranked[:max_items]}
        removed = [item for item in items if item.id not in keep]
        self._items = [item for item in items if item.id in keep]
        self.save()
        return len(removed)

    # ---- recall ----

    def all(self) -> list[MemoryItem]:
        return list(self._load())

    def recall(
        self,
        query: str,
        *,
        limit: int = 12,
        char_budget: int = DEFAULT_RECALL_BUDGET,
    ) -> list[MemoryItem]:
        """Relevance-ranked memories for a query, under a character budget.

        Scoring: tag hits weigh most (explicit curation), then text-token
        overlap; pinned items always come first; confidence and retrieval
        history break ties. Deterministic — same store, same task, same
        context (which the tests rely on).
        """
        items = self._load()
        query_tokens = _tokens(query)
        scored: list[tuple[float, MemoryItem]] = []
        for item in items:
            tag_overlap = len(query_tokens & {t.lower() for t in item.tags})
            text_overlap = len(query_tokens & _tokens(item.text))
            score = tag_overlap * 6 + text_overlap * 3 + item.confidence
            if item.pinned:
                score += 100
            if score > item.confidence:  # at least one signal matched
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].confidence, pair[1].updated_at))
        selected: list[MemoryItem] = []
        used = 0
        for _score, item in scored[: limit * 2]:
            cost = len(item.text) + 24
            if used + cost > char_budget:
                continue
            selected.append(item)
            used += cost
            if len(selected) >= limit:
                break
        for item in selected:
            item.hits += 1
        if selected:
            self.save()
        return selected

    # ---- episodes ----

    def record_episode(self, episode: dict[str, Any]) -> None:
        """Append one episode (a task outcome or a run digest) and refresh the index."""
        self.episodes_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"at": _now(), **episode}
        with self.episodes_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self._write_index()

    def recent_episodes(self, limit: int = 5, *, kind: str = "") -> list[dict[str, Any]]:
        if not self.episodes_path.is_file():
            return []
        episodes: list[dict[str, Any]] = []
        try:
            with self.episodes_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if kind and record.get("kind") != kind:
                        continue
                    episodes.append(record)
        except OSError:
            return []
        return episodes[-limit:]

    def consolidate(self) -> dict[str, int]:
        """Roll recorded failures/decisions into memory, then rebuild the index.

        Called at run boundaries: turns evidence the runtime already holds
        (failure lessons with a root cause, accepted decisions) into typed
        memories so the next session starts informed.
        """
        added = 0
        failures_path = self.state_dir / "memory" / "failures.json"
        if failures_path.is_file():
            try:
                failures = json.loads(failures_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                failures = []
            for failure in failures[-20:] if isinstance(failures, list) else []:
                lesson = str(failure.get("lesson", "")).strip()
                root_cause = str(failure.get("root_cause", "")).strip()
                if lesson and root_cause:
                    before = len(self._load())
                    self.add(
                        "lesson",
                        lesson,
                        source="failure-memory",
                        tags=sorted(_tokens(str(failure.get("summary", ""))))[:6],
                        confidence=0.7,
                    )
                    if len(self._load()) > before:
                        added += 1
        self.save()
        return {"lessons_added": added, "items": len(self._load())}

    # ---- rendering ----

    def _write_index(self) -> None:
        """A bounded, human-readable MEMORY.md (the Claude Code index idea)."""
        items = self._load()
        lines = [
            "# Project Memory",
            "",
            f"Updated: {_now()} | items: {len(items)}",
            "",
            "> Generated from `.agents/memory/memory.json`. Edit memories with",
            "> `auto memory --add \"...\" --kind fact`; do not hand-edit this file.",
            "",
        ]
        grouped: dict[str, list[MemoryItem]] = {}
        for item in items:
            grouped.setdefault(item.kind, []).append(item)
        for kind in ("preference", "decision", "fact", "lesson", "incident"):
            kind_items = grouped.get(kind, [])
            if not kind_items:
                continue
            lines.append(f"## {kind}s ({len(kind_items)})")
            lines.append("")
            kind_items.sort(key=lambda i: (-int(i.pinned), -i.confidence, i.updated_at))
            for item in kind_items[:20]:
                pin = " (pinned)" if item.pinned else ""
                lines.append(f"- {item.text}{pin}  \n  `{item.source}` · conf {item.confidence:.2f} · {item.updated_at}")
            if len(kind_items) > 20:
                lines.append(f"- … {len(kind_items) - 20} more")
            lines.append("")
        episodes = self.recent_episodes(8)
        if episodes:
            lines.append("## Recent episodes")
            lines.append("")
            for episode in episodes:
                if episode.get("kind") == "run":
                    lines.append(
                        f"- run: {episode.get('stop_reason', '')} "
                        f"({episode.get('completed', 0)} done, {episode.get('failed', 0)} failed) "
                        f"{episode.get('at', '')}"
                    )
                else:
                    lines.append(
                        f"- {episode.get('outcome', '')}: {episode.get('task_id', '')} "
                        f"{str(episode.get('title', ''))[:60]} {episode.get('at', '')}"
                    )
            lines.append("")
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass


def _new_id() -> str:
    import uuid

    return f"MEM-{uuid.uuid4().hex[:10]}"


# ---- workspace integration ----


def memory_store(workspace) -> MemoryStore:
    return MemoryStore(workspace.paths.state)


def memory_context_section(workspace, task=None, *, budget: int = DEFAULT_RECALL_BUDGET) -> str:
    """The recall section for one task: pinned + relevant + recent episodes.

    With ``task=None`` (bootstrap/director sessions) it returns pinned
    memories and recent episodes — there is no task text to rank against.
    """
    store = memory_store(workspace)
    query = " ".join(
        [
            getattr(task, "title", "") or "",
            getattr(task, "description", "") or "",
            getattr(task, "epic", "") or "",
        ]
    )
    items = store.recall(query, char_budget=budget)
    episodes = store.recent_episodes(3, kind="task")
    if not items and not episodes:
        return ""
    header = (
        "# PROJECT MEMORY (recalled for this task)"
        if task is not None
        else "# PROJECT MEMORY (pinned and recent)"
    )
    lines = [header]
    for item in items:
        pin = " [pinned]" if item.pinned else ""
        lines.append(f"- ({item.kind}{pin}, {item.confidence:.2f}) {item.text}")
    if episodes:
        lines.append("\nRecent task episodes:")
        for episode in episodes:
            lines.append(
                f"- {episode.get('task_id', '')} {episode.get('outcome', '')}: "
                f"{str(episode.get('summary', ''))[:120]}"
            )
    return "\n".join(lines)


def remember(workspace, text: str, *, kind: str = "fact", source: str = "operator",
             tags: list[str] | None = None, pinned: bool = False) -> MemoryItem:
    store = memory_store(workspace)
    return store.add(kind, text, source=source, tags=tags, pinned=pinned)
