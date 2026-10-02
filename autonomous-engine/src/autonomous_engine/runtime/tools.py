"""Agent tools: the callable surface, declared once with JSON schemas.

Until now agents acted only through their final JSON payload (the coder's
EditPlan). Every serious coding CLI instead gives the model *tools* it can
call mid-reasoning — read a file, search the tree, run a command — and feeds
observations back. This module defines that surface, which is the same for
every provider:

    read_file(path)                 -> numbered file content
    list_dir(path)                  -> directory entries
    search(pattern, glob)           -> matching lines with locations
    glob(pattern)                   -> matching paths
    run_command(command, timeout)   -> exit code + output
    git_diff()                      -> the pending change as a diff

Every tool executes through the agent's permission-checked ToolBox, so the
sandbox rules (path containment, write policy, command allowlist, risk
analyzer) hold exactly as they do for the orchestrator-driven calls. Tool
errors are *returned to the model* as results, never raised: a model that
tries to read outside its sandbox learns why and can adapt, which is both
safer and more useful than crashing the attempt.

Write tools (write_file / edit_file) are deliberately not in this surface:
edits stay in the validated EditPlan path where the orchestrator approves
them and verification decides completion.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.security import (
    EndpointNotAllowed,
    find_high_entropy_strings,
    find_secrets,
    validate_endpoint_url,
)

MAX_TOOL_CHARS = 12_000
MAX_SEARCH_HITS = 60
MAX_GLOB_HITS = 200
MAX_WEB_CHARS = 8_000
TOOL_MAX_WRITE_BYTES = 200_000
_MAX_FETCH_REDIRECTS = 5


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]

    def as_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def as_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


def _clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated at {limit} characters]"


def _numbered(content: str, start: int = 1) -> str:
    lines = content.splitlines()
    width = len(str(start + len(lines)))
    return "\n".join(f"{start + i:>{width}}| {line}" for i, line in enumerate(lines))


def _skip_dirs(path: Path) -> bool:
    return any(
        part in {".git", "node_modules", "__pycache__", ".venv", "venv", ".worktrees"}
        for part in path.parts
    )


# ---- handlers (each receives the ToolBox plus keyword arguments) -------------


def _read_file(tools: Any, path: str, start_line: int = 1, end_line: int = 0) -> str:
    content = tools.read_file_full(path)
    if not content:
        return f"{path} is empty"
    lines = content.splitlines()
    end = end_line if end_line and end_line >= start_line else len(lines)
    selected = lines[max(0, start_line - 1) : end]
    return _numbered("\n".join(selected), start=max(1, start_line))


def _list_dir(tools: Any, path: str = ".") -> str:
    # Models call list_dir with file paths too ("what's in this file's
    # directory?") — resolve that instead of failing the call (live test
    # root cause: a plain NotADirectoryError wasted an agent iteration).
    resolved = tools._resolve(str(path))
    if resolved.is_file():
        path = resolved.parent.name or "."
    entries = tools.list_dir(str(path))
    return "\n".join(entries) if entries else "(empty directory)"


def _search(tools: Any, pattern: str, glob: str = "", max_hits: int = MAX_SEARCH_HITS) -> str:
    if not tools.permissions.read_repo:
        return "search is not permitted for this agent"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"invalid regular expression: {exc}"
    hits: list[str] = []
    root: Path = tools.work_root
    for candidate in sorted(root.rglob("*")):
        if len(hits) >= max_hits:
            break
        if not candidate.is_file() or _skip_dirs(candidate.relative_to(root)):
            continue
        try:
            # Repository content is untrusted input: a symlink (checked in by
            # a task or left by an earlier command) must not lead the search
            # outside the sandbox that read_file enforces.
            if not candidate.resolve().is_relative_to(root.resolve()):
                continue
        except OSError:
            continue
        if glob and not fnmatch.fnmatch(candidate.as_posix(), glob) and not fnmatch.fnmatch(
            candidate.name, glob
        ):
            continue
        try:
            if candidate.stat().st_size > 400_000:
                continue
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = candidate.relative_to(root).as_posix()
                hits.append(f"{rel}:{line_number}: {line.strip()[:160]}")
                if len(hits) >= max_hits:
                    break
    return "\n".join(hits) if hits else "no matches"


def _glob(tools: Any, pattern: str) -> str:
    if not tools.permissions.read_repo:
        return "glob is not permitted for this agent"
    root: Path = tools.work_root
    matches: list[str] = []
    for candidate in sorted(root.glob(pattern)):
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            continue
        if _skip_dirs(rel):
            continue
        matches.append(rel.as_posix() + ("/" if candidate.is_dir() else ""))
        if len(matches) >= MAX_GLOB_HITS:
            break
    return "\n".join(matches) if matches else "no matches"


def _run_command(tools: Any, command: str, timeout: int = 120) -> str:
    result = tools.run_command(command, timeout=max(1, min(int(timeout or 120), 900)))
    parts = [f"exit code: {result.returncode}"]
    if result.stdout.strip():
        parts.append("stdout:\n" + _clip(result.stdout.strip()))
    if result.stderr.strip():
        parts.append("stderr:\n" + _clip(result.stderr.strip()))
    if result.timed_out:
        parts.append("(timed out)")
    return "\n".join(parts)


_GIT_REV_OK = re.compile(r"^[A-Za-z0-9._^{}~+]+$")


def _git_diff(tools: Any, base: str = "HEAD") -> str:
    """Read-only git diff, executed through the command allowlist."""
    if "git diff" not in " ".join(tools.permissions.allowed_command_globs) and not any(
        g.startswith("git diff") for g in tools.permissions.allowed_command_globs
    ):
        # fall back to the engine's own git manager when the class lacks git diff
        return "git diff is not permitted for this agent"
    revision = str(base or "HEAD").strip()
    # `base` is model-controlled; a loose token could smuggle git flags such
    # as --output=... or --no-index past the command allowlist (writing or
    # reading outside the sandbox). Only plain revision tokens are accepted.
    if revision.startswith("-") or not _GIT_REV_OK.match(revision):
        return "git_diff: invalid base revision (use a branch, tag, sha, or HEAD~n)"
    return _run_command(tools, f"git diff --unified=2 {revision}")


# ---- write/edit tools (kilo `edit`, codex `apply_patch`, every coding CLI) ----


def flexible_replace(path: str, original: str, search: str, content: str) -> str:
    """Line-based replacement, portable across trailing-newline differences.

    Matching strategy, strongest first (aider/coders/editblock_coder.py):
    an exact line-sequence match, then a match flexible about surrounding
    whitespace (leading indentation and trailing newlines). Reconstructing
    from ``split("\\n")`` keeps the untouched lines byte-identical.
    """
    whole_lines = original.split("\n")
    part_lines = search.split("\n")
    replace_lines = content.split("\n")
    if len(part_lines) > len(whole_lines):
        raise ValueError(
            f"search block not found in {path}; re-read the file and copy the search text verbatim"
        )

    # 1. exact line-sequence match
    for i in range(len(whole_lines) - len(part_lines) + 1):
        if whole_lines[i : i + len(part_lines)] == part_lines:
            return "\n".join(
                whole_lines[:i] + replace_lines + whole_lines[i + len(part_lines) :]
            )

    # 2. whitespace-flexible match (aider's perfect_or_whitespace idea):
    #    strip() on both sides tolerates indentation AND the trailing
    #    newline a model often omits from the last search line.
    stripped_part = [line.strip() for line in part_lines]
    for i in range(len(whole_lines) - len(part_lines) + 1):
        window = whole_lines[i : i + len(part_lines)]
        if [line.strip() for line in window] != stripped_part:
            continue
        first = window[0]
        indent = first[: len(first) - len(first.lstrip())] if first.strip() else ""
        indented = [indent + line if line.strip() else line for line in replace_lines]
        return "\n".join(whole_lines[:i] + indented + whole_lines[i + len(part_lines) :])
    raise ValueError(
        f"search block not found in {path}; re-read the file and copy the search text verbatim"
    )


def _write_file(tools: Any, path: str, content: str) -> str:
    """Create or overwrite a file inside the sandbox's write policy."""
    if len(content) > TOOL_MAX_WRITE_BYTES:
        return f"content exceeds the per-file size limit ({TOOL_MAX_WRITE_BYTES} bytes)"
    written = tools.write_file(path, content)
    return f"wrote {len(content)} chars to {written}"


