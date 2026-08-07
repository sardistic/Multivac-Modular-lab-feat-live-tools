import base64
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
            intent_dispatcher.DispatchContext(
                intent="generate_image",
                message=message,
                prompt="imagine a character based on this",
                raw_prompt="imagine a character based on this",
                user_id=123,
                bot_user=SimpleNamespace(id=999),
                ref_msg=ref_msg,
                live_status_with_progress=AsyncMock(),
                send_or_edit_with_truncation=AsyncMock(),
                prompt_for_image_selection=AsyncMock(),
                moderation_view_factory=lambda **kwargs: None,
            )
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

    @patch("providers.stability_generation.generate_gemini_image")
    @patch("providers.stability_generation.generate_gpt_image", new_callable=AsyncMock)
    async def test_openai_image_failure_always_falls_back_to_gemini(self, mock_generate_gpt, mock_gemini):
        mock_generate_gpt.return_value = None
        mock_gemini.return_value = "gemini-image"
        provider_state = {}

        result = await stability_generation.handle_image_generation(
            message=None,
            prompt="imagine a moonlit library",
            provider_state=provider_state,
        )

        self.assertEqual(result, "gemini-image")
        mock_gemini.assert_called_once()
        self.assertEqual(provider_state["provider"], "Gemini")
        self.assertEqual(provider_state["model"], stability_generation.IMG_MODEL_GEMINI)

    @patch("providers.stability_generation.get_openai_image_client")
    async def test_generate_gpt_image_uses_auto_size(self, mock_get_client):
        generate = AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"img").decode("ascii"))]
            )
        )
        mock_get_client.return_value = SimpleNamespace(images=SimpleNamespace(generate=generate))

        result = await stability_generation.generate_gpt_image("a neon city skyline")

        self.assertEqual(result.read(), b"img")
        self.assertEqual(generate.await_args.kwargs["size"], "auto")

    @patch("providers.stability_generation.get_openai_image_client")
    async def test_edit_image_with_prompt_uses_auto_size(self, mock_get_client):
        edits = AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"edited").decode("ascii"))]
            )
        )
        mock_get_client.return_value = SimpleNamespace(images=SimpleNamespace(edits=edits))

        result = await stability_generation.edit_image_with_prompt(
            "data:image/png;base64,Zm9v",
            "make it more cinematic",
        )

        self.assertEqual(result.read(), b"edited")
        self.assertEqual(edits.await_args.kwargs["size"], "auto")


if __name__ == "__main__":
    unittest.main()
