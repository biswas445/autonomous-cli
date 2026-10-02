"""Provider rate limiting (operator constraint: Kios 5 RPM, Atria 30 RPM).

A process-wide limiter keyed by provider name. `acquire()` sleeps until the
next slot is available, so callers simply await it before every request; the
wait is bounded and re-checks the window after sleeping (thundering-herd safe
within one process, which is where all model calls originate).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field


def _rpm_for(provider: str) -> float:
    override = os.environ.get(f"AUTO_{provider.upper()}_RPM")
    if override:
        with_suppress = override.strip()
        try:
            value = float(with_suppress)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    defaults = {"kios": 5.0, "atria": 30.0}
    return defaults.get(provider.lower(), 0.0)


@dataclass
class RateLimiter:
    """Sliding-window RPM limiter for one provider (0 rpm = unlimited)."""

    provider: str
    rpm: float = 0.0
    _timestamps: deque[float] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.rpm <= 0:
            self.rpm = _rpm_for(self.provider)

    @property
    def limited(self) -> bool:
        return self.rpm > 0

    def _prune(self, now: float) -> None:
        window = 60.0
        while self._timestamps and now - self._timestamps[0] >= window:
            self._timestamps.popleft()

    def _next_slot(self) -> float:
        """Seconds to wait before the next request may start (0 = now)."""
        if not self.limited:
            return 0.0
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._timestamps) < self.rpm:
                return 0.0
            oldest = self._timestamps[0]
            return max(0.05, 60.0 - (now - oldest))

    async def acquire(self, *, max_wait: float = 90.0) -> float:
        """Await until a request slot is free; returns total seconds waited."""
        waited = 0.0
        while True:
            delay = self._next_slot()
            if delay <= 0:
                if self.limited:
                    with self._lock:
                        self._timestamps.append(time.monotonic())
                return waited
            if waited + delay > max_wait:
                # Take the slot anyway: exceeding the documented RPM by a
                # little beats wedging an unattended run forever.
                if self.limited:
                    with self._lock:
                        self._timestamps.append(time.monotonic())
                return waited
            await asyncio.sleep(min(delay, 2.0))
            waited += min(delay, 2.0)


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def limiter_for(provider: str) -> RateLimiter:
    with _limiters_lock:
        if provider not in _limiters:
            _limiters[provider] = RateLimiter(provider=provider)
        return _limiters[provider]


def reset_limiters() -> None:
    with _limiters_lock:
        _limiters.clear()
