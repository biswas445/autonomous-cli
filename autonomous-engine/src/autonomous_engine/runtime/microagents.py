"""Microagents: trigger-matched repository guidance (OpenHands pattern).

OpenHands ships "repo skills" / microagents: small markdown files that live in
the repository next to the code they describe, each declaring *when* it
applies. A file with triggers is injected into the agent's context only when
the task text matches; a file without triggers is always active.

     .agents/microagents/
         database.md        triggers: migration, schema, postgres
         api-conventions.md triggers: endpoint, rest, handler
         legacy-notes.md    (no triggers -> always active)

This is the right place for project knowledge that is too situational to sit
in `instructions.md` (which loads unconditionally) but too important to hope
the agent rediscovers: "our migrations run twice on prod", "the payment
service uses idempotency keys".

Format (frontmatter optional, both styles accepted):

    ---
    triggers: migration, schema
    ---
    # Guidance
    ...markdown...

    triggers: migration, schema
    # Guidance
    ...markdown...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MICROAGENTS_DIRNAME = "microagents"
_README_TEMPLATE = """# Microagents

Microagents are small guidance files injected into agent context only when
they are relevant to the current task. Add one file per topic:

    .agents/microagents/database.md

```markdown
---
triggers: migration, schema, postgres
---
# Database rules

- Migrations must be idempotent; they run twice on production.
```

Rules:
- `triggers:` is a comma-separated list of keywords matched against the task
  title and description (case-insensitive substring match).
- A file **without** triggers is injected into every task — use sparingly.
- `README.md` is never loaded.
- Keep each file short; it competes for the same context budget as everything
  else (`auto microagents` shows what loads and when).
"""


@dataclass
class Microagent:
    name: str
    triggers: list[str] = field(default_factory=list)
    content: str = ""
    path: str = ""

    def matches(self, text: str) -> bool:
        """True when this microagent applies to `text`.

        No triggers means always active (OpenHands: `trigger=None`). Matching
        is a case-insensitive substring test, which is predictable and cheap —
        a regex would invite silent over/under-matching.
        """
        if not self.triggers:
            return True
        lowered = (text or "").lower()
        return any(trigger.lower() in lowered for trigger in self.triggers if trigger.strip())


def _parse_microagent(path: Path) -> Microagent | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    triggers: list[str] = []
    body = raw
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        # frontmatter block
        end = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end = index
                break
        if end is not None:
            header = "\n".join(lines[1:end])
            triggers = _triggers_from_header(header)
            body = "\n".join(lines[end + 1 :])
    else:
        # a bare `triggers:` line in the first few lines
        for index, line in enumerate(lines[:5]):
            if line.lower().startswith("triggers:"):
                triggers = _split_triggers(line.split(":", 1)[1])
                body = "\n".join(lines[:index] + lines[index + 1 :])
                break
    content = body.strip()
    if not content:
        return None
    return Microagent(
        name=path.stem,
        triggers=triggers,
        content=content,
        path=str(path),
    )


def _triggers_from_header(header: str) -> list[str]:
    for line in header.splitlines():
        if line.lower().startswith("triggers:"):
            return _split_triggers(line.split(":", 1)[1])
    return []


def _split_triggers(value: str) -> list[str]:
    return [
        part.strip().strip('"').strip("'") for part in re.split(r"[,\n]", value) if part.strip()
    ]


def load_microagents(workspace) -> list[Microagent]:
    """Load every microagent under .agents/microagents/ (deterministic order)."""
    directory = workspace.paths.state / MICROAGENTS_DIRNAME
    if not directory.is_dir():
        return []
    agents: list[Microagent] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        parsed = _parse_microagent(path)
        if parsed is not None:
            agents.append(parsed)
    return agents


def matched_microagents(workspace, text: str) -> list[Microagent]:
    return [agent for agent in load_microagents(workspace) if agent.matches(text)]


def microagents_context_section(workspace, task) -> str:
    """The context section for this task; empty when nothing matches."""
    text = f"{getattr(task, 'title', '')} {getattr(task, 'description', '')}"
    matched = matched_microagents(workspace, text)
    if not matched:
        return ""
    parts = ["# APPLICABLE MICROAGENTS (repository guidance matching this task)"]
    for agent in matched:
        trigger_note = (
            f"triggers: {', '.join(agent.triggers)}" if agent.triggers else "always active"
        )
        parts.append(f"\n## {agent.name} ({trigger_note})\n{agent.content}")
    return "\n".join(parts)


def ensure_microagents_readme(workspace) -> Path:
    """Create the microagents README template once; never overwrite."""
    directory = workspace.paths.state / MICROAGENTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "README.md"
    if not path.is_file():
        path.write_text(_README_TEMPLATE, encoding="utf-8")
    return path
