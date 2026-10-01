"""Command-line interface: the operator's view of the runtime.

Every command is a thin wrapper over the same deterministic core the tests
exercise. The CLI never decides anything; it renders state and translates an
operator's intent into a control request.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..core.config import BudgetConfig, ProjectConfig
from ..core.state_machine import TaskState
from ..core.store import RunRecord
from ..core.task import TaskGraph, new_id
from ..core.workspace import find_workspace
from ..runtime.bootstrap import init_project, project_summary
from ..runtime.context_setup import open_context
from ..runtime.orchestrator import Orchestrator
from ..runtime.stop import STOP_DESCRIPTIONS

app = typer.Typer(
    name="auto",
    help="Autonomous engineering runtime: plan, implement, verify, and stop on evidence.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code=code)


def _resolve_root(path: str | None) -> Path:
    if path:
        return Path(path).resolve()
    found = find_workspace()
    if found is None:
        _fail("no .agents/ workspace found here or above; run `auto init <path>` first")
    assert found is not None
    return found.root


def _context(path: str | None = None):
    root = _resolve_root(path)
    try:
        return open_context(root)
    except FileNotFoundError as exc:
        _fail(str(exc))
        raise


def _print_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ----------------------------------------------------------------------
# init
# ----------------------------------------------------------------------


@app.command()
def init(
    path: str = typer.Argument(".", help="Directory to initialise."),
    name: str = typer.Option("", "--name", help="Project name (defaults to the directory name)."),
    objective: str = typer.Option("", "--objective", "-o", help="What the project must become."),
    mode: str = typer.Option("autonomous", "--mode", help="autonomous | supervised"),
    max_runtime_hours: float = typer.Option(48.0, "--max-runtime-hours"),
    max_budget: float = typer.Option(250.0, "--max-budget", help="Estimated USD ceiling."),
    parallel: int = typer.Option(4, "--parallel", help="Maximum agents working at once."),
    attempts: int = typer.Option(3, "--attempts", help="Attempts per task before escalation."),
    git: bool = typer.Option(True, "--git/--no-git", help="Create a Git repository if absent."),
    force: bool = typer.Option(False, "--force", help="Re-initialise an existing workspace."),
) -> None:
    """Create the persistent project workspace (`.agents/`)."""
    root = Path(path).resolve()
    budget = BudgetConfig(
        max_runtime_seconds=int(max_runtime_hours * 3600),
        max_token_budget=max_budget,
        max_parallel_agents=max(1, parallel),
        max_task_attempts=max(1, attempts),
    )
    try:
        context = init_project(
            root,
            project_name=name,
            objective=objective,
            run_mode=mode,  # type: ignore[arg-type]
            budget=budget,
            git=git,
            force=force,
        )
    except Exception as exc:
        _fail(f"initialisation failed: {exc}")
        return

    from ..runtime.instructions import ensure_project_instructions

    ensure_project_instructions(context.workspace)
    summary = project_summary(context)
    budget_line = (
        f"${context.config.budget.max_token_budget} / {context.config.budget.max_runtime_seconds}s"
    )
    console.print(
        Panel(
            f"[bold]{summary['name']}[/bold]\n"
            f"root: {summary['root']}\n"
            f"objective: {summary['objective'] or '(none yet)'}\n"
            f"budget: {budget_line}\n"
            f"constitution: {'written' if summary['constitution_written'] else 'missing'}",
            title="project initialised",
            border_style="green",
        )
    )
    console.print('Next: [bold]auto run[/bold]  (or `auto run "the objective"`)')
    console.print("      [dim]auto run --provider echo --max-cycles 15[/dim]  # offline dry run")


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------


@app.command()
def run(
    objective: str = typer.Argument("", help="Objective override (plan.md §33 style)."),
    path: str = typer.Option(
        "", "--path", help="Project directory (defaults to the nearest workspace)."
    ),
    objective_option: str = typer.Option("", "--objective", "-o", help="Objective override."),
    max_cycles: int = typer.Option(
        200, "--max-cycles", help="Safety ceiling on orchestration cycles."
    ),
    provider: str = typer.Option(
        "", "--provider", help="Force a model provider for this run (e.g. echo)."
    ),
    model: str = typer.Option("", "--model", help="Model id to use with --provider."),
    no_director: bool = typer.Option(
        False, "--no-director", help="Use only the deterministic director."
    ),
    parallel: bool = typer.Option(False, "--parallel", help="Run independent tasks in worktrees."),
    supervised: bool = typer.Option(
        False,
        "--supervised",
        "--approval-gates",
        help="Stop for approval before risky tasks (§16).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the run result as JSON."),
) -> None:
    """Run the autonomous loop until a stop condition fires."""
    context = _context(path)
    config: ProjectConfig = context.config
    if provider:
        for route in config.model_routes:
            route.provider = provider
            if model:
                route.model = model
    mode_changed = False
    if supervised and config.run_mode != "supervised":
        config.run_mode = "supervised"
        mode_changed = True
    if parallel and not config.worktree_parallelism:
        config.worktree_parallelism = True
        mode_changed = True
    if mode_changed:
        # Mode changes must survive a crash/resume, so persist them.
        context.workspace.save_config(config)
    effective_objective = objective_option or objective
    context.config = config

    orchestrator = Orchestrator(context, max_cycles=max_cycles, use_model_director=not no_director)
    console.print(
        f"[bold]run[/bold] {context.project.name} — objective: "
        f"[italic]{context.objective or '(none)'}[/italic]"
    )
    console.print(
        f"[dim]provider routes: {config.model_routes[0].provider if config.model_routes else 'n/a'}[/dim]"
    )

    events: list[str] = []

    def on_event(event: str, payload: dict[str, Any]) -> None:
        events.append(event)
        if json_output:
            return
        style = _EVENT_STYLES.get(event.split(".")[0], "dim")
        detail = payload.get("detail") or payload.get("reason") or payload.get("summary") or ""
        console.print(
            f"[{style}]{payload.get('timestamp', '')[-8:]}[/{style}] "
            f"[{style}]{event}[/{style}] {str(detail)[:100]}"
        )

    try:
        result = asyncio.run(orchestrator.run_loop(effective_objective))
    except KeyboardInterrupt as exc:
        orchestrator.control.request(stop=True, reason="keyboard interrupt")
        console.print(
            "[yellow]interrupted; the run state was persisted and can be resumed[/yellow]"
        )
        raise typer.Exit(130) from exc
    except Exception as exc:
        console.print(f"[bold red]run failed:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        _print_json(result.as_dict())
    else:
        _render_run_result(orchestrator, result)
    if result.status != "completed":
        raise typer.Exit(2)


_EVENT_STYLES = {
    "run": "bold cyan",
    "bootstrap": "cyan",
    "intent": "cyan",
    "plan": "cyan",
    "task": "magenta",
    "agent": "blue",
    "verification": "green",
    "director": "yellow",
    "checkpoint": "green",
    "git": "green",
    "escalation": "bold red",
    "stop": "bold",
    "failure": "red",
    "persistence": "red",
    "recovery": "yellow",
}


def _render_run_result(orchestrator: Orchestrator, result: Any) -> None:
    progress = orchestrator.graph.progress()
    stop = result.stop
    colour = "green" if result.status == "completed" else "yellow"
    console.print(
        Panel(
            f"[bold]status:[/bold] [{colour}]{result.status}[/{colour}]\n"
            f"reason: {stop.reason.value if stop else 'n/a'}\n"
            f"{stop.describe() if stop else ''}\n\n"
            f"cycles: {result.cycles}   cost: ${result.cost_usd:.4f}\n"
            f"tasks: {progress['completed']}/{progress['total']} complete, "
            f"{progress['failed']} failed, {progress['active']} active",
            title="run finished",
            border_style=colour,
        )
    )
    if stop and stop.reason.value in STOP_DESCRIPTIONS:
        console.print(f"[dim]{STOP_DESCRIPTIONS[stop.reason.value]}[/dim]")


# ----------------------------------------------------------------------
# control
# ----------------------------------------------------------------------


@app.command()
def pause(
    path: str = typer.Option("", "--path"),
    reason: str = typer.Option("operator request", "--reason"),
) -> None:
    """Ask a running loop to pause at the next cycle boundary."""
    context = _context(path)
    context.workspace.save_run({**(context.workspace.load_run()), "status": "pausing"})
    _context_signal(context).request(pause=True, reason=reason)
    console.print("[yellow]pause requested;[/yellow] the loop stops at the next cycle boundary.")


@app.command()
def resume(path: str = typer.Option("", "--path")) -> None:
    """Clear a pause request."""
    context = _context(path)
    _context_signal(context).request(pause=False, resume=True)
    context.workspace.save_run({**(context.workspace.load_run()), "status": "running"})
    console.print("[green]resumed;[/green] run `auto run` to continue execution.")


@app.command()
def stop(
    path: str = typer.Option("", "--path"),
    reason: str = typer.Option("operator request", "--reason"),
) -> None:
    """Ask a running loop to stop and record the reason."""
    context = _context(path)
    _context_signal(context).request(stop=True, reason=reason)
    console.print(f"[yellow]stop requested:[/yellow] {reason}")


@app.command()
def approve(
    escalation_id: str = typer.Argument(..., help="Escalation id, or 'all'."),
    path: str = typer.Option("", "--path"),
) -> None:
    """Approve a pending escalation."""
    _resolve_escalation(path, escalation_id, "approved")


@app.command()
def reject(
    escalation_id: str = typer.Argument(..., help="Escalation id, or 'all'."),
    path: str = typer.Option("", "--path"),
    reason: str = typer.Option("rejected by the operator", "--reason"),
) -> None:
    """Reject a pending escalation."""
    _resolve_escalation(path, escalation_id, "rejected", reason)


def _resolve_escalation(
    path: str | None, escalation_id: str, resolution: str, note: str = ""
) -> None:
    context = _context(path)
    if escalation_id == "all":
        pending = context.workspace.pending_escalations()
        for item in pending:
            context.workspace.resolve_escalation(item["id"], resolution, note)
        _context_signal(context).request(
            approvals=[i["id"] for i in pending] if resolution == "approved" else None,
            rejections=[i["id"] for i in pending] if resolution == "rejected" else None,
        )
        console.print(f"[green]{resolution}[/green] {len(pending)} escalation(s).")
        return
    item = context.workspace.resolve_escalation(escalation_id, resolution, note)
    if item is None:
        _fail(f"unknown escalation: {escalation_id}")
    _context_signal(context).request(
        approvals=[escalation_id] if resolution == "approved" else None,
        rejections=[escalation_id] if resolution == "rejected" else None,
    )
    console.print(f"[green]{resolution}[/green] {escalation_id}")


def _context_signal(context) -> Any:
    from ..runtime.control import ControlChannel

    return ControlChannel(context.workspace.paths.execution)


# ----------------------------------------------------------------------
# inspection
# ----------------------------------------------------------------------


def _operational_view(context, graph, run_doc) -> dict[str, str]:
    """The system's thought process as operations, not hidden reasoning (§17)."""
    from ..runtime.roadmap import build_roadmap

    active = graph.active_tasks()
    current_task = f"{active[0].id} {active[0].title} [{active[0].status.value}]" if active else ""
    milestone = ""
    for m in build_roadmap(graph)["milestones"]:
        if m["status"] == "in_progress":
            milestone = f"{m['id']} {m['name']}"
            break
    # Final-state truth: the newest task verification record, not raw events
    # (pre-adjudication failures are transient; the graph holds the outcome).
    last_verification = ""
    verified = [t for t in graph.all() if t.verification]
    if verified:
        latest = max(verified, key=lambda t: str(t.verification.get("created_at", "")))
        mark = "PASS" if latest.verification.get("passed") else "FAIL"
        last_verification = f"{mark} {latest.id} {latest.verification.get('summary', '')}"
    pending = context.workspace.pending_escalations()
    next_action = ""
    if pending:
        next_action = f"waiting for human: {pending[0]['id']} ({pending[0].get('kind', '')})"
    elif graph.failed_tasks():
        failed = graph.failed_tasks()[0]
        next_action = f"debugger diagnosing {failed.id} {failed.title}"
    elif graph.ready_tasks():
        ready = graph.ready_tasks()[0]
        next_action = f"implement {ready.id} {ready.title}"
    if not next_action:
        next_action = "idle" if graph.tasks else "not planned yet"
    return {
        "phase": milestone or ("(planning)" if not graph.tasks else "(no active milestone)"),
        "current task": current_task or "(none)",
        "agent": (
            active[0].assigned_agent if active and active[0].assigned_agent else "orchestrator"
        ),
        "last verification": last_verification or "(none yet)",
        "next action": next_action,
    }


