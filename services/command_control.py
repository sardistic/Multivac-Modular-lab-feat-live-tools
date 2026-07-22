"""Persistent supervisor control channel for live Discord command Cogs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from services.command_runtime import CommandModuleLoader, CommandRuntimeError


CONTROL_VERSION = 1
MAX_CONTROL_BYTES = 256_000
MAX_OPERATIONS = 20


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_CONTROL_BYTES:
        raise CommandRuntimeError(f"Command control file is too large: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CommandRuntimeError("Command control file must contain an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


class CommandControlWorker:
    def __init__(self, loader: CommandModuleLoader, control_dir: str | Path):
        self.loader = loader
        self.control_dir = Path(control_dir)
        self.request_path = self.control_dir / "command-request.json"
        self.result_path = self.control_dir / "command-result.json"
        self.active_path = self.control_dir / "active-commands.json"

    def active_state(self) -> dict[str, Any]:
        state = _read_json(self.active_path)
        if state is None:
            return {"version": CONTROL_VERSION, "sources": {}}
        if state.get("version") != CONTROL_VERSION or not isinstance(
            state.get("sources"), dict
        ):
            raise CommandRuntimeError("Invalid active command state")
        return state

    async def restore_active(self) -> dict[str, Any]:
        state = self.active_state()
        restored = []
        errors = []
        for source_id, record in sorted(state["sources"].items()):
            try:
                await self._activate(source_id, record)
                restored.append(source_id)
            except Exception as exc:
                errors.append(f"{source_id}: {type(exc).__name__}: {exc}")
        if restored:
            try:
                await self.loader.bot.tree.sync()
            except Exception as exc:
                errors.append(f"tree sync: {type(exc).__name__}: {exc}")
        return {"restored": restored, "errors": errors}

    async def process_once(self) -> dict[str, Any] | None:
        request = _read_json(self.request_path)
        if request is None:
            return None
        request_id = request.get("request_id")
        operations = request.get("operations")
        if request.get("version") != CONTROL_VERSION or not isinstance(request_id, str):
            return self._failure(str(request_id or "invalid"), "Invalid command request")
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
            return self._failure(request_id, "Command request has invalid operations")

        prior = _read_json(self.result_path)
        if prior and prior.get("request_id") == request_id:
            return prior

        active = self.active_state()
        updated_sources = dict(active["sources"])
        mutated: list[str] = []
        summaries = []
        try:
            for operation in operations:
                if not isinstance(operation, dict):
                    raise CommandRuntimeError("Command operation must be an object")
                action = operation.get("action")
                source_id = self._source_id(operation.get("source_id"))
                if action == "activate":
                    result = await self._activate(source_id, operation)
                    updated_sources[source_id] = {
                        "relative_path": operation["relative_path"],
                        "sha256": operation["sha256"],
                        "proposal_id": operation.get("proposal_id"),
                    }
                elif action == "unload":
                    result = await self.loader.unload_source(source_id)
                    updated_sources.pop(source_id, None)
                else:
                    raise CommandRuntimeError(f"Unsupported command operation: {action!r}")
                mutated.append(source_id)
                summaries.append({"action": action, **result})

            synced = await self.loader.bot.tree.sync()
            _write_json(
                self.active_path,
                {"version": CONTROL_VERSION, "sources": updated_sources},
            )
            result = {
                "version": CONTROL_VERSION,
                "request_id": request_id,
                "ok": True,
                "generation": self.loader.generation,
                "synced_commands": len(synced),
                "operations": summaries,
            }
            _write_json(self.result_path, result)
            return result
        except Exception as exc:
            rollback_errors = []
            for source_id in reversed(mutated):
                try:
                    await self.loader.rollback_source(source_id)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{source_id}: {rollback_exc}")
            try:
                await self.loader.bot.tree.sync()
            except Exception as sync_exc:
                rollback_errors.append(f"tree sync: {sync_exc}")
            detail = f"{type(exc).__name__}: {exc}"
            if rollback_errors:
                detail += "; rollback errors: " + "; ".join(rollback_errors)
            return self._failure(request_id, detail)

    async def _activate(self, source_id: str, record: dict[str, Any]) -> dict[str, Any]:
        source_id = self._source_id(source_id)
        relative_path = record.get("relative_path")
        digest = record.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(digest, str):
            raise CommandRuntimeError("Activation requires relative_path and sha256")
        return await self.loader.activate(
            relative_path,
            source_id=source_id,
            expected_digest=digest,
            allow_overrides=True,
        )

    @staticmethod
    def _source_id(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith(
            "hotcommand:live_commands/"
        ):
            raise CommandRuntimeError(
                "Supervisor command source must be beneath live_commands/"
            )
        logical = value.removeprefix("hotcommand:")
        path = Path(logical)
        if path.suffix.lower() != ".py" or ".." in path.parts or path.name == "__init__.py":
            raise CommandRuntimeError("Invalid supervisor command source")
        return value

    def _failure(self, request_id: str, detail: str) -> dict[str, Any]:
        result = {
            "version": CONTROL_VERSION,
            "request_id": request_id,
            "ok": False,
            "detail": detail[:3000],
            "generation": self.loader.generation,
        }
        _write_json(self.result_path, result)
        return result


__all__ = ["CommandControlWorker"]
