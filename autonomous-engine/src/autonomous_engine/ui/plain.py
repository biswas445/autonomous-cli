"""Plain mode (spec §55, §56, §82): line-oriented output for CI, pipes, and
TTY-less environments. Same facade, same controls — no full-screen UI.

    [14:02:18] task.created TASK-042
    [14:02:20] agent.started coder-02

If an objective is supplied, the runtime runs attached (events print live);
otherwise it prints the current state and recent events, then exits.
"""

from __future__ import annotations

import time
from pathlib import Path

from .facade import RuntimeFacade

_STYLE = {
    "task.completed": "✓",
    "verification.passed": "✓",
    "git.commit": "✓",
    "task.failed": "✗",
    "verification.failed": "✗",
    "agent.crashed": "✗",
    "escalation.raised": "!",
    "budget.exceeded": "!",
}


def _line(event: dict) -> str:
    ts = str(event.get("timestamp", "")).split("T")[-1][:8]
    mark = _STYLE.get(str(event.get("event", "")), "·")
    task = f" {event['task_id']}" if event.get("task_id") else ""
    summary = (
        event.get("summary")
        or event.get("detail")
        or event.get("reason")
        or event.get("to_state")
        or ""
    )
    text = f"{mark} {event.get('event', '')}{task}"
    if summary:
        text += f" — {str(summary)[:120]}"
    return f"[{ts}] {text}"


def _print_state(facade: RuntimeFacade) -> None:
    progress = facade.progress()
    run = facade.current_run()
    print(f"project: {facade.project().get('name', '')}")
    print(f"objective: {facade.objective() or '(none)'}")
    print(
        f"tasks: {progress['completed']}/{progress['total']} complete, "
        f"{progress['active']} active, {progress['failed']} failed"
    )
    print(f"run: {run.get('status', 'idle')}" + (f" ({run.get('stop_reason')})" if run.get("stop_reason") else ""))
    pending = facade.escalations()
    for item in pending:
        print(f"! approval required: {item.get('id')} — {str(item.get('reason', ''))[:100]}")


def run_plain(root: Path, *, objective: str = "", follow: bool | None = None) -> int:
    facade = RuntimeFacade(root)
    try:
        if objective:
            _run_attached(facade, objective)
            return 0
        _print_state(facade)
        for event in facade.recent_events(30):
            print(_line(event))
        if follow is None:
            import sys

            follow = bool(sys.stdin.isatty())
        if follow:
            _follow(facade)
        return 0
    finally:
        facade.close()


def _run_attached(facade: RuntimeFacade, objective: str) -> None:
    """Run the loop in-process, printing every event as a plain line."""
    import asyncio

    from ..runtime.orchestrator import Orchestrator

    async def _go() -> None:
        orchestrator = Orchestrator(facade.ctx, use_model_director=True, on_event=lambda e, p: print(_line({"event": e, **p})))
        result = await orchestrator.run_loop(objective)
        print(f"run finished: {result.status} ({result.summary})")

    asyncio.run(_go())


def _follow(facade: RuntimeFacade, interval: float = 1.0) -> None:
    """Tail the event log until Ctrl+C (cheap incremental reads)."""
    print("[plain mode] following events; Ctrl+C to exit", flush=True)
    try:
        while True:
            for event in facade.events_since():
                print(_line(event), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[plain mode] detached; runtime state persists")
