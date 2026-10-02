"""UI runner (directive #2, #12): the TUI is a *pure client* of the runtime.

Three connection modes:

    * ipc      — a live runtime daemon owns execution; the UI sends ops
                 (run.start/pause/stop/approve…) and receives pushed events
                 (including model.stream) over the named pipe/TCP transport.
    * observe  — no daemon: the UI renders persisted state/event log only
                 (spec §47), never owns state.
    * process  — explicit `--attached` fallback for `auto ui -o objective`:
                 the old in-process loop, retained so a single command can
                 still bootstrap a project without a daemon.

The UI never mutates runtime state directly: every control goes over IPC or
through the existing control-channel file, and the daemon remains the single
source of truth. A dropped connection triggers reconnect with backoff; events
missed while disconnected are repaired with events.replay (catch-up).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .facade import RuntimeFacade
from .state import UIState


class UIRunner:
    """The TUI's connection to the runtime: IPC client, never the owner."""

    def __init__(
        self,
        facade: RuntimeFacade,
        state: UIState,
        *,
        attached: bool = True,
        allow_process_fallback: bool = False,
    ):
        self.facade = facade
        self.state = state
        self.attached = attached
        self.allow_process_fallback = allow_process_fallback
        self.mode = "observe"  # ipc | observe | process
        self._client: Any = None  # RuntimeClient
        self._last_event_ts = ""
        self._reconnect_thread: threading.Thread | None = None
        self._stop_reconnect = threading.Event()
        self._process_task: Any = None  # asyncio task for the fallback mode
        self._loop: Any = None
        self._finished_report: Any = None
        self._crashed = ""

    # ---- mode detection / attach ----------------------------------

    def ensure_connection(self) -> bool:
        """Attach to the daemon if one is live; else observe (or fallback)."""
        if self.mode == "ipc" and self._client is not None and self._client.connected:
            return True
        if not self.attached:
            self.mode = "observe"
            return False
        from ..runtime.runtime_server import RuntimeClient

        client = RuntimeClient(self.facade.root, on_event=self._on_push)
        if client.connect(retries=1):
            self._client = client
            self.mode = "ipc"
            self._replay_since_last()
            return True
        self.mode = "process" if self.allow_process_fallback else "observe"
        return False

    def _replay_since_last(self) -> None:
        """Catch-up: replay events the client missed while disconnected."""
        if self._client is None:
            return
        try:
            events = (
                self._client.events_since(self._last_event_ts)
                if self._last_event_ts
                else self._client.replay_events(limit=100)
            )
        except Exception:
            return
        self._ingest_events(events)

    # ---- event intake (both push and replay go through here) ------

    def _on_push(self, message: dict[str, Any]) -> None:
        if "event" in message or "hb" in message:
            self._ingest_events([message])
        elif message.get("op") == "hello":
            return

    def _ingest_events(self, events: list[dict[str, Any]]) -> None:
        for record in events:
            name = str(record.get("event", ""))
            if not name:
                continue  # heartbeats etc. carry no event name
            timestamp = str(record.get("timestamp", ""))
            if timestamp:
                self._last_event_ts = max(self._last_event_ts, timestamp)
        self.state.ingest([e for e in events if e.get("event")])

    # ---- lifecycle from the TUI's perspective ---------------------

    def start(self, objective: str = "") -> None:
        """Start autonomous execution — via IPC when a daemon owns the run."""
        if self.ensure_connection() and self._client is not None:
            try:
                reply = self._client.start_run(objective)
                if reply.get("started"):
                    self.state.ingest(
                        [
                            {
                                "event": "ui.run_started_ipc",
                                "timestamp": "",
                                "actor": "ui",
                                "detail": f"objective accepted by the runtime daemon: {objective[:80]}",
                            }
                        ]
                    )
                    return
                self._crashed = str(reply.get("reason", "daemon refused to start a run"))
                return
            except Exception as exc:
                self._crashed = f"IPC failure: {exc}"
                self.mode = "observe"
                return
        if self.allow_process_fallback:
            self._start_process_loop(objective)
        else:
            self._crashed = "no runtime daemon; start one with `auto runtime start`"

    def _start_process_loop(self, objective: str) -> None:
        """Explicit-fallback mode: run the daemon loop inside the UI process."""
        import asyncio

        self.mode = "process"
        self._loop = asyncio.get_running_loop()
        self._process_task = self._loop.create_task(self._run_process_loop(objective))

    async def _run_process_loop(self, objective: str) -> None:
        from ..runtime.daemon import DaemonLoop
        from ..runtime.orchestrator import NoObjective

        def on_event(event: str, payload: dict[str, Any]) -> None:
            record = {"event": event, "timestamp": payload.get("timestamp", ""), **payload}
            self._ingest_events([record])

        try:
            if objective.strip():
                from ..runtime.orchestrator import Orchestrator

                orchestrator = Orchestrator(
                    self.facade.ctx, use_model_director=True, on_event=on_event
                )
                await orchestrator.run_loop(objective)
            daemon = DaemonLoop(self.facade.ctx, poll_seconds=5.0, on_event=on_event)
            self._finished_report = await daemon.run()
        except NoObjective:
            self._crashed = "no objective recorded; submit one in the input bar"
        except Exception as exc:  # the UI must survive any runtime failure
            self._crashed = f"{type(exc).__name__}: {exc}"

    # ---- controls (IPC first, control file as fallback) -----------

    def pause(self) -> None:
        if self._ipc_call("pause"):
            return
        self.facade.pause()

    def resume(self) -> None:
        if self._ipc_call("resume"):
            return
        self.facade.resume()

    def cancel(self, reason: str = "cancelled from the UI") -> None:
        if self._ipc_call("stop", reason=reason):
            return
        self.facade.cancel(reason)

    def approve(self, escalation_id: str) -> bool:
        if self._ipc_call("approve", escalation_id=escalation_id):
            return True
        return self.facade.approve(escalation_id)

    def reject(self, escalation_id: str) -> bool:
        if self._ipc_call("reject", escalation_id=escalation_id):
            return True
        return self.facade.reject(escalation_id)

    def _ipc_call(self, op: str, **args: Any) -> bool:
        if self._client is None or not self._client.connected:
            return False
        try:
            getattr(self._client, op)(**args)
            return True
        except Exception:
            return False

    # ---- reconnect (background, non-blocking for the UI) ----------

    def schedule_reconnect(self) -> None:
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="ui-reconnect"
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        backoff = 1.0
        while not self._stop_reconnect.is_set() and not self.connected:
            if self.ensure_connection():
                self.state.ingest(
                    [
                        {
                            "event": "ui.reconnected",
                            "timestamp": "",
                            "actor": "ui",
                            "detail": f"runtime daemon reconnected over {self._client.transport}",
                        }
                    ]
                )
                return
            time.sleep(min(backoff, 10.0))
            backoff *= 2

    # ---- status for the TUI ---------------------------------------

    @property
    def connected(self) -> bool:
        return self.mode == "ipc" and self._client is not None and self._client.connected

    @property
    def running(self) -> bool:
        if self.mode == "process":
            return self._process_task is not None and not self._process_task.done()
        if self.mode == "ipc":
            try:
                status = self._client.status() if self._client else {}
                return bool(status.get("daemon_running"))
            except Exception:
                return False
        return False

    @property
    def crashed(self) -> str:
        return self._crashed

    def report(self):
        return self._finished_report

    async def stop_and_wait(self) -> None:
        """Graceful shutdown of whatever we own (process mode only)."""
        if self.mode == "process" and self._process_task is not None:
            self.facade.cancel("stopped from the UI")
            try:
                import asyncio

                await asyncio.wait_for(self._process_task, timeout=30.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._process_task.cancel()
        elif self.mode == "ipc" and self._client is not None:
            with_no_raise = self._ipc_call("stop", reason="UI closing")
            if not with_no_raise:
                self.facade.cancel("stopped from the UI")

    def close(self) -> None:
        import contextlib

        self._stop_reconnect.set()
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None
