"""Atomic lifecycle manager for trusted, hotloaded Discord Cog modules."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import discord
from discord.ext import commands


MAX_COMMAND_MODULE_BYTES = 256_000


class CommandRuntimeError(ValueError):
    pass


@dataclass(frozen=True)
class CommandBundle:
    source_id: str
    version: str
    digest: str
    relative_path: str
    module: ModuleType
    cog_names: tuple[str, ...]
    displaced_commands: tuple[commands.Command, ...]


class CommandModuleLoader:
    """Loads standalone extension modules that add one or more Cogs."""

    def __init__(
        self,
        bot: commands.Bot,
        root: str | Path,
        *,
        history_limit: int = 5,
    ):
        self.bot = bot
        self.root = Path(root).resolve()
        self._history_limit = max(1, int(history_limit))
        self._active: dict[str, CommandBundle] = {}
        self._history: dict[str, deque[CommandBundle | None]] = {}
        self._source_order: list[str] = []
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

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
        if len(payload) > MAX_COMMAND_MODULE_BYTES:
            raise CommandRuntimeError(
                f"Command module exceeds {MAX_COMMAND_MODULE_BYTES:,} bytes: {path.name}"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if expected_digest and digest.lower() != expected_digest.lower():
            raise CommandRuntimeError(
                f"Command module digest mismatch for {path.name}: expected {expected_digest}, got {digest}"
            )
        logical_source = self._validated_source_id(
            source_id or self.source_id(relative_path)
        )
        module_name = f"_multivac_live_command_{digest}"
        module = self._import_module(module_name, path)
        setup = getattr(module, "setup", None)
        if not inspect.iscoroutinefunction(setup):
            sys.modules.pop(module_name, None)
            raise CommandRuntimeError("Live command module must export async setup(bot)")
        teardown = getattr(module, "teardown", None)
        if teardown is not None and not inspect.iscoroutinefunction(teardown):
            sys.modules.pop(module_name, None)
            raise CommandRuntimeError("Live command teardown(bot) must be async")
        override_names = self._override_names(module)
        if override_names and not allow_overrides:
            sys.modules.pop(module_name, None)
            raise CommandRuntimeError(
                "COMMAND_OVERRIDES requires explicit activation authorization"
            )

        previous = self._active.get(logical_source)
        if previous is not None:
            await self._remove_bundle(previous)
        displaced: tuple[commands.Command, ...] = ()
        try:
            displaced = self._displace_commands(override_names)
            cog_names = await self._install_module(module)
        except Exception:
            self._restore_displaced(displaced)
            if previous is not None:
                await self._restore_bundle(previous)
            sys.modules.pop(module_name, None)
            raise

        version = str(getattr(module, "COMMAND_VERSION", digest[:12]))
        bundle = CommandBundle(
            source_id=logical_source,
            version=version,
            digest=digest,
            relative_path=str(relative_path).replace("\\", "/"),
            module=module,
            cog_names=cog_names,
            displaced_commands=displaced,
        )
        history = self._history.setdefault(
            logical_source, deque(maxlen=self._history_limit)
        )
        history.append(previous)
        if logical_source not in self._source_order:
            self._source_order.append(logical_source)
        self._active[logical_source] = bundle
        self._generation += 1
        sys.modules.pop(module_name, None)
        return self._result(bundle)

    async def unload_source(self, source_id: str) -> dict[str, Any]:
        source_id = self._validated_source_id(source_id)
        bundle = self._active.get(source_id)
        if bundle is None:
            raise CommandRuntimeError(f"Command source is not active: {source_id}")
        await self._remove_bundle(bundle)
        history = self._history.setdefault(
            source_id, deque(maxlen=self._history_limit)
        )
        history.append(bundle)
        self._active.pop(source_id, None)
        self._generation += 1
        return {"source_id": source_id, "generation": self._generation}

    async def rollback_source(self, source_id: str) -> dict[str, Any]:
        source_id = self._validated_source_id(source_id)
        history = self._history.get(source_id)
        if not history:
            raise CommandRuntimeError(f"No command rollback version exists for: {source_id}")
        current = self._active.get(source_id)
        previous = history.pop()
        if current is not None:
            await self._remove_bundle(current)
        try:
            if previous is None:
                self._active.pop(source_id, None)
            else:
                await self._restore_bundle(previous)
                self._active[source_id] = previous
        except Exception:
            if current is not None:
                await self._restore_bundle(current)
                self._active[source_id] = current
            history.append(previous)
            raise
        self._generation += 1
        return {
            "source_id": source_id,
            "generation": self._generation,
            "version": previous.version if previous else None,
        }

    def status(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "sources": [
                {
                    "source_id": source_id,
                    "active": source_id in self._active,
                    "version": self._active[source_id].version
                    if source_id in self._active
                    else None,
                    "cogs": list(self._active[source_id].cog_names)
                    if source_id in self._active
                    else [],
                    "overrides": [
                        command.name
                        for command in self._active[source_id].displaced_commands
                    ]
                    if source_id in self._active
                    else [],
                    "history": len(self._history.get(source_id, ())),
                }
                for source_id in self._source_order
            ],
        }

    def source_id(self, relative_path: str | Path) -> str:
        path = self._resolve(relative_path)
        return f"hotcommand:{path.relative_to(self.root).as_posix()}"

    async def _install_module(self, module: ModuleType) -> tuple[str, ...]:
        before_cogs = set(self.bot.cogs)
        before_commands = set(self.bot.all_commands)
        before_tree = self._tree_commands()
        try:
            await module.setup(self.bot)
            new_cogs = tuple(sorted(set(self.bot.cogs) - before_cogs))
            if not new_cogs:
                raise CommandRuntimeError("Live command setup must add at least one Cog")
            for name in set(self.bot.all_commands) - before_commands:
                command = self.bot.all_commands[name]
                if command.cog is None or command.cog.qualified_name not in new_cogs:
                    raise CommandRuntimeError(
                        "Live command setup may add commands only through its new Cogs"
                    )
            for key, command in self._tree_commands().items():
                if key in before_tree:
                    continue
                binding = getattr(command, "binding", None)
                if binding is None or binding.qualified_name not in new_cogs:
                    raise CommandRuntimeError(
                        "Live command setup may add app commands only through its new Cogs"
                    )
            return new_cogs
        except Exception:
            await self._cleanup_additions(before_cogs, before_commands, before_tree)
            raise

    async def _restore_bundle(self, bundle: CommandBundle) -> None:
        displaced: tuple[commands.Command, ...] = ()
        try:
            displaced = self._displace_commands(
                tuple(command.name for command in bundle.displaced_commands)
            )
            cog_names = await self._install_module(bundle.module)
            if set(cog_names) != set(bundle.cog_names):
                await self._remove_cogs(cog_names)
                raise CommandRuntimeError("Restored command module changed its Cog contract")
        except Exception:
            self._restore_displaced(displaced)
            raise

    async def _remove_bundle(self, bundle: CommandBundle) -> None:
        await self._remove_cogs(bundle.cog_names)
        teardown = getattr(bundle.module, "teardown", None)
        if teardown is not None:
            await teardown(self.bot)
        self._restore_displaced(bundle.displaced_commands)

    @staticmethod
    def _override_names(module: ModuleType) -> tuple[str, ...]:
        raw = getattr(module, "COMMAND_OVERRIDES", ())
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise CommandRuntimeError("COMMAND_OVERRIDES must be a sequence")
        names: list[str] = []
        for name in raw:
            if not isinstance(name, str) or not name or name in names:
                raise CommandRuntimeError(f"Invalid command override name: {name!r}")
            names.append(name)
        return tuple(names)

    def _displace_commands(
        self, names: tuple[str, ...]
    ) -> tuple[commands.Command, ...]:
        displaced: list[commands.Command] = []
        try:
            for name in names:
                command = self.bot.get_command(name)
                if command is None:
                    raise CommandRuntimeError(f"Declared command override is not active: {name}")
                if command.name != name:
                    raise CommandRuntimeError(f"Command aliases cannot be overridden directly: {name}")
                if command.cog is not None:
                    raise CommandRuntimeError(
                        f"Commands owned by another Cog cannot be overridden: {name}"
                    )
                removed = self.bot.remove_command(name)
                if removed is None:
                    raise CommandRuntimeError(f"Unable to displace command: {name}")
                displaced.append(removed)
            return tuple(displaced)
        except Exception:
            self._restore_displaced(tuple(displaced))
            raise

    def _restore_displaced(self, displaced: tuple[commands.Command, ...]) -> None:
        for command in displaced:
            if self.bot.get_command(command.name) is None:
                self.bot.add_command(command)

    async def _remove_cogs(self, cog_names: tuple[str, ...]) -> None:
        for cog_name in reversed(cog_names):
            if self.bot.get_cog(cog_name) is not None:
                await self.bot.remove_cog(cog_name)

    async def _cleanup_additions(
        self,
        before_cogs: set[str],
        before_commands: set[str],
        before_tree: dict[tuple[str, Any], Any],
    ) -> None:
        await self._remove_cogs(tuple(set(self.bot.cogs) - before_cogs))
        for name in set(self.bot.all_commands) - before_commands:
            self.bot.remove_command(name)
        for key, command in self._tree_commands().items():
            if key not in before_tree:
                self.bot.tree.remove_command(
                    command.name,
                    type=getattr(command, "type", discord.AppCommandType.chat_input),
                )

    def _tree_commands(self) -> dict[tuple[str, Any], Any]:
        return {
            (
                command.name,
                getattr(command, "type", discord.AppCommandType.chat_input),
            ): command
            for command in self.bot.tree.get_commands()
        }

    def _resolve(self, relative_path: str | Path) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise CommandRuntimeError("Command module path must be relative")
        candidate = self.root / raw
        if candidate.is_symlink():
            raise CommandRuntimeError("Symlinked command modules are not accepted")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise CommandRuntimeError("Command module path escapes the configured root") from exc
        if resolved.suffix.lower() != ".py" or not resolved.is_file():
            raise CommandRuntimeError("Command module must be an existing .py file")
        return resolved

    @staticmethod
    def _validated_source_id(source_id: str) -> str:
        if not source_id.startswith("hotcommand:"):
            raise CommandRuntimeError("Command source IDs must start with 'hotcommand:'")
        logical = source_id.removeprefix("hotcommand:")
        path = Path(logical)
        if not logical or "\\" in logical or path.is_absolute() or ".." in path.parts:
            raise CommandRuntimeError("Invalid command source ID")
        return source_id

    @staticmethod
    def _import_module(module_name: str, path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise CommandRuntimeError(f"Unable to import command module: {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    def _result(self, bundle: CommandBundle) -> dict[str, Any]:
        commands_added = sorted(
            {
                command.qualified_name
                for cog_name in bundle.cog_names
                for command in self.bot.get_cog(cog_name).get_commands()
            }
        )
        return {
            "source_id": bundle.source_id,
            "version": bundle.version,
            "digest": bundle.digest,
            "generation": self._generation,
            "cogs": list(bundle.cog_names),
            "commands": commands_added,
            "overrides": [command.name for command in bundle.displaced_commands],
        }


__all__ = ["CommandModuleLoader", "CommandRuntimeError"]
