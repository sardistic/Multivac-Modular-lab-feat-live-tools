import unittest

from services.code_changes import get_baseline_sha, inspect_patch, validate_patch


README_PATCH = """diff --git a/docs/validator-test.txt b/docs/validator-test.txt
new file mode 100644
--- /dev/null
+++ b/docs/validator-test.txt
@@ -0,0 +1 @@
+validator
"""


class CodeChangePolicyTests(unittest.TestCase):
    def test_safe_text_patch_is_inspected(self):
        report = inspect_patch(README_PATCH)
        self.assertTrue(report["ok"])
        self.assertEqual(report["files"], ["docs/validator-test.txt"])

    def test_protected_path_is_rejected(self):
        patch = """diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1 +1 @@
-import os
+import sys
"""
        report = inspect_patch(patch)
        self.assertFalse(report["ok"])
        self.assertTrue(any("Protected path" in error for error in report["errors"]))

    def test_public_dashboard_cannot_be_changed_by_proposals(self):
        patch = """diff --git a/dashboard/app.js b/dashboard/app.js
--- a/dashboard/app.js
+++ b/dashboard/app.js
@@ -1 +1 @@
-safe
+unsafe
"""
        report = inspect_patch(patch)
        self.assertFalse(report["ok"])
        self.assertTrue(any("dashboard/app.js" in error for error in report["errors"]))

    def test_traversal_and_binary_patches_are_rejected(self):
        patch = """diff --git a/../outside b/../outside
GIT binary patch
literal 0
"""
        report = inspect_patch(patch)
        self.assertFalse(report["ok"])
        self.assertTrue(any("Unsafe patch path" in error for error in report["errors"]))
        self.assertTrue(any("Binary" in error for error in report["errors"]))

    def test_validation_applies_only_to_temporary_baseline_snapshot(self):
        report = validate_patch(get_baseline_sha(), README_PATCH)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["baseline_sha"], get_baseline_sha())

    def test_validation_recounts_incorrect_llm_hunk_totals(self):
        patch = """diff --git a/docs/validator-recount.txt b/docs/validator-recount.txt
new file mode 100644
--- /dev/null
+++ b/docs/validator-recount.txt
@@ -0,0 +1,9 @@
+Git should recount this one-line hunk.
"""
        report = validate_patch(get_baseline_sha(), patch)
        self.assertTrue(report["ok"], report["errors"])


if __name__ == "__main__":
    unittest.main()
