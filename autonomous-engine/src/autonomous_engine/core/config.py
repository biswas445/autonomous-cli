"""Project-level configuration: budgets, permissions, model routing, run modes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RunMode = Literal["autonomous", "supervised"]


class BudgetConfig(BaseModel):
    """Resource limits. Time is a runtime budget, not the definition of success."""

    max_runtime_seconds: int = 48 * 3600
    max_token_budget: float = 250.0  # estimated USD
    max_parallel_agents: int = 4
    max_task_attempts: int = 3  # REPEATED_FAILURE stop condition


class PermissionClass(BaseModel):
    """Explicit least-privilege permissions for one agent class."""

    name: str
    read_repo: bool = False
    write_paths: list[str] = Field(default_factory=list)  # globs, relative to project
    run_commands: bool = False
    allowed_command_globs: list[str] = Field(default_factory=list)  # e.g. ["pytest*", "python*"]
    network: bool = False
    git_write: bool = False
    # Defense in depth (OpenHands risk analyzer): HIGH-risk commands
    # (force pushes, publishes, recursive deletes, destructive SQL) are
    # refused by the sandbox unless this class explicitly opts in.
    allow_high_risk: bool = False

    def can_run_command(self, command: str) -> bool:
        if not self.run_commands:
            return False
        if not self.allowed_command_globs:
            return True
        import fnmatch

        first_token = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        return any(
            fnmatch.fnmatch(first_token, g) or fnmatch.fnmatch(command.strip(), g)
            for g in self.allowed_command_globs
        )


DEFAULT_PERMISSION_CLASSES: dict[str, PermissionClass] = {
    "researcher": PermissionClass(
        name="researcher",
        read_repo=True,
        network=True,
    ),
    "architect": PermissionClass(
        name="architect",
        read_repo=True,
        write_paths=[".agents/architecture/**"],
    ),
    "planner": PermissionClass(
        name="planner",
        read_repo=True,
        write_paths=[".agents/planning/**"],
    ),
    "coder": PermissionClass(
        name="coder",
        read_repo=True,
        write_paths=["src/**", "tests/**", "app/**", "lib/**", "docs/**", "*.toml", "*.md"],
        run_commands=True,
        allowed_command_globs=[
            "pytest*",
            "python*",
            "pip install*",
            "node*",
            "npm*",
            "npx*",
            "ruff*",
            "mypy*",
            "tsc*",
            "go*",
            "cargo*",
            "make*",
        ],
    ),
    "tester": PermissionClass(
        name="tester",
        read_repo=True,
        write_paths=[".agents/verification/**", "tests/**", "reports/**"],
        run_commands=True,
        allowed_command_globs=[
            "pytest*",
            "python*",
            # `test`/`ls`-style file-existence probes the DoD engine and real
            # models emit constantly (live test: `test -f app/store.py` was
            # rejected, failing verification on a healthy implementation).
            "test*",
            "ls*",
            "dir*",
            "node*",
            "npm*",
            "npx*",
            "ruff*",
            "mypy*",
            "tsc*",
            "go*",
            "cargo*",
            "make*",
            "git diff*",
            "git log*",
            "git status*",
        ],
    ),
    "reviewer": PermissionClass(
        name="reviewer",
        read_repo=True,
        write_paths=[".agents/verification/**"],
        run_commands=True,
        allowed_command_globs=["git diff*", "git log*", "git status*"],
    ),
    "debugger": PermissionClass(
        name="debugger",
        read_repo=True,
        write_paths=["src/**", "tests/**", "app/**", "lib/**"],
        run_commands=True,
        allowed_command_globs=[
            "pytest*",
            "python*",
            "node*",
            "npm*",
            "git diff*",
            "git log*",
            "git status*",
        ],
    ),
    "security": PermissionClass(
        name="security",
        read_repo=True,
        write_paths=[".agents/verification/**"],
        run_commands=True,
        allowed_command_globs=["git diff*", "git log*", "git status*"],
    ),
    "director": PermissionClass(
        name="director",
        read_repo=True,
        write_paths=[".agents/**"],
        git_write=True,
    ),
    "release": PermissionClass(
        name="release",
        read_repo=True,
        write_paths=[".agents/**", "dist/**", "CHANGELOG.md"],
        run_commands=True,
        allowed_command_globs=["pytest*", "python*", "git tag*", "git log*"],
        git_write=True,
    ),
}


class ModelRoute(BaseModel):
    """Route an agent role to a provider/model pair, with fallbacks."""

    role: str
    provider: str
    model: str
    fallbacks: list[str] = Field(default_factory=list)  # ["provider/model", ...]


DEFAULT_MODEL_ROUTES: list[ModelRoute] = [
    ModelRoute(role="intent_compiler", provider="echo", model="default"),
    ModelRoute(role="product", provider="echo", model="default"),
    ModelRoute(role="researcher", provider="echo", model="default"),
    ModelRoute(role="architect", provider="echo", model="default"),
    ModelRoute(role="planner", provider="echo", model="default"),
    ModelRoute(role="coder", provider="echo", model="default"),
    ModelRoute(role="tester", provider="echo", model="default"),
    ModelRoute(role="reviewer", provider="echo", model="default"),
    ModelRoute(role="security", provider="echo", model="default"),
    ModelRoute(role="debugger", provider="echo", model="default"),
    ModelRoute(role="director", provider="echo", model="default"),
    ModelRoute(role="release", provider="echo", model="default"),
]


class QualityGatePolicy(BaseModel):
    """Project-configurable quality-gate policy (verification spec §50).

    Schema-versioned and deterministic: the same evidence against the same
    policy always produces the same gate verdict.
    """

    schema_version: int = 1
    # At least one executed command (or machine-checkable criterion) is
    # required to pass: agent assertions alone can never satisfy the gate.
    require_executable_evidence: bool = True
    # Manual (unverifiable) criteria block the gate instead of warning.
    strict_manual_checks: bool = False
    # A task whose verification history flip-flops (fail→pass→fail) requires
    # a clean re-run before the gate passes again.
    block_flaky_history: bool = True


class ProjectConfig(BaseModel):
    """Machine-readable configuration for one autonomous-engine project."""

    schema_version: int = 1
    project_name: str = "untitled"
    run_mode: RunMode = "autonomous"
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    verification: QualityGatePolicy = Field(default_factory=QualityGatePolicy)
    permission_classes: dict[str, PermissionClass] = Field(
        default_factory=lambda: dict(DEFAULT_PERMISSION_CLASSES)
    )
    model_routes: list[ModelRoute] = Field(default_factory=lambda: list(DEFAULT_MODEL_ROUTES))
    enhance_prompt: bool = True  # Intent Compiler toggle
    verify_definitions_of_done: bool = True
    git_checkpoints: bool = True
    worktree_parallelism: bool = False  # v0.3+ capability; off by default
    stop_on_repeated_failure: bool = True
    # §38: complex tasks get a research pass before implementation.
    research_before_coding: bool = True
    research_complexity_threshold: int = 7
    # §61: execution sandbox. "process" uses the ToolBox allowlists directly;
    # "docker" runs agent commands inside a container.
    sandbox_backend: Literal["process", "docker"] = "process"
    sandbox_image: str = "python:3.12-slim"
    # Directive #10: HIGH-risk commands that would be refused on the host run
    # inside the Docker sandbox instead, when Docker is available.
    prefer_docker_high_risk: bool = True
    # User-defined event hooks from .agents/hooks.json (never fatal).
    hooks_enabled: bool = True
    # Agentic tool loop: let agents read/search/run during their reasoning.
    tools_enabled: bool = True
    tool_loop_max_iterations: int = 6
    extra: dict[str, Any] = Field(default_factory=dict)

    def route_for(self, role: str) -> ModelRoute:
        for route in self.model_routes:
            if route.role == role:
                return route
        return ModelRoute(role=role, provider="echo", model="default")

    def permission_for(self, agent_class: str) -> PermissionClass:
        perm = self.permission_classes.get(agent_class)
        if perm is None:
            return PermissionClass(name=agent_class)
        return perm

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
