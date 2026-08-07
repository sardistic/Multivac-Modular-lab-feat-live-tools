import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.reflection_store import ReflectionStore


class ReflectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = str(Path(self.tempdir.name) / "reflection.db")
        self.env = patch.dict("os.environ", {"REFLECTION_DB_PATH": db_path})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ReflectionStore()

    def test_consent_latch_and_forget_remove_derived_state(self):
        self.assertFalse(self.store.user_enabled("42"))
        self.store.set_user_enabled("42", True)
        self.assertTrue(self.store.user_enabled("42"))

        at = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
        self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            idle_minutes=5,
            lookback_minutes=10,
            at=at,
        )
        session = self.store.due_sessions(now=at + timedelta(minutes=6))[0]
        insight_id = self.store.add_insight(
            session=session,
            kind="pain_point",
            summary="The response omitted the requested file link.",
            confidence=0.9,
            evidence_ids=["100"],
        )
        self.store.save_idea(
            {
                "title": "Add the missing file link",
                "problem": "Users cannot open generated files.",
                "proposal": "Attach a link in the response policy.",
                "expected_impact": "Fewer follow-up requests.",
                "risk": "Avoid exposing private paths.",
                "hotload_kind": "behavior",
                "code_paths": ["bot/response_policy.py"],
            },
            [insight_id],
        )

        self.store.forget_user("42")

        self.assertFalse(self.store.user_enabled("42"))
        self.assertEqual(self.store.pending_insights(), [])
        self.assertEqual(self.store.list_ideas(), [])
        self.assertEqual(
            self.store.due_sessions(now=at + timedelta(days=1)),
            [],
        )

    def test_recent_user_signals_are_consent_scoped_and_metadata_only(self):
        self.store.set_user_enabled("42", True)
        self.store.set_user_enabled("99", True)
        own_session_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            idle_minutes=5,
        )
        other_session_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="99",
            message_id="200",
            idle_minutes=5,
        )
        self.store.add_insight(
            session=self.store.get_session(own_session_id),
            kind="behavior_pattern",
            summary="The requester repeatedly prefers the answer before background detail.",
            confidence=0.88,
            evidence_ids=["100"],
        )
        self.store.add_insight(
            session=self.store.get_session(other_session_id),
            kind="pain_point",
            summary="Another requester disliked bullet lists.",
            confidence=0.99,
            evidence_ids=["200"],
        )

        signals = self.store.recent_user_signals("42")

        self.assertEqual(len(signals), 1)
        self.assertIn("answer before background detail", signals[0]["summary"])
        self.assertEqual(
            set(signals[0]),
            {"kind", "summary", "confidence", "occurrences", "last_seen_at"},
        )
        self.assertEqual(self.store.recent_user_signals("unconsented"), [])

    def test_each_channel_message_extends_five_minute_idle_window(self):
        at = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
        first = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            idle_minutes=5,
            lookback_minutes=10,
            at=at,
        )
        second = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="101",
            idle_minutes=5,
            lookback_minutes=10,
            at=at + timedelta(minutes=4),
        )
        active = self.store.record_channel_activity(
            guild_id="1",
            channel_id="2",
            message_id="102",
            idle_minutes=5,
            at=at + timedelta(minutes=8),
        )

        self.assertEqual(first, second)
        self.assertEqual([item["id"] for item in active], [first])
        session = self.store.due_sessions(now=at + timedelta(minutes=14))[0]
        self.assertEqual(session["message_ids"], ["100", "101", "102"])
        self.assertEqual(
            datetime.fromisoformat(session["started_at"]),
            at - timedelta(minutes=10),
        )
        self.assertEqual(
            datetime.fromisoformat(session["expires_at"]),
            at + timedelta(minutes=13),
        )

    def test_channel_message_after_idle_expiry_does_not_reopen_session(self):
        at = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
        session_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            idle_minutes=5,
            at=at,
        )

        active = self.store.record_channel_activity(
            guild_id="1",
            channel_id="2",
            message_id="101",
            idle_minutes=5,
            at=at + timedelta(minutes=6),
        )

        self.assertEqual(active, [])
        session = self.store.get_session(session_id)
        self.assertEqual(session["message_ids"], ["100"])

    def test_final_synthesis_retry_is_not_treated_as_a_live_session(self):
        now = datetime.now(timezone.utc)
        session_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            idle_minutes=5,
            at=now - timedelta(minutes=6),
        )
        self.assertTrue(self.store.claim_session(session_id))
        self.store.retry_session(session_id, "provider unavailable")

        active = self.store.record_channel_activity(
            guild_id="1",
            channel_id="2",
            message_id="101",
            idle_minutes=5,
            at=now,
        )
        replacement_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="102",
            idle_minutes=5,
            at=now,
        )

        self.assertEqual(active, [])
        self.assertNotEqual(replacement_id, session_id)
        self.assertEqual(self.store.get_session(session_id)["message_ids"], ["100"])

    def test_runtime_errors_are_fingerprinted_and_counted(self):
        first = self.store.record_runtime_error(
            component="provider.openai",
            error_type="TimeoutError",
            summary="request 123 timed out after 10 seconds",
        )
        second = self.store.record_runtime_error(
            component="provider.openai",
            error_type="TimeoutError",
            summary="request 456 timed out after 20 seconds",
        )

        self.assertEqual(first, second)
        insight = self.store.pending_insights()[0]
        self.assertEqual(insight["kind"], "runtime_error")
        self.assertEqual(insight["occurrences"], 2)
        self.assertEqual(insight["recent_occurrences"], 2)
        self.assertEqual(insight["actor_count"], 0)
        self.assertEqual(self.store.recent_insights()[0]["id"], first)

        self.store.record_run(
            "plan", "failed", model="gpt-5.6-sol", detail="provider unavailable"
        )
        run = self.store.recent_runs()[0]
        self.assertEqual(run["stage"], "plan")
        self.assertEqual(run["status"], "failed")

    def test_budget_reservations_enforce_daily_cap(self):
        reservation = self.store.reserve_budget("extract", 0.6, 1.0)
        self.assertIsNotNone(reservation)
        self.assertIsNone(self.store.reserve_budget("plan", 0.5, 1.0))

        self.store.settle_budget(reservation, 0.4)
        second = self.store.reserve_budget("cleanup", 0.6, 1.0)
        self.assertIsNotNone(second)
        self.store.release_budget(second)

        status = self.store.budget_status(1.0)
        self.assertAlmostEqual(status["spent"], 0.4)
        self.assertAlmostEqual(status["reserved"], 0.0)
        self.assertAlmostEqual(status["remaining"], 0.6)

    def test_prune_removes_old_terminal_sessions_but_not_pending_work(self):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        terminal_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            idle_minutes=5,
            at=old,
        )
        self.store.finish_session(terminal_id, status="complete")
        pending_id = self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="101",
            idle_minutes=5,
            at=old,
        )

        self.store.prune(session_days=30, audit_days=90)

        due = self.store.due_sessions(now=datetime.now(timezone.utc))
        self.assertEqual([item["id"] for item in due], [pending_id])


if __name__ == "__main__":
    unittest.main()
