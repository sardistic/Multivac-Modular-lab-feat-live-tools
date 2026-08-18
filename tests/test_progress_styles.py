import unittest
from collections import Counter
from unittest.mock import patch

from services import progress
from services.progress import (
    DEFAULT_STYLE,
    RESERVED_STYLES,
    STYLE_TABLE,
    build_progress_bar,
    pick_style,
    render_progress_status,
    render_stage_checklist,
    resolve_style,
)


class StyleSelectionTests(unittest.TestCase):
    """The roll must be reproducible and must never surface a reserved style."""

    def test_classic_holds_a_fifth_of_the_pool(self):
        draws = Counter(pick_style(seed) for seed in range(4000))
        share = draws["classic"] / 4000
        self.assertAlmostEqual(share, 0.20, delta=0.03)

    def test_reserved_styles_are_never_rolled(self):
        draws = {pick_style(seed) for seed in range(4000)}
        for reserved in RESERVED_STYLES:
            self.assertNotIn(reserved, draws)

    def test_every_pooled_style_is_reachable(self):
        draws = set(pick_style(seed) for seed in range(4000))
        expected = {
            name for name, spec in STYLE_TABLE.items()
            if spec[1] > 0 and name not in RESERVED_STYLES
        }
        # Emoji styles drop out unless guild assets are configured.
        expected -= {"emoji_head", "emoji_tiles"}
        self.assertTrue(expected.issubset(draws), f"unreachable: {expected - draws}")

    def test_same_seed_yields_the_same_style(self):
        self.assertEqual(pick_style(4815162342), pick_style(4815162342))

    def test_unknown_style_falls_back_rather_than_raising(self):
        self.assertEqual(resolve_style("no-such-style"), DEFAULT_STYLE)
        self.assertEqual(resolve_style(None), DEFAULT_STYLE)

    def test_indeterminate_resolves_to_the_reserved_march(self):
        self.assertEqual(resolve_style(None, indeterminate=True), "barberpole")


class EmojiAvailabilityTests(unittest.TestCase):
    def test_emoji_styles_stay_out_of_the_pool_without_config(self):
        with patch.object(progress, "_emoji_config", return_value={}):
            draws = {pick_style(seed) for seed in range(1500)}
        self.assertNotIn("emoji_head", draws)
        self.assertNotIn("emoji_tiles", draws)

    def test_emoji_styles_join_the_pool_once_configured(self):
        config = {"filled": "<:on:1>", "empty": "<:off:2>", "spinner": "<a:think:3>"}
        with patch.object(progress, "_emoji_config", return_value=config):
            draws = {pick_style(seed) for seed in range(2500)}
            self.assertIn("emoji_head", draws)
            self.assertIn("emoji_tiles", draws)


class RendererTests(unittest.TestCase):
    """Every renderer must survive the full progress range without raising."""

    def test_all_styles_render_across_the_range(self):
        for name in STYLE_TABLE:
            for pct in (0.0, 0.01, 0.5, 0.94, 1.0):
                for phase in (0, 1, 7, 40):
                    with self.subTest(style=name, pct=pct, phase=phase):
                        out = build_progress_bar(
                            pct, width=24, phase=phase, style=name, elapsed=pct * 40
                        )
                        self.assertIsInstance(out, str)
                        self.assertTrue(out)

    def test_out_of_range_progress_is_clamped(self):
        for name in STYLE_TABLE:
            with self.subTest(style=name):
                self.assertTrue(build_progress_bar(-5, style=name))
                self.assertTrue(build_progress_bar(12, style=name))

    def test_classic_matches_its_original_shape(self):
        bar = build_progress_bar(0.5, width=24, style="classic")
        self.assertTrue(bar.startswith("["))
        self.assertTrue(bar.endswith("]"))
        self.assertEqual(len(bar), 26)

    def test_drift_is_stable_for_a_fixed_phase(self):
        """Classic re-rolls its tail every frame; drift must not."""
        first = build_progress_bar(0.4, width=24, phase=6, style="drift")
        second = build_progress_bar(0.4, width=24, phase=6, style="drift")
        self.assertEqual(first, second)

    def test_drift_moves_between_phases(self):
        self.assertNotEqual(
            build_progress_bar(0.4, width=24, phase=0, style="drift"),
            build_progress_bar(0.4, width=24, phase=1, style="drift"),
        )

    def test_rich_reports_percentage_and_clock(self):
        bar = build_progress_bar(0.47, width=24, style="rich", elapsed=12)
        self.assertIn("47%", bar)
        self.assertIn("0:12", bar)

    def test_seek_knob_tracks_progress(self):
        early = build_progress_bar(0.05, width=24, style="seek")
        late = build_progress_bar(0.95, width=24, style="seek")
        self.assertLess(early.index("🔘"), late.index("🔘"))

    def test_braille_output_stays_in_the_braille_block(self):
        bar = build_progress_bar(0.5, width=24, phase=3, style="braille")
        for char in bar.strip("⟨⟩"):
            self.assertTrue(0x2800 <= ord(char) <= 0x28FF, f"stray {char!r}")

    def test_indeterminate_styles_never_claim_a_percentage(self):
        for name in ("equalizer", "braille", "barberpole"):
            with self.subTest(style=name):
                self.assertNotIn("%", build_progress_bar(0.5, style=name, phase=2))


