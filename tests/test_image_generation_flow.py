import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import image_handler, intent_dispatcher
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

    @patch("providers.stability_generation.generate_gpt_image", new_callable=AsyncMock)
    async def test_retry_context_replaces_generic_generated_status_text(self, mock_generate_gpt):
        mock_generate_gpt.return_value = "image-bytes"
        reply_msg = SimpleNamespace(
            content="✅ Image generated (GPT Image 1.5)",
            author=SimpleNamespace(display_name="Multivac"),
        )

        result = await stability_generation.handle_image_generation(
            message=None,
            prompt="not good, try again",
            reply_msg=reply_msg,
            retry_context="- draw a complex flow chart of your inner working logic",
        )

        self.assertEqual(result, "image-bytes")
        composed_prompt = mock_generate_gpt.await_args.args[0]
        self.assertIn("not good, try again", composed_prompt)
        self.assertIn("draw a complex flow chart of your inner working logic", composed_prompt)
        self.assertNotIn("✅ Image generated", composed_prompt)

    async def test_retry_context_walks_prior_generation_replies(self):
        original = SimpleNamespace(
            id=1,
            content="@Multivac draw a complex flow chart of your inner working logic",
            reference=None,
        )
        first_status = SimpleNamespace(
            id=2,
            content="✅ Image generated (GPT Image 1.5)",
            reference=SimpleNamespace(resolved=original),
        )
        first_retry = SimpleNamespace(
            id=3,
            content="not good, try again",
            reference=SimpleNamespace(resolved=first_status),
        )
        second_status = SimpleNamespace(
            id=4,
            content="✅ Image generated (GPT Image 1.5)",
            reference=SimpleNamespace(resolved=first_retry),
        )

        context = await image_handler._build_image_retry_context(second_status)

        self.assertLess(context.index("draw a complex flow chart"), context.index("not good, try again"))

    @patch("bot.image_handler.handle_image_generation", new_callable=AsyncMock)
    async def test_generated_image_is_attached_to_status_and_retry_context_is_forwarded(self, mock_generate):
        mock_generate.return_value = io.BytesIO(b"generated-image")
        original = SimpleNamespace(
            id=10,
            content="draw a complex flow chart of your inner working logic",
            reference=None,
        )
        replied_status = SimpleNamespace(
            id=11,
            content="✅ Image generated (GPT Image 1.5)",
            reference=SimpleNamespace(resolved=original),
        )
        output_status = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(channel=channel)

        async def live_status(_message, **kwargs):
            return output_status, await kwargs["coro"]

        await image_handler.handle_generate_image_intent(
            message=message,
            prompt="not good, try again",
            ref_msg=replied_status,
            duration_estimate=10,
            stream_ok=False,
            live_status_with_progress=live_status,
        )

        forwarded = mock_generate.await_args.kwargs["retry_context"]
        self.assertIn("draw a complex flow chart", forwarded)
        edit_kwargs = output_status.edit.await_args.kwargs
        self.assertEqual(edit_kwargs["content"], "✅ Image generated (GPT Image 1.5)")
        self.assertEqual(len(edit_kwargs["attachments"]), 1)
        self.assertEqual(edit_kwargs["attachments"][0].filename, "generated_image.png")
        channel.send.assert_not_awaited()

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
