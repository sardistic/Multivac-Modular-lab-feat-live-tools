import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class ProposalSupervisorStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name) / "base"
        self.releases = Path(self.tmp.name) / "releases"
        self.base.mkdir()
        old_base = os.environ.get("MULTIVAC_BASE_DIR")
        old_releases = os.environ.get("MULTIVAC_RELEASES_DIR")
        self.addCleanup(self._restore_env, "MULTIVAC_BASE_DIR", old_base)
        self.addCleanup(self._restore_env, "MULTIVAC_RELEASES_DIR", old_releases)
        os.environ["MULTIVAC_BASE_DIR"] = str(self.base)
        os.environ["MULTIVAC_RELEASES_DIR"] = str(self.releases)

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


if __name__ == "__main__":
    unittest.main()
