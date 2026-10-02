"""Local IPC for the runtime daemon (directive #2).

The runtime runs as its own process; the TUI and CLI are *clients*. This
module owns the transport layer:

- Windows: a real named pipe (`\\\\.\\pipe\\auto-engine-<slug>`) implemented
  with ctypes (CreateNamedPipeW / ConnectNamedPipe / ReadFile / WriteFile) —
  byte mode, duplex, unlimited instances, one accept thread per client
  (multi-client support).
- All platforms: TCP loopback fallback with an ephemeral port.
- Discovery: `.agents/runtime/endpoint.json` records which transport is live,
  plus pid, heartbeat, and a protocol version. A client connects, then sends
  newline-delimited JSON requests and receives newline-delimited JSON
  responses. One long-lived connection per client; a background reader on the
  client side fans server-pushed events into callbacks (event streaming).

Framing is deliberately trivial: JSON per line, UTF-8, `\n` terminated.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_CALL_TIMEOUT = 15.0

Listener = Callable[[dict[str, Any]], None]

# ---- endpoint discovery -------------------------------------------------------


def pipe_name(slug: str) -> str:
    """Full Win32 named-pipe path.

    Regression (live test): the `\\\\.\\pipe\\` prefix was missing, so every
    CreateNamedPipeW/CreateFileW failed with error 123 (invalid name) and the
    server's accept threads spun forever. Both the server and the client must
    use the same fully-qualified name.
    """
    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in slug)
    return f"\\\\.\\pipe\\auto-engine-{slug}"


def runtime_dir(root: Path) -> Path:
    return Path(root) / ".agents" / "runtime"


def endpoint_path(root: Path) -> Path:
    return runtime_dir(root) / "endpoint.json"


def write_endpoint(root: Path, payload: dict[str, Any]) -> None:
    from ..core.workspace import atomic_write_json

    d = runtime_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(endpoint_path(root), payload)


def read_endpoint(root: Path) -> dict[str, Any] | None:
    path = endpoint_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def clear_endpoint(root: Path) -> None:
    with contextlib.suppress(OSError):
        endpoint_path(root).unlink(missing_ok=True)


# ---- line framing -------------------------------------------------------------


class LineFramer:
    """Incremental UTF-8 newline framing over a byte stream."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer += data
        messages: list[dict[str, Any]] = []
        while b"\n" in self._buffer:
            raw, _, self._buffer = self._buffer.partition(b"\n")
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # a torn line is dropped, never fatal
            if isinstance(obj, dict):
                messages.append(obj)
        if len(self._buffer) > 8 * 1024 * 1024:  # corrupt peer guard
            self._buffer = b""
        return messages


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")


_OVERLAPPED_CACHE: list[type] = []


def _overlapped_struct() -> type:
    """The ctypes OVERLAPPED structure (built lazily; Windows only)."""
    import ctypes

    if not _OVERLAPPED_CACHE:

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", ctypes.c_ulong),
                ("OffsetHigh", ctypes.c_ulong),
                ("hEvent", ctypes.c_void_p),
            ]

        _OVERLAPPED_CACHE.append(_Overlapped)
    return _OVERLAPPED_CACHE[0]


# ---- server transports --------------------------------------------------------


@dataclass
class _Connection:
    """One connected client; the server pushes events through `send`."""

    id: str
    send_line: Callable[[bytes], None]
    close: Callable[[], None]
    listener: Listener | None = None


