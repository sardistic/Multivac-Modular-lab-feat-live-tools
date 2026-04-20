import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import intent_dispatcher
from providers import stability_generation


class ImageGenerationFlowTests(unittest.IsolatedAsyncioTestCase):
    @patch("bot.intent_dispatcher.handle_generate_image_intent", new_callable=AsyncMock)
    async def test_dispatch_intent_passes_reference_message_to_generate_image_handler(self, mock_handle_generate):
        ref_msg = SimpleNamespace(content="silver armor, long white hair")
        message = SimpleNamespace(content="@bot imagine a character based on this")

        await intent_dispatcher.dispatch_intent(
            intent="generate_image",
            message=message,
            prompt="imagine a character based on this",
            raw_prompt="imagine a character based on this",
            user_id=123,
            ref_msg=ref_msg,
            is_reply_to_bot=False,
            image_urls=[],
            gemini_parts=[],
            general_url_match=None,
            stream_ok=False,
            bot_user=SimpleNamespace(id=999),
            get_location_details=None,
            get_weather_data=None,
            live_status_with_progress=AsyncMock(),
            send_or_edit_with_truncation=AsyncMock(),
            prompt_for_image_selection=AsyncMock(),
            moderation_view_factory=lambda **kwargs: None,
        )

        self.assertIs(mock_handle_generate.await_args.kwargs["ref_msg"], ref_msg)

    @patch("providers.stability_generation.generate_gpt_image", new_callable=AsyncMock)
    async def test_handle_image_generation_includes_replied_message_content_in_prompt(self, mock_generate_gpt):
        mock_generate_gpt.return_value = "image-bytes"
        reply_msg = SimpleNamespace(
            content="A calm scholar with round glasses, short black hair, and a dark green coat.",
            author=SimpleNamespace(display_name="Ry7"),
        )

        result = await stability_generation.handle_image_generation(
            message=None,
            prompt="imagine a portrait based on these details",
            reply_msg=reply_msg,
        )

        self.assertEqual(result, "image-bytes")
        composed_prompt = mock_generate_gpt.await_args.args[0]
        self.assertIn("imagine a portrait based on these details", composed_prompt)
        self.assertIn("Replied message context from Ry7", composed_prompt)
        self.assertIn("A calm scholar with round glasses", composed_prompt)


if __name__ == "__main__":
    unittest.main()
