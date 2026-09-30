"""Agent instructions memory, modelled on the Claude Code / Gemini CLI pattern.

Coding CLIs converged on the same memory design: a small set of markdown
files, concatenated broadest-to-most-specific, injected into every session:

    Claude Code:  ~/.claude/CLAUDE.md  ->  ./CLAUDE.md  ->  ./CLAUDE.local.md
    Gemini CLI:   ~/.gemini/GEMINI.md  ->  ./GEMINI.md
    this runtime: ~/.auto_engine/instructions.md -> .agents/instructions.md

The instructions are *human-authored* guidance — how the user wants work done
in this project. They are deliberately separate from the generated project
constitution (the system's law) and from the learned lessons store (evidence
from other projects). Rendering order mirrors the CLIs: global first, project
second, so the most specific guidance appears last, nearest the task.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.workspace import atomic_write

GLOBAL_INSTRUCTIONS_DIRNAME = ".auto_engine"
INSTRUCTIONS_FILENAME = "instructions.md"

_TEMPLATE = """# Project Instructions

> Human-authored guidance for every agent in this project.
> Edit this file freely; it is read verbatim into each agent session.
> Keep it short and durable — under ~200 lines, like a good CLAUDE.md.

## How we work here

- (example) Prefer the standard library over new dependencies.
- (example) Every endpoint returns JSON and logs to stderr.

## Conventions

- (example) Python code is formatted with ruff and typed with mypy.
"""


def global_instructions_path() -> Path:
    override = os.environ.get("AUTO_INSTRUCTIONS_FILE")
    if override:
        return Path(override)
    home = Path.home() / GLOBAL_INSTRUCTIONS_DIRNAME
    return home / INSTRUCTIONS_FILENAME


def project_instructions_path(workspace) -> Path:
    return workspace.paths.state / INSTRUCTIONS_FILENAME


def ensure_project_instructions(workspace) -> Path:
    """Create the project instructions template once at init; never overwrite."""
    path = project_instructions_path(workspace)
    if not path.is_file():
        atomic_write(path, _TEMPLATE)
    return path


def _read_or_empty(path: Path, *, limit_chars: int = 8000) -> str:
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    if (
        not text
        or text.startswith("# Project Instructions")
        and "(example)" in text
        and len(text) < 600
    ):
        # An untouched template carries no information; skip it.
        return ""
    return text[:limit_chars]


def load_instructions(workspace) -> dict[str, str]:
    """Load both instruction scopes: global first, project second."""
    return {
        "global": _read_or_empty(global_instructions_path()),
        "project": _read_or_empty(project_instructions_path(workspace)),
    }


def instructions_context_section(workspace) -> str:
    """Render the concatenated instructions section for agent contexts.

    Broadest scope first (global), most specific last (project) — the Claude
    Code convention, so nearest-scope guidance sits closest to the task.
    """
    scopes = load_instructions(workspace)
    parts: list[str] = []
    if scopes["global"]:
        parts.append("## User-global instructions\n" + scopes["global"])
    if scopes["project"]:
        parts.append("## Project instructions (from .agents/instructions.md)\n" + scopes["project"])
    if not parts:
        return ""
    return "# AGENT INSTRUCTIONS (authored by the operator; these bind you)\n" + "\n\n".join(parts)
