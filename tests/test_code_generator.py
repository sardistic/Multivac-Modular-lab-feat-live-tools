import unittest

from services.code_generator import extract_unified_diff


class CodeGeneratorOutputTests(unittest.TestCase):
    def test_extracts_fenced_unified_diff(self):
        text = """Here is the patch:
```diff
diff --git a/bot/response_policy.py b/bot/response_policy.py
--- a/bot/response_policy.py
+++ b/bot/response_policy.py
@@ -1 +1 @@
-import re
+import re  # generated
```
"""
        patch = extract_unified_diff(text)
        self.assertTrue(patch.startswith("diff --git "))
        self.assertIn("bot/response_policy.py", patch)

    def test_rejects_protected_generated_patch(self):
        text = """diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1 +1 @@
-import os
+import sys
"""
        with self.assertRaisesRegex(ValueError, "violated policy"):
            extract_unified_diff(text)

    def test_rejects_prose_without_diff(self):
        with self.assertRaisesRegex(ValueError, "unified Git diff"):
            extract_unified_diff("I would change several files.")


if __name__ == "__main__":
    unittest.main()
