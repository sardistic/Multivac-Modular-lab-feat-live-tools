import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import chat_handler, video_handler
from PIL import Image
from providers import sora_jobs
from services import tool_handlers


class VideoGenerationFlowTests(unittest.IsolatedAsyncioTestCase):
    def _png_bytes(self, width: int, height: int) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (width, height), color="white").save(buf, format="PNG")
        return buf.getvalue()

    async def test_resolve_video_reference_upload_uses_collected_image_inputs(self):
        message = SimpleNamespace(attachments=[])

        upload = await video_handler._resolve_video_reference_upload(
            message,
            image_urls=["data:image/png;base64,Zm9v"],
        )

        self.assertEqual(upload, (b"foo", "input_1.png", "image/png"))

    @patch("bot.chat_handler.apply_personality_overrides", side_effect=lambda user_id, *, intent, text: text)
    @patch("bot.chat_handler.generate_openai_messages_response_with_tools", new_callable=AsyncMock)
    @patch("bot.chat_handler.build_chat_context")
    async def test_handle_chat_intent_passes_image_urls_into_tool_context(
        self,
        mock_build_chat_context,
        mock_generate,
        _mock_personality,
    ):
        mock_build_chat_context.return_value = [{"role": "user", "content": "make a video from this"}]
        mock_generate.return_value = "queued"

        status_msg = SimpleNamespace(edit=AsyncMock())

        async def fake_live_status_with_progress(message, action_label, emoji, coro, duration_estimate, summarizer=None):
            return status_msg, await coro

        send_mock = AsyncMock()
        message = SimpleNamespace(
            guild=SimpleNamespace(id=111),
            channel=SimpleNamespace(id=222),
            author=SimpleNamespace(id=333),
        )

        await chat_handler.handle_chat_intent(
            message=message,
            prompt="make a video from this",
            raw_prompt="make a video from this",
            user_id=333,
            ref_msg=None,
            is_reply_to_bot=False,
            image_urls=["data:image/png;base64,abc"],
            gemini_parts=[],
            duration_estimate=6,
            stream_ok=False,
            live_status_with_progress=fake_live_status_with_progress,
            send_or_edit_with_truncation=send_mock,
            moderation_view_factory=lambda **kwargs: None,
        )

        self.assertEqual(
            mock_generate.await_args.kwargs["tool_context"]["image_urls"],
            ["data:image/png;base64,abc"],
        )

    @patch("providers.sora_utils.create_sora_job", new_callable=AsyncMock)
    @patch("services.database_utils.log_sora_usage")
    @patch("services.database_utils.check_sora_limit", return_value=True)
    async def test_handle_generate_sora_video_uses_context_image_reference(
        self,
        _mock_limit,
        _mock_log_usage,
        mock_create_job,
    ):
        mock_create_job.return_value = {"ok": True, "data": {"id": "vid_123"}}

        result = await tool_handlers.handle_generate_sora_video(
            {
                "prompt": "animate this image with a slow camera move",
                "_context": {
                    "user_id": "123",
                    "image_urls": ["data:image/png;base64,Zm9v"],
                },
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(mock_create_job.await_args.args[0], "animate this image with a slow camera move")
        self.assertEqual(mock_create_job.await_args.kwargs["image_data"], b"foo")
        self.assertEqual(mock_create_job.await_args.kwargs["image_filename"], "tool_input_1.png")
        self.assertEqual(mock_create_job.await_args.kwargs["image_content_type"], "image/png")

    def test_select_reference_video_size_prefers_closest_landscape_ratio(self):
        image_data = self._png_bytes(1500, 1000)

        selected = sora_jobs.select_reference_video_size(image_data)

        self.assertEqual(selected, "1792x1024")

    def test_select_reference_video_size_prefers_closest_portrait_ratio(self):
        image_data = self._png_bytes(1080, 1920)

        selected = sora_jobs.select_reference_video_size(image_data)

        self.assertEqual(selected, "720x1280")

    def test_select_reference_video_size_keeps_default_for_square_images(self):
        image_data = self._png_bytes(1024, 1024)

        selected = sora_jobs.select_reference_video_size(image_data)

        self.assertEqual(selected, sora_jobs.DEFAULT_VIDEO_SIZE)


if __name__ == "__main__":
    unittest.main()
