import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import chat_context
from bot import provider_intents
from bot.image_handler import (
    _build_image_explanation_messages,
    _build_image_extraction_messages,
)
from bot.persona import (
    MISTAKE_NOT_PERSONA_PROMPT,
    conversation_persona_scope,
    parse_persona_toggle,
)
from bot.response_policy import (
    PERSONALIZATION_PRIORITY_SYSTEM_MESSAGE,
    build_message_user_style_system_messages,
    build_user_style_system_messages,
)
from services.sqlite_store import SQLiteStore


class PersonaStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.store = SQLiteStore(base_dir=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_scope_disable_restart_isolation_and_reenable(self):
        first = conversation_persona_scope(
            guild_id="g1", channel_id="c1", user_id="u1"
        )
        other_channel = conversation_persona_scope(
            guild_id="g1", channel_id="c2", user_id="u1"
        )
        other_user = conversation_persona_scope(
            guild_id="g1", channel_id="c1", user_id="u2"
        )

        self.assertTrue(self.store.get_conversation_persona_enabled(first))
        self.store.set_conversation_persona_enabled(first, False)
        self.assertFalse(self.store.get_conversation_persona_enabled(first))
        self.assertTrue(self.store.get_conversation_persona_enabled(other_channel))
        self.assertTrue(self.store.get_conversation_persona_enabled(other_user))

        restarted = SQLiteStore(base_dir=self.root)
        self.assertFalse(restarted.get_conversation_persona_enabled(first))
        restarted.set_conversation_persona_enabled(first, True)
        self.assertTrue(self.store.get_conversation_persona_enabled(first))


class PersonaToggleParsingTests(unittest.TestCase):
    def test_disable_requests(self):
        for text in (
            "drop the persona",
            "Please stop role-playing.",
            "answer normally from now on",
            "disable Mistake Not…",
            "leave character please",
        ):
            with self.subTest(text=text):
                self.assertIs(parse_persona_toggle(text), False)

    def test_enable_requests(self):
        for text in (
            "enable Mistake Not",
            "Resume Mistake Not…",
            "resume the persona",
            "please go back into character",
        ):
            with self.subTest(text=text):
                self.assertIs(parse_persona_toggle(text), True)

    def test_discussion_does_not_change_state(self):
        for text in (
            "How do I disable Mistake Not?",
            "Do not drop the persona",
            "Explain what leaving character means",
            "Answer normally, then compare both styles",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_persona_toggle(text))


class PersonaPromptAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.message = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20),
            author=SimpleNamespace(id=30, display_name="Test User"),
        )

    def _build(self, *, enabled: bool):
        with patch(
            "bot.response_policy.build_user_awareness_block",
            return_value="stored awareness",
        ), patch.object(
            chat_context, "build_timeline_prompt_block", return_value="stored timeline"
        ), patch.object(
            chat_context,
            "build_channel_message_window",
            return_value=[{"role": "assistant", "content": "older reply"}],
        ), patch.object(
            chat_context, "search_history_for_context", return_value=None
        ), patch(
            "bot.response_policy.get_user_instruction",
            return_value="Prefer terse technical prose",
        ), patch(
            "bot.persona.get_conversation_persona_enabled", return_value=enabled
        ):
            return chat_context.build_chat_context(
                self.message,
                30,
                "Design the deployment plan",
                task_instructions=["Return a concrete preferred plan."],
            )

    def test_enabled_prompt_is_once_and_ordered_before_history(self):
        messages = self._build(enabled=True)
        contents = [m.get("content") for m in messages]
        joined = "\n".join(str(content) for content in contents)

        self.assertEqual(joined.count(MISTAKE_NOT_PERSONA_PROMPT), 1)
        self.assertEqual(joined.count(PERSONALIZATION_PRIORITY_SYSTEM_MESSAGE), 1)
        self.assertLess(contents.index("Return a concrete preferred plan."), contents.index("stored awareness"))
        self.assertLess(contents.index("stored awareness"), contents.index(MISTAKE_NOT_PERSONA_PROMPT))
        preference_index = next(
            i for i, content in enumerate(contents) if "Prefer terse technical prose" in str(content)
        )
        persona_index = contents.index(MISTAKE_NOT_PERSONA_PROMPT)
        self.assertLess(preference_index, persona_index)
        self.assertLess(persona_index, contents.index("older reply"))
        self.assertEqual(messages[-1], {"role": "user", "content": "Design the deployment plan"})

    def test_disabled_prompt_is_absent_without_removing_other_rules(self):
        messages = self._build(enabled=False)
        joined = "\n".join(str(m.get("content")) for m in messages)
        self.assertNotIn(MISTAKE_NOT_PERSONA_PROMPT, joined)
        self.assertIn("stored awareness", joined)
        self.assertIn("Prefer terse technical prose", joined)
        self.assertIn("Return a concrete preferred plan.", joined)

    def test_individual_profiles_and_rules_remain_isolated_and_override_persona(self):
        other = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20),
            author=SimpleNamespace(id=31, display_name="Other User"),
        )

        def awareness(user_id, display_name=None):
            return f"profile-for-{user_id}: preferred voice {display_name}"

        def instruction(user_id):
            return f"rule-for-{user_id}"

        with patch(
            "bot.response_policy.build_user_awareness_block",
            side_effect=awareness,
        ), patch(
            "bot.response_policy.get_user_instruction",
            side_effect=instruction,
        ), patch(
            "bot.persona.get_conversation_persona_enabled",
            return_value=True,
        ):
            first = build_message_user_style_system_messages(
                self.message, intent="chat"
            )
            second = build_message_user_style_system_messages(other, intent="chat")

        first_text = "\n".join(m["content"] for m in first)
        second_text = "\n".join(m["content"] for m in second)
        self.assertIn("profile-for-30", first_text)
        self.assertIn("rule-for-30", first_text)
        self.assertNotIn("profile-for-31", first_text)
        self.assertIn("profile-for-31", second_text)
        self.assertIn("rule-for-31", second_text)
        self.assertNotIn("profile-for-30", second_text)
        for messages in (first, second):
            contents = [message["content"] for message in messages]
            persona_index = contents.index(MISTAKE_NOT_PERSONA_PROMPT)
            self.assertLess(contents.index(PERSONALIZATION_PRIORITY_SYSTEM_MESSAGE), persona_index)
            self.assertLess(
                next(i for i, content in enumerate(contents) if "profile-for-" in content),
                persona_index,
            )
            self.assertLess(
                next(i for i, content in enumerate(contents) if "rule-for-" in content),
                persona_index,
            )

    def test_non_prose_intent_does_not_receive_persona(self):
        with patch("bot.persona.get_conversation_persona_enabled", return_value=True):
            messages = build_user_style_system_messages(
                30,
                intent="generate_image",
                guild_id=10,
                channel_id=20,
            )
        self.assertEqual(messages, [])

    def test_vision_extraction_is_neutral_and_explanation_is_styled(self):
        style = [{"role": "system", "content": MISTAKE_NOT_PERSONA_PROMPT}]
        extraction = _build_image_extraction_messages(
            prompt="Explain this",
            image_urls=["https://example.test/image.png"],
            reply_context="",
        )
        explanation = _build_image_explanation_messages(
            prompt="Explain this",
            extracted_notes="visible text",
            image_urls=["https://example.test/image.png"],
            reply_context="",
            style_messages=style,
        )
        extraction_text = "\n".join(str(m.get("content")) for m in extraction)
        explanation_text = "\n".join(str(m.get("content")) for m in explanation)
        self.assertNotIn(MISTAKE_NOT_PERSONA_PROMPT, extraction_text)
        self.assertEqual(explanation_text.count(MISTAKE_NOT_PERSONA_PROMPT), 1)


