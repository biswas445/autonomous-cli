"""Repository map, inspired by Aider's repo-map (aider.chat/docs/repomap.html).

Aider showed that a compact map of "where things are defined" beats feeding
whole files into the context: file names plus their key symbols (classes,
functions, imports-as-references), ranked by relevance to the task and by
git recency. That lets a coding agent see the shape of the repository in a
few hundred tokens instead of thousands.

This implementation is dependency-free where Aider uses tree-sitter:

* Python files are parsed with the stdlib ``ast`` module (defs + imports);
* JavaScript/TypeScript/Go use conservative regex declarations;
* ranking combines task-keyword overlap on symbols (Aider's graph ranking,
  simplified) with git recency (files changed recently rank higher);
* output is a budgeted markdown tree suitable for one context section.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".agents",
    ".worktrees",
}
SCAN_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java")
MAX_FILE_BYTES = 200_000
MAX_FILES = 200

# (regex, group name) per language: conservative declaration extractors.
_DECL_RES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    ".js": [
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M), "def"),
        (re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", re.M), "class"),
    ],
    ".ts": None,  # shared with .js below
    ".tsx": None,
    ".jsx": None,
    ".go": [
        (re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M), "def"),
        (re.compile(r"^type\s+([A-Za-z_]\w*)\s+struct", re.M), "class"),
    ],
    ".rb": [(re.compile(r"^\s*def\s+([\w?!.]+)", re.M), "def")],
    ".java": [
        (re.compile(r"^\s*(?:public|private|protected)?\s*class\s+([A-Za-z_]\w*)", re.M), "class"),
        (
            re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\]]+\s+([a-z]\w*)\s*\(", re.M),
            "def",
        ),
    ],
}
_DECL_RES[".ts"] = _DECL_RES[".js"]
_DECL_RES[".tsx"] = _DECL_RES[".js"]
_DECL_RES[".jsx"] = _DECL_RES[".js"]


@dataclass
class FileTags:
    path: str
    defs: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return self.defs + self.refs


def extract_tags_python(path: Path) -> tuple[list[str], list[str]]:
    """Top-level defs/classes and imported names via the stdlib AST."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return [], []
    defs: list[str] = []
    refs: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    defs.append(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name and name not in ("*",):
                    refs.append(name)
    return defs, refs


def extract_tags_regex(path: Path, suffix: str) -> tuple[list[str], list[str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
    except OSError:
        return [], []
    defs: list[str] = []
    for pattern, _kind in _DECL_RES.get(suffix, []):
        for match in pattern.finditer(text):
            defs.append(match.group(1))
    return defs, []


class RepoMap:
    """Builds a budgeted, task-ranked map of the repository's symbols."""

    def __init__(self, root: Path, *, max_chars: int = 6000, max_files: int = 40):
        self.root = Path(root)
        self.max_chars = max_chars
        self.max_files = max_files

    # ---- file discovery ----

    def _source_files(self) -> list[Path]:
        files: list[Path] = []
        if not self.root.is_dir():
            return files
        for path in self.root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in SCAN_SUFFIXES:
                files.append(path)
                if len(files) >= MAX_FILES:
                    break
        return sorted(files)

    def _git_recency(self, rel_paths: list[str]) -> dict[str, int]:
        """Rank from git: most recently committed files get the highest score.

        Falls back to an empty map when git is unavailable (no repo, no git
        binary) — recency then contributes nothing to the ranking.
        """
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv
                ["git", "log", f"-{max(1, len(rel_paths) * 2)}", "--name-only", "--pretty=format:"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if proc.returncode != 0:
            return {}
        recency: dict[str, int] = {}
        seen_order: list[str] = []
        for line in proc.stdout.splitlines():
            name = line.strip().replace("\\", "/")
            if name and name not in seen_order:
                seen_order.append(name)
        for rank, name in enumerate(seen_order):
            recency[name] = len(seen_order) - rank
        return recency

    # ---- ranking ----

    def ranked_files(
        self, keywords: set[str] | None = None, *, include_recency: bool = True
    ) -> list[FileTags]:
        keywords = {k.lower() for k in (keywords or set()) if len(k) > 2}
        tags_by_file: list[tuple[FileTags, int]] = []
        recency = (
            self._git_recency([p.name for p in self._source_files()]) if include_recency else {}
        )
        for path in self._source_files():
            rel = path.relative_to(self.root).as_posix()
            suffix = path.suffix.lower()
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if suffix == ".py":
                defs, refs = extract_tags_python(path)
            else:
                defs, refs = extract_tags_regex(path, suffix)
            if not defs and not refs:
                continue
            tags = FileTags(path=rel, defs=defs, refs=refs)
            score = 0
            lowered_symbols = [s.lower() for s in tags.symbols]
            for keyword in keywords:
                if any(keyword in symbol for symbol in lowered_symbols):
                    score += 10
                if keyword in rel.lower():
                    score += 5
            score += recency.get(rel, 0) + recency.get(path.name, 0)
            tags_by_file.append((tags, score))
        tags_by_file.sort(key=lambda pair: (-pair[1], pair[0].path))
        return [tags for tags, _ in tags_by_file[: self.max_files]]

    # ---- rendering ----

    def render(self, keywords: set[str] | None = None, *, include_recency: bool = True) -> str:
        """The map: one line per file with its key symbols, budgeted."""
        ranked = self.ranked_files(keywords, include_recency=include_recency)
        if not ranked:
            return ""
        lines: list[str] = []
        used = 0
        for tags in ranked:
            shown = tags.defs[:8]
            refs_note = ""
            if not shown and tags.refs:
                shown = tags.refs[:5]
            elif tags.refs:
                external = [r for r in tags.refs if r not in tags.defs][:5]
                refs_note = f" (imports: {', '.join(external)})" if external else ""
            line = f"- {tags.path}: {', '.join(shown) or '(no symbols)'}{refs_note}"
            if used + len(line) > self.max_chars:
                lines.append("... (repo map truncated at budget)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


def keywords_for_task(
    title: str, description: str = "", artifacts: list[str] | None = None
) -> set[str]:
    """Task keywords for repo-map ranking: title + description + named files."""
    text = " ".join([title or "", description or "", " ".join(artifacts or [])])
    words = re.split(r"[^A-Za-z0-9_]+", text)
    return {w for w in words if len(w) > 2 and not w.isdigit()}


def repo_map_section(root: Path, task: Any | None = None, *, max_chars: int = 6000) -> str:
    """Ready-to-inject context section; empty string when nothing to show."""
    keywords = (
        keywords_for_task(task.title, task.description, getattr(task, "artifacts", []))
        if task is not None
        else None
    )
    body = RepoMap(root, max_chars=max_chars).render(keywords)
    if not body:
        return ""
    return "# REPOSITORY MAP (definitions per file; ask to read a file for its body)\n" + body
