import unittest

from bot.ui_messages import collapse_list_spacing


class CollapseListSpacingTests(unittest.TestCase):
    """Deterministic backstop: blank lines between list items collapse to a
    single newline (Discord renders \\n\\n as a blank line -> double-spaced),
    while genuine prose paragraph breaks are preserved."""

    def test_numbered_list_double_spacing_collapsed(self):
        src = "1. First tip here.\n\n2. Second tip.\n\n3. Third tip."
        self.assertEqual(collapse_list_spacing(src), "1. First tip here.\n2. Second tip.\n3. Third tip.")

    def test_bulleted_list_collapsed(self):
        src = "- alpha\n\n- beta\n\n- gamma"
        self.assertEqual(collapse_list_spacing(src), "- alpha\n- beta\n- gamma")

    def test_multi_sentence_items_collapsed(self):
        src = "1. Do a thing. It matters.\n\n2. Do another. Also good."
        self.assertEqual(collapse_list_spacing(src), "1. Do a thing. It matters.\n2. Do another. Also good.")

    def test_prose_paragraphs_preserved(self):
        src = "First paragraph of prose.\n\nSecond paragraph of prose."
        self.assertEqual(collapse_list_spacing(src), src)

    def test_intro_line_before_list_preserved(self):
        src = "Here are the tips:\n\n1. One\n2. Two"
        # blank line between the intro sentence and the list stays
        self.assertEqual(collapse_list_spacing(src), src)

    def test_already_tight_list_unchanged(self):
        src = "1. One\n2. Two\n3. Three"
        self.assertEqual(collapse_list_spacing(src), src)

    def test_no_newlines_noop(self):
        self.assertEqual(collapse_list_spacing("just a sentence"), "just a sentence")
        self.assertEqual(collapse_list_spacing(""), "")


if __name__ == "__main__":
    unittest.main()
