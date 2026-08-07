import os
import subprocess
import unittest
import uuid

from services import git_utils

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IS_GIT = subprocess.run(
    ["git", "-C", _REPO_ROOT, "rev-parse", "--is-inside-work-tree"],
    capture_output=True,
).returncode == 0


@unittest.skipUnless(_IS_GIT, "requires a git work tree")
class GitSearchNoMatchTests(unittest.TestCase):
    """git grep exits 1 on no matches; that must read as 'no results', not a
    tool failure — otherwise the model gives up instead of trying another
    pattern (e.g. it searched @bot.tree.command when commands use
    @bot.hybrid_command)."""

    def setUp(self):
        self._old = git_utils.REPO_PATH
        git_utils.REPO_PATH = _REPO_ROOT

    def tearDown(self):
        git_utils.REPO_PATH = self._old

    def test_no_match_returns_empty_not_error(self):
        # Runtime-generated needle so this literal can't appear in the (tracked)
        # test source and match itself via git grep.
        needle = "nomatch" + uuid.uuid4().hex
        results = git_utils.search_code(needle)
        self.assertEqual(results, [])

    def test_real_pattern_returns_matches(self):
        # A pattern that exists in a non-internal file (the actual command decorator).
        results = git_utils.search_code("@bot.hybrid_command")
        self.assertTrue(results)
        self.assertNotIn("error", results[0])
        self.assertTrue(any(r["file"].endswith("discord_bot.py") for r in results))


if __name__ == "__main__":
    unittest.main()
