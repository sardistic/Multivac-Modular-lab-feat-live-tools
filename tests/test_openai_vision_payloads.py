import unittest
from unittest.mock import AsyncMock, patch

from providers.openai_images import build_user_content_chat
from providers.openai_messages import generate_openai_messages_response_with_tools


class OpenAIVisionPayloadTests(unittest.IsolatedAsyncioTestCase):
    def test_build_user_content_chat_uses_high_detail_images(self):
        content = build_user_content_chat("describe this", ["data:image/png;base64,abc"])

        self.assertEqual(content[0], {"type": "text", "text": "describe this"})
        self.assertEqual(
            content[1],
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc", "detail": "high"},
            },
        )

    @patch("providers.openai_messages._create_chat_completion_with_token_fallback", new_callable=AsyncMock)
    async def test_generate_openai_messages_response_with_tools_respects_empty_tool_list(self, mock_create):
        mock_create.return_value = type(
            "Resp",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "finish_reason": "stop",
                            "message": type("Message", (), {"content": "ok", "tool_calls": None})(),
                        },
                    )()
                ]
            },
        )()

        result = await generate_openai_messages_response_with_tools(
            [{"role": "user", "content": "describe this image"}],
            tools=[],
        )

        self.assertEqual(result, "ok")
        self.assertIsNone(mock_create.await_args.kwargs["tools"])
        self.assertIsNone(mock_create.await_args.kwargs["tool_choice"])


if __name__ == "__main__":
    unittest.main()