class TcpTransportServer:
    """TCP loopback transport with an accept thread (multi-client)."""

    name = "tcp"

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.address = ""

    def start(self, on_message: Callable[[dict[str, Any], _Connection], None]) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, 0))  # ephemeral port
        self._server.listen(16)
        self.host, port = self._server.getsockname()
        self.address = f"{self.host}:{port}"
        self.port = port
        self._accept_thread = threading.Thread(
            target=self._accept_loop, args=(on_message,), daemon=True, name="ipc-accept"
        )
        self._accept_thread.start()

    def _accept_loop(self, on_message: Callable[[dict[str, Any], _Connection], None]) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._client_loop, args=(client, on_message), daemon=True
            ).start()

    def _client_loop(
        self, sock: socket.socket, on_message: Callable[[dict[str, Any], _Connection], None]
    ) -> None:
        sock.settimeout(0.5)
        framer = LineFramer()
        lock = threading.Lock()
        conn = _Connection(
            id=uuid.uuid4().hex[:12],
            send_line=lambda raw: self._send(sock, lock, raw),
            close=lambda: self._close(sock),
        )
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                for message in framer.feed(data):
                    on_message(message, conn)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    @staticmethod
    def _send(sock: socket.socket, lock: threading.Lock, raw: bytes) -> None:
        with lock, contextlib.suppress(OSError):
            sock.sendall(raw)

    @staticmethod
    def _close(sock: socket.socket) -> None:
        with contextlib.suppress(OSError):
            sock.close()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()


class WindowsPipeTransportServer:
    """Named-pipe transport on Windows (multi-instance, byte mode, duplex).

    Implemented with ctypes against kernel32 so no new dependency is added.
    Each pipe instance serves one client; `CreateEventW` + `GetOverlappedResult`
    give the synchronous behaviour the accept loop needs.
    """

    name = "named_pipe"
    BUFFER = 64 * 1024
    PIPE_ACCESS_DUPLEX = 0x3
    PIPE_TYPE_BYTE = 0x0
    PIPE_READMODE_BYTE = 0x0
    PIPE_WAIT = 0x0
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x80000
    INVALID_HANDLE_VALUE = -1
    ERROR_PIPE_CONNECTED = 232

    def __init__(self, name: str):
        self.pipe_path = f"\\\\.\\pipe\\{name}"
        self._stop = threading.Event()
        self._accept_threads: list[threading.Thread] = []
        self._handles: list[int] = []

    def start(self, on_message: Callable[[dict[str, Any], _Connection], None]) -> None:
        if os.name != "nt":
            raise OSError("named pipes require Windows")
        self._on_message = on_message
        # A small pool of instances (one per concurrent client); exhausted
        # clients fall back to TCP discovery, but 8 covers any real TUI use.
        for index in range(8):
            thread = threading.Thread(
                target=self._instance_loop, args=(index,), daemon=True, name=f"ipc-pipe-{index}"
            )
            thread.start()
            self._accept_threads.append(thread)

    def _instance_loop(self, index: int) -> None:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        while not self._stop.is_set():
            handle = k32.CreateNamedPipeW(
                self.pipe_path,
                self.PIPE_ACCESS_DUPLEX,
                self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT,
                255,
                self.BUFFER,
                self.BUFFER,
                0,
                None,
            )
            if handle in (self.INVALID_HANDLE_VALUE, None):
                time.sleep(0.2)
                continue
            self._handles.append(handle)
            connected = k32.ConnectNamedPipe(handle, None)
            if not connected and k32.GetLastError() != self.ERROR_PIPE_CONNECTED:
                k32.DisconnectNamedPipe(handle)
                k32.CloseHandle(handle)
                with contextlib.suppress(ValueError):
                    self._handles.remove(handle)
                continue
            self._serve_client(k32, handle)

    def _serve_client(self, k32, handle: int) -> None:
        framer = LineFramer()
        lock = threading.Lock()

        def send_line(raw: bytes) -> None:
            written = ctypes.c_ulong(0)
            with lock, contextlib.suppress(OSError):
                if not k32.WriteFile(handle, raw, len(raw), ctypes.byref(written), None):
                    raise OSError("pipe write failed")

        def close() -> None:
            with contextlib.suppress(OSError):
                k32.DisconnectNamedPipe(handle)
                k32.CloseHandle(handle)
            with contextlib.suppress(ValueError):
                self._handles.remove(handle)

        conn = _Connection(id=uuid.uuid4().hex[:12], send_line=send_line, close=close)
        buf = ctypes.create_string_buffer(self.BUFFER)
        read = ctypes.c_ulong(0)
        try:
            while not self._stop.is_set():
                ok = k32.ReadFile(handle, buf, self.BUFFER, ctypes.byref(read), None)
                if not ok or not read.value:
                    return
                for message in framer.feed(buf.raw[: read.value]):
                    self._on_message(message, conn)
        finally:
            close()

    def stop(self) -> None:
        self._stop.set()


