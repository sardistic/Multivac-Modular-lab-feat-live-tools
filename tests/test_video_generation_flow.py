import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import chat_handler, video_handler
from bot.draft_verifier import DraftVerdict
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

    @patch(
        "bot.chat_handler.verify_chat_draft",
        new_callable=AsyncMock,
        return_value=DraftVerdict(),
    )
    @patch("bot.chat_handler.apply_personality_overrides", side_effect=lambda user_id, *, intent, text: text)
    @patch("bot.chat_handler.generate_openai_messages_response_with_tools", new_callable=AsyncMock)
    @patch("bot.chat_handler.build_chat_context")
    async def test_handle_chat_intent_passes_image_urls_into_tool_context(
        self,
        mock_build_chat_context,
        mock_generate,
        _mock_personality,
        _mock_verifier,
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

    def test_build_video_config_options_includes_veo_when_available(self):
        options = video_handler.build_video_config_options(include_veo=True)
        options_by_value = {option["value"]: option for option in options}

        self.assertEqual(options_by_value["sora|sora-2-pro|8"]["cost"], 2.40)
        self.assertIn("720p $2.40", options_by_value["sora|sora-2-pro|8"]["label"])
        self.assertEqual(options_by_value["veo|veo-3.1-generate-preview|6"]["cost"], 2.40)
        self.assertEqual(options_by_value["veo|veo-3.1-fast-generate-preview|8"]["cost"], 0.80)

    def test_video_cost_message_matches_dropdown_prices(self):
        message = video_handler._build_video_cost_message("a cinematic sunrise", include_veo=True)

        self.assertIn("Sora estimates (OpenAI 720p pricing)", message)
        self.assertIn("1024p at $0.50/second", message)
        self.assertIn("Veo 3.1**: 4s $1.60 | 6s $2.40 | 8s $3.20", message)
        self.assertIn("Veo 3.1 Fast**: 4s $0.40 | 6s $0.60 | 8s $0.80", message)
        self.assertIn("native audio is included", message)

    @patch("bot.video_handler.check_sora_limit", return_value=True)
    async def test_handle_generate_video_intent_uses_original_message_for_status_handoff(self, _mock_limit):
        class FakeView:
            def __init__(self, author_id, video_options):
                self.author_id = author_id
                self.video_options = video_options
                self.value = "sora|sora-2|4"

            async def wait(self):
                return

        confirm_msg = SimpleNamespace(edit=AsyncMock())
        message = SimpleNamespace(
            attachments=[],
            reply=AsyncMock(return_value=confirm_msg),
        )
        captured = {}

        async def fake_live_status_with_progress(base_message, **kwargs):
            captured["base_message"] = base_message
            captured["existing_status_msg"] = kwargs.get("existing_status_msg")
            kwargs["coro"].close()
            status_msg = SimpleNamespace(edit=AsyncMock(), reply=AsyncMock())
            return status_msg, (None, "stopped")

        with patch("bot.video_handler.VideoConfirmationView", FakeView):
            await video_handler.handle_generate_video_intent(
                message=message,
                prompt="generate a video of this",
                user_id=123,
                live_status_with_progress=fake_live_status_with_progress,
                stream_ok=False,
                image_urls=None,
            )

        self.assertIs(captured["base_message"], message)
        self.assertIs(captured["existing_status_msg"], confirm_msg)

    @patch("bot.video_handler.generate_veo_video", new_callable=AsyncMock)
    @patch("bot.video_handler.log_veo_usage")
    @patch("bot.video_handler.check_veo_limit", return_value=True)
    async def test_handle_generate_video_intent_routes_selected_veo_option(
        self,
        _mock_limit,
        mock_log_usage,
        mock_generate_veo,
    ):
        class FakeView:
            def __init__(self, author_id, video_options):
                self.author_id = author_id
                self.video_options = video_options
                self.value = "veo|veo-3.1-fast-generate-preview|6"

            async def wait(self):
                return

        confirm_msg = SimpleNamespace(edit=AsyncMock())
        status_msg = SimpleNamespace(edit=AsyncMock(), reply=AsyncMock())
        message = SimpleNamespace(
            attachments=[],
            reply=AsyncMock(return_value=confirm_msg),
        )
        mock_generate_veo.return_value = (b"video-bytes", None)

        async def fake_live_status_with_progress(base_message, **kwargs):
            return status_msg, await kwargs["coro"]

        with patch("bot.video_handler.veo_is_available", return_value=True):
            with patch("bot.video_handler.VideoConfirmationView", FakeView):
                await video_handler.handle_generate_video_intent(
                    message=message,
                    prompt="generate a cinematic canyon flythrough",
                    user_id=123,
                    live_status_with_progress=fake_live_status_with_progress,
                    stream_ok=False,
                    image_urls=None,
                )

        self.assertEqual(mock_generate_veo.await_args.kwargs["model"], "veo-3.1-fast-generate-preview")
        self.assertEqual(mock_generate_veo.await_args.kwargs["seconds"], 6)
        self.assertFalse(mock_generate_veo.await_args.kwargs["generate_audio"])
        mock_log_usage.assert_called_once()
        status_msg.reply.assert_awaited()

    def test_select_reference_video_size_prefers_closest_landscape_ratio(self):
        image_data = self._png_bytes(1500, 1000)

        selected = sora_jobs.select_reference_video_size(image_data, model="sora-2-pro")

        self.assertEqual(selected, "1792x1024")

    def test_select_reference_video_size_prefers_closest_portrait_ratio(self):
        image_data = self._png_bytes(1080, 1920)

        selected = sora_jobs.select_reference_video_size(image_data, model="sora-2")

        self.assertEqual(selected, "720x1280")

    def test_select_reference_video_size_keeps_default_for_square_images(self):
        image_data = self._png_bytes(1024, 1024)

        selected = sora_jobs.select_reference_video_size(image_data, model="sora-2")

        self.assertEqual(selected, sora_jobs.DEFAULT_VIDEO_SIZE)

    def test_select_reference_video_size_limits_standard_model_to_standard_sizes(self):
        image_data = self._png_bytes(500, 757)

        selected = sora_jobs.select_reference_video_size(image_data, model="sora-2")

        self.assertEqual(selected, "720x1280")

    def test_prepare_reference_image_for_size_matches_exact_target_dimensions(self):
        image_data = self._png_bytes(500, 757)

        prepared = sora_jobs.prepare_reference_image_for_size(image_data, "720x1280")

        with Image.open(BytesIO(prepared)) as img:
            self.assertEqual(img.size, (720, 1280))

    def test_prepare_reference_image_for_size_preserves_existing_matching_dimensions(self):
        image_data = self._png_bytes(720, 1280)

        prepared = sora_jobs.prepare_reference_image_for_size(image_data, "720x1280")

        with Image.open(BytesIO(prepared)) as img:
            self.assertEqual(img.size, (720, 1280))


if __name__ == "__main__":
    unittest.main()
