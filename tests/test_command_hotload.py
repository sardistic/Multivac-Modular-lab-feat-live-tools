import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from services.command_control import CommandControlWorker
from services.command_runtime import CommandModuleLoader


def _module_source(version: str, value: str, command_name: str = "livehello") -> str:
    return f'''from discord.ext import commands

COMMAND_VERSION = {version!r}

class LiveCommandCog(commands.Cog):
    @commands.hybrid_command(name={command_name!r}, description="hot command")
    async def live_command(self, ctx):
        return {value!r}

async def setup(bot):
    await bot.add_cog(LiveCommandCog())
'''


def _bot() -> commands.Bot:
    return commands.Bot(command_prefix="!", intents=discord.Intents.none())


class CommandModuleLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_and_rollback_replace_cog_atomically(self):
        bot = _bot()
        self.addAsyncCleanup(bot.close)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "command.py"
            module.write_text(_module_source("1", "first"), encoding="utf-8")
            loader = CommandModuleLoader(bot, root)

            first = await loader.activate("command.py")
            old_command = bot.get_command("livehello")
            old_cog = old_command.cog
            self.assertEqual(first["version"], "1")
            self.assertEqual(await old_command.callback(old_cog, None), "first")

            module.write_text(_module_source("2", "second"), encoding="utf-8")
            second = await loader.activate("command.py")
            new_command = bot.get_command("livehello")
            self.assertEqual(second["version"], "2")
            self.assertEqual(await new_command.callback(new_command.cog, None), "second")
            self.assertEqual(await old_command.callback(old_cog, None), "first")

            await loader.rollback_source(first["source_id"])
            restored = bot.get_command("livehello")
            self.assertEqual(await restored.callback(restored.cog, None), "first")

    async def test_existing_command_name_cannot_be_overridden(self):
        bot = _bot()
        self.addAsyncCleanup(bot.close)

        async def existing(_ctx):
            return "existing"

        bot.add_command(commands.Command(existing, name="livehello"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "conflict.py").write_text(
                _module_source("1", "conflict"), encoding="utf-8"
            )
            loader = CommandModuleLoader(bot, root)
            with self.assertRaises(commands.CommandRegistrationError):
                await loader.activate("conflict.py")

        self.assertIs(bot.get_command("livehello").callback, existing)
        self.assertIsNone(bot.get_cog("LiveCommandCog"))

    async def test_declared_builtin_override_restores_original_on_unload(self):
        bot = _bot()
        self.addAsyncCleanup(bot.close)

        async def existing(_ctx):
            return "builtin"

        original = commands.Command(existing, name="livehello")
        bot.add_command(original)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "override.py"
            module.write_text(
                "COMMAND_OVERRIDES = ('livehello',)\n" + _module_source("1", "live"),
                encoding="utf-8",
            )
            loader = CommandModuleLoader(bot, root)
            with self.assertRaisesRegex(Exception, "explicit activation authorization"):
                await loader.activate("override.py")
            self.assertIs(bot.get_command("livehello"), original)

            loaded = await loader.activate("override.py", allow_overrides=True)
            live = bot.get_command("livehello")
            self.assertIsNot(live, original)
            self.assertEqual(await live.callback(live.cog, None), "live")
            self.assertEqual(loaded["overrides"], ["livehello"])

            await loader.unload_source(loaded["source_id"])
            self.assertIs(bot.get_command("livehello"), original)


class CommandControlWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_persists_and_restores_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = base / "artifacts"
            control = base / "control"
            module = artifacts / "proposal-1" / "live_commands" / "hello.py"
            module.parent.mkdir(parents=True)
            payload = _module_source("1", "persisted").encode()
            module.write_bytes(payload)
            operation = {
                "action": "activate",
                "source_id": "hotcommand:live_commands/hello.py",
                "relative_path": "proposal-1/live_commands/hello.py",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "proposal_id": 1,
            }
            control.mkdir()
            (control / "command-request.json").write_text(
                json.dumps(
                    {"version": 1, "request_id": "command-1", "operations": [operation]}
                ),
                encoding="utf-8",
            )

            bot = _bot()
            self.addAsyncCleanup(bot.close)
            worker = CommandControlWorker(CommandModuleLoader(bot, artifacts), control)
            with patch.object(bot.tree, "sync", AsyncMock(return_value=[])) as sync:
                result = await worker.process_once()
            self.assertTrue(result["ok"])
            sync.assert_awaited_once()
            command = bot.get_command("livehello")
            self.assertEqual(await command.callback(command.cog, None), "persisted")

            restarted = _bot()
            self.addAsyncCleanup(restarted.close)
            restored_worker = CommandControlWorker(
                CommandModuleLoader(restarted, artifacts), control
            )
            with patch.object(restarted.tree, "sync", AsyncMock(return_value=[])):
                restored = await restored_worker.restore_active()
            self.assertEqual(restored["errors"], [])
            self.assertEqual(
                restored["restored"], ["hotcommand:live_commands/hello.py"]
            )
            command = restarted.get_command("livehello")
            self.assertEqual(await command.callback(command.cog, None), "persisted")

    async def test_tree_sync_failure_restores_previous_command_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = base / "artifacts"
            control = base / "control"
            old_module = artifacts / "old" / "live_commands" / "hello.py"
            new_module = artifacts / "new" / "live_commands" / "hello.py"
            old_module.parent.mkdir(parents=True)
            new_module.parent.mkdir(parents=True)
            old_payload = _module_source("1", "old").encode()
            new_payload = _module_source("2", "new").encode()
            old_module.write_bytes(old_payload)
            new_module.write_bytes(new_payload)
            source_id = "hotcommand:live_commands/hello.py"
            control.mkdir()

            def request(request_id: str, relative_path: str, payload: bytes) -> None:
                (control / "command-request.json").write_text(
                    json.dumps({
                        "version": 1,
                        "request_id": request_id,
                        "operations": [{
                            "action": "activate",
                            "source_id": source_id,
                            "relative_path": relative_path,
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }],
                    }),
                    encoding="utf-8",
                )

            bot = _bot()
            self.addAsyncCleanup(bot.close)
            worker = CommandControlWorker(CommandModuleLoader(bot, artifacts), control)
            request("initial", "old/live_commands/hello.py", old_payload)
            with patch.object(bot.tree, "sync", AsyncMock(return_value=[])):
                self.assertTrue((await worker.process_once())["ok"])

            request("update", "new/live_commands/hello.py", new_payload)
            with patch.object(
                bot.tree,
                "sync",
                AsyncMock(side_effect=[RuntimeError("Discord sync failed"), []]),
            ):
                result = await worker.process_once()
            self.assertFalse(result["ok"])
            command = bot.get_command("livehello")
            self.assertEqual(await command.callback(command.cog, None), "old")
            active = json.loads((control / "active-commands.json").read_text())
            self.assertEqual(
                active["sources"][source_id]["relative_path"],
                "old/live_commands/hello.py",
            )


if __name__ == "__main__":
    unittest.main()