def _edit_file(tools: Any, path: str, search: str, content: str) -> str:
    """Aider-style SEARCH/REPLACE on an existing file, inside the write policy."""
    if not str(search).strip():
        return "edit_file requires a non-empty 'search' block copied verbatim from the file"
    original = tools.read_file_full(path)
    if search in original:
        updated = original.replace(search, content, 1)
    else:
        try:
            updated = flexible_replace(path, original, search, content)
        except ValueError as exc:
            return str(exc)  # a failed edit is an observation, not a crash
    written = tools.write_file(path, updated)
    return f"edited {written}"


# ---- environment awareness (codex `curr_time`, `get_context_remaining`) ------


def _current_time(tools: Any) -> str:
    now = datetime.now(UTC)
    return "UTC now: " + now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _budget_status(tools: Any) -> str:
    """The run's remaining budget, so agents can pace themselves (codex pattern)."""
    budget = getattr(tools, "budget", None)
    if budget is None:
        return "budget tracking is not available in this sandbox"
    snapshot = budget.snapshot()
    return "\n".join(f"{key}: {value}" for key, value in snapshot.items())


# ---- memory tools (gemini `save_memory`, kilo `recall`) ----------------------


def _save_memory(tools: Any, text: str, kind: str = "fact", tags: str = "", pinned: bool = False) -> str:
    """Persist a durable fact/decision/lesson into project memory."""
    workspace = getattr(tools, "workspace", None)
    if workspace is None:
        return "memory is not available in this sandbox"
    text = " ".join(str(text).split())
    if not text:
        return "refusing to store an empty memory"
    # Secret redaction (kilo's redact step): never let a model write a
    # credential into persistent memory.
    if find_secrets(text) or find_high_entropy_strings(text):
        return (
            "refusing to store a potential secret in memory; "
            "remove the credential and store only the non-secret fact"
        )
    from .memory import KINDS, memory_store

    if kind not in KINDS:
        kind = "fact"
    tag_list = [t.strip() for t in str(tags).split(",") if t.strip()]
    # Secret redaction covers metadata too: a credential in a tag or source
    # would land in memory.json and MEMORY.md just the same.
    scanned_metadata = " ".join([*tag_list, f"agent:{tools.permissions.name}"])
    if find_secrets(scanned_metadata) or find_high_entropy_strings(scanned_metadata):
        return (
            "refusing to store a potential secret in memory; "
            "remove the credential and store only the non-secret fact"
        )
    store = memory_store(workspace)
    item = store.add(
        kind, text, source=f"agent:{tools.permissions.name}", tags=tag_list, pinned=bool(pinned)
    )
    return f"saved memory {item.id} ({item.kind}): {item.text[:160]}"


