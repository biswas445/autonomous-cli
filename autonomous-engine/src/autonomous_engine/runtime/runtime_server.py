"""The persistent runtime server (directive #2): one owned daemon process.

`RuntimeServer` owns the Orchestrator/DaemonLoop in *its* process and serves
local IPC clients (TUI, CLI) over a named pipe (Windows) or TCP loopback.
The TUI becomes a pure client: it never owns runtime state or execution.

Protocol (JSON per line):
    request  {"id": "...", "op": "...", **args}
    response {"id": "...", "ok": true, **result} | {"id": "...", "error": "..."}

Ops: hello, status, events.since, events.replay, run.start, run.stop,
     run.pause, run.resume, approve, reject, remember, config.get, ping.

Server-initiated messages: {"event": <event-log record>} pushed to every
connected client as events happen, plus {"hb": timestamp} heartbeats.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.events import EventLog
from ..core.workspace import Workspace
from . import ipc
from .ipc import IPCClient, RuntimeEndpoint

HEARTBEAT_SECONDS = 2.0
STALE_AFTER_SECONDS = 15.0


def slug_for(root: Path) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9]+", "-", str(root).lower()).strip("-")[-48:]


@dataclass
class ServerStats:
    started_at: float = field(default_factory=time.time)
    clients_served: int = 0
    requests: int = 0


class RuntimeServer:
    """Owns runtime execution and exposes it to local clients."""

    def __init__(self, root: Path, *, prefer_transport: str = "auto"):
        self.root = Path(root).resolve()
        self.workspace = Workspace(self.root)
        if not self.workspace.exists():
            raise FileNotFoundError(f"no autonomous-engine project at {self.root}")
        self.events: EventLog = self.workspace.events
        self.slug = slug_for(self.root)
        self.server, self.transport_name = ipc.create_server(self.slug, prefer_transport)
        self.stats = ServerStats()
        self._clients_lock = threading.Lock()
        self._clients: dict[str, ipc._Connection] = {}
        self._run_task: Any = None  # asyncio task running the daemon loop
        self._loop: Any = None  # asyncio loop the daemon runs on
        self._daemon = None  # DaemonLoop while a run is active
        self._stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._context = None  # RuntimeContext, opened lazily
        self._state_lock = threading.Lock()

    # ---- lifecycle ----

    def serve_forever(self) -> None:
        """Block serving clients until `stop()` (runs its own asyncio loop)."""
        import asyncio

        self.server.start(self._on_message)  # type: ignore[arg-type]
        self._write_endpoint()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()
        self.events.append("runtime.server_started", transport=self.transport_name)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_forever()
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        with contextlib.suppress(Exception):
            if self._run_task is not None and self._loop is not None:
                fut = asyncio_run_coroutine(self._loop, self._stop_run())
                fut.result(timeout=30)
        with contextlib.suppress(Exception):
            if self._context is not None:
                self._context.db.close()
        self.server.stop()
        ipc.clear_endpoint(self.root)
        self.events.append("runtime.server_stopped")

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._stop.set()

    def _write_endpoint(self) -> None:
        from ..core.task import now_iso

        address = (
            self.server.pipe_path
            if self.transport_name == "named_pipe"
            else self.server.address
        )
        ipc.write_endpoint(
            self.root,
            {
                "transport": self.transport_name,
                "address": address,
                "pid": __import__("os").getpid(),
                "started_at": now_iso(),
                "heartbeat_at": now_iso(),
                "protocol": ipc.PROTOCOL_VERSION,
            },
        )

    def _heartbeat_loop(self) -> None:
        from ..core.task import now_iso

        while not self._stop.is_set():
            if self._stop.wait(HEARTBEAT_SECONDS):
                return
            doc = ipc.read_endpoint(self.root)
            if doc is not None:
                doc["heartbeat_at"] = now_iso()
                doc["clients"] = len(self._clients)
                ipc.write_endpoint(self.root, doc)
            self.broadcast({"hb": now_iso()})

    # ---- client handling ----

    def _on_message(self, message: dict[str, Any], conn: ipc._Connection) -> None:
        self.stats.requests += 1
        op = str(message.get("op", ""))
        request_id = str(message.get("id", ""))
        with self._clients_lock:
            if conn.id not in self._clients:
                self._clients[conn.id] = conn
                self.stats.clients_served += 1
        try:
            result = self._handle(op, message)
        except Exception as exc:  # noqa: BLE001 - errors go to the caller
            response = {"id": request_id, "error": f"{type(exc).__name__}: {exc}"[:500]}
        else:
            response = {"id": request_id, "ok": True, **(result or {})}
        with contextlib.suppress(Exception):
            conn.send_line(ipc.encode_message(response))

    def _handle(self, op: str, message: dict[str, Any]) -> dict[str, Any]:
        if op in ("", "hello"):
            return {
                "protocol": ipc.PROTOCOL_VERSION,
                "pid": __import__("os").getpid(),
                "transport": self.transport_name,
            }
        if op == "ping":
            return {"pong": True}
        if op == "status":
            return self._status()
        if op == "events.since":
            return {"events": self._events_since(str(message.get("since", "")))}
        if op == "events.replay":
            return {"events": self.events.read_last(int(message.get("limit", 200)))}
        if op == "run.start":
            return self._start_run(str(message.get("objective", "")))
        if op == "run.stop":
            self._request_stop(str(message.get("reason", "stop requested via IPC")))
            return {"stopping": True}
        if op == "run.pause":
            self.workspace.pause_requested = True  # pragma: no cover - not used
            return {}
        if op in ("pause", "resume", "stop"):
            return self._control(op, message)
        if op == "approve":
            return self._approve(str(message.get("escalation_id", "all")), True)
        if op == "reject":
            return self._approve(str(message.get("escalation_id", "all")), False)
        if op == "remember":
            from .memory import remember

            text = str(message.get("text", "")).strip()
            if not text:
                raise ValueError("empty memory text")
            remember(self.workspace, text, kind="fact", source="ipc-client")
            return {"remembered": True}
        if op == "config.get":
            return {"config": self._load_context().config.model_dump(mode="json")}
        raise ValueError(f"unknown op: {op}")

    # ---- operations ----

    def _load_context(self):
        if self._context is None:
            from .context_setup import open_context

            self._context = open_context(self.root)
        return self._context

    def _status(self) -> dict[str, Any]:
        graph = self.workspace.load_graph()
        run = self.workspace.load_run()
        return {
            "status": {
                "project": self.workspace.load_project().get("name", ""),
                "objective": self.workspace.load_project().get("objective", ""),
                "run_status": run.get("status", "idle"),
                "stop_reason": run.get("stop_reason", ""),
                "progress": graph.progress(),
                "clients": len(self._clients),
                "transport": self.transport_name,
                "daemon_running": self._run_task is not None and not self._run_task.done(),
            }
        }

    def _events_since(self, since: str) -> list[dict[str, Any]]:
        return self.events.tail_since(since) if since else self.events.read_last(100)

    def _start_run(self, objective: str) -> dict[str, Any]:
        with self._state_lock:
            if self._run_task is not None and not self._run_task.done():
                return {"started": False, "reason": "a run is already active"}
            if self._loop is None:
                raise RuntimeError("server loop is not running")
            self._run_task = asyncio_run_coroutine(self._loop, self._run_async(objective))
            return {"started": True}

    async def _run_async(self, objective: str) -> None:
        from .daemon import DaemonLoop

        def on_event(event: str, payload: dict[str, Any]) -> None:
            self.broadcast({"event": event, **payload})

        context = self._load_context()
        if objective.strip():
            from .orchestrator import Orchestrator

            orchestrator = Orchestrator(context, use_model_director=True, on_event=on_event)
            await orchestrator.run_loop(objective)
        daemon = DaemonLoop(context, poll_seconds=5.0, on_event=on_event)
        self._daemon = daemon
        report = await daemon.run()
        self._daemon = None
        self.broadcast({"event": "runtime.run_report", **report.as_dict()})

    async def _stop_run(self) -> None:
        # A graceful stop goes through the control channel the loop polls.
        self._request_stop("server shutting down")

    def _request_stop(self, reason: str) -> None:
        from .control import ControlChannel

        ControlChannel(self.workspace.paths.execution).request(stop=True, reason=reason)

    def _control(self, op: str, message: dict[str, Any]) -> dict[str, Any]:
        from .control import ControlChannel

        channel = ControlChannel(self.workspace.paths.execution)
        if op == "pause":
            channel.request(pause=True, reason=str(message.get("reason", "pause via IPC")))
        elif op == "resume":
            channel.request(resume=True, reason="resume via IPC")
        else:
            channel.request(stop=True, reason=str(message.get("reason", "stop via IPC")))
        return {"requested": op}

    def _approve(self, escalation_id: str, approved: bool) -> dict[str, Any]:
        from .control import ControlChannel

        channel = ControlChannel(self.workspace.paths.execution)
        if escalation_id in ("", "all"):
            ids = [e.get("id", "") for e in self.workspace.pending_escalations()]
            if not ids:
                return {"approved": 0}
        else:
            ids = [escalation_id]
        if approved:
            channel.request(approvals=[str(i) for i in ids])
        else:
            channel.request(rejections=[str(i) for i in ids])
        return {"approved": len(ids)}

    # ---- push ----

    def broadcast(self, message: dict[str, Any]) -> None:
        raw = ipc.encode_message(message)
        with self._clients_lock:
            clients = list(self._clients.values())
        for conn in clients:
            with contextlib.suppress(Exception):
                conn.send_line(raw)


def asyncio_run_coroutine(loop: Any, coro: Any) -> Any:
    """Schedule a coroutine on a running loop from any thread; return the future."""
    return asyncio_run_coroutine_threadsafe(loop, coro)


def asyncio_run_coroutine_threadsafe(loop: Any, coro: Any) -> Any:
    import asyncio

    return asyncio.run_coroutine_threadsafe(coro, loop)


# ---- client side --------------------------------------------------------------


def is_runtime_alive(root: Path) -> tuple[bool, RuntimeEndpoint | None]:
    """Endpoint exists, pid lives, and heartbeat is fresh."""
    import os

    from ..core.task import now_iso

    doc = ipc.read_endpoint(root)
    if doc is None:
        return False, None
    endpoint = RuntimeEndpoint.from_endpoint_doc(doc)
    if endpoint is None:
        return False, None
    if endpoint.pid > 0:
        try:
            os.kill(endpoint.pid, 0)
        except OSError:
            if os.name == "nt":
                pass  # os.kill(pid, 0) is unreliable on Windows; heartbeat decides
            else:
                return False, endpoint
    heartbeat_at = str(doc.get("heartbeat_at", ""))
    if heartbeat_at:
        from datetime import UTC, datetime

        try:
            parsed = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
            age = (datetime.now(UTC) - parsed).total_seconds()
            if age > STALE_AFTER_SECONDS:
                return False, endpoint
        except ValueError:
            return False, endpoint
    _ = now_iso  # keep import parity with callers that log liveness
    return True, endpoint


def clear_stale_endpoint(root: Path) -> None:
    alive, _endpoint = is_runtime_alive(root)
    if not alive:
        ipc.clear_endpoint(root)


class RuntimeClient:
    """The TUI/CLI's connection to the runtime daemon (attach/detach)."""

    def __init__(self, root: Path, *, on_event: ipc.Listener | None = None):
        self.root = Path(root).resolve()
        self.on_event = on_event
        self.client: IPCClient | None = None
        self.transport = ""

    def connect(self, *, retries: int = 2, retry_delay: float = 0.5) -> bool:
        clear_stale_endpoint(self.root)
        for attempt in range(retries + 1):
            if self._try_connect():
                return True
            if attempt < retries:
                time.sleep(retry_delay)
        return False

    def _try_connect(self) -> bool:
        doc = ipc.read_endpoint(self.root)
        if doc is None:
            return False
        endpoint = RuntimeEndpoint.from_endpoint_doc(doc)
        if endpoint is None:
            return False
        client = IPCClient()
        client.on_event = self._dispatch_event
        try:
            if endpoint.transport == "named_pipe":
                client.connect_pipe(endpoint.address)
            else:
                host, _, port = endpoint.address.partition(":")
                client.connect_tcp(host, int(port))
            reply = client.call({"op": "hello"})
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "hello failed"))
        except Exception:
            client.close()
            return False
        self.client = client
        self.transport = client.transport
        return True

    def _dispatch_event(self, message: dict[str, Any]) -> None:
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event(message)

    # -- typed wrappers --

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        if self.client is None:
            raise ConnectionError("not connected to the runtime daemon")
        return self.client.call({"op": op, **args})

    def status(self) -> dict[str, Any]:
        return self.call("status").get("status", {})

    def replay_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self.call("events.replay", limit=limit).get("events", []))

    def events_since(self, since: str) -> list[dict[str, Any]]:
        return list(self.call("events.since", since=since).get("events", []))

    def start_run(self, objective: str = "") -> dict[str, Any]:
        return self.call("run.start", objective=objective)

    def stop_run(self, reason: str = "stop requested") -> dict[str, Any]:
        return self.call("run.stop", reason=reason)

    def pause(self) -> dict[str, Any]:
        return self.call("pause")

    def resume(self) -> dict[str, Any]:
        return self.call("resume")

    def approve(self, escalation_id: str = "all") -> dict[str, Any]:
        return self.call("approve", escalation_id=escalation_id)

    def reject(self, escalation_id: str = "all") -> dict[str, Any]:
        return self.call("reject", escalation_id=escalation_id)

    def remember(self, text: str) -> dict[str, Any]:
        return self.call("remember", text=text)

    @property
    def connected(self) -> bool:
        return self.client is not None and not self.client._closed.is_set()

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
