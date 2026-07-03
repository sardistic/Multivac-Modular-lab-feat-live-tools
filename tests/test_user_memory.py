import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from services.sqlite_store import SQLiteStore
from services import usage_costs


class UserMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: sqlite3's context manager commits but doesn't
        # close, so Windows may still hold a lock on the db file at teardown.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = SQLiteStore(base_dir=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_fact_crud(self):
        fid = self.store.add_user_fact("u1", "Has a dog named Kevin", "relationship")
        self.store.add_user_fact("u1", "Prefers concise answers", "preference")
        self.store.add_user_fact("u2", "Other user's fact")

        facts = self.store.list_user_facts("u1")
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[-1]["id"], fid)

        deleted = self.store.delete_user_facts_matching("u1", "kevin")
        self.assertEqual(deleted, 1)
        self.assertEqual(len(self.store.list_user_facts("u1")), 1)
        # Other users untouched
        self.assertEqual(len(self.store.list_user_facts("u2")), 1)

    def test_profile_roundtrip(self):
        self.assertIsNone(self.store.get_user_profile("u1"))
        self.store.set_user_profile("u1", "- likes trains")
        prof = self.store.get_user_profile("u1")
        self.assertEqual(prof["profile"], "- likes trains")
        self.assertTrue(prof["updated_at"])

    def test_user_seen_roundtrip(self):
        self.assertIsNone(self.store.get_user_seen("u1"))
        self.store.set_user_seen("u1", intent="chat", prompt="hello there")
        seen = self.store.get_user_seen("u1")
        self.assertEqual(seen["last_intent"], "chat")
        self.assertEqual(seen["last_prompt"], "hello there")

    def test_sora_limit_status_reports_reset(self):
        status = self.store.sora_limit_status("u9", limit=2)
        self.assertTrue(status["allowed"])
        self.assertEqual(status["remaining"], 2)

        self.store.log_sora_usage("u9", video_id="v1")
        self.store.log_sora_usage("u9", video_id="v2")
        status = self.store.sora_limit_status("u9", limit=2)
        self.assertFalse(status["allowed"])
        self.assertEqual(status["remaining"], 0)
        self.assertGreater(status["resets_in_seconds"], 0)
        self.assertLessEqual(status["resets_in_seconds"], 3600)


class UsageCostsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = usage_costs.DB_PATH
        usage_costs.DB_PATH = str(Path(self._tmp.name) / "usage.db")

    def tearDown(self):
        usage_costs.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_record_with_request_context_attributes_user(self):
        usage_costs.set_request_context(user_id="42", intent="chat")
        usage_costs.record(
            "gpt-5.5",
            {"prompt_tokens": 1000, "completion_tokens": 500},
            usage_costs.estimate_cost("gpt-5.5", {"prompt_tokens": 1000, "completion_tokens": 500}),
        )
        mine = usage_costs.today_for_user("42")
        self.assertEqual(mine["calls"], 1)
        self.assertEqual(mine["total_tokens"], 1500)
        self.assertGreater(mine["cost"], 0)

        other = usage_costs.today_for_user("99")
        self.assertEqual(other["calls"], 0)

        top = usage_costs.top_users_today()
        self.assertEqual(top[0]["user_id"], "42")

    def test_estimate_cost_known_and_unknown_models(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        self.assertAlmostEqual(usage_costs.estimate_cost("gpt-5.5", usage), 1.25)
        self.assertEqual(usage_costs.estimate_cost("mystery-model", usage), 0.0)

    def test_responses_api_token_keys_normalized(self):
        usage_costs.set_request_context(user_id="7")
        usage_costs.record("gpt-5.5", {"input_tokens": 200, "output_tokens": 100}, 0.0)
        mine = usage_costs.today_for_user("7")
        self.assertEqual(mine["prompt_tokens"], 200)
        self.assertEqual(mine["completion_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