def _recall_memory(tools: Any, query: str, limit: int = 8) -> str:
    """Search project memory for relevant facts/decisions/lessons (kilo recall)."""
    workspace = getattr(tools, "workspace", None)
    if workspace is None:
        return "memory is not available in this sandbox"
    from .memory import memory_store

    items = memory_store(workspace).recall(str(query), limit=max(1, min(int(limit or 8), 20)))
    if not items:
        return "no memories recalled for this query"
    lines = [f"- ({item.kind}, conf {item.confidence:.2f}{', pinned' if item.pinned else ''}) {item.text}" for item in items]
    return "\n".join(lines)


# ---- network (kilo/opencode `webfetch` + `websearch`, behind network policy) --


def _strip_html(body: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def _web_fetch(tools: Any, url: str, max_chars: int = MAX_WEB_CHARS) -> str:
    """Fetch a URL through the network policy (SSRF-guarded, read-only)."""
    if not tools.permissions.network:
        return "network access is not permitted for this agent"
    import httpx

    # Every hop of a redirect chain must pass the endpoint policy — following
    # redirects blindly would let a public page bounce the fetch into
    # loopback/private/metadata space (SSRF).
    current: str = str(url)
    status_code = 0
    content_type = ""
    text = ""
    try:
        for _ in range(_MAX_FETCH_REDIRECTS):
            validate_endpoint_url(current)
            with httpx.stream(
                "GET",
                current,
                timeout=30.0,
                follow_redirects=False,
                headers={"User-Agent": "autonomous-engine/0.6 (+https://github.com)"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        return "fetch failed: redirect without a location header"
                    current = str(httpx.URL(response.url).join(location))
                    continue
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")
                # Stream and cap the read: buffering an unbounded body would
                # let a huge URL exhaust the runtime's memory.
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_TOOL_CHARS:
                        break
                text = b"".join(chunks).decode(
                    response.charset_encoding or "utf-8", errors="replace"
                )
                break
        else:
            return "blocked by network policy: too many redirects"
    except EndpointNotAllowed as exc:
        return f"blocked by network policy: {exc}"
    except httpx.HTTPError as exc:
        return f"fetch failed: {exc}"
    if "html" in content_type:
        text = _strip_html(text)
    body = text[: max(200, min(int(max_chars or MAX_WEB_CHARS), MAX_TOOL_CHARS))]
    return f"status: {status_code} | content-type: {content_type}\n\n{body}"


_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_DDG_SNIPPET = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_MAX_SEARCH_RESULTS = 8


def _decode_ddg_href(href: str) -> str:
    """DuckDuckGo wraps target URLs in a redirect; unwrap when present."""
    from urllib.parse import parse_qs, urlsplit

    if "uddg=" in href:
        query = parse_qs(urlsplit(href).query)
        target = (query.get("uddg") or [""])[0]
        if target:
            return target
    if href.startswith("//"):
        return "https:" + href
    return href


def _web_search(tools: Any, query: str, max_results: int = _MAX_SEARCH_RESULTS) -> str:
    """Web search through the network policy (gemini web-search / kilo websearch).

    Uses DuckDuckGo's HTML endpoint — no API key, gated by the same network
    permission and endpoint policy as web_fetch. Results are title/URL/snippet
    triples; fetching the full page is web_fetch's job.
    """
    if not tools.permissions.network:
        return "network access is not permitted for this agent"
    query = str(query).strip()
    if not query:
        return "web_search requires a non-empty query"
    import html as html_mod

    import httpx

    try:
        response = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "autonomous-engine/0.6 (+https://github.com)"},
        )
    except httpx.HTTPError as exc:
        return f"search failed: {exc}"
    titles = [html_mod.unescape(re.sub(r"(?s)<[^>]+>", "", t)).strip() for t in _DDG_RESULT.findall(response.text)]
    urls = [_decode_ddg_href(u) for u, _ in _DDG_RESULT.findall(response.text)]
    snippets = [
        html_mod.unescape(re.sub(r"(?s)<[^>]+>", "", s)).strip()
        for s in _DDG_SNIPPET.findall(response.text)
    ]
    if not titles:
        return "no results (the search engine returned nothing usable; try different terms or web_fetch the URL directly)"
    lines: list[str] = []
    for index in range(min(len(titles), max(1, min(int(max_results or _MAX_SEARCH_RESULTS), _MAX_SEARCH_RESULTS)))):
        snippet = snippets[index] if index < len(snippets) else ""
        lines.append(f"{index + 1}. {titles[index]}\n   {urls[index]}\n   {snippet[:200]}")
    return "\n".join(lines)


# ---- batch read (gemini read-many-files) -------------------------------------


def _read_files(tools: Any, paths: list[str], max_chars_each: int = 4000) -> str:
    """Read several files in one call, each truncated (fewer round-trips)."""
    if not isinstance(paths, list) or not paths:
        return "read_files requires a non-empty 'paths' array"
    sections: list[str] = []
    for rel in [str(p) for p in paths][:8]:
        try:
            content = tools.read_file(rel)
        except PermissionError as exc:
            sections.append(f"## {rel}\npermission denied: {exc}")
            continue
        except FileNotFoundError:
            sections.append(f"## {rel}\nnot found")
            continue
        limit = max(200, min(int(max_chars_each or 4000), 20_000))
        if len(content) > limit:
            content = content[:limit] + f"\n... [truncated at {limit} characters]"
        sections.append(f"## {rel}\n{content}")
    return "\n\n".join(sections)


_TOOL_DEFS: tuple[tuple[str, str, dict[str, Any], Callable[..., str]], ...] = (
    (
        "read_file",
        "Read a file from the repository, with line numbers. Use start_line/end_line for large files.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path."},
                "start_line": {"type": "integer", "description": "First line (1-based)."},
                "end_line": {"type": "integer", "description": "Last line (0 = to end)."},
            },
            "required": ["path"],
        },
        _read_file,
    ),
    (
        "list_dir",
        "List the entries of a directory in the repository.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory, default '.'."}},
        },
        _list_dir,
    ),
    (
        "search",
        "Search file contents with a regular expression; returns path:line matches.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "glob": {"type": "string", "description": "Optional file filter, e.g. '*.py'."},
            },
            "required": ["pattern"],
        },
        _search,
    ),
    (
        "glob",
        "Find files by glob pattern, e.g. 'src/**/*.py'.",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        _glob,
    ),
    (
        "run_command",
        "Run an allowed development command (tests, linters, builds) and return its output.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line, no shell features."},
                "timeout": {"type": "integer", "description": "Seconds, default 120."},
            },
            "required": ["command"],
        },
        _run_command,
    ),
    (
        "git_diff",
        "Show the pending uncommitted changes as a unified diff.",
        {
            "type": "object",
            "properties": {"base": {"type": "string", "description": "Revision, default HEAD."}},
        },
        _git_diff,
    ),
    (
        "write_file",
        "Create or overwrite a file inside your write policy. Use for NEW files; prefer edit_file for changes.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
        _write_file,
    ),
    (
        "edit_file",
        "Replace an exact text block inside an existing file (SEARCH/REPLACE). Copy the search text verbatim from a recent read_file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path."},
                "search": {"type": "string", "description": "Exact existing text to find (enough lines to be unique)."},
                "content": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "search", "content"],
        },
        _edit_file,
    ),
    (
        "current_time",
        "Current UTC time, for timestamps and deadline reasoning.",
        {"type": "object", "properties": {}},
        _current_time,
    ),
    (
        "budget_status",
        "Show the run's remaining time/cost/token budget so you can pace your work.",
        {"type": "object", "properties": {}},
        _budget_status,
    ),
    (
        "save_memory",
        "Persist a durable fact, decision, or lesson into project memory. Never store secrets.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The memory, one self-contained sentence."},
                "kind": {"type": "string", "description": "fact | decision | lesson | preference | incident."},
                "tags": {"type": "string", "description": "Comma-separated tags for later recall."},
                "pinned": {"type": "boolean", "description": "Always load into future context."},
            },
            "required": ["text"],
        },
        _save_memory,
    ),
    (
        "recall_memory",
        "Search project memory for relevant facts, decisions, and lessons.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Terms to search for."},
                "limit": {"type": "integer", "description": "Max results, default 8."},
            },
            "required": ["query"],
        },
        _recall_memory,
    ),
    (
        "web_fetch",
        "Fetch a public URL (documentation, API references) as text. Requires network permission.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Fully-qualified http(s) URL."},
                "max_chars": {"type": "integer", "description": "Max characters returned, default 8000."},
            },
            "required": ["url"],
        },
        _web_fetch,
    ),
    (
        "web_search",
        "Search the web for documentation and answers. Requires network permission. Returns title/URL/snippet results.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
                "max_results": {"type": "integer", "description": "Max results, default 8."},
            },
            "required": ["query"],
        },
        _web_search,
    ),
    (
        "read_files",
        "Read several files in one call (batch of read_file, each truncated).",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project-relative file paths (max 8).",
                },
                "max_chars_each": {"type": "integer", "description": "Per-file char cap, default 4000."},
            },
            "required": ["paths"],
        },
        _read_files,
    ),
)

