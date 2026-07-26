"""Discord-safe pseudo-progress rendering for long-running bot work."""

from __future__ import annotations

import asyncio
import math

FULL_BLOCK = "━"
PARTIAL_BLOCKS = [
    (0.00, " "),
    (0.125, "▏"),
    (0.25, "▎"),
    (0.375, "▍"),
    (0.5, "▌"),
    (0.625, "▋"),
    (0.75, "▊"),
    (0.875, "▉"),
    (1.00, "█"),
]
PULSE_FRAMES = ("◐", "◓", "◑", "◒")


def _resolve_label(action_label) -> str:
    """Allow a callable label so long operations can describe phase changes."""
    try:
        value = action_label() if callable(action_label) else action_label
        return str(value or "Working")
    except Exception:
        return "Working"


def estimated_progress(elapsed: float, duration_estimate: float) -> float:
    """Ease toward 94% without claiming a pseudo-timed operation is complete."""
    duration = max(1.0, float(duration_estimate or 1))
    elapsed = max(0.0, float(elapsed or 0))
    return min(0.94, 0.94 * (1.0 - math.exp(-2.2 * elapsed / duration)))


def build_progress_bar(
    progress: float,
    width: int = 18,
    fancy: bool = True,
    *,
    phase: int = 0,
) -> str:
    """Render a stable fill with one moving scan light in the unfilled area."""
    progress = max(0.0, min(1.0, float(progress)))
    width = max(4, int(width))
    if progress >= 1.0:
        return f"[{FULL_BLOCK * width}]"

    filled = min(width - 1, int(progress * width))
    cells = [FULL_BLOCK] * filled
    cells.append("╸")
    remaining = width - len(cells)
    cells.extend("·" for _ in range(remaining))
    if fancy and remaining > 2:
        scan_start = filled + 1
        scan_index = scan_start + (int(phase) % remaining)
        cells[scan_index] = "•"
    return f"[{''.join(cells)}]"


def render_progress_status(
    action_label,
    *,
    emoji: str = "💬",
    progress: float = 0.0,
    phase: int = 0,
    detail: str | None = None,
    elapsed: float | None = None,
    done: bool = False,
    failed: bool = False,
    width: int = 18,
) -> str:
    label = _resolve_label(action_label)
    marker = "⚠" if failed else ("✓" if done else PULSE_FRAMES[int(phase) % len(PULSE_FRAMES)])
    pct = 100 if done and not failed else min(99, max(0, round(progress * 100)))
    bar = build_progress_bar(
        1.0 if done and not failed else progress,
        width=width,
        fancy=not done,
        phase=phase,
    )
    timing = ""
    if elapsed is not None and not done:
        timing = f" · {max(0, int(elapsed))}s"
    render = f"{emoji} **{label}** {marker}  {bar}  `{pct:02d}%`{timing}"
    if detail:
        render += f"\n↳ {str(detail).strip()[:900]}"
    return render


async def start_progress_bar(
    message,
    task: asyncio.Task,
    action_label="Working",
    emoji="💬",
    duration_estimate=40,
    progress_tracker: dict | None = None,
    summarizer=None,
):
    """Animate one Discord message without exceeding a conservative edit rate."""
    animation_update_interval = 0.2
    discord_edit_interval = 1.5
    loop = asyncio.get_running_loop()
    last_discord_edit = -discord_edit_interval
    start_time = loop.time()
    phase = 0
    last_progress = 0.0

    try:
        while not task.done():
            now = loop.time()
            elapsed = now - start_time
            if progress_tracker and "progress" in progress_tracker:
                progress = float(progress_tracker["progress"])
                if progress > 1:
                    progress /= 100.0
                progress = min(0.99, max(0.0, progress))
            else:
                progress = estimated_progress(elapsed, duration_estimate)
            last_progress = max(last_progress, progress)

            if now - last_discord_edit >= discord_edit_interval:
                detail = None
                if summarizer is not None:
                    try:
                        detail = summarizer()
                    except Exception:
                        detail = None
                render = render_progress_status(
                    action_label,
                    emoji=emoji,
                    progress=last_progress,
                    phase=phase,
                    detail=detail,
                    elapsed=elapsed,
                )
                try:
                    await message.edit(content=render)
                    last_discord_edit = now
                except Exception:
                    pass
            phase += 1
            await asyncio.sleep(animation_update_interval)

        failed = task.cancelled()
        if not failed:
            try:
                failed = task.exception() is not None
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                failed = True
        final_render = render_progress_status(
            action_label,
            emoji=emoji,
            progress=last_progress,
            phase=phase,
            done=not failed,
            failed=failed,
        )
        try:
            await message.edit(content=final_render)
        except Exception:
            pass
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def select_partial_block(ratio: float) -> str:
    """Retained for compatibility with earlier callers."""
    for threshold, char in PARTIAL_BLOCKS:
        if ratio <= threshold:
            return char
    return "█"
