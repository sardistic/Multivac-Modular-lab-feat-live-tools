import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.intent_dispatcher import (
    chat_model_for_intent,
    chat_reasoning_for_intent,
    resolve_keyword_intent,
    validate_classified_intent,
)
from providers.openai_client import OPENAI_DEEP_MODEL
from services import reverse_image_search as reverse
from services.tool_handlers import handle_reverse_image_search, handle_web_search
from services.tool_specs import TOOL_SPECS


class _Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class _AsyncClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.captured.update({"url": url, **kwargs})
        return self.response


class ReverseImageSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_vision_parser_distinguishes_matches_from_similarity(self):
        parsed = reverse._vision_result(
            {
                "responses": [
                    {
                        "webDetection": {
                            "bestGuessLabels": [{"label": "example manga panel"}],
                            "fullMatchingImages": [{"url": "https://img.test/exact.png"}],
                            "pagesWithMatchingImages": [
                                {"url": "https://page.test/chapter", "pageTitle": "Chapter"}
                            ],
                            "visuallySimilarImages": [{"url": "https://img.test/similar.png"}],
                        }
                    }
                ]
            },
            10,
        )

        self.assertTrue(parsed["match_found"])
        self.assertEqual(parsed["provider"], "google_cloud_vision_web_detection")
        self.assertEqual(parsed["exact_or_partial_matches"][0]["match_type"], "full_image_match")
        self.assertEqual(parsed["visually_similar_images"][0]["match_type"], "visually_similar")

    async def test_google_vision_sends_image_content_and_records_metered_call(self):
        captured = {}
        response = _Response({"responses": [{"webDetection": {}}]})
        data_url = "data:image/png;base64," + base64.b64encode(b"image-bytes").decode()
        with patch.object(reverse, "GOOGLE_API_KEY", "test-key"), patch.object(
            reverse.httpx,
            "AsyncClient",
            return_value=_AsyncClient(response, captured),
        ), patch("services.usage_costs.record_metered") as metered:
            result = await reverse._google_vision(data_url, 5)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["headers"], {"x-goog-api-key": "test-key"})
        sent = captured["json"]["requests"][0]
        self.assertEqual(base64.b64decode(sent["image"]["content"]), b"image-bytes")
        self.assertEqual(sent["features"][0]["type"], "WEB_DETECTION")
        metered.assert_called_once()

    async def test_handler_uses_attached_image_from_request_context(self):
        lookup = AsyncMock(return_value={"ok": True, "provider": "test"})
        with patch("services.reverse_image_search.reverse_image_search", lookup):
            result = await handle_reverse_image_search(
                {
                    "image_index": 1,
                    "mode": "exact",
                    "_context": {"image_urls": ["first", "second"]},
                }
            )
        self.assertTrue(result["ok"])
        self.assertEqual(lookup.await_args.args[0], "second")
        self.assertEqual(lookup.await_args.kwargs["mode"], "exact")

    async def test_keyword_image_flag_uses_keyword_search_not_reverse_lookup(self):
        keyword = AsyncMock(return_value={"query": "cats", "results": []})
        with patch("services.google_search.google_web_search", keyword):
            result = await handle_web_search({"q": "cats", "image": True})
        self.assertEqual(result["query"], "cats")
        self.assertTrue(keyword.await_args.kwargs["image"])

    def test_attachment_reverse_request_routes_to_sol_with_high_reasoning(self):
        intent = resolve_keyword_intent(
            "reverse image search this and find the source",
            "reverse image search this and find the source",
            True,
        )
        self.assertEqual(intent, "chat_reverse_image")
        self.assertEqual(chat_model_for_intent(intent), OPENAI_DEEP_MODEL)
        self.assertEqual(chat_reasoning_for_intent(intent), "high")
        self.assertEqual(
            resolve_keyword_intent(
                "claude reverse image search this",
                "claude reverse image search this",
                True,
            ),
            "claude_chat",
        )
        self.assertEqual(
            validate_classified_intent(
                "chat_reverse_image",
                "reverse search this",
                has_attachments=False,
            ),
            "clarify",
        )

    def test_reverse_tool_schema_does_not_mislabel_keyword_search(self):
        by_name = {item["function"]["name"]: item["function"] for item in TOOL_SPECS}
        self.assertIn("reverse_image_search", by_name)
        image_help = by_name["web_search"]["parameters"]["properties"]["image"]["description"]
        self.assertIn("not a reverse-image lookup", image_help)


if __name__ == "__main__":
    unittest.main()
