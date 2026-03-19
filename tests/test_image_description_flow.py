import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import image_handler


class ImageDescriptionFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_needs_explanation_retry_for_incomplete_outline(self):
        text = (
            "1. What the image is\n"
            "It's a screenshot of a post.\n\n"
            "2. The key text or quote\n"
            "The post says something about Dune."
        )

        self.assertTrue(
            image_handler._needs_explanation_retry(
                "explain this image and the quote in it what should i understand about it",
                text,
            )
        )

    @patch("bot.image_handler.apply_personality_overrides", side_effect=lambda user_id, *, intent, text: text)
    @patch("bot.image_handler.generate_openai_messages_response", new_callable=AsyncMock)
    async def test_handle_describe_image_intent_retries_until_takeaway_exists(self, mock_generate, _mock_personality):
        mock_generate.side_effect = [
            "1. Image type and setting\nA screenshot.\n\n2. Visible text\nA Dune quote.\n\n3. Uncertain or partially legible text\nNone.\n\n4. Visual cues that matter\nNested quote card.",
            "1. What the image is\nIt's a screenshot.\n\n2. The key text or quote\nA Dune quote.",
            (
                "1. What the image is\nIt's a screenshot of a post quoting Dune.\n\n"
                "2. The key text or quote\nThe quote warns against handing human thinking over to machines.\n\n"
                "3. What it means / what you should understand\nThe point is that the post is connecting Dune's anti-machine warning to modern AI dependence."
            ),
        ]

        status_msg = SimpleNamespace()

        async def fake_live_status_with_progress(message, action_label, emoji, coro, duration_estimate, summarizer=None):
            return status_msg, await coro

        send_mock = AsyncMock()
        message = SimpleNamespace(author=SimpleNamespace(id=123))

        await image_handler.handle_describe_image_intent(
            message=message,
            prompt="explain this image and the quote in it what should i understand about it",
            image_urls=["data:image/png;base64,abc"],
            ref_msg=None,
            is_reply_to_bot=False,
            duration_estimate=8,
            stream_ok=False,
            live_status_with_progress=fake_live_status_with_progress,
            send_or_edit_with_truncation=send_mock,
        )

        self.assertEqual(mock_generate.await_count, 3)
        sent_text = send_mock.await_args.args[0]
        self.assertIn("3. What it means / what you should understand", sent_text)


if __name__ == "__main__":
    unittest.main()
