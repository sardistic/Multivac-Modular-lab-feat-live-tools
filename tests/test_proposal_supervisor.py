import importlib.util
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ProposalSupervisorStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name) / "base"
        self.releases = Path(self.tmp.name) / "releases"
        self.base.mkdir()
        old_base = os.environ.get("MULTIVAC_BASE_DIR")
        old_releases = os.environ.get("MULTIVAC_RELEASES_DIR")
        old_state = os.environ.get("MULTIVAC_STATE_DIR")
        self.addCleanup(self._restore_env, "MULTIVAC_BASE_DIR", old_base)
        self.addCleanup(self._restore_env, "MULTIVAC_RELEASES_DIR", old_releases)
        self.addCleanup(self._restore_env, "MULTIVAC_STATE_DIR", old_state)
        os.environ["MULTIVAC_BASE_DIR"] = str(self.base)
        os.environ["MULTIVAC_RELEASES_DIR"] = str(self.releases)
        # The release validator supplies a writable state root because the
        # candidate checkout is mounted read-only. Keep each dynamically loaded
        # supervisor test isolated instead of sharing that outer state database.
        os.environ["MULTIVAC_STATE_DIR"] = str(self.base)

        spec = importlib.util.spec_from_file_location(
            "test_supervisor_module",
            Path(__file__).resolve().parent.parent / "ops" / "proposal_supervisor.py",
        )
        self.supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.supervisor)
        sqlite3.connect(self.base / "conversation_history.db").close()
        self.supervisor.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _restore_env(key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def test_state_defaults_to_baseline_and_roundtrips_release(self):
        self.assertEqual(
            self.supervisor.read_state()["active_release"], str(self.base.resolve())
        )
        release = self.releases / "proposal-1-deadbeef"
        self.supervisor.write_state(release, 1)
        state = self.supervisor.read_state()
        self.assertEqual(state["active_release"], str(release))
        self.assertEqual(state["proposal_id"], 1)

    def test_deployment_audit_schema_records_previous_release(self):
        with self.supervisor.db_connect() as conn:
            conn.execute(
                """
                INSERT INTO code_deployments
                    (proposal_id, release_path, patch_sha256, status,
                     previous_release, previous_proposal_id, created_at)
                VALUES (7, '/release/7', 'abc', 'building', '/release/6', 6, 'now')
                """
            )
            row = conn.execute(
                "SELECT previous_release, previous_proposal_id FROM code_deployments"
            ).fetchone()
        self.assertEqual(tuple(row), ("/release/6", 6))

    def test_initialize_configures_private_shared_group_control_directory(self):
        control_dir = mock.Mock()
        with (
            mock.patch.object(self.supervisor, "TOOL_CONTROL_DIR", control_dir),
            mock.patch.object(self.supervisor.os, "name", "posix"),
            mock.patch.object(self.supervisor.os, "geteuid", return_value=0, create=True),
            mock.patch.object(self.supervisor.os, "chown", create=True) as chown,
            mock.patch.object(self.supervisor.os, "access", return_value=True),
        ):
            self.supervisor.initialize()

        chown.assert_called_once_with(control_dir, -1, 65532)
        control_dir.chmod.assert_called_once_with(0o2770)

    def test_baseline_check_reads_canonical_ref(self):
        row = {"baseline_sha": "a" * 40}
        result = mock.Mock(stdout=("a" * 40) + "\n")
        with mock.patch.object(self.supervisor, "run", return_value=result) as run:
            self.supervisor.require_current_baseline(row)

        run.assert_called_once_with(
            ["git", "rev-parse", "--verify", "refs/heads/main^{commit}"],
            cwd=self.base,
        )

    def test_promotion_advances_canonical_ref(self):
        commit = "b" * 40
        with mock.patch.object(self.supervisor, "run") as run:
            self.supervisor.promote_release(commit)

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["git", "push", "origin", f"{commit}:refs/heads/main"],
                    cwd=self.base,
                    timeout=90,
                ),
                mock.call(
                    ["git", "merge", "--ff-only", commit],
                    cwd=self.base,
                    timeout=60,
                ),
                mock.call(
                    ["git", "update-ref", "refs/heads/main", commit],
                    cwd=self.base,
                    timeout=60,
                ),
            ],
        )

    def test_failed_activation_restores_previous_release(self):
        release = self.releases / "proposal-9-abc"
        row = {
            "id": 9,
            "owner_id": "owner-1",
            "baseline_sha": "a" * 40,
            "patch": "diff --git a/a.py b/a.py",
        }
        with (
            mock.patch.object(self.supervisor, "proposal", return_value=row),
            mock.patch.object(self.supervisor, "edit_approval_progress"),
            mock.patch.object(self.supervisor, "refresh_dashboard"),
            mock.patch.object(self.supervisor, "require_current_baseline"),
            mock.patch.object(self.supervisor, "validate_again"),
            mock.patch.object(
                self.supervisor, "create_worktree", return_value=(release, "hash")
            ),
            mock.patch.object(self.supervisor, "test_release"),
            mock.patch.object(self.supervisor, "restore_pristine_release"),
            mock.patch.object(self.supervisor, "commit_release", return_value="b" * 40),
            mock.patch.object(self.supervisor, "promote_release"),
            mock.patch.object(self.supervisor, "sign_release", return_value="signature"),
            mock.patch.object(self.supervisor, "activate_release") as activate,
            mock.patch.object(self.supervisor, "notify_owner"),
            mock.patch.object(
                self.supervisor,
                "healthy",
                side_effect=[(False, "new release failed"), (True, "baseline restored")],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Health check failed"):
                self.supervisor.deploy(9)

        self.assertEqual(
            activate.call_args_list,
            [mock.call(release, 9), mock.call(self.base.resolve(), None)],
        )
        with self.supervisor.db_connect() as conn:
            deployment = conn.execute(
                "SELECT status, detail FROM code_deployments WHERE proposal_id=9"
            ).fetchone()
        self.assertEqual(deployment["status"], "failed")
        self.assertIn("Health check failed", deployment["detail"])

    def test_only_standalone_live_tool_modules_use_hotload_path(self):
        tool = {
            "patch": "diff --git a/live_tools/echo.py b/live_tools/echo.py",
            "validation_json": json.dumps({"files": ["live_tools/echo.py"]}),
        }
        mixed = {
            "patch": tool["patch"],
            "validation_json": json.dumps(
                {"files": ["live_tools/echo.py", "readme.md"]}
            ),
        }
        self.assertTrue(self.supervisor.is_tool_only_proposal(tool))
        self.assertFalse(self.supervisor.is_tool_only_proposal(mixed))

        command = {
            "patch": "diff --git a/live_commands/hello.py b/live_commands/hello.py",
            "validation_json": json.dumps({"files": ["live_commands/hello.py"]}),
        }
        mixed_kinds = {
            "patch": command["patch"],
            "validation_json": json.dumps(
                {"files": ["live_commands/hello.py", "live_tools/echo.py"]}
            ),
        }
        self.assertTrue(self.supervisor.is_command_only_proposal(command))
        self.assertEqual(self.supervisor.hotload_kind(command), "command")
        self.assertIsNone(self.supervisor.hotload_kind(mixed_kinds))

        behavior = {
            "patch": "diff --git a/live_components/chat.py b/live_components/chat.py",
            "validation_json": json.dumps({"files": ["live_components/chat.py"]}),
        }
        self.assertTrue(self.supervisor.is_behavior_only_proposal(behavior))
        self.assertEqual(self.supervisor.hotload_kind(behavior), "behavior")

    def test_tool_only_deploy_activates_without_recreating_container(self):
        release = self.releases / "proposal-12-abc"
        artifact = self.supervisor.TOOL_ARTIFACTS_DIR / "proposal-12-hash"
        operation = {
            "action": "activate",
            "source_id": "hotload:live_tools/echo.py",
            "relative_path": "proposal-12-hash/live_tools/echo.py",
            "sha256": "d" * 64,
            "proposal_id": 12,
        }
        active = {
            "relative_path": operation["relative_path"],
            "sha256": operation["sha256"],
            "proposal_id": 12,
        }
        row = {
            "id": 12,
            "public_id": "calm-tool",
            "owner_id": "owner-1",
            "baseline_sha": "a" * 40,
            "patch": "diff --git a/live_tools/echo.py b/live_tools/echo.py",
            "validation_json": json.dumps({"files": ["live_tools/echo.py"]}),
        }
        with (
            mock.patch.object(self.supervisor, "proposal", return_value=row),
            mock.patch.object(self.supervisor, "edit_approval_progress"),
            mock.patch.object(self.supervisor, "refresh_dashboard"),
            mock.patch.object(self.supervisor, "require_current_baseline"),
            mock.patch.object(self.supervisor, "validate_again"),
            mock.patch.object(
                self.supervisor, "create_worktree", return_value=(release, "hash")
            ),
            mock.patch.object(self.supervisor, "test_release"),
            mock.patch.object(self.supervisor, "test_tool_modules") as test_tools,
            mock.patch.object(self.supervisor, "restore_pristine_release"),
            mock.patch.object(self.supervisor, "commit_release", return_value="b" * 40),
            mock.patch.object(
                self.supervisor,
                "publish_tool_artifacts",
                return_value=(artifact, "signature", [operation], {}),
            ),
            mock.patch.object(
                self.supervisor,
                "request_tool_activation",
                return_value={"ok": True, "generation": 4},
            ) as activate_tools,
            mock.patch.object(self.supervisor, "activate_release") as activate_release,
            mock.patch.object(self.supervisor, "healthy", return_value=(True, "ready")),
            mock.patch.object(self.supervisor, "promote_release"),
            mock.patch.object(
                self.supervisor,
                "read_active_tools",
                return_value={"version": 1, "sources": {operation["source_id"]: active}},
            ),
            mock.patch.object(self.supervisor, "notify_owner"),
            mock.patch.object(self.supervisor, "prune_releases"),
        ):
            self.supervisor.deploy(12)

        test_tools.assert_called_once_with(release, ["live_tools/echo.py"])
        activate_tools.assert_called_once_with(12, [operation])
        activate_release.assert_not_called()
        with self.supervisor.db_connect() as conn:
            deployment = conn.execute(
                """
                SELECT status, deployment_kind, release_path, hotload_state_json
                FROM code_deployments WHERE proposal_id=12
                """
            ).fetchone()
        self.assertEqual(deployment["status"], "active")
        self.assertEqual(deployment["deployment_kind"], "tool")
        self.assertEqual(deployment["release_path"], str(artifact))
        self.assertEqual(json.loads(deployment["hotload_state_json"]), {operation["source_id"]: active})

    def test_published_tool_artifact_has_content_signature(self):
        release = self.releases / "proposal-20-abc"
        module = release / "live_tools" / "echo.py"
        module.parent.mkdir(parents=True)
        module.write_text("TOOL_SPECS = []\nTOOL_HANDLERS = {}\n", encoding="utf-8")
        key_path = Path(self.tmp.name) / "signing.key"
        key_path.write_bytes(b"test-signing-key")
        row = {"id": 20, "baseline_sha": "a" * 40}
        with mock.patch.object(self.supervisor, "SIGNING_KEY_PATH", key_path):
            artifact, signature, operations, previous = self.supervisor.publish_tool_artifacts(
                row, release, "b" * 64, ["live_tools/echo.py"]
            )

        manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
        unsigned = dict(manifest)
        unsigned.pop("signature")
        payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(key_path.read_bytes(), payload, hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(manifest["signature"], expected)
        self.assertEqual(operations[0]["source_id"], "hotload:live_tools/echo.py")
        self.assertEqual(previous, {})
        self.assertEqual((artifact / "live_tools" / "echo.py").read_text(), module.read_text())

    def test_command_only_deploy_syncs_without_recreating_container(self):
        release = self.releases / "proposal-13-abc"
        artifact = self.supervisor.TOOL_ARTIFACTS_DIR / "proposal-13-hash"
        operation = {
            "action": "activate",
            "source_id": "hotcommand:live_commands/hello.py",
            "relative_path": "proposal-13-hash/live_commands/hello.py",
            "sha256": "e" * 64,
            "proposal_id": 13,
        }
        active = {
            "relative_path": operation["relative_path"],
            "sha256": operation["sha256"],
            "proposal_id": 13,
        }
        row = {
            "id": 13,
            "public_id": "calm-command",
            "owner_id": "owner-1",
            "baseline_sha": "a" * 40,
            "patch": "diff --git a/live_commands/hello.py b/live_commands/hello.py",
            "validation_json": json.dumps({"files": ["live_commands/hello.py"]}),
        }
        with (
            mock.patch.object(self.supervisor, "proposal", return_value=row),
            mock.patch.object(self.supervisor, "edit_approval_progress"),
            mock.patch.object(self.supervisor, "refresh_dashboard"),
            mock.patch.object(self.supervisor, "require_current_baseline"),
            mock.patch.object(self.supervisor, "validate_again"),
            mock.patch.object(
                self.supervisor, "create_worktree", return_value=(release, "hash")
            ),
            mock.patch.object(self.supervisor, "test_release"),
            mock.patch.object(self.supervisor, "test_command_modules") as test_commands,
            mock.patch.object(self.supervisor, "restore_pristine_release"),
            mock.patch.object(self.supervisor, "commit_release", return_value="b" * 40),
            mock.patch.object(
                self.supervisor,
                "publish_command_artifacts",
                return_value=(artifact, "signature", [operation], {}),
            ),
            mock.patch.object(
                self.supervisor,
                "request_command_activation",
                return_value={"ok": True, "generation": 5, "synced_commands": 20},
            ) as activate_commands,
            mock.patch.object(self.supervisor, "activate_release") as activate_release,
            mock.patch.object(self.supervisor, "promote_release"),
            mock.patch.object(
                self.supervisor,
                "read_active_commands",
                return_value={"version": 1, "sources": {operation["source_id"]: active}},
            ),
            mock.patch.object(self.supervisor, "notify_owner"),
            mock.patch.object(self.supervisor, "prune_releases"),
        ):
            self.supervisor.deploy(13)

        test_commands.assert_called_once_with(release, ["live_commands/hello.py"])
        activate_commands.assert_called_once_with(13, [operation])
        activate_release.assert_not_called()
        with self.supervisor.db_connect() as conn:
            deployment = conn.execute(
                "SELECT status, deployment_kind, hotload_state_json FROM code_deployments WHERE proposal_id=13"
            ).fetchone()
        self.assertEqual(deployment["status"], "active")
        self.assertEqual(deployment["deployment_kind"], "command")
        self.assertEqual(json.loads(deployment["hotload_state_json"]), {operation["source_id"]: active})

    def test_behavior_only_deploy_activates_without_recreating_container(self):
        release = self.releases / "proposal-14-abc"
        artifact = self.supervisor.TOOL_ARTIFACTS_DIR / "proposal-14-hash"
        operation = {
            "action": "activate",
            "source_id": "hotbehavior:live_components/chat.py",
            "relative_path": "proposal-14-hash/live_components/chat.py",
            "sha256": "f" * 64,
            "proposal_id": 14,
        }
        active = {
            "relative_path": operation["relative_path"],
            "sha256": operation["sha256"],
            "proposal_id": 14,
        }
        row = {
            "id": 14,
            "public_id": "calm-behavior",
            "owner_id": "owner-1",
            "baseline_sha": "a" * 40,
            "patch": "diff --git a/live_components/chat.py b/live_components/chat.py",
            "validation_json": json.dumps({"files": ["live_components/chat.py"]}),
        }
        with (
            mock.patch.object(self.supervisor, "proposal", return_value=row),
            mock.patch.object(self.supervisor, "edit_approval_progress"),
            mock.patch.object(self.supervisor, "refresh_dashboard"),
            mock.patch.object(self.supervisor, "require_current_baseline"),
            mock.patch.object(self.supervisor, "validate_again"),
            mock.patch.object(
                self.supervisor, "create_worktree", return_value=(release, "hash")
            ),
            mock.patch.object(self.supervisor, "test_release"),
            mock.patch.object(self.supervisor, "test_behavior_modules") as test_behaviors,
            mock.patch.object(self.supervisor, "restore_pristine_release"),
            mock.patch.object(self.supervisor, "commit_release", return_value="b" * 40),
            mock.patch.object(
                self.supervisor,
                "publish_behavior_artifacts",
                return_value=(artifact, "signature", [operation], {}),
            ),
            mock.patch.object(
                self.supervisor,
                "request_behavior_activation",
                return_value={"ok": True, "generation": 6},
            ) as activate_behaviors,
            mock.patch.object(self.supervisor, "activate_release") as activate_release,
            mock.patch.object(self.supervisor, "promote_release"),
            mock.patch.object(
                self.supervisor,
                "read_active_behaviors",
                return_value={"version": 1, "sources": {operation["source_id"]: active}},
            ),
            mock.patch.object(self.supervisor, "notify_owner"),
            mock.patch.object(self.supervisor, "prune_releases"),
        ):
            self.supervisor.deploy(14)

        test_behaviors.assert_called_once_with(release, ["live_components/chat.py"])
        activate_behaviors.assert_called_once_with(14, [operation])
        activate_release.assert_not_called()
        with self.supervisor.db_connect() as conn:
            deployment = conn.execute(
                "SELECT status, deployment_kind FROM code_deployments WHERE proposal_id=14"
            ).fetchone()
        self.assertEqual(tuple(deployment), ("active", "behavior"))

    def test_tool_rollback_restores_recorded_previous_artifact(self):
        source_id = "hotload:live_tools/echo.py"
        current = {
            "relative_path": "proposal-2/live_tools/echo.py",
            "sha256": "2" * 64,
            "proposal_id": 2,
        }
        previous = {
            "relative_path": "proposal-1/live_tools/echo.py",
            "sha256": "1" * 64,
            "proposal_id": 1,
        }
        self.supervisor._atomic_json(
            self.supervisor.TOOL_CONTROL_DIR / "active-tools.json",
            {"version": 1, "sources": {source_id: current}},
        )
        with self.supervisor.db_connect() as conn:
            conn.execute(
                """
                INSERT INTO code_deployments
                    (proposal_id, release_path, patch_sha256, status,
                     previous_release, previous_proposal_id, created_at,
                     deployment_kind, hotload_state_json, previous_hotload_state_json)
                VALUES (2, '/artifact/2', 'abc', 'active', '', NULL, 'now',
                        'tool', ?, ?)
                """,
                (json.dumps({source_id: current}), json.dumps({source_id: previous})),
            )
        with mock.patch.object(
            self.supervisor,
            "request_tool_activation",
            return_value={"ok": True, "generation": 8},
        ) as request, mock.patch.object(self.supervisor, "refresh_dashboard"):
            result = self.supervisor.rollback_deployment(2)

        self.assertEqual(result, 2)
        request.assert_called_once_with(
            2,
            [{
                "action": "activate",
                "source_id": source_id,
                "relative_path": previous["relative_path"],
                "sha256": previous["sha256"],
                "proposal_id": 1,
            }],
        )
        with self.supervisor.db_connect() as conn:
            status = conn.execute(
                "SELECT status FROM code_deployments WHERE proposal_id=2"
            ).fetchone()[0]
        self.assertEqual(status, "rolled_back")


if __name__ == "__main__":
    unittest.main()
