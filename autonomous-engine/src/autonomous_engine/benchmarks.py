"""Evaluation harness (plan.md §41): benchmark the system, not the model.

Without evaluations you cannot tell whether an orchestration change actually
helps. This harness runs a fixed set of benchmark projects end-to-end with
the offline echo provider (deterministic, reproducible, free), then scores
each run on the questions that matter:

    Did it finish?  How many cycles?  How much did it cost?
    How many retries?  How many escalations?  Any replans?

Results are written as JSON + a markdown report so runs are comparable over
time. Real-model benchmarks use the same harness with a configured provider.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime.bootstrap import init_project
from .runtime.context_setup import open_context
from .runtime.metrics import collect_metrics
from .runtime.orchestrator import Orchestrator
from .runtime.roadmap import build_roadmap


@dataclass
class BenchmarkCase:
    id: str
    goal: str
    description: str = ""
    max_cycles: int = 40


# §41's benchmark families: an API, a dashboard, a CLI tool, an e-commerce
# backend, and a refactor-shaped goal. The echo provider makes them
# deterministic; the same cases run against real models unchanged.
BENCHMARK_CASES: list[BenchmarkCase] = [
    BenchmarkCase(
        "rest-api",
        "Build a REST API for managing notes with create, list, and delete endpoints",
        "Project A: REST API",
    ),
    BenchmarkCase(
        "saas-dashboard",
        "Build a SaaS dashboard showing user activity metrics and a settings page",
        "Project B: SaaS dashboard",
    ),
    BenchmarkCase(
        "cli-tool",
        "Build a CLI tool that converts CSV files to JSON with validation",
        "Project C: CLI tool",
    ),
    BenchmarkCase(
        "ecommerce-backend",
        "Build an e-commerce backend with product catalog, cart, and checkout",
        "Project D: e-commerce backend",
    ),
    BenchmarkCase(
        "refactor",
        "Refactor the repository so the codebase has a clean module structure and tests",
        "Project E: existing repository refactor",
    ),
]


@dataclass
class CaseResult:
    case_id: str
    goal: str
    status: str = ""
    stop_reason: str = ""
    finished: bool = False
    cycles: int = 0
    cost_usd: float = 0.0
    tasks_completed: int = 0
    tasks_failed: int = 0
    retries: int = 0
    escalations: int = 0
    replans: int = 0
    commits: int = 0
    milestones_done: int = 0
    runtime_seconds: float = 0.0
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


def run_case(case: BenchmarkCase, out_dir: Path, *, max_cycles: int | None = None) -> CaseResult:
    """Run one benchmark case offline and collect its evidence."""
    result = CaseResult(case_id=case.id, goal=case.goal)
    project_root = out_dir / case.id
    started = time.perf_counter()
    try:
        init_project(project_root, project_name=case.id, objective=case.goal)
        context = open_context(project_root)
        orchestrator = Orchestrator(context, use_model_director=False)
        run_result = asyncio_run(orchestrator.run_loop(case.goal))
        elapsed = time.perf_counter() - started

        result.status = run_result.status
        result.stop_reason = run_result.stop.reason.value if run_result.stop else ""
        result.finished = run_result.status == "completed"
        result.cycles = run_result.cycles
        result.cost_usd = round(run_result.cost_usd, 4)
        result.runtime_seconds = round(elapsed, 2)

        graph = context.workspace.load_graph()
        result.tasks_completed = len(graph.completed_tasks())
        result.tasks_failed = len(graph.failed_tasks())
        result.retries = sum(1 for t in graph.all() if t.attempts > 1)
        result.escalations = len(context.workspace.load_escalations())
        result.milestones_done = sum(
            1 for m in build_roadmap(graph)["milestones"] if m["status"] == "done"
        )
        result.metrics = collect_metrics(context.workspace, context.store)
        result.replans = result.metrics.get("replans", 0)
        result.commits = result.metrics.get("commits", 0)
        context.db.close()
    except Exception as exc:  # a benchmark case failing IS a result
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def run_benchmark(
    out_dir: Path,
    *,
    cases: list[BenchmarkCase] | None = None,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Run all benchmark cases and write results.json + report.md."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    case_results = [
        run_case(case, out_dir, max_cycles=max_cycles) for case in (cases or BENCHMARK_CASES)
    ]
    finished = sum(1 for r in case_results if r.finished)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "echo (offline, deterministic)",
        "cases": len(case_results),
        "finished": finished,
        "finish_rate": round(finished / len(case_results), 3) if case_results else 0.0,
        "total_cost_usd": round(sum(r.cost_usd for r in case_results), 4),
        "total_cycles": sum(r.cycles for r in case_results),
        "total_retries": sum(r.retries for r in case_results),
        "total_escalations": sum(r.escalations for r in case_results),
        "results": [r.__dict__ | {"metrics": r.metrics} for r in case_results],
    }
    (out_dir / "results.json").write_text(json_dump(payload), encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    return payload


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Autonomous Engineering Benchmark Report",
        "",
        f"Generated: {payload['generated_at']}",
        f"Provider: {payload['provider']}",
        "",
        "| case | finished | status | cycles | tasks | retries | escalations | replans | cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in payload["results"]:
        lines.append(
            f"| {r['case_id']} | {'yes' if r['finished'] else 'no'} "
            f"| {r['status'] or r['error'][:30]} | {r['cycles']} "
            f"| {r['tasks_completed']}c/{r['tasks_failed']}f | {r['retries']} "
            f"| {r['escalations']} | {r['replans']} | ${r['cost_usd']} |"
        )
    lines += [
        "",
        f"**Finish rate:** {payload['finish_rate']:.0%} ({payload['finished']}/{payload['cases']})",
        f"**Total cost:** ${payload['total_cost_usd']} (offline estimate)",
        f"**Total cycles:** {payload['total_cycles']}",
        "",
        "Re-runs are deterministic (echo provider); use them to compare "
        "orchestration changes, then repeat with a real provider.",
        "",
    ]
    return "\n".join(lines)


def json_dump(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
