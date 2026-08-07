import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers.openai_images import build_user_content_chat
from providers.openai_messages import generate_openai_messages_response, generate_openai_messages_response_with_tools


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

    @patch("providers.openai_messages.USE_RESPONSES", False)
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

    @patch("providers.openai_messages.USE_RESPONSES", False)
    @patch("providers.openai_messages._create_chat_completion_with_token_fallback", new_callable=AsyncMock)
    async def test_generate_openai_messages_response_continues_after_length_finish(self, mock_create):
        mock_create.side_effect = [
            type(
                "Resp",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "length",
                                "message": type("Message", (), {"content": "The visible quote from the book page says"})(),
                            },
                        )()
                    ]
                },
            )(),
            type(
                "Resp",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type(
                                    "Message",
                                    (),
                                    {"content": " that men handed thinking to machines and were then dominated by those who controlled them."},
                                )(),
                            },
                        )()
                    ]
                },
            )(),
        ]

        result = await generate_openai_messages_response(
            [{"role": "user", "content": "Explain the quote"}],
            max_tokens=1400,
        )

        self.assertIn("The visible quote from the book page says", result)
        self.assertIn("handed thinking to machines", result)
        self.assertEqual(mock_create.await_count, 2)

    @patch("providers.openai_messages.USE_RESPONSES", True)
    @patch("providers.openai_messages.get_openai_client")
    async def test_generate_openai_messages_response_continues_in_responses_mode(self, mock_get_client):
        create_mock = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    output_text="The photographed Dune passage reads",
                    status="incomplete",
                    incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                ),
                SimpleNamespace(
                    output_text=" that turning thinking over to machines can let other people dominate those who rely on them.",
                    status="completed",
                    incomplete_details=None,
                ),
            ]
        )
        mock_get_client.return_value = SimpleNamespace(
            responses=SimpleNamespace(create=create_mock)
        )

        result = await generate_openai_messages_response(
            [{"role": "user", "content": "Explain the quote"}],
            max_tokens=1400,
        )

        self.assertIn("The photographed Dune passage reads", result)
        self.assertIn("turning thinking over to machines", result)
        self.assertEqual(create_mock.await_count, 2)


if __name__ == "__main__":
    unittest.main()
