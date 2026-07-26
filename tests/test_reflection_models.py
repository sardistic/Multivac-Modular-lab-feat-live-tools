import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.reflection_models import ReflectionModels
from services.reflection_store import ReflectionStore


class ReflectionModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = str(Path(self.tempdir.name) / "reflection.db")
        self.env = patch.dict(
            "os.environ",
            {
                "REFLECTION_DB_PATH": db_path,
                "REFLECTION_DAILY_BUDGET_USD": "1.50",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ReflectionStore()

    async def test_extract_uses_flex_structured_output_and_settles_budget(self):
        response = SimpleNamespace(
            model="gpt-5.4-nano",
            service_tier="flex",
            output_text=json.dumps({"useful": False, "insights": []}),
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        models = ReflectionModels(self.store)

        with patch(
            "services.reflection_models.get_openai_client", return_value=client
        ), patch("services.reflection_models.usage_costs.record") as record:
            result = await models.extract(
                [
                    {"message_id": "1", "role": "requester", "content": "Help"},
                    {"message_id": "2", "role": "assistant", "content": "Done"},
                ]
            )

        self.assertEqual(result, {"useful": False, "insights": []})
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["service_tier"], "flex")
        self.assertFalse(kwargs["store"])
        self.assertEqual(kwargs["reasoning"], {"effort": "low"})
        self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
        self.assertTrue(kwargs["text"]["format"]["strict"])
        record.assert_called_once()
        budget = self.store.budget_status(models.daily_cap)
        self.assertGreater(budget["spent"], 0)
        self.assertEqual(budget["reserved"], 0)

    async def test_zero_budget_fails_before_provider_call(self):
        with patch.dict("os.environ", {"REFLECTION_DAILY_BUDGET_USD": "0"}):
            models = ReflectionModels(self.store)
        create = AsyncMock()
        client = SimpleNamespace(responses=SimpleNamespace(create=create))

        with patch(
            "services.reflection_models.get_openai_client", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
                await models.extract(
                    [
                        {"message_id": "1", "role": "requester", "content": "Help"},
                        {"message_id": "2", "role": "assistant", "content": "Done"},
                    ]
                )

        create.assert_not_awaited()

    async def test_message_pulse_uses_tiny_low_effort_flex_call(self):
        response = SimpleNamespace(
            model="gpt-5.4-nano",
            service_tier="flex",
            output_text=json.dumps(
                {
                    "useful": False,
                    "kind": "behavior_pattern",
                    "summary": "",
                    "confidence": 0,
                }
            ),
            usage={"input_tokens": 80, "output_tokens": 20},
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        models = ReflectionModels(self.store)

        with patch(
            "services.reflection_models.get_openai_client", return_value=client
        ), patch("services.reflection_models.usage_costs.record"):
            result = await models.pulse(
                {
                    "message_id": "1",
                    "role": "participant",
                    "content": "ordinary conversation",
                }
            )

        self.assertFalse(result["useful"])
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["model"], models.extract_model)
        self.assertEqual(kwargs["max_output_tokens"], 220)
        self.assertEqual(kwargs["reasoning"], {"effort": "low"})
        self.assertEqual(kwargs["service_tier"], "flex")
        self.assertFalse(kwargs["store"])

    async def test_global_usage_log_failure_does_not_release_charged_budget(self):
        response = SimpleNamespace(
            model="gpt-5.4-nano",
            service_tier="flex",
            output_text=json.dumps({"useful": False, "insights": []}),
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(return_value=response))
        )
        models = ReflectionModels(self.store)

        with patch(
            "services.reflection_models.get_openai_client", return_value=client
        ), patch(
            "services.reflection_models.usage_costs.record",
            side_effect=OSError("usage database unavailable"),
        ):
            result = await models.extract(
                [
                    {"message_id": "1", "role": "requester", "content": "Help"},
                    {"message_id": "2", "role": "assistant", "content": "Done"},
                ]
            )

        self.assertFalse(result["useful"])
        budget = self.store.budget_status(models.daily_cap)
        self.assertGreater(budget["spent"], 0)
        self.assertEqual(budget["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
