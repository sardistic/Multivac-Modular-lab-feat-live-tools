"""Atomic runtime and lifecycle manager for reviewed behavior components."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Mapping


MAX_BEHAVIOR_MODULE_BYTES = 256_000
BEHAVIOR_KINDS = ("events", "intents", "providers", "settings")
CALLABLE_BEHAVIOR_KINDS = ("events", "intents", "providers")
BehaviorHandler = Callable[..., Any]


class BehaviorRuntimeError(ValueError):
    pass


@dataclass(frozen=True)
class BehaviorSource:
    source_id: str
    version: str
    handlers: Mapping[str, Mapping[str, Any]]
    overrides: frozenset[tuple[str, str]]
    module_name: str | None = None
    digest: str | None = None


@dataclass(frozen=True)
class BehaviorSnapshot:
    generation: int
    handlers: Mapping[str, Mapping[str, Any]]
    owners: Mapping[str, Mapping[str, str]]

    def get(self, kind: str, name: str) -> BehaviorHandler | None:
        return self.handlers.get(kind, {}).get(name)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise BehaviorRuntimeError(
        f"Behavior settings must contain only JSON-like values, got {type(value).__name__}"
    )


def _normalize_handlers(raw: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise BehaviorRuntimeError("BEHAVIOR_HANDLERS must be a mapping")
    normalized: dict[str, Mapping[str, BehaviorHandler]] = {}
    count = 0
    unknown = sorted(set(raw).difference(BEHAVIOR_KINDS))
    if unknown:
        raise BehaviorRuntimeError(f"Unknown behavior handler kinds: {unknown}")
    for kind in BEHAVIOR_KINDS:
        values = raw.get(kind, {})
        if not isinstance(values, Mapping):
            raise BehaviorRuntimeError(f"BEHAVIOR_HANDLERS[{kind!r}] must be a mapping")
        handlers: dict[str, Any] = {}
        for name, handler in values.items():
            if not isinstance(name, str) or not name:
                raise BehaviorRuntimeError(f"Invalid {kind} handler: {name!r}")
            if kind in CALLABLE_BEHAVIOR_KINDS and not callable(handler):
                raise BehaviorRuntimeError(f"Invalid {kind} handler: {name!r}")
            handlers[name] = _freeze(handler) if kind == "settings" else handler
            count += 1
        normalized[kind] = MappingProxyType(handlers)
    if not count:
        raise BehaviorRuntimeError("A behavior component must register at least one handler")
    return MappingProxyType(normalized)


def _normalize_overrides(raw: Any) -> frozenset[tuple[str, str]]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, Mapping):
        raise BehaviorRuntimeError("BEHAVIOR_OVERRIDES must be a mapping")
    unknown = sorted(set(raw).difference(BEHAVIOR_KINDS))
    if unknown:
        raise BehaviorRuntimeError(f"Unknown behavior override kinds: {unknown}")
    result: set[tuple[str, str]] = set()
    for kind, names in raw.items():
        if not isinstance(names, (list, tuple, set, frozenset)):
            raise BehaviorRuntimeError(f"BEHAVIOR_OVERRIDES[{kind!r}] must be a sequence")
        for name in names:
            if not isinstance(name, str) or not name:
                raise BehaviorRuntimeError(f"Invalid {kind} override: {name!r}")
            result.add((kind, name))
    return frozenset(result)


class BehaviorRegistry:
    """Copy-on-write event, intent, and provider handler registry."""

    def __init__(self, *, history_limit: int = 5):
        self._lock = threading.RLock()
        self._history_limit = max(1, int(history_limit))
        self._sources: dict[str, BehaviorSource] = {}
        self._source_order: list[str] = []
        self._history: dict[str, deque[BehaviorSource | None]] = {}
        self._inflight: dict[tuple[int, str], int] = {}
        empty = MappingProxyType({kind: MappingProxyType({}) for kind in BEHAVIOR_KINDS})
        self._snapshot = BehaviorSnapshot(0, empty, empty)

    def snapshot(self) -> BehaviorSnapshot:
        with self._lock:
            return self._snapshot

    def source(self, source_id: str) -> BehaviorSource | None:
        with self._lock:
            return self._sources.get(source_id)

    def replace_source(
        self,
        source_id: str,
        handlers: Any,
        *,
        version: str,
        overrides: Any = None,
        allow_overrides: bool = False,
        module_name: str | None = None,
        digest: str | None = None,
    ) -> BehaviorSnapshot:
        if not isinstance(source_id, str) or not source_id:
            raise BehaviorRuntimeError("Behavior source_id must be non-empty")
        candidate = BehaviorSource(
            source_id=source_id,
            version=str(version or "unknown"),
            handlers=_normalize_handlers(handlers),
            overrides=_normalize_overrides(overrides),
            module_name=module_name,
            digest=digest,
        )
        with self._lock:
            conflicts = self._conflicts(candidate)
            undeclared = conflicts.difference(candidate.overrides)
            if conflicts and (not allow_overrides or undeclared):
                detail = sorted(f"{kind}:{name}" for kind, name in conflicts)
                if undeclared:
                    detail = sorted(f"{kind}:{name}" for kind, name in undeclared)
                raise BehaviorRuntimeError(
                    "Behavior overrides must be explicitly declared and authorized: "
                    + ", ".join(detail)
                )
            history = self._history.setdefault(source_id, deque(maxlen=self._history_limit))
            history.append(self._sources.get(source_id))
            if source_id not in self._source_order:
                self._source_order.append(source_id)
            self._sources[source_id] = candidate
            self._rebuild()
            return self._snapshot

    def unload_source(self, source_id: str) -> BehaviorSnapshot:
        with self._lock:
            if source_id not in self._sources:
                raise BehaviorRuntimeError(f"Behavior source is not active: {source_id}")
            history = self._history.setdefault(source_id, deque(maxlen=self._history_limit))
            history.append(self._sources.pop(source_id))
            self._rebuild()
            return self._snapshot

    def rollback_source(self, source_id: str) -> BehaviorSnapshot:
        with self._lock:
            history = self._history.get(source_id)
            if not history:
                raise BehaviorRuntimeError(f"No behavior rollback version exists for: {source_id}")
            previous = history.pop()
            if previous is None:
                self._sources.pop(source_id, None)
            else:
                self._sources[source_id] = previous
            self._rebuild()
            return self._snapshot

    async def invoke(
        self,
        kind: str,
        name: str,
        *args: Any,
        snapshot: BehaviorSnapshot | None = None,
        **kwargs: Any,
    ) -> Any:
        active = snapshot or self.snapshot()
        handler = active.get(kind, name)
        if handler is None:
            raise BehaviorRuntimeError(f"No active behavior handler for {kind}:{name}")
        if kind not in CALLABLE_BEHAVIOR_KINDS:
            raise BehaviorRuntimeError(f"Behavior values of kind {kind!r} are not callable")
        owner = active.owners[kind][name]
        key = (active.generation, owner)
        with self._lock:
            self._inflight[key] = self._inflight.get(key, 0) + 1
        try:
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            with self._lock:
                remaining = self._inflight.get(key, 1) - 1
                if remaining > 0:
                    self._inflight[key] = remaining
                else:
                    self._inflight.pop(key, None)

    async def wait_for_source(
        self, source_id: str, *, through_generation: int, timeout: float
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                active = sum(
                    count
                    for (generation, owner), count in self._inflight.items()
                    if owner == source_id and generation <= through_generation
                )
            if not active:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generation": self._snapshot.generation,
                "handlers": {
                    kind: sorted(values) for kind, values in self._snapshot.handlers.items()
                },
                "sources": [
                    {
                        "source_id": source_id,
                        "active": source_id in self._sources,
                        "version": self._sources[source_id].version
                        if source_id in self._sources else None,
                        "digest": self._sources[source_id].digest
                        if source_id in self._sources else None,
                        "history": len(self._history.get(source_id, ())),
                    }
                    for source_id in self._source_order
                ],
            }

    def _conflicts(self, candidate: BehaviorSource) -> set[tuple[str, str]]:
        occupied: set[tuple[str, str]] = set()
        for source_id, source in self._sources.items():
            if source_id == candidate.source_id:
                continue
            for kind, handlers in source.handlers.items():
                occupied.update((kind, name) for name in handlers)
        return {
            (kind, name)
            for kind, handlers in candidate.handlers.items()
            for name in handlers
            if (kind, name) in occupied
        }

    def _rebuild(self) -> None:
        handlers = {kind: {} for kind in BEHAVIOR_KINDS}
        owners = {kind: {} for kind in BEHAVIOR_KINDS}
        for source_id in self._source_order:
            source = self._sources.get(source_id)
            if source is None:
                continue
            for kind, values in source.handlers.items():
                for name, handler in values.items():
                    handlers[kind][name] = handler
                    owners[kind][name] = source_id
        generation = self._snapshot.generation + 1
        self._snapshot = BehaviorSnapshot(
            generation,
            MappingProxyType({k: MappingProxyType(v) for k, v in handlers.items()}),
            MappingProxyType({k: MappingProxyType(v) for k, v in owners.items()}),
        )


class BehaviorContext:
    """Resources owned by one active component generation."""

    def __init__(self, *, source_id: str, bot: Any = None, services: Mapping[str, Any] | None = None):
        self.source_id = source_id
        self.bot = bot
        self.services = MappingProxyType(dict(services or {}))
        self.state: Any = None
        self.stop_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._resources: list[Any] = []

    def create_task(self, awaitable, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def track_resource(self, resource: Any) -> Any:
        """Own an async client, closeable resource, or Discord View."""
        self._resources.append(resource)
        return resource

    async def close(self, module: ModuleType, *, timeout: float) -> list[str]:
        self.stop_event.set()
        errors: list[str] = []
        teardown = getattr(module, "teardown", None)
        if teardown is not None:
            try:
                await teardown(self)
            except Exception as exc:
                errors.append(f"teardown: {type(exc).__name__}: {exc}")
        for resource in reversed(self._resources):
            closer = (
                getattr(resource, "aclose", None)
                or getattr(resource, "close", None)
                or getattr(resource, "stop", None)
            )
            if closer is None:
                errors.append(f"resource: {type(resource).__name__} is not closeable")
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(f"resource: {type(exc).__name__}: {exc}")
        self._resources.clear()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=max(0.0, timeout))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    errors.append(f"task: {type(task.exception()).__name__}: {task.exception()}")
        return errors


@dataclass(frozen=True)
class BehaviorBundle:
    source_id: str
    version: str
    digest: str
    relative_path: str
    module: ModuleType
    handlers: Mapping[str, Mapping[str, Any]]
    overrides: frozenset[tuple[str, str]]
    context: BehaviorContext
    generation: int


class BehaviorModuleLoader:
    def __init__(
        self,
        registry: BehaviorRegistry,
        root: str | Path,
        *,
        bot: Any = None,
        services: Mapping[str, Any] | None = None,
        history_limit: int = 5,
        drain_timeout: float = 30.0,
    ):
        self.registry = registry
        self.root = Path(root).resolve()
        self.bot = bot
        self.services = dict(services or {})
        self.history_limit = max(1, int(history_limit))
        self.drain_timeout = max(0.1, float(drain_timeout))
        self._active: dict[str, BehaviorBundle] = {}
        self._history: dict[str, deque[BehaviorBundle | None]] = {}

    async def activate(
        self,
        relative_path: str | Path,
        *,
        source_id: str | None = None,
        expected_digest: str | None = None,
        allow_overrides: bool = False,
    ) -> dict[str, Any]:
        path = self._resolve(relative_path)
        payload = path.read_bytes()
        if len(payload) > MAX_BEHAVIOR_MODULE_BYTES:
            raise BehaviorRuntimeError(
                f"Behavior module exceeds {MAX_BEHAVIOR_MODULE_BYTES:,} bytes: {path.name}"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if expected_digest and digest.lower() != expected_digest.lower():
            raise BehaviorRuntimeError(
                f"Behavior module digest mismatch for {path.name}: expected {expected_digest}, got {digest}"
            )
        logical_source = self._validated_source_id(source_id or self.source_id(relative_path))
        module_name = f"_multivac_live_behavior_{digest}"
        module = self._import_module(module_name, path)
        try:
            prepared = await self._prepare(
                module,
                logical_source,
                digest,
                str(relative_path).replace("\\", "/"),
            )
            previous = self._active.get(logical_source)
            snapshot = self.registry.replace_source(
                logical_source,
                prepared.handlers,
                version=prepared.version,
                overrides={
                    kind: [name for candidate_kind, name in prepared.overrides if candidate_kind == kind]
                    for kind in BEHAVIOR_KINDS
                },
                allow_overrides=allow_overrides,
                module_name=module_name,
                digest=digest,
            )
            prepared = replace(prepared, generation=snapshot.generation)
        except Exception:
            if 'prepared' in locals():
                await prepared.context.close(module, timeout=self.drain_timeout)
            sys.modules.pop(module_name, None)
            raise

        history = self._history.setdefault(logical_source, deque(maxlen=self.history_limit))
        history.append(previous)
        self._active[logical_source] = prepared
        warnings: list[str] = []
        if previous is not None:
            drained = await self.registry.wait_for_source(
                logical_source,
                through_generation=previous.generation,
                timeout=self.drain_timeout,
            )
            if not drained:
                warnings.append("old generation exceeded drain timeout; owned tasks were cancelled")
            warnings.extend(await previous.context.close(previous.module, timeout=self.drain_timeout))
        sys.modules.pop(module_name, None)
        return self._result(prepared, warnings)

    async def unload_source(self, source_id: str) -> dict[str, Any]:
        source_id = self._validated_source_id(source_id)
        current = self._active.get(source_id)
        if current is None:
            raise BehaviorRuntimeError(f"Behavior source is not active: {source_id}")
        snapshot = self.registry.unload_source(source_id)
        history = self._history.setdefault(source_id, deque(maxlen=self.history_limit))
        history.append(current)
        self._active.pop(source_id, None)
        await self.registry.wait_for_source(
            source_id, through_generation=current.generation, timeout=self.drain_timeout
        )
        warnings = await current.context.close(current.module, timeout=self.drain_timeout)
        return {"source_id": source_id, "generation": snapshot.generation, "warnings": warnings}

    async def rollback_source(self, source_id: str) -> dict[str, Any]:
        source_id = self._validated_source_id(source_id)
        history = self._history.get(source_id)
        if not history:
            raise BehaviorRuntimeError(f"No behavior rollback version exists for: {source_id}")
        current = self._active.get(source_id)
        previous = history[-1]
        restored: BehaviorBundle | None = None
        if previous is not None:
            restored = await self._prepare(
                previous.module,
                previous.source_id,
                previous.digest,
                previous.relative_path,
            )
        try:
            snapshot = self.registry.rollback_source(source_id)
        except Exception:
            if restored is not None:
                await restored.context.close(restored.module, timeout=self.drain_timeout)
            raise
        history.pop()
        warnings: list[str] = []
        if current is not None:
            await self.registry.wait_for_source(
                source_id, through_generation=current.generation, timeout=self.drain_timeout
            )
            warnings.extend(await current.context.close(current.module, timeout=self.drain_timeout))
        if restored is None:
            self._active.pop(source_id, None)
            return {"source_id": source_id, "generation": snapshot.generation, "warnings": warnings}
        restored = replace(restored, generation=snapshot.generation)
        self._active[source_id] = restored
        return self._result(restored, warnings)

    async def _prepare(
        self, module: ModuleType, source_id: str, digest: str, relative_path: str
    ) -> BehaviorBundle:
        setup = getattr(module, "setup", None)
        if not inspect.iscoroutinefunction(setup):
            raise BehaviorRuntimeError("Behavior module must export async setup(context)")
        for hook_name in ("healthcheck", "teardown"):
            hook = getattr(module, hook_name, None)
            if hook is not None and not inspect.iscoroutinefunction(hook):
                raise BehaviorRuntimeError(f"Behavior {hook_name}(context) must be async")
        raw_handlers = getattr(module, "BEHAVIOR_HANDLERS", {})
        if isinstance(raw_handlers, Mapping):
            raw_handlers = dict(raw_handlers)
            settings = getattr(module, "BEHAVIOR_SETTINGS", None)
            if settings is not None:
                if "settings" in raw_handlers:
                    raise BehaviorRuntimeError(
                        "Declare settings with BEHAVIOR_SETTINGS, not both component fields"
                    )
                raw_handlers["settings"] = settings
        handlers = _normalize_handlers(raw_handlers)
        overrides = _normalize_overrides(getattr(module, "BEHAVIOR_OVERRIDES", None))
        declared_without_handler = {
            (kind, name) for kind, name in overrides if name not in handlers[kind]
        }
        if declared_without_handler:
            detail = sorted(f"{kind}:{name}" for kind, name in declared_without_handler)
            raise BehaviorRuntimeError("Overrides without matching handlers: " + ", ".join(detail))
        context = BehaviorContext(source_id=source_id, bot=self.bot, services=self.services)
        try:
            context.state = await setup(context)
            healthcheck = getattr(module, "healthcheck", None)
            if healthcheck is not None and await healthcheck(context) is False:
                raise BehaviorRuntimeError("Behavior healthcheck returned false")
        except Exception:
            await context.close(module, timeout=self.drain_timeout)
            raise
        return BehaviorBundle(
            source_id=source_id,
            version=str(getattr(module, "BEHAVIOR_VERSION", digest[:12])),
            digest=digest,
            relative_path=relative_path,
            module=module,
            handlers=handlers,
            overrides=overrides,
            context=context,
            generation=0,
        )

    def _resolve(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise BehaviorRuntimeError("Behavior path must be relative and confined")
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise BehaviorRuntimeError("Behavior path escapes configured root") from exc
        if path.suffix.lower() != ".py" or path.name == "__init__.py" or not path.is_file():
            raise BehaviorRuntimeError("Behavior path must identify a standalone .py file")
        return path

    @staticmethod
    def source_id(relative_path: str | Path) -> str:
        return "behavior:" + str(relative_path).replace("\\", "/")

    @staticmethod
    def _validated_source_id(source_id: str) -> str:
        if not isinstance(source_id, str) or not source_id.startswith(("behavior:", "hotbehavior:")):
            raise BehaviorRuntimeError("Invalid behavior source ID")
        return source_id

    @staticmethod
    def _import_module(module_name: str, path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise BehaviorRuntimeError(f"Unable to import behavior module: {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    @staticmethod
    def _result(bundle: BehaviorBundle, warnings: list[str]) -> dict[str, Any]:
        return {
            "source_id": bundle.source_id,
            "version": bundle.version,
            "sha256": bundle.digest,
            "relative_path": bundle.relative_path,
            "generation": bundle.generation,
            "handlers": {kind: sorted(values) for kind, values in bundle.handlers.items()},
            "warnings": warnings,
        }


__all__ = [
    "BEHAVIOR_KINDS",
    "BehaviorContext",
    "BehaviorModuleLoader",
    "BehaviorRegistry",
    "BehaviorRuntimeError",
    "BehaviorSnapshot",
]
