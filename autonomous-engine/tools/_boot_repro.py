"""Temporary live-debug repro: run the real bootstrap with real models.

Prints per-stage progress so a hang or crash can be localized to one stage.
Never prints secret values.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

from autonomous_engine.models.ratelimit import reset_limiters
from autonomous_engine.models.router import ensure_env_providers
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.orchestrator import Orchestrator

ROOT = Path(r"D:\autonomous twin coder\autonomous-engine\live-test\run1")


def on_event(event: str, payload: dict) -> None:
    print(f"  event: {event} {str(payload.get('detail') or '')[:60]}", flush=True)


async def main() -> None:
    reset_limiters()
    ensure_env_providers(Path(r"D:\autonomous twin coder"))
    context = open_context(ROOT)
    context.config = context.workspace.load_config()
    orchestrator = Orchestrator(context, max_cycles=200, use_model_director=True, on_event=on_event)
    print("[repro] starting bootstrap", flush=True)
    try:
        await asyncio.wait_for(
            orchestrator._bootstrap("Build a small notes REST API"), timeout=240
        )
        print("[repro] bootstrap OK", flush=True)
    except asyncio.TimeoutError:
        print("[repro] BOOTSTRAP TIMED OUT after 240s", flush=True)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                print(f"  pending task: {task.get_name()} {not task.done()}", flush=True)
    except Exception:
        traceback.print_exc(limit=10)
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
