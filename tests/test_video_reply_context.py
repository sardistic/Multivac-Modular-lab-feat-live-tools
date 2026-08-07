import types
import unittest

from bot.video_handler import _compose_reply_aware_video_prompt, _VIDEO_REPLY_PROMPT_MAX_CHARS


def _msg(content):
    return types.SimpleNamespace(content=content)


class VideoReplyPromptTests(unittest.TestCase):
    """Regression: 'make this a video' in reply to a post must animate THAT
    post, not generate an unrelated clip. The replied text has to reach the
    generation prompt."""

    def test_reply_text_is_folded_into_prompt(self):
        greentext = ">be me\n>post something cursed\n>it goes viral"
        out = _compose_reply_aware_video_prompt("make this a video", _msg(greentext))
        self.assertIn("make this a video", out)
        self.assertIn("be me", out)
        self.assertIn("goes viral", out)

    def test_no_reply_returns_bare_prompt(self):
        self.assertEqual(_compose_reply_aware_video_prompt("a cat surfing", None), "a cat surfing")
        self.assertEqual(_compose_reply_aware_video_prompt("a cat surfing", _msg("")), "a cat surfing")

    def test_empty_instruction_uses_reply_as_subject(self):
        out = _compose_reply_aware_video_prompt("", _msg("a neon city in the rain"))
        self.assertEqual(out, "a neon city in the rain")

    def test_long_reply_is_truncated(self):
        out = _compose_reply_aware_video_prompt("make a video", _msg("x " * 5000))
        self.assertLessEqual(len(out), _VIDEO_REPLY_PROMPT_MAX_CHARS + 60)
        self.assertTrue(out.rstrip().endswith("..."))


if __name__ == "__main__":
    unittest.main()