TOOLS: dict[str, ToolSpec] = {
    name: ToolSpec(name=name, description=description, parameters=parameters, handler=handler)
    for name, description, parameters, handler in _TOOL_DEFS
}

# Convenience groups used by agents when declaring their surface.
READ_ONLY_TOOLS = ("read_file", "read_files", "list_dir", "search", "glob", "git_diff")
DEVELOPER_TOOLS = (
    "read_file",
    "read_files",
    "list_dir",
    "search",
    "glob",
    "git_diff",
    "run_command",
    "write_file",
    "edit_file",
)
MEMORY_TOOLS = ("save_memory", "recall_memory")
ENVIRONMENT_TOOLS = ("current_time", "budget_status")
NETWORK_TOOLS = ("web_fetch", "web_search")
# Builders get memory too: a coder that just learned "library X breaks under
# Node 24" records it where the next agent will recall it (plan.md §28/§29).
BUILDER_TOOLS = (*DEVELOPER_TOOLS, *MEMORY_TOOLS, "current_time")


def tool_specs(names: tuple[str, ...] | list[str]) -> list[ToolSpec]:
    return [TOOLS[name] for name in names if name in TOOLS]


def openai_tool_schemas(names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [spec.as_openai_schema() for spec in tool_specs(names)]


def anthropic_tool_schemas(names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [spec.as_anthropic_schema() for spec in tool_specs(names)]


def execute_tool(tools: Any, name: str, arguments: dict[str, Any]) -> str:
    """Run one tool call; every failure becomes a model-visible observation."""
    spec = TOOLS.get(name)
    if spec is None:
        return f"unknown tool: {name}. Available: {', '.join(sorted(TOOLS))}"
    try:
        return _clip(str(spec.handler(tools, **(arguments or {}))))
    except PermissionError as exc:
        return f"permission denied: {exc}"
    except FileNotFoundError as exc:
        return f"not found: {exc}"
    except TypeError as exc:
        return f"invalid arguments for {name}: {exc}"
    except Exception as exc:  # a tool crash must not kill the attempt
        return f"tool error ({name}): {exc}"
