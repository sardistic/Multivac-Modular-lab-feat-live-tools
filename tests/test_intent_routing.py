import unittest

from bot.intent_dispatcher import resolve_keyword_intent


class KeywordIntentRoutingTests(unittest.TestCase):
    def test_routing_table(self):
        # (raw_prompt, prompt, has_attachments, expected_intent)
        cases = [
            # Explicit provider prefixes
            ("gemini imagine a castle", "gemini imagine a castle", False, "generate_image"),
            ("claude what is entropy", "claude what is entropy", False, "claude_chat"),
            ("gemini what is entropy", "gemini what is entropy", False, "gemini_chat"),
            # Gemini + video keywords beat image editing and chat
            ("gemini make this a video", "gemini make this a video", True, "generate_video"),
            ("gemini animate this", "gemini animate this", True, "generate_video"),
            # Gemini + edit keywords + attachment routes to image editing
            ("gemini edit this image", "gemini edit this image", True, "edit_image"),
            ("gemini remove the background", "gemini remove the background", True, "edit_image"),
            # Same wording without an attachment stays chat
            ("gemini remove the background", "gemini remove the background", False, "gemini_chat"),
            # Image-generation phrasing beyond the literal "gemini imagine"
            ("gemini generate an image of a liminal rose", "gemini generate an image of a liminal rose", False, "generate_image"),
            ("gemini draw a picture of a cat", "gemini draw a picture of a cat", False, "generate_image"),
            ("gemini create art of a sunset", "gemini create art of a sunset", False, "generate_image"),
            # But plain gemini questions without image nouns stay chat
            ("gemini generate a haiku", "gemini generate a haiku", False, "gemini_chat"),
            # Generic video generation phrasing
            ("generate a video of a cat", "generate a video of a cat", False, "generate_video"),
            ("generate a sora clip", "generate a sora clip", False, "generate_video"),
            # No keyword match: defer to the LLM classifier
            ("what's the weather", "what's the weather", False, None),
            ("describe this", "describe this", True, None),
            ("", "", False, None),
        ]
        for raw_prompt, prompt, has_attachments, expected in cases:
            with self.subTest(raw=raw_prompt, attachments=has_attachments):
                self.assertEqual(
                    resolve_keyword_intent(raw_prompt, prompt, has_attachments),
                    expected,
                )

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(
            resolve_keyword_intent("  Claude hello", "  Claude hello", False),
            "claude_chat",
        )
        self.assertEqual(
            resolve_keyword_intent("GEMINI IMAGINE a dog", "GEMINI IMAGINE a dog", False),
            "generate_image",
        )

    def test_mention_stripped_prompt_still_matches(self):
        # raw_prompt may retain the mention while prompt has it stripped
        self.assertEqual(
            resolve_keyword_intent("<@123> something", "claude something", False),
            "claude_chat",
        )


if __name__ == "__main__":
    unittest.main()
