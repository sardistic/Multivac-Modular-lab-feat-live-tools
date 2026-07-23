import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.reflection_store import ReflectionStore
from services.reflection_worker import ReflectionErrorHandler, ReflectionWorker


class ReflectionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = str(Path(self.tempdir.name) / "reflection.db")
        self.env = patch.dict(
            "os.environ",
            {
                "REFLECTION_DB_PATH": db_path,
                "REFLECTION_ENABLED": "true",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ReflectionStore()

    async def test_invocation_is_consent_for_bounded_session(self):
        worker = ReflectionWorker(self.store)
        kwargs = {
            "guild_id": "1",
            "channel_id": "2",
            "user_id": "42",
            "message_id": "100",
        }
        self.assertIsNotNone(worker.note_invocation(**kwargs))
        self.assertTrue(worker.user_enabled("42"))

    async def test_session_uses_bounded_surrounding_history_and_stores_only_insight(self):
        history = AsyncMock(
            return_value=[
                {"message_id": "90", "role": "participant", "content": "This keeps failing."},
                {"message_id": "100", "role": "requester", "content": "@Multivac try it; token=abcdef123456"},
                {"message_id": "101", "role": "assistant", "content": "I could not complete that."},
                {"message_id": "102", "role": "participant", "content": "That is the third time."},
            ]
        )
        worker = ReflectionWorker(self.store, history_fetcher=history)
        worker.set_user_enabled("42", True)
        worker.models.extract = AsyncMock(
            return_value={
                "useful": True,
                "insights": [
                    {
                        "kind": "pain_point",
                        "summary": "A repeated task fails without an actionable explanation.",
                        "confidence": 0.95,
                        "evidence_message_ids": ["100", "101", "not-in-window"],
                    }
                ],
            }
        )
        at = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            window_minutes=20,
            lookback_minutes=10,
            at=at,
        )
        session = self.store.due_sessions()[0]

        await worker._process_session(session)

        sent = worker.models.extract.await_args.args[0]
        self.assertEqual([item["message_id"] for item in sent], ["90", "100", "101", "102"])
        self.assertIn("[REDACTED]", sent[1]["content"])
        insight = self.store.pending_insights()[0]
        self.assertEqual(insight["evidence_ids"], ["100", "101"])
        self.assertNotIn("This keeps failing", str(insight))

    async def test_three_matching_errors_can_become_a_reviewable_fix_idea(self):
        worker = ReflectionWorker(self.store)
        for request_id in (100, 200, 300):
            worker.note_runtime_error(
                component="providers.openai",
                error_type="TimeoutError",
                summary=f"request {request_id} timed out",
            )
        worker.models.plan = AsyncMock(
            return_value={
                "ideas": [
                    {
                        "title": "Bound provider timeouts",
                        "problem": "The same provider timeout occurred repeatedly.",
                        "proposal": "Add a bounded retry and clearer failure response.",
                        "expected_impact": "Fewer failed user requests.",
                        "risk": "Retries must remain bounded.",
                        "hotload_kind": "behavior",
                        "code_paths": ["providers/openai_client.py", "not/available.py"],
                        "insight_ids": [1],
                    }
                ]
            }
        )
        code_context = (
            "BASELINE abc\n===== providers/openai_client.py =====\nsource"
        )
        with patch.object(worker, "_code_context", return_value=code_context):
            await worker._maybe_plan()

        ideas = self.store.list_ideas()
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0]["code_paths"], ["providers/openai_client.py"])
        self.assertEqual(ideas[0]["status"], "active")

    async def test_consent_withdrawal_during_extraction_prevents_persistence(self):
        history = AsyncMock(
            return_value=[
                {"message_id": "100", "role": "requester", "content": "This failed again."},
                {"message_id": "101", "role": "assistant", "content": "I could not do that."},
            ]
        )
        worker = ReflectionWorker(self.store, history_fetcher=history)
        worker.set_user_enabled("42", True)

        async def withdraw_then_return(_transcript):
            worker.set_user_enabled("42", False)
            return {
                "useful": True,
                "insights": [
                    {
                        "kind": "pain_point",
                        "summary": "A task failed.",
                        "confidence": 0.9,
                        "evidence_message_ids": ["100", "101"],
                    }
                ],
            }

        worker.models.extract = AsyncMock(side_effect=withdraw_then_return)
        at = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.store.record_invocation(
            guild_id="1",
            channel_id="2",
            user_id="42",
            message_id="100",
            window_minutes=20,
            at=at,
        )
        session = self.store.due_sessions()[0]

        await worker._process_session(session)

        self.assertEqual(self.store.pending_insights(), [])

    async def test_error_handler_counts_templates_without_log_arguments(self):
        worker = ReflectionWorker(self.store)
        handler = ReflectionErrorHandler(worker)
        record = logging.LogRecord(
            name="discord_bot",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="provider call failed for user %s",
            args=("private-user-value",),
            exc_info=None,
        )

        handler.emit(record)
        handler.emit(
            logging.LogRecord(
                name="discord_bot",
                level=logging.ERROR,
                pathname=__file__,
                lineno=2,
                msg="provider payload failed: private-user-value",
                args=(),
                exc_info=None,
            )
        )

        summaries = [item["summary"] for item in self.store.pending_insights()]
        self.assertNotIn("private-user-value", " ".join(summaries))

    async def test_activity_exposes_only_sanitized_structured_records(self):
        worker = ReflectionWorker(self.store)
        worker.note_runtime_error(
            component="discord_bot",
            error_type="RuntimeError",
            summary="request failed for person@example.com at https://example.com/private",
        )
        self.store.record_run(
            "extract",
            "failed",
            model="gpt-5.4-nano",
            detail="token=abcdef123456 for person@example.com",
        )

        activity = worker.activity(5)

        self.assertEqual(activity["signal_threshold"], 3)
        self.assertIn("[EMAIL]", activity["observations"][0]["summary"])
        self.assertIn("[URL]", activity["observations"][0]["summary"])
        self.assertNotIn("evidence_ids", activity["observations"][0])
        self.assertNotIn("actor_hashes", activity["observations"][0])
        self.assertIn("[REDACTED]", activity["runs"][0]["detail"])
        self.assertIn("[EMAIL]", activity["runs"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