class StatusCompositionTests(unittest.TestCase):
    def test_label_and_emoji_lead_the_line(self):
        out = render_progress_status("Thinking", emoji="💬", progress=0.3, style="drift")
        self.assertTrue(out.startswith("💬 Thinking "))

    def test_diagnostic_supplies_its_own_flavour_line(self):
        out = render_progress_status("Preparing", progress=0.4, phase=0, style="diagnostic")
        self.assertIn("\n↳ ", out)

    def test_caller_detail_wins_over_style_flavour(self):
        out = render_progress_status(
            "Preparing", progress=0.4, detail="real detail", style="diagnostic"
        )
        self.assertIn("↳ real detail", out)

    def test_finished_bar_settles_instead_of_shimmering(self):
        out = render_progress_status("Done", progress=1.0, done=True, style="classic")
        self.assertNotIn("↳", out)
        for noisy in (".", ":", "-"):
            self.assertNotIn(noisy, out.split("[", 1)[1])

    def test_failure_is_marked(self):
        out = render_progress_status("Nope", progress=0.5, failed=True, style="classic")
        self.assertIn("⚠", out)


class StageChecklistTests(unittest.TestCase):
    STAGES = ("Opening", "Indexing", "Reading", "Routing")

    def test_stages_split_into_done_active_and_pending(self):
        out = render_stage_checklist(self.STAGES, 2).splitlines()
        self.assertTrue(out[0].startswith("✅"))
        self.assertTrue(out[1].startswith("✅"))
        self.assertTrue(out[2].startswith(("⏳", "⌛")))
        self.assertTrue(out[3].startswith("⬜"))

    def test_all_stages_complete_at_the_end(self):
        out = render_stage_checklist(self.STAGES, len(self.STAGES))
        self.assertEqual(out.count("✅"), len(self.STAGES))
        self.assertNotIn("⬜", out)

    def test_failure_marks_the_stage_that_stalled(self):
        out = render_stage_checklist(self.STAGES, 1, failed=True).splitlines()
        self.assertTrue(out[1].startswith("⚠️"))

    def test_every_stage_is_named(self):
        out = render_stage_checklist(self.STAGES, 1)
        for stage in self.STAGES:
            self.assertIn(stage, out)


class PreviewSinkTests(unittest.IsolatedAsyncioTestCase):
    """Partial frames ride along on the edits the bar already makes."""

    class FakeMessage:
        def __init__(self):
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    async def test_sink_attaches_once_per_sequence_bump(self):
        import asyncio

        message = self.FakeMessage()
        sink = {"seq": 0, "build": lambda: ["FRAME"]}

        async def work():
            await asyncio.sleep(0.05)
            sink["seq"] = 1
            await asyncio.sleep(3.2)
            return "done"

        task = asyncio.create_task(work())
        await progress.start_progress_bar(
            message, task, duration_estimate=4, preview_sink=sink, style="classic"
        )
        await task

        attached = [e for e in message.edits if e.get("attachments")]
        self.assertEqual(len(attached), 1, "one bump must produce exactly one upload")
        self.assertEqual(attached[0]["attachments"], ["FRAME"])

    async def test_no_bump_means_no_uploads(self):
        import asyncio

        message = self.FakeMessage()
        sink = {"seq": 0, "build": lambda: ["FRAME"]}

        async def work():
            await asyncio.sleep(1.8)
            return "done"

        task = asyncio.create_task(work())
        await progress.start_progress_bar(
            message, task, duration_estimate=2, preview_sink=sink, style="classic"
        )
        await task

        self.assertFalse([e for e in message.edits if e.get("attachments")])
        self.assertTrue(message.edits, "the bar should still have drawn")

    async def test_a_failing_builder_does_not_break_the_bar(self):
        import asyncio

        def boom():
            raise RuntimeError("encode failed")

        message = self.FakeMessage()
        sink = {"seq": 1, "build": boom}

        async def work():
            await asyncio.sleep(1.8)
            return "done"

        task = asyncio.create_task(work())
        await progress.start_progress_bar(
            message, task, duration_estimate=2, preview_sink=sink, style="classic"
        )
        await task

        self.assertTrue(message.edits)
        self.assertFalse([e for e in message.edits if e.get("attachments")])


if __name__ == "__main__":
    unittest.main()
