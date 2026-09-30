"""Architecture memory (plan.md §60): lessons that survive across projects.

Lessons are evidence-based by construction: they are only recorded from
verified outcomes — release-time failure lessons that carry a recorded root
cause, and accepted architecture decisions. Nothing is learned from bare
model opinions. The store is global (one per machine) so a future project's
architect sees what past projects learned the hard way.

Override the location with ``AUTO_LESSONS_FILE`` (used by tests).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MAX_LESSONS = 200


def lessons_file(project_root: Path | None = None) -> Path:
    override = os.environ.get("AUTO_LESSONS_FILE")
    if override:
        return Path(override)
    home = Path.home() / ".auto_engine"
    home.mkdir(parents=True, exist_ok=True)
    return home / "lessons.json"


def load_lessons(*, limit: int = 20) -> list[dict[str, Any]]:
    path = lessons_file()
    if not path.is_file():
        return []
    try:
        lessons = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(lessons, list):
        return []
    return lessons[-limit:]


def record_lesson(
    statement: str,
    *,
    source_project: str,
    evidence: str = "",
    kind: str = "lesson",
) -> bool:
    """Append one lesson; returns False when it was a duplicate.

    ``kind`` is one of: ``lesson`` (a failure lesson with evidence),
    ``decision`` (an accepted architecture decision).
    """
    statement = statement.strip()
    if not statement:
        return False
    path = lessons_file()
    lessons: list[dict[str, Any]] = []
    if path.is_file():
        try:
            lessons = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            lessons = []
    fingerprint = statement.lower()
    if any(str(item.get("statement", "")).lower() == fingerprint for item in lessons):
        return False
    lessons.append(
        {
            "statement": statement[:500],
            "kind": kind,
            "source_project": source_project,
            "evidence": evidence[:500],
            "recorded_at": _now(),
        }
    )
    lessons = lessons[-MAX_LESSONS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(lessons, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True


def lessons_context_section(*, limit: int = 12) -> str:
    """Render the lessons as a context section for architect/planner prompts."""
    lessons = load_lessons(limit=limit)
    if not lessons:
        return ""
    lines = ["# LESSONS FROM OTHER PROJECTS (evidence-based; do not repeat known failures)"]
    for lesson in lessons:
        lines.append(
            f"- [{lesson.get('kind', 'lesson')}] {lesson.get('statement', '')}"
            + (f" (evidence: {lesson['evidence']})" if lesson.get("evidence") else "")
        )
    return "\n".join(lines)


def record_project_lessons(workspace: Any, project_name: str) -> int:
    """Harvest a finished project's verified failure lessons into the global store.

    Only lessons with both a recorded root cause and a stated lesson are
    learned — raw failures without an understood cause teach nothing reliable.
    """
    recorded = 0
    for failure in workspace.load_failures():
        lesson = str(failure.get("lesson", "")).strip()
        root_cause = str(failure.get("root_cause", "")).strip()
        if not lesson or not root_cause:
            continue
        if record_lesson(
            lesson,
            source_project=project_name,
            evidence=root_cause,
            kind="lesson",
        ):
            recorded += 1
    return recorded


def record_decision_lessons(decisions: list[Any], project_name: str) -> int:
    """Record accepted architecture decisions as cross-project knowledge."""
    recorded = 0
    for decision in decisions:
        if getattr(decision, "status", "") != "accepted":
            continue
        if record_lesson(
            f"{getattr(decision, 'title', '')}: {getattr(decision, 'body', '')}",
            source_project=project_name,
            evidence="; ".join(getattr(decision, "evidence", []) or [])[:500],
            kind="decision",
        ):
            recorded += 1
    return recorded


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