class InternalPromptIsolationTests(unittest.TestCase):
    def test_intent_classifier_does_not_receive_persona(self):
        from providers import openai_intents

        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="chat_light"))],
            usage=None,
            model="gpt-5.6-luna",
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        async def run():
            with patch.object(openai_intents, "get_openai_client", return_value=client), patch(
                "services.usage_costs.record_response"
            ):
                return await openai_intents.classify_intent("Explain entropy")

        self.assertEqual(asyncio.run(run()), "chat_light")
        payload = "\n".join(
            str(message.get("content"))
            for message in create.await_args.kwargs["messages"]
        )
        self.assertNotIn("Mistake Not", payload)


class ProviderPromptCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20),
            author=SimpleNamespace(id=30, display_name="Test User"),
        )
        self.status = SimpleNamespace(edit=AsyncMock())
        self.sent = AsyncMock()

    async def _live_status(self, message, *, coro, **kwargs):
        return self.status, await coro

    async def test_claude_and_gemini_routes_receive_one_persona_message(self):
        verdict = SimpleNamespace(action="accept", revised_answer=None)
        style = [{"role": "system", "content": MISTAKE_NOT_PERSONA_PROMPT}]

        with patch.object(provider_intents, "build_channel_message_window", return_value=[]), patch.object(
            provider_intents,
            "build_message_user_style_system_messages",
            return_value=style,
        ), patch.object(
            provider_intents, "verify_chat_draft", new=AsyncMock(return_value=verdict)
        ), patch.object(
            provider_intents, "invoke_provider", new=AsyncMock(return_value="Claude answer")
        ) as claude_invoke:
            await provider_intents.handle_claude_chat_intent(
                message=self.message,
                prompt="Claude, explain this",
                stream_ok=False,
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=self.sent,
            )

        claude_messages = claude_invoke.await_args.args[2]
        claude_payload = "\n".join(str(m.get("content")) for m in claude_messages)
        self.assertEqual(claude_payload.count(MISTAKE_NOT_PERSONA_PROMPT), 1)

        gemini_invoke = AsyncMock(return_value=("Gemini answer", []))
        with patch.object(provider_intents, "build_channel_message_window", return_value=[]), patch.object(
            provider_intents,
            "build_message_user_style_system_messages",
            return_value=style,
        ), patch.object(
            provider_intents, "verify_chat_draft", new=AsyncMock(return_value=verdict)
        ), patch.object(provider_intents, "invoke_provider", new=gemini_invoke):
            await provider_intents.handle_gemini_chat_intent(
                message=self.message,
                prompt="Gemini explain this",
                gemini_parts=[],
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=self.sent,
                moderation_view_factory=Mock(),
            )

        gemini_context = gemini_invoke.await_args_list[0].kwargs["context"]
        gemini_payload = "\n".join(str(m.get("content")) for m in gemini_context)
        self.assertEqual(gemini_payload.count(MISTAKE_NOT_PERSONA_PROMPT), 1)

    async def test_url_summary_receives_persona_after_task_instruction(self):
        style = [{"role": "system", "content": MISTAKE_NOT_PERSONA_PROMPT}]
        invoke = AsyncMock(return_value="summary")
        with patch.object(
            provider_intents, "_youtube_transcript_for", new=AsyncMock(return_value=None)
        ), patch.object(
            provider_intents, "fetch_url_content", return_value="<p>source</p>"
        ), patch.object(
            provider_intents, "extract_main_text", return_value=("Title", "source")
        ), patch.object(
            provider_intents,
            "build_message_user_style_system_messages",
            return_value=style,
        ), patch.object(provider_intents, "invoke_provider", new=invoke):
            await provider_intents.handle_summarize_url_intent(
                message=self.message,
                url="https://example.test",
                duration_estimate=1,
                stream_ok=False,
                live_status_with_progress=self._live_status,
                send_or_edit_with_truncation=self.sent,
            )

        messages = invoke.await_args.args[2]
        payload = "\n".join(str(m.get("content")) for m in messages)
        self.assertEqual(payload.count(MISTAKE_NOT_PERSONA_PROMPT), 1)
        self.assertIn("Summarize crisply", messages[0]["content"])
        self.assertEqual(messages[1]["content"], MISTAKE_NOT_PERSONA_PROMPT)


if __name__ == "__main__":
    unittest.main()
