import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers import claude_utils


class ClaudeFableConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_route_uses_fable(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello")],
            stop_reason="end_turn",
            usage=None,
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(messages=SimpleNamespace(create=create))

        with patch.object(claude_utils, "ANTHROPIC_API_KEY", "test-key"), patch.object(
            claude_utils.anthropic, "AsyncAnthropic", return_value=client
        ):
            result = await claude_utils.generate_claude_response(
                [{"role": "user", "content": "hello"}]
            )

        self.assertEqual(result, "hello")
        self.assertEqual(create.await_args.kwargs["model"], "claude-fable-5")
        self.assertNotIn("temperature", create.await_args.kwargs)

    async def test_legacy_claude_model_keeps_temperature(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello")],
            stop_reason="end_turn",
            usage=None,
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(messages=SimpleNamespace(create=create))

        with patch.object(claude_utils, "ANTHROPIC_API_KEY", "test-key"), patch.object(
            claude_utils.anthropic, "AsyncAnthropic", return_value=client
        ):
            await claude_utils.generate_claude_response(
                [{"role": "user", "content": "hello"}],
                model="claude-sonnet-4-20250514",
                temperature=0.2,
            )

        self.assertEqual(create.await_args.kwargs["temperature"], 0.2)

    async def test_fable_refusal_is_reported_without_index_error(self):
        response = SimpleNamespace(
            content=[],
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber"),
            usage=None,
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(messages=SimpleNamespace(create=create))

        with patch.object(claude_utils, "ANTHROPIC_API_KEY", "test-key"), patch.object(
            claude_utils.anthropic, "AsyncAnthropic", return_value=client
        ):
            result = await claude_utils.generate_claude_response(
                [{"role": "user", "content": "hello"}]
            )

        self.assertEqual(result, "❌ Claude Fable declined this request (cyber).")


if __name__ == "__main__":
    unittest.main()
