import asyncio
import unittest
from unittest.mock import AsyncMock

from services.progress import (
    build_progress_bar,
    estimated_progress,
    render_progress_status,
    start_progress_bar,
)


class ProgressRenderingTests(unittest.IsolatedAsyncioTestCase):
    def test_estimated_progress_is_monotonic_and_waits_below_completion(self):
        early = estimated_progress(5, 40)
        later = estimated_progress(40, 40)
        very_late = estimated_progress(400, 40)

        self.assertGreater(early, 0)
        self.assertGreater(later, early)
        self.assertLessEqual(very_late, 0.94)
        self.assertLess(very_late, 1)

    def test_render_uses_classic_shaded_bar_without_numeric_telemetry(self):
        active = render_progress_status(
            "Thinking",
            emoji="🧠",
            progress=0.42,
            phase=2,
            detail="Connecting the useful pieces…",
            elapsed=8,
        )
        finished = render_progress_status(
            "Thinking",
            emoji="🧠",
            progress=0.42,
            done=True,
        )

        bar = build_progress_bar(0.42, phase=2)
        self.assertTrue(bar.startswith("[██████████"))
        self.assertEqual(len(bar), 26)
        self.assertNotIn("%", active)
        self.assertNotIn("8s", active)
        self.assertNotIn("**Thinking**", active)
        self.assertIn("Connecting the useful pieces", active)
        self.assertEqual(finished, f"🧠 Thinking [{'█' * 24}]")

    async def test_live_animation_finishes_cleanly(self):
        message = type("Message", (), {"edit": AsyncMock()})()

        async def work():
            await asyncio.sleep(0.01)
            return "done"

        task = asyncio.create_task(work())
        await start_progress_bar(
            message,
            task,
            action_label="Working",
            duration_estimate=10,
        )

        self.assertEqual(await task, "done")
        self.assertGreaterEqual(message.edit.await_count, 2)
        self.assertEqual(
            message.edit.await_args.kwargs["content"],
            f"💬 Working [{'█' * 24}]",
        )


if __name__ == "__main__":
    unittest.main()
