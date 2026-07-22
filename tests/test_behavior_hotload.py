import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from services.behavior_control import BehaviorControlWorker
from services.behavior_registry import dispatch_event, invoke_provider
from services.behavior_runtime import (
    BehaviorModuleLoader,
    BehaviorRegistry,
    BehaviorRuntimeError,
)


def _component(version: str, value: str, *, route: str = "chat", kind: str = "intents") -> str:
    return f'''import asyncio

BEHAVIOR_VERSION = {version!r}

async def handler(*args, **kwargs):
    return {value!r}

BEHAVIOR_HANDLERS = {{{kind!r}: {{{route!r}: handler}}}}

async def setup(context):
    context.state = {value!r}
    return context.state

async def healthcheck(context):
    return context.state == {value!r}

async def teardown(context):
    context.state = "closed"
'''


class BehaviorRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_are_immutable_and_generation_scoped(self):
        registry = BehaviorRegistry()
        first = registry.replace_source(
            "behavior:settings.py",
            {"settings": {"intent.model.chat": "model-a", "nested": {"x": 1}}},
            version="1",
        )
        registry.replace_source(
            "behavior:settings.py",
            {"settings": {"intent.model.chat": "model-b"}},
            version="2",
        )
        self.assertEqual(first.get("settings", "intent.model.chat"), "model-a")
        self.assertEqual(
            registry.snapshot().get("settings", "intent.model.chat"), "model-b"
        )
        with self.assertRaises(TypeError):
            first.get("settings", "nested")["x"] = 2

    async def test_override_requires_declaration_and_authorization(self):
        async def first(_ctx):
            return "first"

        async def second(_ctx):
            return "second"

        registry = BehaviorRegistry()
        registry.replace_source(
            "behavior:first.py", {"intents": {"chat": first}}, version="1"
        )
        with self.assertRaises(BehaviorRuntimeError):
            registry.replace_source(
                "behavior:second.py", {"intents": {"chat": second}}, version="1"
            )
        with self.assertRaises(BehaviorRuntimeError):
            registry.replace_source(
                "behavior:second.py",
                {"intents": {"chat": second}},
                overrides={"intents": ["chat"]},
                version="1",
            )
        snapshot = registry.replace_source(
            "behavior:second.py",
            {"intents": {"chat": second}},
            overrides={"intents": ["chat"]},
            allow_overrides=True,
            version="1",
        )
        self.assertEqual(
            await registry.invoke("intents", "chat", None, snapshot=snapshot),
            "second",
        )

    async def test_request_snapshot_drains_before_old_teardown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "component.py"
            module.write_text(
                '''import asyncio
release = asyncio.Event()
closed = False
async def handler(_ctx):
    await release.wait()
    return "old"
BEHAVIOR_HANDLERS = {"intents": {"chat": handler}}
async def setup(context): return None
async def teardown(context):
    global closed
    closed = True
''',
                encoding="utf-8",
            )
            registry = BehaviorRegistry()
            loader = BehaviorModuleLoader(registry, root, drain_timeout=2)
            await loader.activate("component.py")
            old_bundle = loader._active["behavior:component.py"]
            old_snapshot = registry.snapshot()
            old_call = asyncio.create_task(
                registry.invoke("intents", "chat", None, snapshot=old_snapshot)
            )
            await asyncio.sleep(0)

            module.write_text(_component("2", "new"), encoding="utf-8")
            activation = asyncio.create_task(loader.activate("component.py"))
            for _ in range(100):
                if registry.snapshot().get("intents", "chat") is not old_snapshot.get("intents", "chat"):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(await registry.invoke("intents", "chat", None), "new")
            self.assertFalse(old_bundle.module.closed)
            old_bundle.module.release.set()
            self.assertEqual(await old_call, "old")
            await activation
            self.assertTrue(old_bundle.module.closed)


class BehaviorLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_and_rollback_restore_prior_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "component.py"
            module.write_text(_component("1", "first"), encoding="utf-8")
            registry = BehaviorRegistry()
            loader = BehaviorModuleLoader(registry, root, drain_timeout=1)
            first = await loader.activate("component.py")
            self.assertEqual(await registry.invoke("intents", "chat", None), "first")

            module.write_text(_component("2", "second"), encoding="utf-8")
            second = await loader.activate("component.py")
            self.assertGreater(second["generation"], first["generation"])
            self.assertEqual(await registry.invoke("intents", "chat", None), "second")

            await loader.rollback_source(first["source_id"])
            self.assertEqual(await registry.invoke("intents", "chat", None), "first")

    async def test_owned_task_receives_stop_and_is_drained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "task.py"
            module.write_text(
                '''stopped = False
async def handler(_ctx): return "ok"
BEHAVIOR_HANDLERS = {"intents": {"task": handler}}
async def setup(context):
    async def worker():
        global stopped
        await context.stop_event.wait()
        stopped = True
    context.create_task(worker(), name="owned-worker")
async def teardown(context): pass
''',
                encoding="utf-8",
            )
            registry = BehaviorRegistry()
            loader = BehaviorModuleLoader(registry, root, drain_timeout=1)
            loaded = await loader.activate("task.py")
            bundle = loader._active[loaded["source_id"]]
            await loader.unload_source(loaded["source_id"])
            self.assertTrue(bundle.module.stopped)


class BehaviorControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_persists_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = base / "artifacts"
            control = base / "control"
            module = artifacts / "proposal-1" / "live_components" / "chat.py"
            module.parent.mkdir(parents=True)
            control.mkdir()
            payload = _component("1", "persisted").encode()
            module.write_bytes(payload)
            source_id = "hotbehavior:live_components/chat.py"
            operation = {
                "action": "activate",
                "source_id": source_id,
                "relative_path": "proposal-1/live_components/chat.py",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            (control / "behavior-request.json").write_text(
                json.dumps({"version": 1, "request_id": "behavior-1", "operations": [operation]}),
                encoding="utf-8",
            )
            registry = BehaviorRegistry()
            worker = BehaviorControlWorker(
                BehaviorModuleLoader(registry, artifacts, drain_timeout=1), control
            )
            result = await worker.process_once()
            self.assertTrue(result["ok"])
            self.assertEqual(await registry.invoke("intents", "chat", None), "persisted")

            restored_registry = BehaviorRegistry()
            restored_worker = BehaviorControlWorker(
                BehaviorModuleLoader(restored_registry, artifacts, drain_timeout=1), control
            )
            restored = await restored_worker.restore_active()
            self.assertEqual(restored["errors"], [])
            self.assertEqual(restored["restored"], [source_id])
            self.assertEqual(
                await restored_registry.invoke("intents", "chat", None), "persisted"
            )

    async def test_failed_batch_rolls_back_prior_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = base / "artifacts"
            control = base / "control"
            good = artifacts / "proposal" / "live_components" / "good.py"
            bad = artifacts / "proposal" / "live_components" / "bad.py"
            good.parent.mkdir(parents=True)
            control.mkdir()
            good_payload = _component("1", "temporary", route="one").encode()
            bad_payload = b"BEHAVIOR_HANDLERS = {}\n"
            good.write_bytes(good_payload)
            bad.write_bytes(bad_payload)
            operations = []
            for name, payload in (("good", good_payload), ("bad", bad_payload)):
                operations.append({
                    "action": "activate",
                    "source_id": f"hotbehavior:live_components/{name}.py",
                    "relative_path": f"proposal/live_components/{name}.py",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            (control / "behavior-request.json").write_text(
                json.dumps({"version": 1, "request_id": "failed", "operations": operations}),
                encoding="utf-8",
            )
            registry = BehaviorRegistry()
            worker = BehaviorControlWorker(
                BehaviorModuleLoader(registry, artifacts, drain_timeout=1), control
            )
            result = await worker.process_once()
            self.assertFalse(result["ok"])
            self.assertIsNone(registry.snapshot().get("intents", "one"))
            self.assertFalse((control / "active-behaviors.json").exists())


class BehaviorDispatchHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_and_provider_helpers_use_fallbacks(self):
        async def fallback(value):
            return f"fallback:{value}"

        self.assertEqual(await dispatch_event("unregistered", fallback, "x"), "fallback:x")
        self.assertEqual(await invoke_provider("unregistered", fallback, "x"), "fallback:x")


if __name__ == "__main__":
    unittest.main()
