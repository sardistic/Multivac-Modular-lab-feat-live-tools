import asyncio
import os
import unittest

from bot.intent_dispatcher import resolve_keyword_intent, wants_gemini


class KeywordRoutingTests(unittest.TestCase):
    """Keywords pick the provider; the LLM classifier picks the intent.
    resolve_keyword_intent only shortcuts explicit 'claude ...' messages."""

    def test_claude_prefix_shortcuts_to_claude_chat(self):
        self.assertEqual(
            resolve_keyword_intent("claude what is entropy", "claude what is entropy", False),
            "claude_chat",
        )
        self.assertEqual(
            resolve_keyword_intent("<@123> something", "Claude something", False),
            "claude_chat",
        )

    def test_imagine_is_a_deterministic_image_command(self):
        cases = [
            "imagine a castle at dusk",
            "imagine a cool hackerman yelling at a chatbot",
            "gemini imagine a liminal rose",
            "IMAGINE: neon rain",
        ]
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(resolve_keyword_intent(prompt, prompt, False), "generate_image")

    def test_everything_else_defers_to_classifier(self):
        cases = [
            ("gemini what is entropy", False),
            ("gemini generate an image of a liminal rose", False),
            ("gemini make this a video", True),
            ("gemini edit this image", True),
            ("generate imagine of liminal rose", False),
            ("make me a picture of a dog", False),
            ("generate a video of a cat", False),
            ("what's the weather", False),
            ("imagines are weird", False),  # not the command word
            ("", False),
        ]
        for prompt, has_attachments in cases:
            with self.subTest(prompt=prompt):
                self.assertIsNone(resolve_keyword_intent(prompt, prompt, has_attachments))


class ProviderSelectionTests(unittest.TestCase):
    def test_wants_gemini(self):
        positive = [
            "gemini imagine a castle",
            "Gemini generate an image of a rose",
            "generate an image of a rose with gemini",
            "make a picture using gemini",
        ]
        negative = [
            "imagine a castle",
            "generate an image of the gemini zodiac sign",
            "draw the gemini constellation",
            "",
        ]
        for p in positive:
            with self.subTest(p=p):
                self.assertTrue(wants_gemini(p))
        for p in negative:
            with self.subTest(p=p):
                self.assertFalse(wants_gemini(p))


class ClassifierOutageFallbackTests(unittest.TestCase):
    """When the OpenAI classifier can't run, routing must degrade gracefully:
    an explicit 'gemini ...' request still reaches Gemini instead of bouncing
    to 'chat' (which would hit the same dead OpenAI backend)."""

    def test_keyword_fallback_routes_gemini_prefix(self):
        from providers.openai_intents import _keyword_fallback_intent

        self.assertEqual(_keyword_fallback_intent("gemini tell me a joke"), "gemini_chat")
        self.assertEqual(_keyword_fallback_intent("  GEMINI what's up"), "gemini_chat")

    def test_keyword_fallback_defaults_to_chat(self):
        from providers.openai_intents import _keyword_fallback_intent

        for text in ("what's the weather", "tell me about entropy", "", "claude hi"):
            with self.subTest(text=text):
                self.assertEqual(_keyword_fallback_intent(text), "chat")

    def test_classify_intent_falls_back_when_openai_down(self):
        import providers.openai_intents as oi

        def _boom():
            raise RuntimeError("insufficient_quota")

        original = oi.get_openai_client
        oi.get_openai_client = _boom
        try:
            self.assertEqual(asyncio.run(oi.classify_intent("gemini lose weight tips")), "gemini_chat")
            self.assertEqual(asyncio.run(oi.classify_intent("how do magnets work")), "chat")
        finally:
            oi.get_openai_client = original


class ChatOutageDetectionTests(unittest.TestCase):
    """The Gemini chat fallback fires on OpenAI's error sentinel strings and
    nothing else."""

    def test_detects_openai_error_sentinels(self):
        from bot.chat_handler import _is_openai_outage

        self.assertTrue(_is_openai_outage("⚠️ OpenAI tools error: Error code: 429 - quota"))
        self.assertTrue(_is_openai_outage("⚠️ OpenAI error: connection reset"))

    def test_ignores_normal_answers(self):
        from bot.chat_handler import _is_openai_outage

        for text in ("Here's how magnets work…", "", None, "⚠️ Response Blocked by Safety Filters"):
            with self.subTest(text=text):
                self.assertFalse(_is_openai_outage(text))


@unittest.skipUnless(
    os.getenv("RUN_LIVE_INTENT_TESTS") == "1",
    "live classifier test; set RUN_LIVE_INTENT_TESTS=1 to run",
)
class LiveReplyToImageClassifierTests(unittest.TestCase):
    """Replies to a just-generated image must split by what they ask for:
    reactions are commentary, change requests are edits, 'another one' is a
    fresh generation. Hits the real classifier model, so opt-in only."""

    RECENT_TURNS = [
        "user: imagine a retro movie theater marquee at night",
        "bot: ✅ Image generated (GPT Image 1.5) [image attached]",
    ]

    def _classify(self, text: str) -> str:
        from providers.openai_intents import classify_intent

        return asyncio.run(
            classify_intent(
                text,
                recent_turns=self.RECENT_TURNS,
                prev_intent="generate_image",
            )
        )

    def test_bare_reaction_is_chat_light(self):
        for reaction in ("kino", "nice", "based", "lol"):
            with self.subTest(reaction=reaction):
                self.assertEqual(self._classify(reaction), "chat_light")

    def test_change_request_is_edit_image(self):
        for prompt in ("make it darker", "remove the text on the marquee"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self._classify(prompt), "edit_image")

    def test_another_one_is_generate_image(self):
        for prompt in ("another one", "same but at sunrise"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self._classify(prompt), "generate_image")


@unittest.skipUnless(
    os.getenv("RUN_LIVE_INTENT_TESTS") == "1",
    "live classifier test; set RUN_LIVE_INTENT_TESTS=1 to run",
)
class LiveWriteVsDepictTests(unittest.TestCase):
    """Asking to WRITE text about a visual/horror subject must stay text, even
    when the text-intent word is garbled ('fan function' for 'fanfiction').
    Regression for 'one sentence doki doki fan function horror' -> image."""

    def _classify(self, text: str) -> str:
        from providers.openai_intents import classify_intent

        return asyncio.run(classify_intent(text))

    def test_write_requests_stay_text(self):
        for prompt in (
            "one sentence doki doki fan function horror",
            "write one sentence of doki doki horror",
            "a haiku about a burning city",
            "give me a caption for a spooky doki doki scene",
        ):
            with self.subTest(prompt=prompt):
                self.assertIn(self._classify(prompt), {"chat", "chat_light"})

    def test_real_image_requests_still_route_to_image(self):
        for prompt in (
            "a moody picture of rain on neon streets",
            "draw a doki doki character in horror style",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(self._classify(prompt), "generate_image")


if __name__ == "__main__":
    unittest.main()
