import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers import openai_messages
from services import tool_handlers
from services.tool_specs import TOOL_SPECS


def _tool_spec(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} test tool",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _tool_call_response(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(
                                name=name,
                                arguments=json.dumps(arguments),
                            ),
                        )
                    ],
                ),
            )
        ]
    )


class WebResearchToolLoopTests(unittest.IsolatedAsyncioTestCase):
    @patch("providers.openai_messages.USE_RESPONSES", False)
    async def test_search_can_be_followed_by_page_read_and_synthesized_answer(self):
        specs = [_tool_spec("web_search"), _tool_spec("summarize_url")]
        snapshot = SimpleNamespace(tool_specs=lambda: specs)
        responses = [
            _tool_call_response("web_search", {"q": "latest lunar mission"}, "search-1"),
            _tool_call_response(
                "summarize_url",
                {"url": "https://example.test/mission"},
                "page-1",
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content="The mission launched yesterday.",
                            tool_calls=None,
                        ),
                    )
                ]
            ),
        ]
        create = AsyncMock(side_effect=responses)
        execute = AsyncMock(
            side_effect=[
                [
                    {
                        "title": "Mission update",
                        "url": "https://example.test/mission",
                        "snippet": "A new launch",
                    }
                ],
                {
                    "ok": True,
                    "title": "Mission update",
                    "condensed": "The mission launched yesterday.",
                },
            ]
        )

        with patch.object(openai_messages, "get_tool_snapshot", return_value=snapshot), patch.object(
            openai_messages,
            "_create_chat_completion_with_token_fallback",
            create,
        ), patch.object(openai_messages, "execute_tool", execute):
            result = await openai_messages.generate_openai_messages_response_with_tools(
                [{"role": "user", "content": "What is the latest lunar mission news?"}]
            )

        self.assertEqual(result, "The mission launched yesterday.")
        self.assertEqual(
            [call.args[0] for call in execute.await_args_list],
            ["web_search", "summarize_url"],
        )
        first_messages = create.await_args_list[0].kwargs["messages"]
        research_instruction = first_messages[0]["content"]
        self.assertIn("Decide for yourself when fresh web research", research_instruction)
        self.assertIn("open the most relevant result", research_instruction)
        self.assertIn("instead of returning a bare list", research_instruction)

    @patch("providers.openai_messages.USE_RESPONSES", False)
    async def test_url_instruction_requires_reading_relevant_page(self):
        specs = [_tool_spec("summarize_url")]
        snapshot = SimpleNamespace(tool_specs=lambda: specs)
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="ok", tool_calls=None),
                    )
                ]
            )
        )

        with patch.object(openai_messages, "get_tool_snapshot", return_value=snapshot), patch.object(
            openai_messages,
            "_create_chat_completion_with_token_fallback",
            create,
        ):
            await openai_messages.generate_openai_messages_response_with_tools(
                [
                    {
                        "role": "user",
                        "content": "What do you think of https://example.test/article?",
                    }
                ]
            )

        instruction = create.await_args.kwargs["messages"][0]["content"]
        self.assertIn("contains an HTTP(S) URL", instruction)
        self.assertIn("call `summarize_url` and read it before answering", instruction)
        self.assertIn("never infer a page's contents from its URL", instruction)


class WebResearchHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_reader_returns_extracted_content(self):
        with patch.object(
            tool_handlers,
            "fetch_url_content",
            return_value="<html><title>Example</title><article><p>Useful facts.</p></article></html>",
        ), patch.object(
            tool_handlers,
            "extract_main_text",
            return_value=("Example", "Useful facts."),
        ):
            result = await tool_handlers.handle_summarize_url(
                {"url": "https://example.test/article", "max_len": 6000}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Example")
        self.assertEqual(result["condensed"], "Useful facts.")

    async def test_page_reader_reports_empty_extraction(self):
        with patch.object(tool_handlers, "fetch_url_content", return_value="<html></html>"), patch.object(
            tool_handlers,
            "extract_main_text",
            return_value=("", ""),
        ):
            result = await tool_handlers.handle_summarize_url(
                {"url": "https://example.test/empty"}
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_readable_content")

    def test_tool_descriptions_define_research_loop(self):
        specs = {spec["function"]["name"]: spec["function"] for spec in TOOL_SPECS}
        self.assertIn("use summarize_url", specs["web_search"]["description"])
        self.assertIn("Open and read", specs["summarize_url"]["description"])
        self.assertEqual(
            specs["summarize_url"]["parameters"]["properties"]["max_len"]["default"],
            6000,
        )


if __name__ == "__main__":
    unittest.main()