@app.command()
def status(
    path: str = typer.Option("", "--path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show project state, progress, budget, and pending escalations."""
    context = _context(path)
    graph = context.workspace.load_graph()
    run_doc = context.workspace.load_run()
    payload = {
        "name": context.project.name,
        "objective": context.objective,
        "progress": graph.progress(),
        "run": run_doc,
        "escalations": context.workspace.pending_escalations(),
        "budget": context.config.budget.model_dump(),
        "operational": _operational_view(context, graph, run_doc),
    }
    if json_output:
        _print_json(payload)
        return

    progress = graph.progress()
    table = Table(title=f"{context.project.name}", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("objective", context.objective or "(none)")
    table.add_row("tasks", f"{progress['completed']}/{progress['total']} complete")
    table.add_row("active", str(progress["active"]))
    table.add_row("failed", str(progress["failed"]))
    table.add_row("pending", str(progress["pending"]))
    table.add_row("run", f"{run_doc.get('status', 'none')} (cycle {run_doc.get('cycle', 0)})")
    budget = run_doc.get("budget") or {}
    if budget:
        table.add_row(
            "cost", f"${budget.get('cost_usd', 0):.4f} of ${budget.get('max_token_budget', 0)}"
        )
        table.add_row(
            "elapsed",
            f"{budget.get('elapsed_seconds', 0)}s of {budget.get('max_runtime_seconds', 0)}s",
        )
    pending = context.workspace.pending_escalations()
    table.add_row("escalations", str(len(pending)))
    console.print(table)

    console.print("\n[bold]Operational view[/bold]")
    ops_table = Table(show_header=False, box=None)
    ops_table.add_column("key", style="bold cyan")
    ops_table.add_column("value")
    for key, value in _operational_view(context, graph, run_doc).items():
        ops_table.add_row(key, str(value)[:120])
    console.print(ops_table)

    if pending:
        console.print("\n[bold]Pending escalations[/bold]")
        for item in pending:
            console.print(
                f"  [yellow]{item['id']}[/yellow] {item.get('kind', '')}: {item.get('reason', '')[:120]}"
            )


@app.command()
def tasks(
    path: str = typer.Option("", "--path"),
    all_tasks: bool = typer.Option(False, "--all", help="Include terminal tasks."),
) -> None:
    """List the task graph."""
    context = _context(path)
    graph = context.workspace.load_graph()
    if not graph.tasks:
        console.print("[dim]no tasks yet; run `auto run` to plan[/dim]")
        return
    table = Table(title="task graph", show_header=True, header_style="bold")
    for column in ("id", "title", "status", "prio", "risk", "attempts", "deps"):
        table.add_column(column)
    for task in sorted(graph.all(), key=lambda t: (t.priority, t.created_at)):
        if not all_tasks and task.status in (TaskState.COMPLETED, TaskState.CANCELLED):
            continue
        table.add_row(
            task.id,
            task.title[:44],
            task.status.value,
            str(task.priority),
            task.risk,
            str(task.attempts),
            ",".join(task.dependencies) or "—",
        )
    console.print(table)


@app.command()
def inspect(
    task_id: str = typer.Argument(..., help="Task id, or 'graph', or 'run'."),
    path: str = typer.Option("", "--path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show a task's evidence, attempts, verification, or the whole graph."""
    context = _context(path)
    if task_id == "graph":
        _print_json(context.workspace.load_graph().model_dump(mode="json"))
        return
    if task_id == "run":
        _print_json(
            {
                "run": context.workspace.load_run(),
                "escalations": context.workspace.load_escalations(),
                "budget": (context.store.total_usage()).model_dump(),
            }
        )
        return
    graph = context.workspace.load_graph()
    try:
        task = graph.get(task_id)
    except KeyError:
        _fail(f"unknown task: {task_id}")
        return
    payload = {
        "task": task.model_dump(mode="json"),
        "verification": task.verification,
        "attempts": [a.model_dump(mode="json") for a in task.attempts_history],
        "history": [h.model_dump(mode="json") for h in task.history],
    }
    if json_output:
        _print_json(payload)
        return
    console.print(
        Panel(
            f"[bold]{task.title}[/bold]\n{json.dumps(task.verification, indent=2, default=str)}",
            title=f"{task.id} [{task.status.value}]",
            border_style="magenta",
        )
    )
    if task.attempts_history:
        console.print("\n[bold]Attempts[/bold]")
        for attempt in task.attempts_history:
            console.print(
                f"  {attempt.attempt_number}. [{attempt.outcome}] {attempt.failure_summary[:120] or '—'}"
            )


@app.command()
def events(
    path: str = typer.Option("", "--path"),
    limit: int = typer.Option(30, "--limit", "-n"),
    event_filter: str = typer.Option("", "--event", help="Substring filter on the event name."),
) -> None:
    """Tail the append-only event log."""
    context = _context(path)
    records = context.workspace.events.read_last(limit)
    if event_filter:
        records = [r for r in records if event_filter in str(r.get("event", ""))]
    for record in records:
        detail = record.get("detail") or record.get("reason") or record.get("summary") or ""
        console.print(
            f"[dim]{record.get('timestamp', '')}[/dim] [bold]{record.get('event', '')}[/bold] {str(detail)[:120]}"
        )


@app.command()
def checkpoints(
    path: str = typer.Option("", "--path"), json_output: bool = typer.Option(False, "--json")
) -> None:
    """List restorable checkpoints."""
    context = _context(path)
    records = context.store.list_checkpoints()
    if json_output:
        _print_json(records)
        return
    if not records:
        console.print("[dim]no checkpoints yet[/dim]")
        return
    table = Table(title="checkpoints", show_header=True, header_style="bold")
    for column in ("id", "created", "commit"):
        table.add_column(column)
    for record in records:
        table.add_row(record["id"], record["created_at"], (record["git_commit"] or "—")[:12])
    console.print(table)


@app.command()
def rollback(
    checkpoint_id: str = typer.Argument(..., help="Checkpoint id to restore."),
    path: str = typer.Option("", "--path"),
    git: bool = typer.Option(
        True, "--git/--no-git", help="Also reset the working tree to that commit."
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirm; this discards uncommitted work."),
) -> None:
    """Restore the task graph (and optionally the tree) to a checkpoint."""
    if not yes:
        _fail("rollback discards work; re-run with --yes to confirm")
    from ..git.manager import GitError, GitManager

    context = _context(path)
    orchestrator = Orchestrator(context, use_model_director=False)
    try:
        result = orchestrator.restore_checkpoint(checkpoint_id)
    except KeyError as exc:
        _fail(str(exc))
        return
    if git and result.get("git_commit"):
        try:
            GitManager(context.repo_root).reset_hard(result["git_commit"])
            result["git"] = f"reset to {result['git_commit'][:12]}"
        except GitError as exc:
            result["git"] = f"git reset failed: {exc}"
    console.print(
        Panel(json.dumps(result, indent=2, default=str), title="rolled back", border_style="yellow")
    )


@app.command(name="constitution")
def constitution(path: str = typer.Option("", "--path")) -> None:
    """Print the project constitution every agent is bound by."""
    context = _context(path)
    path_obj = context.workspace.paths.constitution
    if not path_obj.is_file():
        _fail("no constitution; run `auto init` or `auto run`")
    console.print(path_obj.read_text(encoding="utf-8"))


@app.command()
def benchmark(
    out: str = typer.Option("benchmark-results", "--out", help="Directory for results."),
    goals: int = typer.Option(5, "--goals", help="How many benchmark cases to run (1-5)."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run the offline evaluation harness across benchmark projects (§41)."""
    from ..benchmarks import BENCHMARK_CASES, run_benchmark

    if goals < 1:
        _fail("--goals must be at least 1")
    cases = BENCHMARK_CASES[:goals]
    console.print(f"[bold]benchmark[/bold] running {len(cases)} case(s) offline…")
    payload = run_benchmark(Path(out), cases=cases)
    if json_output:
        _print_json(payload)
        return
    from rich.markdown import Markdown

    console.print(Markdown((Path(out) / "report.md").read_text(encoding="utf-8")))
    console.print(f"\n[dim]full results: {Path(out) / 'results.json'}[/dim]")


@app.command()
def daemon(
    objective: str = typer.Argument("", help="Objective override (optional)."),
    path: str = typer.Option("", "--path"),
    poll_seconds: float = typer.Option(5.0, "--poll-seconds", help="Human-gate poll interval."),
    max_restarts: int = typer.Option(5, "--max-restarts", help="Bounded retries for dead ends."),
    max_cycles: int = typer.Option(200, "--max-cycles", help="Cycles per orchestrated run."),
) -> None:
    """Long-running mode: keep executing, resolving human gates as they open (§45)."""
    from ..runtime.daemon import DaemonLoop

    context = _context(path)
    console.print(
        Panel(
            f"[bold]daemon[/bold] {context.project.name}\n"
            f"objective: {context.objective or '(none)'}\n"
            f"waiting on: approvals, pauses; bounded restarts: {max_restarts}\n"
            f"[dim]stop with: auto stop — approve with: auto approve <id|all>[/dim]",
            border_style="cyan",
        )
    )

    def on_event(event: str, payload: dict[str, Any]) -> None:
        detail = payload.get("detail") or payload.get("reason") or payload.get("stop_reason") or ""
        console.print(
            f"[dim]{payload.get('timestamp', '')[-8:]}[/dim] [bold]{event}[/bold] {str(detail)[:100]}"
        )

    loop = DaemonLoop(
        context,
        poll_seconds=poll_seconds,
        max_restarts=max_restarts,
        max_cycles_per_run=max_cycles,
        on_event=on_event,
    )
    try:
        report = asyncio.run(loop.run())
    except KeyboardInterrupt as exc:
        console.print("[yellow]daemon interrupted; state was persisted[/yellow]")
        raise typer.Exit(130) from exc
    _print_json(report.as_dict())
    if report.status != "completed":
        raise typer.Exit(2)


@app.command()
def microagents(
    path: str = typer.Option("", "--path"),
    match: str = typer.Option("", "--match", help="Show which microagents fire for this text."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List repository microagents and when they are injected (OpenHands pattern)."""
    from ..runtime.microagents import (
        ensure_microagents_readme,
        load_microagents,
        matched_microagents,
    )

    context = _context(path)
    ensure_microagents_readme(context.workspace)
    agents = load_microagents(context.workspace)
    if json_output:
        _print_json(
            [
                {"name": a.name, "triggers": a.triggers, "path": a.path, "chars": len(a.content)}
                for a in agents
            ]
        )
        return
    if not agents:
        console.print("[dim]no microagents yet; add .agents/microagents/<topic>.md[/dim]")
        console.print("      [dim]see .agents/microagents/README.md for the format[/dim]")
        return
    matched_names = (
        {a.name for a in matched_microagents(context.workspace, match)} if match else None
    )
    table = Table(title="microagents", show_header=True, header_style="bold")
    for column in ("name", "triggers", "chars", "fires" if match else ""):
        table.add_column(column)
    for agent in agents:
        fires = ""
        if match:
            fires = "yes" if agent.name in matched_names else "no"
        table.add_row(
            agent.name,
            ", ".join(agent.triggers) or "(always active)",
            str(len(agent.content)),
            fires,
        )
    console.print(table)


@app.command()
def risk(
    command: str = typer.Argument(..., help="Command line to classify."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Classify a command's risk level (OpenHands security analyzer pattern)."""
    from ..runtime.risk import classify_command, should_require_confirmation

    assessment = classify_command(command)
    payload = {
        **assessment.as_dict(),
        "requires_confirmation_autonomous": should_require_confirmation(assessment.risk),
        "requires_confirmation_supervised": should_require_confirmation(
            assessment.risk, run_mode="supervised"
        ),
    }
    if json_output:
        _print_json(payload)
        return
    colour = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "green", "UNKNOWN": "dim"}[
        assessment.risk.value
    ]
    console.print(f"[{colour}]{assessment.risk.value}[/{colour}] {assessment.reason}")


@app.command()
def tools(
    agent_class: str = typer.Option(
        "coder", "--class", "-c", help="Permission class to inspect: coder, researcher, tester, ..."
    ),
    path: str = typer.Option("", "--path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List the sandboxed tools an agent class may call (the agent tool surface)."""
    from ..core.config import DEFAULT_PERMISSION_CLASSES
    from ..runtime.tools import BUILDER_TOOLS, MEMORY_TOOLS, NETWORK_TOOLS, READ_ONLY_TOOLS, TOOLS

    permissions = DEFAULT_PERMISSION_CLASSES.get(
        agent_class, DEFAULT_PERMISSION_CLASSES["coder"]
    )

    def _available(names: tuple[str, ...]) -> list[str]:
        allowed: list[str] = []
        for name in names:
            if name == "run_command" and not permissions.run_commands:
                continue
            if name in NETWORK_TOOLS and not permissions.network:
                continue
            if name in ("write_file", "edit_file") and not permissions.write_paths:
                continue
            allowed.append(name)
        return allowed

    builder_write = [t for t in BUILDER_TOOLS if t in ("write_file", "edit_file")]
    surface = {
        "agent_class": permissions.name,
        "read": list(READ_ONLY_TOOLS) if permissions.read_repo else [],
        "write": _available(builder_write),
        "commands": _available(["run_command"]),
        "memory": list(MEMORY_TOOLS),
        "environment": ["current_time", "budget_status"],
        "network": _available(list(NETWORK_TOOLS)),
        "total_registered": len(TOOLS),
    }
    if json_output:
        _print_json(surface)
        return
    console.print(f"[bold]{permissions.name}[/bold] tool surface")
    for group in ("read", "write", "commands", "memory", "environment", "network"):
        if surface[group]:
            console.print(f"  {group:12} {', '.join(surface[group])}")
        else:
            console.print(f"  {group:12} [dim](none)[/dim]")
    console.print(f"\n[dim]{surface['total_registered']} tools registered in the runtime[/dim]")


@app.command()
def hooks(
    path: str = typer.Option("", "--path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List configured event hooks and show recent hook runs (.agents/hooks.json)."""
    from ..runtime.hooks import default_hooks_template, load_hooks

    context = _context(path)
    configured = load_hooks(context.workspace)
    if json_output:
        _print_json(
            [{"event": h.event, "command": h.command, "timeout": h.timeout} for h in configured]
        )
        return
    if not configured:
        console.print("[dim]no hooks configured[/dim]")
        console.print("[dim]create .agents/hooks.json, e.g.:[/dim]")
        console.print(default_hooks_template())
        return
    table = Table(title="event hooks", show_header=True, header_style="bold")
    for column in ("event", "command", "timeout"):
        table.add_column(column)
    for hook in configured:
        table.add_row(hook.event, hook.command, f"{hook.timeout}s")
    console.print(table)
    log_path = context.workspace.paths.agent_logs / "hooks" / "hooks.log"
    if log_path.is_file():
        console.print("\n[bold]recent runs[/bold]")
        for line in log_path.read_text(encoding="utf-8").splitlines()[-5:]:
            console.print(f"  [dim]{line[:160]}[/dim]")


@app.command()
def remember(
    fact: str = typer.Argument(..., help="A durable fact to persist for every future agent."),
    path: str = typer.Option("", "--path"),
    kind: str = typer.Option("fact", "--kind", help="fact | preference | decision | lesson"),
    pinned: bool = typer.Option(False, "--pinned", help="Always load this into context."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags for recall."),
) -> None:
    """Save something into project memory (Gemini CLI's save-memory pattern)."""
    from ..runtime.memory import remember as remember_memory

    context = _context(path)
    item = remember_memory(
        context.workspace,
        fact,
        kind=kind,
        source="operator",
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        pinned=pinned,
    )
    console.print(f"[green]remembered[/green] ({item.kind}/{item.id}): {fact[:160]}")


@app.command()
def memory(
    path: str = typer.Option("", "--path"),
    query: str = typer.Option("", "--query", "-q", help="Show what recall returns for this text."),
    add: str = typer.Option("", "--add", help="Add a memory."),
    kind: str = typer.Option("fact", "--kind"),
    tags: str = typer.Option("", "--tags"),
    pinned: bool = typer.Option(False, "--pinned"),
    prune: bool = typer.Option(False, "--prune", help="Drop the weakest overflow memories."),
    consolidate: bool = typer.Option(
        False, "--consolidate", help="Roll failure lessons into typed memories."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect and curate the project's typed memory (facts, decisions, lessons)."""
    from ..runtime.memory import memory_store

    context = _context(path)
    store = memory_store(context.workspace)
    messages: list[str] = []
    if add:
        item = store.add(
            kind,
            add,
            source="operator",
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            pinned=pinned,
        )
        messages.append(f"added {item.id} ({item.kind})")
    if consolidate:
        messages.append(f"consolidated: {store.consolidate()}")
    if prune:
        messages.append(f"pruned {store.prune()} item(s)")

    if json_output:
        payload = {
            "messages": messages,
            "index": str(store.index_path),
            "items": [i.as_dict() for i in (store.recall(query) if query else store.all())],
        }
        if not query:
            payload["episodes"] = store.recent_episodes(5)
        _print_json(payload)
        return

    for message in messages:
        console.print(f"[green]{message}[/green]")

    if query:
        recalled = store.recall(query)
        if not recalled:
            console.print(f"[dim]nothing recalled for: {query}[/dim]")
            return
        table = Table(title=f"recall: {query}", show_header=True, header_style="bold")
        for column in ("kind", "confidence", "text", "source"):
            table.add_column(column)
        for item in recalled:
            table.add_row(item.kind, f"{item.confidence:.2f}", item.text[:70], item.source)
        console.print(table)
        return

    items = store.all()
    if not items:
        console.print('[dim]memory is empty; add with `auto memory --add "..."`[/dim]')
    else:
        counts: dict[str, int] = {}
        for item in items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        table = Table(title="project memory", show_header=True, header_style="bold")
        for column in ("kind", "count", "pinned"):
            table.add_column(column)
        for item_kind in ("preference", "decision", "fact", "lesson", "incident"):
            if counts.get(item_kind):
                pinned_count = sum(1 for i in items if i.kind == item_kind and i.pinned)
                table.add_row(item_kind, str(counts[item_kind]), str(pinned_count))
        table.add_row("[bold]total[/bold]", f"[bold]{len(items)}[/bold]", "")
        console.print(table)
    episodes = store.recent_episodes(5)
    if episodes:
        console.print("\n[bold]recent episodes[/bold]")
        for episode in episodes:
            if episode.get("kind") == "run":
                console.print(
                    f"  [dim]{episode.get('at', '')}[/dim] run: {episode.get('stop_reason', '')} "
                    f"({episode.get('completed', 0)} done / {episode.get('failed', 0)} failed)"
                )
            else:
                console.print(
                    f"  [dim]{episode.get('at', '')}[/dim] {episode.get('outcome', '')}: "
                    f"{episode.get('task_id', '')} {str(episode.get('title', ''))[:50]}"
                )
    console.print(f"\n[dim]index: {store.index_path}[/dim]")


@app.command()
def instructions(
    path: str = typer.Option("", "--path"),
    edit_hint: bool = typer.Option(False, "--paths", help="Only show the file paths."),
) -> None:
    """Show the agent instructions memory (project + global, CLAUDE.md style)."""
    from ..runtime.instructions import (
        ensure_project_instructions,
        global_instructions_path,
        load_instructions,
        project_instructions_path,
    )

    context = _context(path)
    ensure_project_instructions(context.workspace)
    global_path = global_instructions_path()
    project_path = project_instructions_path(context.workspace)
    if edit_hint:
        console.print(f"global:  {global_path}")
        console.print(f"project: {project_path}")
        return
    scopes = load_instructions(context.workspace)
    for title, path_obj, body in (
        ("user-global", global_path, scopes["global"]),
        ("project", project_path, scopes["project"]),
    ):
        console.print(f"[bold]{title}[/bold] [dim]({path_obj})[/dim]")
        console.print(body or "[dim](empty)[/dim]")
        console.print()


@app.command()
def metrics(
    path: str = typer.Option("", "--path"),
    json_output: bool = typer.Option(False, "--json"),
    save: bool = typer.Option(False, "--save", help="Also write .agents/metrics.json"),
) -> None:
    """Project metrics computed from persisted evidence (plan.md §40)."""
    from ..runtime.metrics import collect_metrics, render_metrics_markdown

    context = _context(path)
    data = collect_metrics(context.workspace, context.store)
    if save:
        context.workspace.write_json_artifact("metrics.json", data)
        from ..core.workspace import atomic_write

        atomic_write(context.workspace.paths.state / "metrics.md", render_metrics_markdown(data))
    if json_output:
        _print_json(data)
        return
    table = Table(title="project metrics", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key, value in data.items():
        if key == "cost_usd":
            value = f"${value}"
        table.add_row(key, str(value))
    console.print(table)


@app.command(name="roadmap")
def roadmap(
    path: str = typer.Option("", "--path"), json_output: bool = typer.Option(False, "--json")
) -> None:
    """Show the milestone roadmap derived from the task graph (plan.md §31)."""
    from ..runtime.roadmap import build_roadmap

    context = _context(path)
    data = build_roadmap(context.workspace.load_graph())
    milestones = data.get("milestones", [])
    if json_output:
        _print_json(data)
        return
    if not milestones:
        console.print("[dim]no tasks yet; run `auto run` to plan[/dim]")
        return
    style = {"done": "green", "in_progress": "yellow", "open": "dim"}
    table = Table(title="milestone roadmap", show_header=True, header_style="bold")
    for column in ("id", "milestone", "status", "tasks"):
        table.add_column(column)
    for milestone in milestones:
        table.add_row(
            milestone["id"],
            milestone["name"][:44],
            f"[{style.get(milestone['status'], 'white')}]{milestone['status']}[/{style.get(milestone['status'], 'white')}]",
            f"{milestone['tasks_completed']}/{milestone['tasks_total']}",
        )
    console.print(table)


@app.command()
def config(
    path: str = typer.Option("", "--path"),
    set_key: str = typer.Option("", "--set", help="budget.max_token_budget=500"),
    json_output: bool = typer.Option(True, "--json/--table"),
) -> None:
    """Show or update configuration (`--set budget.max_token_budget=500`)."""
    context = _context(path)
    config_obj: ProjectConfig = context.config
    if set_key:
        key, _, value = set_key.partition("=")
        _apply_config_set(config_obj, key.strip(), value.strip())
        context.workspace.save_config(config_obj)
        if not json_output:
            console.print(f"[green]set[/green] {key} = {value}")
    if json_output:
        _print_json(config_obj.model_dump(mode="json"))
    else:
        for key, value in sorted(config_obj.model_dump(mode="json").items()):
            console.print(f"{key}: {value}")


def _set_run_mode(config_obj: ProjectConfig, value: str) -> None:
    if value not in ("autonomous", "supervised"):
        _fail("run_mode must be 'autonomous' or 'supervised'")
    config_obj.run_mode = value  # type: ignore[assignment]


def _apply_config_set(config_obj: ProjectConfig, key: str, value: str) -> None:
    """Apply one dotted configuration key. Unknown keys are rejected."""
    sections: dict[str, Any] = {
        "budget.max_token_budget": lambda: setattr(
            config_obj.budget, "max_token_budget", float(value)
        ),
        "budget.max_runtime_seconds": lambda: setattr(
            config_obj.budget, "max_runtime_seconds", int(float(value))
        ),
        "budget.max_parallel_agents": lambda: setattr(
            config_obj.budget, "max_parallel_agents", int(value)
        ),
        "budget.max_task_attempts": lambda: setattr(
            config_obj.budget, "max_task_attempts", int(value)
        ),
        "run_mode": lambda: _set_run_mode(config_obj, value),
        "enhance_prompt": lambda: setattr(
            config_obj, "enhance_prompt", value.lower() in {"1", "true", "yes"}
        ),
        "git_checkpoints": lambda: setattr(
            config_obj, "git_checkpoints", value.lower() in {"1", "true", "yes"}
        ),
        "worktree_parallelism": lambda: setattr(
            config_obj, "worktree_parallelism", value.lower() in {"1", "true", "yes"}
        ),
    }
    if key not in sections:
        _fail(f"unknown configuration key: {key}")
    try:
        sections[key]()
    except ValueError as exc:
        _fail(f"invalid value for {key}: {exc}")


@app.command()
def ui(
    path: str = typer.Option("", "--path"),
    objective: str = typer.Option("", "--objective", "-o", help="Submit an objective on launch."),
    observe: bool = typer.Option(
        False, "--observe", help="Attach to a runtime owned by another process (read-only control)."
    ),
) -> None:
    """Launch the interactive terminal UI over the runtime."""
    from ..ui.app import launch

    root = Path(path) if path else Path.cwd()
    if not (root / ".agents").is_dir():
        # Walk up like the rest of the CLI does.
        from ..core.workspace import find_workspace

        found = find_workspace(root)
        if found is None:
            _fail("no autonomous-engine project found; run `auto init` first")
            return
        root = found.root
    launch(root, attached=not observe, objective=objective)


@app.command()
def reset(
    path: str = typer.Option("", "--path"),
    hard: bool = typer.Option(False, "--hard", help="Also delete the SQLite state and event log."),
    yes: bool = typer.Option(False, "--yes", help="Confirm a hard reset."),
) -> None:
    """Reset the run to a clean, planned-from-scratch state."""
    context = _context(path)
    if hard:
        if not yes:
            _fail("a hard reset deletes all recorded history; re-run with --yes")
        context.workspace.events.path.unlink(missing_ok=True)
        context.db.close()
        context.workspace.paths.database.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            context.workspace.paths.database.with_name(
                context.workspace.paths.database.name + suffix
            ).unlink(missing_ok=True)
        console.print(
            "[yellow]hard reset:[/yellow] history deleted; re-run `auto init --force` to rebuild the schema."
        )
        return
    context.workspace.save_graph(TaskGraph())
    context.workspace.save_run({})
    context.store.start_run(RunRecord(id=new_id("RUN"), project_id=context.store.project_id))
    console.print("[green]reset:[/green] task graph cleared; the next `auto run` will plan again.")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
