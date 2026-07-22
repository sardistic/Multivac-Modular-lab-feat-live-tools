import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.code_changes import get_baseline_sha, inspect_patch, validate_patch


README_PATCH = """diff --git a/docs/validator-test.txt b/docs/validator-test.txt
new file mode 100644
--- /dev/null
+++ b/docs/validator-test.txt
@@ -0,0 +1 @@
+validator
"""


class CodeChangePolicyTests(unittest.TestCase):
    def test_baseline_uses_canonical_branch_from_detached_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            tracked = repo / "tracked.txt"
            tracked.write_text("canonical\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "canonical",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            canonical = subprocess.run(
                ["git", "rev-parse", "main"], cwd=repo, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(["git", "switch", "--detach"], cwd=repo, check=True, capture_output=True)
            tracked.write_text("detached\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "detached",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            detached = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            self.assertNotEqual(canonical, detached)
            with (
                mock.patch("services.code_changes.REPO_PATH", str(repo)),
                mock.patch.dict(os.environ, {"MULTIVAC_CANONICAL_BRANCH": "main"}),
            ):
                self.assertEqual(get_baseline_sha(), canonical)

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

    def test_live_tool_authority_cannot_be_changed_by_proposals(self):
        for path in (
            "services/tool_runtime.py",
            "services/tool_control.py",
            "services/tools_registry.py",
            "services/tool_dispatch.py",
            "services/command_runtime.py",
            "services/command_control.py",
            "services/behavior_runtime.py",
            "services/behavior_registry.py",
            "services/behavior_control.py",
            "dev/validate_tool_modules.py",
            "dev/validate_command_modules.py",
            "dev/validate_behavior_modules.py",
        ):
            with self.subTest(path=path):
                patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-safe
+unsafe
"""
                report = inspect_patch(patch)
                self.assertFalse(report["ok"])
                self.assertTrue(any(path in error for error in report["errors"]))

    def test_live_tools_must_be_standalone_tool_only_modules(self):
        mixed = """diff --git a/live_tools/echo.py b/live_tools/echo.py
new file mode 100644
--- /dev/null
+++ b/live_tools/echo.py
@@ -0,0 +1 @@
+TOOL_SPECS = []
diff --git a/readme.md b/readme.md
--- a/readme.md
+++ b/readme.md
@@ -1 +1 @@
-old
+new
"""
        report = inspect_patch(mixed)
        self.assertFalse(report["ok"])
        self.assertTrue(any("proposed separately" in error for error in report["errors"]))

        mixed_kinds = """diff --git a/live_tools/echo.py b/live_tools/echo.py
new file mode 100644
--- /dev/null
+++ b/live_tools/echo.py
@@ -0,0 +1 @@
+TOOL_SPECS = []
diff --git a/live_commands/hello.py b/live_commands/hello.py
new file mode 100644
--- /dev/null
+++ b/live_commands/hello.py
@@ -0,0 +1 @@
+async def setup(bot): pass
"""
        report = inspect_patch(mixed_kinds)
        self.assertFalse(report["ok"])
        self.assertTrue(any("separate proposals" in error for error in report["errors"]))

        mixed_behavior = """diff --git a/live_components/chat.py b/live_components/chat.py
new file mode 100644
--- /dev/null
+++ b/live_components/chat.py
@@ -0,0 +1 @@
+BEHAVIOR_HANDLERS = {}
diff --git a/live_commands/hello.py b/live_commands/hello.py
new file mode 100644
--- /dev/null
+++ b/live_commands/hello.py
@@ -0,0 +1 @@
+async def setup(bot): pass
"""
        report = inspect_patch(mixed_behavior)
        self.assertFalse(report["ok"])
        self.assertTrue(any("separate proposals" in error for error in report["errors"]))

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
