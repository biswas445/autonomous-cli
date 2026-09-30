"""UI runner: execute the runtime from inside the TUI without blocking it.

The orchestrator runs as a asyncio task in the same process; every event it
emits is fed straight into the UI state store (true push, spec §40). The UI
never awaits a cycle — it renders while the loop works.

Two connection modes (spec §46/§47):
    * attached — this process drives the run (DaemonLoop with restarts);
    * observe  — another process owns the run; the UI only reads its
      persisted state/event log and reconnects to whatever it finds.

The ControlChannel file is the control plane in both modes, so pause /
resume / cancel / approvals behave identically attached or detached.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .facade import RuntimeFacade
from .state import UIState


class UIRunner:
    """Owns the background runtime task for the attached mode."""

    def __init__(self, facade: RuntimeFacade, state: UIState, *, attached: bool = True):
        self.facade = facade
        self.state = state
        self.attached = attached
        self._task: asyncio.Task | None = None
        self._loop: Any = None
        self._finished_report: Any = None
        self._crashed: str = ""

    # ---- lifecycle ----

    def start(self, objective: str = "") -> None:
        """Launch the daemon loop in the background (attached mode only)."""
        if not self.attached or self.running:
            return
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._run_loop(objective))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def crashed(self) -> str:
        return self._crashed

    def report(self):
        return self._finished_report

    async def stop_and_wait(self) -> None:
        """Ask the runtime to stop, then wait for a clean background exit."""
        if self.running:
            self.facade.cancel("stopped from the UI")
            try:
                await asyncio.wait_for(self._task, timeout=30.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()

    async def _run_loop(self, objective: str) -> None:
        from ..runtime.daemon import DaemonLoop
        from ..runtime.orchestrator import NoObjective

        def on_event(event: str, payload: dict[str, Any]) -> None:
            # Push path (spec §40): runtime events reach the UI directly.
            record = {"event": event, "timestamp": payload.get("timestamp", ""), **payload}
            self.state.ingest([record])

        try:
            if objective.strip():
                from ..runtime.orchestrator import Orchestrator

                # A freshly submitted objective bootstraps through one run
                # first (intent -> plan -> execute), then the daemon takes
                # over for gates/restarts.
                orchestrator = Orchestrator(
                    self.facade.ctx, use_model_director=True, on_event=on_event
                )
                await orchestrator.run_loop(objective)
            daemon = DaemonLoop(
                self.facade.ctx,
                poll_seconds=5.0,
                on_event=on_event,
            )
            self._finished_report = await daemon.run()
        except NoObjective:
            self._crashed = "no objective recorded; submit one in the input bar"
        except Exception as exc:  # the UI must survive any runtime failure
            self._crashed = f"{type(exc).__name__}: {exc}"