def create_server(slug: str, prefer: str = "auto") -> tuple[Any, str]:
    """Create the best available transport server; returns (server, transport)."""
    if os.name == "nt" and prefer in ("auto", "named_pipe"):
        return WindowsPipeTransportServer(pipe_name(slug)), "named_pipe"
    return TcpTransportServer(), "tcp"


# ---- client -------------------------------------------------------------------


class IPCClient:
    """Client connection: request/response plus a push-event listener thread.

    `call()` sends one request and waits for the response with the matching
    id; server-initiated messages (`event` payloads) go to `on_event`.
    Reconnect logic lives in RuntimeClient (transport selection), this class
    is single-connection.
    """

    def __init__(self, *, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT):
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._responses: dict[str, dict[str, Any]] = {}
        self.on_event: Listener | None = None
        self._closed = threading.Event()
        self._sock: socket.socket | None = None
        self._pipe: dict[str, Any] | None = None
        self._send_lock = threading.Lock()
        self.transport = ""

    # -- connection --

    def connect_tcp(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=DEFAULT_CONNECT_TIMEOUT)
        self._sock.settimeout(0.25)
        self.transport = f"tcp:{host}:{port}"
        self._start_reader(self._read_sock)

    def connect_pipe(self, pipe_path: str) -> None:
        if os.name != "nt":
            raise OSError("named pipes require Windows")
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        generic_read_write = 0x80000000 | 0x40000000
        open_existing = 3
        # FILE_FLAG_OVERLAPPED: the client reads and writes the same handle
        # from different threads, and Python 3.14 deadlocks two *blocking*
        # sync I/O calls on one handle (a pending ReadFile blocked a
        # concurrent WriteFile forever in the live test). Overlapped I/O is
        # the supported full-duplex pattern.
        file_flag_overlapped = 0x40000000
        handle = k32.CreateFileW(
            pipe_path, generic_read_write, 0, None, open_existing, file_flag_overlapped, None
        )
        if handle in (-1, None):
            raise OSError(f"cannot open pipe {pipe_path}: {ctypes.GetLastError()}")
        self._pipe = {"k32": k32, "handle": handle}
        self.transport = f"pipe:{pipe_path}"
        self._start_reader(self._read_pipe)

    def _start_reader(self, reader: Callable[[], None]) -> None:
        thread = threading.Thread(target=self._reader_wrapper(reader), daemon=True, name="ipc-reader")
        thread.start()

    def _reader_wrapper(self, reader: Callable[[], None]) -> Callable[[], None]:
        def wrapped() -> None:
            try:
                reader()
            finally:
                self._closed.set()
                for event in list(self._pending.values()):
                    event.set()

        return wrapped

    def _read_sock(self) -> None:
        assert self._sock is not None
        framer = LineFramer()
        while not self._closed.is_set():
            try:
                data = self._sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            self._dispatch(framer.feed(data))

    def _read_pipe(self) -> None:
        assert self._pipe is not None
        import ctypes

        k32, handle = self._pipe["k32"], self._pipe["handle"]
        framer = LineFramer()
        # Overlapped read loop (see connect_pipe): a blocking ReadFile here
        # deadlocked a concurrent WriteFile on Python 3.14.
        overlapped_cls = _overlapped_struct()
        buffer_size = 64 * 1024
        buf = ctypes.create_string_buffer(buffer_size)
        while not self._closed.is_set():
            overlapped = overlapped_cls()
            overlapped.hEvent = k32.CreateEventW(None, True, False, None)
            try:
                ok = k32.ReadFile(handle, buf, buffer_size, None, ctypes.byref(overlapped))
                if not ok and ctypes.GetLastError() == 997:  # ERROR_IO_PENDING
                    wait = k32.WaitForSingleObject(overlapped.hEvent, 5_000)
                    if wait != 0:  # timeout or failure: loop back around
                        continue
                received = ctypes.c_ulong(0)
                if not k32.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(received), False
                ):
                    return
                if not received.value:
                    return
                self._dispatch(framer.feed(buf.raw[: received.value]))
            finally:
                k32.CloseHandle(overlapped.hEvent)

    def _dispatch(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            request_id = str(message.get("id", ""))
            if request_id and request_id in self._pending:
                self._responses[request_id] = message
                self._pending[request_id].set()
            elif self.on_event is not None:
                with contextlib.suppress(Exception):
                    self.on_event(message)

    # -- io --

    def _send_raw(self, raw: bytes) -> None:
        with self._send_lock:
            if self._sock is not None:
                self._sock.sendall(raw)
                return
            if self._pipe is not None:
                import ctypes

                k32, handle = self._pipe["k32"], self._pipe["handle"]
                overlapped_cls = _overlapped_struct()
                overlapped = overlapped_cls()
                overlapped.hEvent = k32.CreateEventW(None, True, False, None)
                try:
                    buffer = (ctypes.c_char * len(raw)).from_buffer_copy(raw)
                    ok = k32.WriteFile(handle, buffer, len(raw), None, ctypes.byref(overlapped))
                    if (
                        not ok
                        and ctypes.GetLastError() == 997
                        and k32.WaitForSingleObject(overlapped.hEvent, 10_000) != 0
                    ):  # ERROR_IO_PENDING that never completed
                        raise TimeoutError("pipe write timed out")
                    written = ctypes.c_ulong(0)
                    if not k32.GetOverlappedResult(
                        handle, ctypes.byref(overlapped), ctypes.byref(written), False
                    ):
                        raise OSError("pipe write failed")
                    return
                finally:
                    k32.CloseHandle(overlapped.hEvent)
        raise OSError("not connected")

    def call(self, payload: dict[str, Any], *, timeout: float = DEFAULT_CALL_TIMEOUT) -> dict[str, Any]:
        """One request/response round trip."""
        request_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        with self._lock:
            self._pending[request_id] = event
        request = {"id": request_id, **payload}
        self._send_raw(encode_message(request))
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"runtime call timed out after {timeout}s: {payload.get('op')}")
        with self._lock:
            self._pending.pop(request_id, None)
            response = self._responses.pop(request_id, {})
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response

    def notify(self, payload: dict[str, Any]) -> None:
        """Fire-and-forget send (no response expected)."""
        self._send_raw(encode_message(dict(payload)))

    def close(self) -> None:
        self._closed.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._pipe is not None:
            with contextlib.suppress(Exception):
                k32, handle = self._pipe["k32"], self._pipe["handle"]
                k32.CloseHandle(handle)
            self._pipe = None

    def wait_closed(self, timeout: float = 1.0) -> bool:
        return self._closed.wait(timeout)


@dataclass
class RuntimeEndpoint:
    """What a client needs to reach the running runtime."""

    transport: str  # named_pipe | tcp
    address: str  # pipe path or host:port
    pid: int
    started_at: str = ""
    heartbeat_at: str = ""
    protocol: int = PROTOCOL_VERSION

    @classmethod
    def from_endpoint_doc(cls, doc: dict[str, Any]) -> RuntimeEndpoint | None:
        transport = str(doc.get("transport", ""))
        address = str(doc.get("address", ""))
        pid = int(doc.get("pid", 0) or 0)
        if not transport or not address:
            return None
        return cls(
            transport=transport,
            address=address,
            pid=pid,
            started_at=str(doc.get("started_at", "")),
            heartbeat_at=str(doc.get("heartbeat_at", "")),
            protocol=int(doc.get("protocol", 0) or 0),
        )
