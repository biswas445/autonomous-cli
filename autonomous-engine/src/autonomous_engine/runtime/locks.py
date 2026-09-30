"""Resource locks: prevent two agents from corrupting the same surface (§22).

Parallel execution is only safe if tasks that touch the same files, the same
database, or the same service do not run at the same time. Locks are declared
deterministically from each task's declared artifacts and locked paths, held
for the duration of a task, and released even when the task fails.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

# A task touching any of these needs exclusive access; they are global
# singletons that a single lock name covers.
GLOBAL_RESOURCES = (
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "poetry.lock",
    "uv.lock",
    "state.sqlite",
    "config.json",
    "task_graph.json",
)


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./").lower()


def resource_keys(paths: list[str]) -> list[str]:
    """Map declared paths to lock keys.

    Lock granularity is the top-level directory plus every shared build file,
    which is coarse enough to be safe and fine enough to allow real parallelism.
    """
    keys: set[str] = set()
    for path in paths or []:
        normalised = _norm(path)
        if not normalised:
            continue
        base = normalised.split("/", 1)[0]
        if normalised in GLOBAL_RESOURCES:
            keys.add(f"file:{normalised}")
        elif base:
            keys.add(f"dir:{base}")
    return sorted(keys)


@dataclass
class LockHandle:
    keys: list[str]
    task_id: str

    def release(self) -> None:
        pass  # handles are released through the owning LockManager


@dataclass
class LockManager:
    """In-process, thread-safe resource locks."""

    held: dict[str, str] = field(default_factory=dict)  # key -> task_id
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def conflicts(self, task_id: str, keys: list[str]) -> list[str]:
        with self._lock:
            return sorted(k for k in keys if k in self.held and self.held[k] != task_id)

    def acquire(self, task_id: str, keys: list[str]) -> tuple[bool, list[str]]:
        """Try to take every key. All-or-nothing, so tasks never half-own a path."""
        with self._lock:
            blocking = [k for k in keys if k in self.held and self.held[k] != task_id]
            if blocking:
                return False, blocking
            for key in keys:
                self.held[key] = task_id
            return True, []

    def release(self, task_id: str) -> list[str]:
        with self._lock:
            released = [k for k, owner in self.held.items() if owner == task_id]
            for key in released:
                self.held.pop(key, None)
            return sorted(released)

    def release_keys(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self.held.pop(key, None)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self.held)

    def is_free(self, keys: list[str]) -> bool:
        with self._lock:
            return not any(k in self.held for k in keys)

    def as_dict(self) -> dict[str, Any]:
        return {"held": self.snapshot(), "count": len(self.held)}
