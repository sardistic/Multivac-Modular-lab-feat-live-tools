"""Atomic runtime registry and explicit loader for trusted tool modules.

Hotloaded modules execute inside the bot process and therefore have the bot's
full authority.  This module intentionally provides no file watcher: an
external, reviewed activation step must choose the exact file to load.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Iterable, Mapping


ToolHandler = Callable[[dict[str, Any]], Any]
MAX_TOOL_MODULE_BYTES = 256_000


class ToolRegistryError(ValueError):
    """Raised when a tool bundle cannot be activated safely."""


@dataclass(frozen=True)
class ToolSource:
    source_id: str
    version: str
    specs: tuple[dict[str, Any], ...]
    handlers: Mapping[str, ToolHandler]
    allow_overrides: bool = False
    module_name: str | None = None
    digest: str | None = None


@dataclass(frozen=True)
class ToolSnapshot:
    """One internally consistent generation of schemas and handlers."""

    generation: int
    _specs: tuple[dict[str, Any], ...]
    handlers: Mapping[str, ToolHandler]
    sources: Mapping[str, str]

    @property
    def specs(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._specs))

    def tool_specs(self) -> list[dict[str, Any]]:
        # API clients and tests may normalize these dictionaries in-place.
        return copy.deepcopy(list(self._specs))


def _validated_bundle(
    source_id: str,
    version: str,
    specs: Iterable[Mapping[str, Any]],
    handlers: Mapping[str, ToolHandler],
    *,
    allow_overrides: bool,
    module_name: str | None,
    digest: str | None,
) -> ToolSource:
    if not source_id or not isinstance(source_id, str):
        raise ToolRegistryError("Tool source_id must be a non-empty string")
    if not isinstance(handlers, Mapping):
        raise ToolRegistryError("TOOL_HANDLERS must be a mapping")

    normalized_specs: list[dict[str, Any]] = []
    names: list[str] = []
    for raw in specs:
        if not isinstance(raw, Mapping):
            raise ToolRegistryError("Every tool spec must be a mapping")
        spec = copy.deepcopy(dict(raw))
        function = spec.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if spec.get("type") != "function" or not isinstance(name, str) or not name:
            raise ToolRegistryError("Every tool spec must define type=function and function.name")
        if name in names:
            raise ToolRegistryError(f"Duplicate tool spec in {source_id}: {name}")
        names.append(name)
        normalized_specs.append(spec)

    handler_names = set(handlers)
    if set(names) != handler_names:
        missing = sorted(set(names) - handler_names)
        extra = sorted(handler_names - set(names))
        raise ToolRegistryError(
            f"Tool specs and handlers must match (missing handlers={missing}, extra handlers={extra})"
        )
    for name, handler in handlers.items():
        if not isinstance(name, str) or not callable(handler):
            raise ToolRegistryError(f"Invalid handler for tool {name!r}")

    return ToolSource(
        source_id=source_id,
        version=str(version or "unknown"),
        specs=tuple(normalized_specs),
        handlers=MappingProxyType(dict(handlers)),
        allow_overrides=bool(allow_overrides),
        module_name=module_name,
        digest=digest,
    )


class ToolRegistry:
    """Copy-on-write registry with bounded, per-source rollback history."""

    def __init__(self, *, history_limit: int = 5):
        self._lock = threading.RLock()
        self._history_limit = max(1, int(history_limit))
        self._sources: dict[str, ToolSource] = {}
        self._source_order: list[str] = []
        self._history: dict[str, deque[ToolSource | None]] = {}
        self._generation = 0
        self._snapshot = ToolSnapshot(
            generation=0,
            _specs=(),
            handlers=MappingProxyType({}),
            sources=MappingProxyType({}),
        )

    def snapshot(self) -> ToolSnapshot:
        # The object and all maps it exposes are immutable, so returning the
        # current reference is an atomic read under CPython and alternate VMs.
        with self._lock:
            return self._snapshot

    def has_source(self, source_id: str) -> bool:
        with self._lock:
            return source_id in self._sources

    def source(self, source_id: str) -> ToolSource | None:
        with self._lock:
            return self._sources.get(source_id)

    def replace_source(
        self,
        source_id: str,
        specs: Iterable[Mapping[str, Any]],
        handlers: Mapping[str, ToolHandler],
        *,
        version: str = "unknown",
        allow_overrides: bool = False,
        module_name: str | None = None,
        digest: str | None = None,
    ) -> ToolSnapshot:
        candidate = _validated_bundle(
            source_id,
            version,
            specs,
            handlers,
            allow_overrides=allow_overrides,
            module_name=module_name,
            digest=digest,
        )
        with self._lock:
            self._ensure_no_forbidden_overrides(candidate)
            history = self._history.setdefault(
                source_id, deque(maxlen=self._history_limit)
            )
            history.append(self._sources.get(source_id))
            if source_id not in self._source_order:
                self._source_order.append(source_id)
            self._sources[source_id] = candidate
            self._rebuild_snapshot()
            return self._snapshot

    def unload_source(self, source_id: str) -> ToolSnapshot:
        with self._lock:
            if source_id not in self._sources:
                raise ToolRegistryError(f"Tool source is not active: {source_id}")
            history = self._history.setdefault(
                source_id, deque(maxlen=self._history_limit)
            )
            history.append(self._sources.pop(source_id))
            self._rebuild_snapshot()
            return self._snapshot

    def rollback_source(self, source_id: str) -> ToolSnapshot:
        with self._lock:
            history = self._history.get(source_id)
            if not history:
                raise ToolRegistryError(f"No rollback version exists for: {source_id}")
            previous = history.pop()
            if previous is None:
                self._sources.pop(source_id, None)
            else:
                self._ensure_no_forbidden_overrides(previous, replacing=source_id)
                self._sources[source_id] = previous
            self._rebuild_snapshot()
            return self._snapshot

    async def execute(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        *,
        context: Mapping[str, Any] | None = None,
        snapshot: ToolSnapshot | None = None,
    ) -> Any:
        active = snapshot or self.snapshot()
        if name == "list_available_tools":
            return {
                "tools": [
                    {
                        "name": spec.get("function", {}).get("name"),
                        "description": spec.get("function", {}).get("description"),
                    }
                    for spec in active.specs
                ],
                "generation": active.generation,
            }
        handler = active.handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"unknown_tool: {name}"}
        call_args = dict(args or {})
        if context:
            call_args["_context"] = dict(context)
        result = handler(call_args)
        if inspect.isawaitable(result):
            return await result
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generation": self._snapshot.generation,
                "tools": list(self._snapshot.handlers),
                "sources": [
                    {
                        "source_id": source_id,
                        "active": source_id in self._sources,
                        "version": self._sources[source_id].version
                        if source_id in self._sources
                        else None,
                        "digest": self._sources[source_id].digest
                        if source_id in self._sources
                        else None,
                        "history": len(self._history.get(source_id, ())),
                    }
                    for source_id in self._source_order
                ],
            }

    def _ensure_no_forbidden_overrides(
        self, candidate: ToolSource, *, replacing: str | None = None
    ) -> None:
        owner_by_name: dict[str, str] = {}
        for source_id, source in self._sources.items():
            if source_id == candidate.source_id or source_id == replacing:
                continue
            for name in source.handlers:
                owner_by_name[name] = source_id
        conflicts = sorted(set(candidate.handlers).intersection(owner_by_name))
        if conflicts and not candidate.allow_overrides:
            owners = {name: owner_by_name[name] for name in conflicts}
            raise ToolRegistryError(
                f"Tool source {candidate.source_id} would override active tools: {owners}"
            )

    def _rebuild_snapshot(self) -> None:
        specs_by_name: dict[str, dict[str, Any]] = {}
        handlers: dict[str, ToolHandler] = {}
        sources: dict[str, str] = {}
        ordered_names: list[str] = []
        for source_id in self._source_order:
            source = self._sources.get(source_id)
            if source is None:
                continue
            for spec in source.specs:
                name = spec["function"]["name"]
                if name not in specs_by_name:
                    ordered_names.append(name)
                specs_by_name[name] = copy.deepcopy(spec)
                handlers[name] = source.handlers[name]
                sources[name] = source_id
        self._generation += 1
        self._snapshot = ToolSnapshot(
            generation=self._generation,
            _specs=tuple(specs_by_name[name] for name in ordered_names),
            handlers=MappingProxyType(handlers),
            sources=MappingProxyType(sources),
        )


class ToolModuleLoader:
    """Loads explicitly selected Python tool bundles beneath one trusted root."""

    def __init__(self, registry: ToolRegistry, root: str | Path):
        self.registry = registry
        self.root = Path(root).resolve()

    def activate(
        self,
        relative_path: str | Path,
        *,
        allow_overrides: bool = False,
        source_id: str | None = None,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        path = self._resolve(relative_path)
        payload = path.read_bytes()
        if len(payload) > MAX_TOOL_MODULE_BYTES:
            raise ToolRegistryError(
                f"Tool module exceeds {MAX_TOOL_MODULE_BYTES:,} bytes: {path.name}"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if expected_digest and digest.lower() != expected_digest.lower():
            raise ToolRegistryError(
                f"Tool module digest mismatch for {path.name}: expected {expected_digest}, got {digest}"
            )
        source_id = self._validated_source_id(source_id or self.source_id(relative_path))
        module_name = f"_multivac_live_tool_{digest}"
        module = self._import_module(module_name, path)
        specs = getattr(module, "TOOL_SPECS", None)
        handlers = getattr(module, "TOOL_HANDLERS", None)
        if specs is None or handlers is None:
            sys.modules.pop(module_name, None)
            raise ToolRegistryError(
                "Hotload module must export TOOL_SPECS and TOOL_HANDLERS"
            )
        declared_raw = getattr(module, "TOOL_OVERRIDES", ())
        if not isinstance(declared_raw, (list, tuple, set, frozenset)):
            sys.modules.pop(module_name, None)
            raise ToolRegistryError("TOOL_OVERRIDES must be a sequence")
        declared: set[str] = set()
        for name in declared_raw:
            if not isinstance(name, str) or not name or name in declared:
                sys.modules.pop(module_name, None)
                raise ToolRegistryError(f"Invalid tool override name: {name!r}")
            declared.add(name)
        handler_names = set(handlers) if isinstance(handlers, Mapping) else set()
        if not declared.issubset(handler_names):
            sys.modules.pop(module_name, None)
            raise ToolRegistryError(
                "TOOL_OVERRIDES must name handlers exported by this module"
            )
        active = self.registry.snapshot()
        external_conflicts = {
            name
            for name in handler_names
            if name in active.sources and active.sources[name] != source_id
        }
        current = self.registry.source(source_id)
        if declared and not allow_overrides:
            sys.modules.pop(module_name, None)
            raise ToolRegistryError(
                "TOOL_OVERRIDES requires explicit activation authorization"
            )
        if external_conflicts != declared and not (
            current is not None
            and current.allow_overrides
            and not external_conflicts
            and declared
        ):
            sys.modules.pop(module_name, None)
            missing = sorted(external_conflicts - declared)
            stale = sorted(declared - external_conflicts)
            raise ToolRegistryError(
                f"Tool overrides must exactly match active conflicts (missing={missing}, stale={stale})"
            )
        version = str(getattr(module, "TOOL_VERSION", digest[:12]))
        try:
            snapshot = self.registry.replace_source(
                source_id,
                specs,
                handlers,
                version=version,
                allow_overrides=bool(declared),
                module_name=module_name,
                digest=digest,
            )
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        # Active handlers and rollback snapshots retain exactly the module
        # globals they need. Keeping every content-addressed name in
        # sys.modules would otherwise leak one entry per reload.
        sys.modules.pop(module_name, None)
        return {
            "source_id": source_id,
            "version": version,
            "digest": digest,
            "generation": snapshot.generation,
            "tools": sorted(handlers),
            "overrides": sorted(declared),
        }

    def unload(self, relative_path: str | Path) -> dict[str, Any]:
        source_id = self.source_id(relative_path)
        return self.unload_source(source_id)

    def unload_source(self, source_id: str) -> dict[str, Any]:
        source_id = self._validated_source_id(source_id)
        snapshot = self.registry.unload_source(source_id)
        return {"source_id": source_id, "generation": snapshot.generation}

    def rollback(self, relative_path: str | Path) -> dict[str, Any]:
        source_id = self.source_id(relative_path)
        return self.rollback_source(source_id)

    def rollback_source(self, source_id: str) -> dict[str, Any]:
        source_id = self._validated_source_id(source_id)
        snapshot = self.registry.rollback_source(source_id)
        return {"source_id": source_id, "generation": snapshot.generation}

    def source_id(self, relative_path: str | Path) -> str:
        path = self._resolve(relative_path)
        relative = path.relative_to(self.root).as_posix()
        return f"hotload:{relative}"

    @staticmethod
    def _validated_source_id(source_id: str) -> str:
        if not source_id.startswith("hotload:"):
            raise ToolRegistryError("Hotloaded source IDs must start with 'hotload:'")
        logical = source_id.removeprefix("hotload:")
        if not logical or "\\" in logical or Path(logical).is_absolute() or ".." in Path(logical).parts:
            raise ToolRegistryError("Invalid hotloaded source ID")
        return source_id

    def _resolve(self, relative_path: str | Path) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise ToolRegistryError("Tool module path must be relative to the configured root")
        candidate = self.root.joinpath(raw)
        if candidate.is_symlink():
            raise ToolRegistryError("Symlinked tool modules are not accepted")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolRegistryError("Tool module path escapes the configured root") from exc
        if resolved.suffix.lower() != ".py" or not resolved.is_file():
            raise ToolRegistryError("Tool module must be an existing .py file")
        return resolved

    @staticmethod
    def _import_module(module_name: str, path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ToolRegistryError(f"Unable to import tool module: {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module
