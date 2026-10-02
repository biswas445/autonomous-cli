"""Context engineering: per-model budgets, provenance/trust, compaction,
and session snapshots (context spec §11, §14, §16–§22, §41, §76).

Extends the deterministic `ContextBuilder` without changing its contract:
- per-model char budgets derived from the capability registry's context limit
- provenance + trust classification for every accepted section
- loss-aware compaction of over-budget history (durable lines survive,
  temporary lines are summarized — never silently dropped)
- persisted session snapshots for post-hoc inspection and reconstruction
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.task import now_iso
from .context import AgentContext

# Rough chars-per-token estimate (conservative: 4 is standard English; code
# is denser). The capability registry holds the authoritative token limits.
_CHARS_PER_TOKEN = 4
_MIN_CHAR_BUDGET = 24_000
_MAX_CHAR_BUDGET = 400_000
_MAX_SNAPSHOTS_PER_ROLE = 20

# Durable content survives compaction; temporary content is summarized.
_DURABLE_PATTERN = re.compile(
    r"decision|discovery|failure|milestone|escalation|qa\.gate|task\.(created|completed|failed)|budget",
    re.IGNORECASE,
)
_TEMPORARY_PATTERN = re.compile(r"heartbeat|cycle\.completed|daemon\.|supervisor\.", re.IGNORECASE)

TRUST_PROJECT_POLICY = "project_policy"
TRUST_RUNTIME_POLICY = "runtime_policy"
TRUST_UNTRUSTED_REPOSITORY = "untrusted_repository"
TRUST_OBSERVED_RESULT = "observed_result"

_SECTION_TRUST: dict[str, str] = {
    "instructions": TRUST_PROJECT_POLICY,
    "project_rules": TRUST_PROJECT_POLICY,
    "constitution": TRUST_RUNTIME_POLICY,
    "memory": TRUST_OBSERVED_RESULT,
    "microagents": TRUST_UNTRUSTED_REPOSITORY,
    "dependencies": TRUST_OBSERVED_RESULT,
    "relevant_architecture": TRUST_PROJECT_POLICY,
    "repo_map": TRUST_UNTRUSTED_REPOSITORY,
    "relevant_files": TRUST_UNTRUSTED_REPOSITORY,
    "recent_events": TRUST_OBSERVED_RESULT,
    "current_failures": TRUST_OBSERVED_RESULT,
    "project_state": TRUST_OBSERVED_RESULT,
}


def budget_for_model(context_limit_tokens: int | None) -> int:
    """Input char budget for a model: tokens×4 minus output/prompt headroom."""
    if not context_limit_tokens:
        return _MIN_CHAR_BUDGET
    usable = max(1_000, int(context_limit_tokens * 0.6))  # headroom for output + rules
    return max(_MIN_CHAR_BUDGET, min(_MAX_CHAR_BUDGET, usable * _CHARS_PER_TOKEN))


@dataclass
class CompactionResult:
    """Loss-aware outcome of compacting an over-budget section (spec §20/§21)."""

    text: str = ""
    kept_lines: int = 0
    summarized_lines: int = 0
    dropped_lines: int = 0
    durable_kept: int = 0

    def note(self) -> str:
        return (
            f"[compacted: {self.kept_lines} lines kept "
            f"({self.durable_kept} durable), {self.summarized_lines} summarized, "
            f"{self.dropped_lines} dropped]"
        )


def compact_history(text: str, char_budget: int) -> CompactionResult:
    """Compress event/history text to a char budget, keeping durable lines.

    Classification (spec §21): lines matching durable signals (decisions,
    discoveries, failures, milestones, budget, task lifecycle) are kept in
    full; temporary lines (heartbeats, per-cycle noise) are replaced by a
    single summary line; anything else is dropped first. Never reorders the
    durable lines — chronology is evidence.
    """
    lines = text.splitlines()
    durable = [ln for ln in lines if _DURABLE_PATTERN.search(ln)]
    temporary = [ln for ln in lines if _TEMPORARY_PATTERN.search(ln) and not _DURABLE_PATTERN.search(ln)]
    other = [
        ln
        for ln in lines
        if ln not in durable and ln not in temporary
    ]
    result = CompactionResult()
    out: list[str] = []
    used = 0
    for ln in durable:
        cost = len(ln) + 1
        if used + cost > char_budget:
            result.dropped_lines += 1
            continue
        out.append(ln)
        used += cost
        result.kept_lines += 1
        result.durable_kept += 1
    for ln in other:
        if used + len(ln) + 1 > char_budget:
            result.dropped_lines += 1
            continue
        out.append(ln)
        used += len(ln) + 1
        result.kept_lines += 1
    if temporary:
        result.summarized_lines = len(temporary)
        note = f"[{len(temporary)} routine/temporary lines omitted (heartbeats, cycle noise)]"
        if used + len(note) + 1 <= char_budget:
            out.append(note)
    result.text = "\n".join(out)
    return result


def provenance_for(section: str) -> str:
    """Trust level of a context section (spec §14): untrusted repository
    content can never masquerade as policy."""
    return _SECTION_TRUST.get(section, TRUST_OBSERVED_RESULT)


def attach_provenance(ctx: AgentContext) -> dict[str, Any]:
    """Record where every accepted section came from and how much it may be
    trusted. Returned as displayable metadata, also embedded in snapshots."""
    provenance: dict[str, Any] = {}
    for name, body in ctx.sections.items():
        provenance[name] = {
            "trust": provenance_for(name),
            "chars": len(body),
        }
    for name in ctx.dropped_sections:
        provenance[name] = {"trust": provenance_for(name), "chars": 0, "omitted": True}
    return provenance


class SessionSnapshotStore:
    """Persisted context snapshots (spec §22, §41): what a model actually
    received, kept for debugging, audits, and reconstruction after resets."""

    def __init__(self, state_dir: Path):
        self.root = state_dir / "context" / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, ctx: AgentContext, *, role: str, model: str = "") -> Path:
        task_id = ctx.task.id if ctx.task is not None else "bootstrap"
        role_dir = self.root / role
        role_dir.mkdir(parents=True, exist_ok=True)
        sequence = len(list(role_dir.glob(f"{task_id}-*.json"))) + 1
        path = role_dir / f"{task_id}-{sequence:04d}.json"
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "role": role,
            "model": model,
            "char_budget": ctx.char_budget,
            "sections": {k: len(v) for k, v in ctx.sections.items()},
            "dropped_sections": list(ctx.dropped_sections),
            "provenance": attach_provenance(ctx),
            "created_at": now_iso(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._prune(role_dir, task_id)
        return path

    def _prune(self, role_dir: Path, task_id: str) -> None:
        """Bound snapshot history: keep the newest N per (role, task)."""
        snapshots = sorted(role_dir.glob(f"{task_id}-*.json"))
        for old in snapshots[: max(0, len(snapshots) - _MAX_SNAPSHOTS_PER_ROLE)]:
            old.unlink(missing_ok=True)

    def latest(self, role: str, task_id: str) -> dict[str, Any] | None:
        role_dir = self.root / role
        candidates = sorted(role_dir.glob(f"{task_id}-*.json"))
        if not candidates:
            return None
        try:
            return json.loads(candidates[-1].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
