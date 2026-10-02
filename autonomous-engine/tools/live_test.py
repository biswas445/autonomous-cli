"""Live test harness (temporary): real models, monitored 10-minute run.

Sets up a fresh project under live-test/ with real model routes from .env
(kios 5 RPM, atria 30 RPM — the process-wide rate limiter enforces both),
runs the daemon for a bounded period, and prints the monitored data flow.
Never prints secret values.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from autonomous_engine.models.router import ModelRouter, ensure_env_providers
from autonomous_engine.models.ratelimit import limiter_for, reset_limiters
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.bootstrap import init_project
from autonomous_engine.runtime.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parent.parent / "live-test" / "run1"
BUDGET_USD = 5.0
MAX_SECONDS = 600.0  # 10 minutes
MONITOR_INTERVAL = 15.0


def setup_project() -> None:
    if ROOT.exists():
        return
    init_project(ROOT, project_name="live-run1", objective="Build a small notes REST API")
    config_path = ROOT / ".agents" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    # Real routes: atria for reasoning-heavy roles, kios for the coding/test
    # roles, with cross-provider fallbacks.
    atria = "atria/Atria-Dawn-Preview"
    kios = "kios/muse-spark-1.3-contributor"
    kios2 = "kios/step-3.7-flash"
    routes = {
        "intent_compiler": (kios, [atria]),
        "product": (kios, [atria]),
        "researcher": (kios, [atria]),
        "architect": (atria, [kios]),
        "planner": (kios, [atria]),
        "coder": (kios, [atria]),
        "tester": (kios2, [kios]),
        "reviewer": (atria, [kios]),
        "security": (atria, [kios]),
        "debugger": (atria, [kios]),
        "director": (kios, [atria]),
        "release": (kios2, [kios]),
    }
    config["model_routes"] = [
        {"role": role, "provider": primary.split("/")[0], "model": primary.split("/")[1],
         "fallbacks": fallbacks}
        for role, (primary, fallbacks) in routes.items()
    ]
    config["budget"]["max_token_budget"] = BUDGET_USD
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


async def main() -> int:
    setup_project()
    reset_limiters()
    ensure_env_providers(ROOT)
    print(f"[live] project: {ROOT}")
    print(
        f"[live] rate limits: kios {limiter_for('kios').rpm} rpm, "
        f"atria {limiter_for('atria').rpm} rpm"
    )
    context = open_context(ROOT)
    context.config = context.workspace.load_config()
    events: list[str] = []
    last_heartbeat = time.monotonic()

    def on_event(event: str, payload: dict) -> None:
        nonlocal last_heartbeat
        events.append(event)
        detail = str(
            payload.get("detail") or payload.get("reason") or payload.get("summary") or ""
        )[:90]
        print(f"  {payload.get('timestamp', '')[11:19]} {event:32} {detail}")

    orchestrator = Orchestrator(context, max_cycles=200, use_model_director=True, on_event=on_event)
    started = time.monotonic()
    try:
        run_task = asyncio.create_task(orchestrator.run_loop())
        while not run_task.done():
            await asyncio.sleep(MONITOR_INTERVAL)
            elapsed = time.monotonic() - started
            if elapsed > MAX_SECONDS:
                print(f"[live] {MAX_SECONDS}s elapsed — requesting graceful stop")
                orchestrator.control.request(stop=True, reason="live test time limit")
                break
            progress = orchestrator.graph.progress()
            budget = orchestrator.budget.snapshot()
            print(
                f"[monitor] {elapsed:.0f}s | cycles {orchestrator._cycle_index} | "
                f"tasks {progress.get('completed', 0)}/{progress.get('total', 0)} | "
                f"active {progress.get('active', 0)} | cost ${budget.get('cost_usd', 0):.4f} | "
                f"locks {len(orchestrator.locks.snapshot())}"
            )
        result = await asyncio.wait_for(run_task, timeout=120)
        print(f"[live] run finished: status={result.status} stop={result.stop.reason.value if result.stop else '?'}")
        return 0
    except Exception as exc:
        print(f"[live] RUN FAILED: {exc!r}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
