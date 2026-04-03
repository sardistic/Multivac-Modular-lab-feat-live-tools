import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from providers import gemini_client, veo_utils


class GeminiVeoConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_get_gemini_client_masks_google_api_key_during_client_construction(self):
        original_google_api_key = os.environ.get("GOOGLE_API_KEY")
        os.environ["GOOGLE_API_KEY"] = "google-cse-key"
        captured = {}

        def fake_client(**kwargs):
            captured["google_api_key_during_call"] = os.environ.get("GOOGLE_API_KEY")
            captured["kwargs"] = kwargs
            return "fake-client"

        try:
            with patch.object(gemini_client, "SDK_AVAILABLE", True):
                with patch.object(gemini_client, "GEMINI_API_KEY", "gemini-video-key"):
                    with patch.object(gemini_client, "genai", SimpleNamespace(Client=fake_client)):
                        client = gemini_client.get_gemini_client()

            self.assertEqual(client, "fake-client")
            self.assertIsNone(captured["google_api_key_during_call"])
            self.assertEqual(captured["kwargs"]["api_key"], "gemini-video-key")
            self.assertEqual(captured["kwargs"]["vertexai"], False)
        finally:
            if original_google_api_key is None:
                os.environ.pop("GOOGLE_API_KEY", None)
            else:
                os.environ["GOOGLE_API_KEY"] = original_google_api_key

    async def test_generate_veo_video_omits_generate_audio_field_for_gemini_api(self):
        captured = {}

        class FakeConfig:
            def __init__(self, **kwargs):
                captured["config_kwargs"] = kwargs
                self.kwargs = kwargs

        fake_types = SimpleNamespace(GenerateVideosConfig=FakeConfig)
        operation = SimpleNamespace(
            done=True,
            response=SimpleNamespace(generated_videos=[SimpleNamespace(video="file-123")]),
        )
        fake_client = SimpleNamespace(
            models=SimpleNamespace(generate_videos=lambda **kwargs: operation),
            files=SimpleNamespace(download=lambda **kwargs: b"video-bytes"),
        )

        with patch.object(veo_utils, "types", fake_types):
            with patch.object(veo_utils, "get_gemini_client", return_value=fake_client):
                content, err = await veo_utils.generate_veo_video(
                    "a dramatic coastal flyover",
                    model="veo-3.1-fast-generate-preview",
                    seconds=6,
                    generate_audio=False,
                )

        self.assertEqual(content, b"video-bytes")
        self.assertIsNone(err)
        self.assertNotIn("generate_audio", captured["config_kwargs"])
        self.assertEqual(captured["config_kwargs"]["duration_seconds"], 6)


if __name__ == "__main__":
    unittest.main()
