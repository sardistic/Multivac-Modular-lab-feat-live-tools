"""In-process abuse limits for Discord and provider-backed operations."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import weakref
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitRule:
    user: int
    guild: int
    global_: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    scope: str = ""


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _rule(name: str, *, user: int, guild: int, global_: int, window: int) -> RateLimitRule:
    prefix = f"{name.upper()}_RATE_LIMIT"
    return RateLimitRule(
        user=_env_int(f"{prefix}_USER", user),
        guild=_env_int(f"{prefix}_GUILD", guild),
        global_=_env_int(f"{prefix}_GLOBAL", global_),
        window_seconds=_env_int(f"{prefix}_WINDOW_SECONDS", window),
    )


DEFAULT_RULES = {
    "request": _rule("request", user=20, guild=300, global_=1000, window=60),
    "url": _rule("url", user=20, guild=200, global_=600, window=300),
    "image": _rule("image", user=4, guild=50, global_=200, window=3600),
    "compute": _rule("compute", user=6, guild=60, global_=200, window=3600),
    "code": _rule("code", user=2, guild=20, global_=50, window=86400),
}


class SlidingWindowLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)

    def check(
        self,
        action: str,
        *,
        user_id: str | int,
        guild_id: str | int,
        rule: RateLimitRule | None = None,
    ) -> RateLimitDecision:
        selected = rule or DEFAULT_RULES[action]
        now = float(self._clock())
        cutoff = now - selected.window_seconds
        identities = (
            ("user", str(user_id), selected.user),
            ("guild", str(guild_id), selected.guild),
            ("global", "*", selected.global_),
        )
        with self._lock:
            for scope, identity, limit in identities:
                events = self._events[(action, scope, identity)]
                while events and events[0] <= cutoff:
                    events.popleft()
                if limit > 0 and len(events) >= limit:
                    retry = max(1, int(events[0] + selected.window_seconds - now) + 1)
                    return RateLimitDecision(False, retry_after=retry, scope=scope)
            for scope, identity, limit in identities:
                if limit > 0:
                    self._events[(action, scope, identity)].append(now)
        return RateLimitDecision(True)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


RATE_LIMITER = SlidingWindowLimiter()


def check_rate_limit(action: str, *, user_id: str | int, guild_id: str | int) -> RateLimitDecision:
    return RATE_LIMITER.check(action, user_id=user_id, guild_id=guild_id)


class ProviderCapacityError(RuntimeError):
    pass


class _LoopCapacity:
    def __init__(self):
        self.global_sem = asyncio.Semaphore(max(1, int(os.getenv("PROVIDER_MAX_CONCURRENT", "12"))))
        self.user_limit = max(1, int(os.getenv("PROVIDER_MAX_CONCURRENT_PER_USER", "2")))
        self.users: dict[str, asyncio.Semaphore] = {}


_LOOP_CAPACITY: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CAPACITY_LOCK = threading.Lock()


def _capacity_for_loop() -> _LoopCapacity:
    loop = asyncio.get_running_loop()
    with _CAPACITY_LOCK:
        capacity = _LOOP_CAPACITY.get(loop)
        if capacity is None:
            capacity = _LoopCapacity()
            _LOOP_CAPACITY[loop] = capacity
        return capacity


@asynccontextmanager
async def provider_capacity(user_id: str | int | None):
    capacity = _capacity_for_loop()
    user_key = str(user_id or "background")
    user_sem = capacity.users.setdefault(user_key, asyncio.Semaphore(capacity.user_limit))
    timeout = max(1.0, float(os.getenv("PROVIDER_QUEUE_TIMEOUT_SECONDS", "30")))
    global_acquired = False
    user_acquired = False
    try:
        await asyncio.wait_for(capacity.global_sem.acquire(), timeout=timeout)
        global_acquired = True
        await asyncio.wait_for(user_sem.acquire(), timeout=timeout)
        user_acquired = True
        yield
    except asyncio.TimeoutError as exc:
        raise ProviderCapacityError("provider queue is currently full") from exc
    finally:
        if user_acquired:
            user_sem.release()
        if global_acquired:
            capacity.global_sem.release()


__all__ = [
    "DEFAULT_RULES",
    "ProviderCapacityError",
    "RATE_LIMITER",
    "RateLimitDecision",
    "RateLimitRule",
    "SlidingWindowLimiter",
    "check_rate_limit",
    "provider_capacity",
]
