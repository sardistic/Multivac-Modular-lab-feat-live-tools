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

    def test_everything_else_defers_to_classifier(self):
        cases = [
            ("gemini what is entropy", False),
            ("gemini generate an image of a liminal rose", False),
            ("gemini make this a video", True),
            ("gemini edit this image", True),
            ("generate imagine of liminal rose", False),
            ("imagine a castle at dusk", False),
            ("make me a picture of a dog", False),
            ("generate a video of a cat", False),
            ("what's the weather", False),
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


if __name__ == "__main__":
    unittest.main()
