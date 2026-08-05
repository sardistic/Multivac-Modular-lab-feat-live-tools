import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import chat_handler, draft_verifier, provider_intents
from bot.draft_verifier import DraftVerdict


class DraftVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_verifier_receives_brevity_and_user_style_context(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "action": "revise",
                                "reason": "Too long for a simple request.",
                                "research_query": "",
                                "revised_answer": "Short answer.",
                            }
                        )
                    )
                )
            ],
            usage=None,
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch.object(draft_verifier, "get_openai_client", return_value=client), patch.object(
            draft_verifier,
            "get_user_instruction",
            return_value="Always be dry and concise.",
        ), patch.object(
            draft_verifier,
            "get_user_profile",
            return_value={"profile": "- prefers terse replies\n- likes dry humor"},
        ), patch.object(
            draft_verifier,
            "_load_reflection_signals",
            return_value=[
                {
                    "kind": "pain_point",
                    "summary": "Long preambles delayed the requested answer.",
                    "confidence": 0.91,
                    "occurrences": 3,
                }
            ],
        ):
            verdict = await draft_verifier.verify_chat_draft(
                user_id="123",
                display_name="Sardistic",
                prompt="thoughts?",
                draft="A very long answer that keeps going.",
                research_used=False,
            )

        self.assertEqual(verdict.action, "revise")
        self.assertEqual(verdict.revised_answer, "Short answer.")
        kwargs = create.await_args.kwargs
        self.assertEqual(
            kwargs["response_format"]["json_schema"]["schema"]["additionalProperties"],
            False,
        )
        audit = kwargs["messages"][1]["content"]
        self.assertIn('"prompt_word_count": 1', audit)
        self.assertIn('"draft_word_count": 7', audit)
        self.assertIn("Always be dry and concise.", audit)
        self.assertIn("prefers terse replies", audit)
        self.assertIn("Long preambles delayed the requested answer.", audit)
        self.assertIn('"occurrences": 3', audit)
        self.assertIn("what the task", kwargs["messages"][0]["content"])
        self.assertIn(
            "explicit behavioral instructions outrank reflection",
            kwargs["messages"][0]["content"],
        )
        self.assertIn(
            "Never mention, quote, reveal",
            kwargs["messages"][0]["content"],
        )

    def test_reflection_context_is_not_opened_when_reflection_is_disabled(self):
        with patch.dict("os.environ", {"REFLECTION_ENABLED": "false"}), patch.object(
            draft_verifier,
            "ReflectionStore",
        ) as store:
            self.assertEqual(draft_verifier._load_reflection_signals("123"), [])
        store.assert_not_called()

    async def test_verifier_failure_accepts_original_draft(self):
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("down")))
            )
        )
        with patch.object(draft_verifier, "get_openai_client", return_value=client), patch.object(
            draft_verifier,
            "get_user_instruction",
            return_value=None,
        ), patch.object(draft_verifier, "get_user_profile", return_value=None):
            verdict = await draft_verifier.verify_chat_draft(
                user_id="123",
                display_name=None,
                prompt="hello",
                draft="Hey.",
                research_used=False,
            )

        self.assertEqual(verdict.action, "accept")


class DraftVerifierChatFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message():
        return SimpleNamespace(
            guild=None,
            channel=SimpleNamespace(id=456),
            author=SimpleNamespace(id=123, display_name="Sardistic"),
        )

    @staticmethod
    async def _live_status(message, *, coro, **kwargs):
        return SimpleNamespace(edit=AsyncMock()), await coro

    async def test_style_revision_replaces_draft_before_personality_overlay(self):
        generate = AsyncMock(return_value="This answer is much too long.")
        verifier = AsyncMock(
            return_value=DraftVerdict(
                action="revise",
                reason="Too long.",
                revised_answer="Short answer.",
            )
        )
        send = AsyncMock()

        with patch.object(
            chat_handler,
            "build_chat_context",
            return_value=[{"role": "user", "content": "thoughts?"}],
        ), patch.object(
            chat_handler,
            "generate_openai_messages_response_with_tools",
            generate,
        ), patch.object(
            chat_handler,
            "verify_chat_draft",
            verifier,
        ), patch.object(
            chat_handler,
            "apply_personality_overrides",
            side_effect=lambda user_id, *, intent, text: f"styled:{text}",
        ):
            await chat_handler.handle_chat_intent(
                message=self._message(),
                prompt="thoughts?",
                raw_prompt="thoughts?",
                user_id=123,
                ref_msg=None,
                is_reply_to_bot=False,
                image_urls=[],
                gemini_parts=[],
                duration_estimate=1,
                stream_ok=False,
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=send,
                moderation_view_factory=Mock(),
            )

        self.assertEqual(send.await_args.args[0], "styled:Short answer.")

    async def test_research_verdict_discards_draft_and_forces_targeted_search(self):
        generate = AsyncMock(
            side_effect=[
                "The event is still ongoing.",
                "Spain won, with a source.",
            ]
        )
        verifier = AsyncMock(
            return_value=DraftVerdict(
                action="research",
                reason="The event has already ended.",
                research_query="2026 world cup final winner",
            )
        )
        send = AsyncMock()

        with patch.object(
            chat_handler,
            "build_chat_context",
            return_value=[{"role": "user", "content": "who won?"}],
        ), patch.object(
            chat_handler,
            "generate_openai_messages_response_with_tools",
            generate,
        ), patch.object(
            chat_handler,
            "verify_chat_draft",
            verifier,
        ), patch.object(
            chat_handler,
            "apply_personality_overrides",
            side_effect=lambda user_id, *, intent, text: text,
        ):
            await chat_handler.handle_chat_intent(
                message=self._message(),
                prompt="who won?",
                raw_prompt="who won?",
                user_id=123,
                ref_msg=None,
                is_reply_to_bot=False,
                image_urls=[],
                gemini_parts=[],
                duration_estimate=1,
                stream_ok=False,
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=send,
                moderation_view_factory=Mock(),
            )

        self.assertEqual(generate.await_count, 2)
        repair_kwargs = generate.await_args_list[1].kwargs
        self.assertEqual(repair_kwargs["forced_tool"], "web_search")
        self.assertIn("2026 world cup final winner", repair_kwargs["forced_tool_args"]["q"])
        self.assertEqual(send.await_args.args[0], "Spain won, with a source.")


class ProviderDraftVerifierFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message():
        return SimpleNamespace(
            guild=None,
            channel=SimpleNamespace(id=456),
            author=SimpleNamespace(id=123, display_name="Sardistic"),
            reply=AsyncMock(),
        )

    @staticmethod
    async def _live_status(message, *, coro, **kwargs):
        return SimpleNamespace(edit=AsyncMock(), reply=AsyncMock()), await coro

    async def test_explicit_gemini_research_verdict_enables_second_search(self):
        generate = AsyncMock(
            side_effect=[
                ("The event is ongoing.", []),
                ("Spain won, with a source.", []),
            ]
        )
        send = AsyncMock()
        with patch.object(
            provider_intents,
            "_generate_gemini_threaded",
            generate,
        ), patch.object(
            provider_intents,
            "build_channel_message_window",
            return_value=[],
        ), patch.object(
            provider_intents,
            "verify_chat_draft",
            new_callable=AsyncMock,
            return_value=DraftVerdict(
                action="research",
                research_query="2026 world cup final winner",
            ),
        ), patch.object(
            provider_intents,
            "apply_personality_overrides",
            side_effect=lambda user_id, *, intent, text: text,
        ):
            await provider_intents.handle_gemini_chat_intent(
                message=self._message(),
                prompt="gemini who won?",
                gemini_parts=[],
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=send,
                moderation_view_factory=Mock(),
            )

        self.assertEqual(generate.await_count, 2)
        self.assertTrue(generate.await_args_list[1].kwargs["force_web_search"])
        self.assertEqual(send.await_args.args[0], "Spain won, with a source.")

    async def test_explicit_claude_research_verdict_reads_web_evidence(self):
        generate = AsyncMock(side_effect=["The event is ongoing.", "Spain won."])
        search = AsyncMock(
            return_value=[
                {
                    "title": "Final",
                    "url": "https://example.test/final",
                    "snippet": "Spain won.",
                }
            ]
        )
        read = AsyncMock(
            return_value={
                "ok": True,
                "url": "https://example.test/final",
                "condensed": "Spain won.",
            }
        )
        send = AsyncMock()
        with patch.object(
            provider_intents,
            "generate_claude_response",
            generate,
        ), patch.object(
            provider_intents,
            "build_channel_message_window",
            return_value=[],
        ), patch.object(
            provider_intents,
            "build_message_user_style_system_messages",
            return_value=[],
        ), patch.object(
            provider_intents,
            "verify_chat_draft",
            new_callable=AsyncMock,
            return_value=DraftVerdict(
                action="research",
                research_query="2026 world cup final winner",
            ),
        ), patch(
            "services.tool_handlers.handle_web_search",
            search,
        ), patch(
            "services.tool_handlers.handle_summarize_url",
            read,
        ), patch.object(
            provider_intents,
            "apply_personality_overrides",
            side_effect=lambda user_id, *, intent, text: text,
        ):
            await provider_intents.handle_claude_chat_intent(
                message=self._message(),
                prompt="claude who won?",
                stream_ok=False,
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=send,
                image_urls=[],
                ref_msg=None,
            )

        search.assert_not_awaited()
        read.assert_not_awaited()
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(generate.await_args.kwargs["forced_tool"], "web_search")
        self.assertIn("2026 world cup final winner", generate.await_args.kwargs["forced_tool_args"]["q"])
        self.assertEqual(send.await_args.args[0], "Spain won.")


if __name__ == "__main__":
    unittest.main()
