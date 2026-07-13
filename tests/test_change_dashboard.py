import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ops.export_change_dashboard import export_snapshot, sanitize_summary


class DashboardSanitizationTests(unittest.TestCase):
    def test_summary_redacts_sensitive_public_fields(self):
        text = (
            "Ask <@123456> to use https://internal.example/x from 10.0.0.8 "
            "with API_KEY=super-secret-value and email me@example.com in /srv/private/file"
        )
        clean = sanitize_summary(text)
        for secret in ("123456", "internal.example", "10.0.0.8", "super-secret-value", "me@example.com", "/srv/private"):
            self.assertNotIn(secret, clean)
        self.assertIn("[redacted]", clean)

    def test_export_excludes_identity_raw_patch_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "audit.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE code_proposals (
                    id INTEGER, owner_id TEXT, request TEXT, baseline_sha TEXT,
                    patch TEXT, status TEXT, validation_json TEXT, created_at TEXT,
                    updated_at TEXT, reviewed_at TEXT
                );
                CREATE TABLE code_deployments (
                    proposal_id INTEGER, status TEXT, patch_sha256 TEXT,
                    activated_at TEXT, finished_at TEXT, detail TEXT, release_path TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO code_proposals VALUES (1,'discord-secret','Change readme','abc123','RAW DIFF','reviewable',?,'now','now',NULL)",
                (json.dumps({"files": ["readme.md"]}),),
            )
            conn.execute(
                "INSERT INTO code_deployments VALUES (1,'active','def456','now',NULL,'Discord ready','/srv/private/release')"
            )
            conn.commit()
            conn.close()
            snapshot = export_snapshot(db_path, base_dir=root)
            serialized = json.dumps(snapshot)
            self.assertNotIn("discord-secret", serialized)
            self.assertNotIn("RAW DIFF", serialized)
            self.assertNotIn("/srv/private", serialized)
            self.assertEqual(snapshot["proposals"][0]["files"], ["readme.md"])
            self.assertEqual(snapshot["proposals"][0]["deployment_result"], "Healthy")


if __name__ == "__main__":
    unittest.main()
