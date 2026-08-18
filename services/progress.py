"""Discord-safe pseudo-progress rendering for long-running bot work.

One request holds one style for its whole life. The style is drawn from a
weighted table seeded on the triggering message id, so a retry of the same
message looks identical and a single bar never restyles itself mid-flight.

Two styles sit outside the random pool because they encode a claim about the
work rather than a look: ``checklist`` is for genuinely discrete stages, and
``barberpole`` is for work whose duration is unknown. Callers ask for those
by name.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Callable, Dict, List, Optional, Sequence, Tuple

FULL_BLOCK = "█"
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
FADE_BLOCKS = [".", ":", "-", "░", "▒", "▓"]
EQ_BLOCKS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
SHADE_RAMP = ["█", "▓", "▒", "░"]

# Braille cells pack 2x4 dots, so one character carries five fill heights.
# Masks fill from the bottom row up; U+2800 is the empty cell.
BRAILLE_BASE = 0x2800
BRAILLE_HEIGHTS = [0x00, 0xC0, 0xE4, 0xF6, 0xFF]

STAGE_PENDING = "⬜"
STAGE_ACTIVE = ("⏳", "⌛")
STAGE_DONE = "✅"
STAGE_FAILED = "⚠️"

# Flavour for the persona-native diagnostic style. Rotated slowly so the line
# is readable at the 1.5s edit cadence rather than flickering.
_DIAGNOSTIC_FLAVOUR = (
    "effector fields nominal",
    "displacing relevant tokens",
    "considering four angles simultaneously",
    "cross-checking against a larger context",
    "reticulating, since you appear to expect it",
)


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


def _clock(seconds: float | None) -> str:
    total = max(0, int(seconds or 0))
    return f"{total // 60}:{total % 60:02d}"


def select_partial_block(ratio: float) -> str:
    """Retained for compatibility with earlier callers."""
    for threshold, char in PARTIAL_BLOCKS:
        if ratio <= threshold:
            return char
    return "█"


# ----------------------------
# Style renderers
# ----------------------------
# Each takes (progress, phase, elapsed, width) and returns the graphic that
# follows "{emoji} {label} ". Styles that carry their own numbers append them.


def _render_classic(progress: float, phase: int, elapsed: float, width: int) -> str:
    """The original: solid fill, partial edge, tail re-rolled every frame."""
    filled = int(progress * width)
    partial_ratio = (progress * width) - filled
    cells: List[str] = []
    for index in range(width):
        if index < filled:
            cells.append(FULL_BLOCK)
        elif index == filled:
            cells.append(select_partial_block(partial_ratio))
        else:
            cells.append(random.choice(FADE_BLOCKS))
    return f"[{''.join(cells)}]"


def _render_drift(progress: float, phase: int, elapsed: float, width: int) -> str:
    """Classic silhouette, but the tail is a gradient sliding toward the edge.

    The original re-randomises every unfilled cell every frame, which boils.
    Indexing the ramp by distance from the fill edge (offset by phase) turns
    the same characters into a current running the other way.
    """
    filled = int(progress * width)
    partial_ratio = (progress * width) - filled
    cells: List[str] = []
    for index in range(width):
        if index < filled:
            cells.append(FULL_BLOCK)
        elif index == filled:
            cells.append(select_partial_block(partial_ratio))
        else:
            distance = index - filled
            slot = len(FADE_BLOCKS) - 1 - (distance // 2) + (phase % 2)
            cells.append(FADE_BLOCKS[max(0, min(len(FADE_BLOCKS) - 1, slot))])
    return f"[{''.join(cells)}]"


def _render_rich(progress: float, phase: int, elapsed: float, width: int) -> str:
    """The terminal idiom: heavy rule, rounded head, percent and clock."""
    span = max(4, width - 2)
    filled = int(progress * span)
    cells = []
    for index in range(span):
        if index < filled:
            cells.append("━")
        elif index == filled:
            cells.append("╸")
        else:
            cells.append("─")
    return f"{''.join(cells)} {int(progress * 100)}% · {_clock(elapsed)}"


def _render_equalizer(progress: float, phase: int, elapsed: float, width: int) -> str:
    """Travelling wave of block heights. Deliberately claims no percentage."""
    span = max(8, width - 4)
    cells = []
    for index in range(span):
        primary = math.sin(index * 0.55 - phase * 0.8) * 0.5 + 0.5
        secondary = math.sin(index * 0.23 + phase * 0.4) * 0.5 + 0.5
        height = int((primary * 0.65 + secondary * 0.35) * len(EQ_BLOCKS))
        cells.append(EQ_BLOCKS[max(0, min(len(EQ_BLOCKS) - 1, height))])
    return "".join(cells)


def _render_braille(progress: float, phase: int, elapsed: float, width: int) -> str:
    """A scanner sweeping the width; 2x4 dots per cell is the densest text motion."""
    span = max(8, width // 2 + 4)
    head = (phase * 1.1) % (span + 6) - 3
    cells = []
    for index in range(span):
        distance = abs(index - head)
        level = int(len(BRAILLE_HEIGHTS) - 1 - distance * 1.4)
        level = max(0, min(len(BRAILLE_HEIGHTS) - 1, level))
        cells.append(chr(BRAILLE_BASE + BRAILLE_HEIGHTS[level]))
    return f"⟨{''.join(cells)}⟩"


def _render_diagnostic(progress: float, phase: int, elapsed: float, width: int) -> str:
    """Persona-native: a Mind reporting on subsystems, not a loading bar."""
    filled = int(progress * width)
    cells = []
    for index in range(width):
        if index < filled:
            cells.append(FULL_BLOCK)
        else:
            distance = index - filled
            cells.append(SHADE_RAMP[distance] if distance < len(SHADE_RAMP) else "·")
    return f"⟦{''.join(cells)}⟧ {int(progress * 100)}%"


def _render_seek(progress: float, phase: int, elapsed: float, width: int) -> str:
    """Music-bot convention: a knob riding a rail. Position, not fill."""
    span = max(8, width - 6)
    position = min(span - 1, int(progress * span))
    cells = ["─"] * span
    cells[position] = "🔘"
    return f"{''.join(cells)} {_clock(elapsed)}"


def _render_barberpole(progress: float, phase: int, elapsed: float, width: int) -> str:
    """Indeterminate march. For work with no honest ETA at all."""
    span = max(8, width - 6)
    cells = ["▰" if (index + phase) % 3 == 0 else "▱" for index in range(span)]
    return "".join(cells)


def _emoji_config() -> Dict[str, str]:
    """Custom-emoji references, e.g. {"spinner": "<a:mn_think:123>"}.

    Custom emoji are guild-scoped, so these are only usable where the bot has
    them installed. Configure via the behaviour registry; absent config drops
    the emoji styles out of the random pool entirely.
    """
    try:
        from services.behavior_registry import get_runtime_setting

        config = get_runtime_setting("progress.emoji", None)
    except Exception:
        config = None
    if not isinstance(config, dict):
        return {}
    return {str(k): str(v) for k, v in config.items() if isinstance(v, str) and v.strip()}


def _emoji_styles_available() -> bool:
    config = _emoji_config()
    return bool(config.get("filled") and config.get("empty"))


def _render_emoji_head(progress: float, phase: int, elapsed: float, width: int) -> str:
    """An animated custom emoji leading a plain bar.

    Discord loops an ``<a:...>`` emoji client-side, so the motion costs no
    edits at all - the cheapest real animation available to a bot.
    """
    config = _emoji_config()
    spinner = config.get("spinner", "")
    span = max(8, width - 4)
    filled = int(progress * span)
    cells = "".join(FULL_BLOCK if i < filled else "░" for i in range(span))
    lead = f"{spinner} " if spinner else ""
    return f"{lead}{cells} {int(progress * 100)}%"


def _render_emoji_tiles(progress: float, phase: int, elapsed: float, width: int) -> str:
    """A purpose-drawn tile set, so the bar renders at a fixed pixel size."""
    config = _emoji_config()
    filled_tile = config.get("filled", "▰")
    empty_tile = config.get("empty", "▱")
    cap_left = config.get("cap_left", "")
    cap_right = config.get("cap_right", "")
    # Custom emoji are wide; keep the tile count well under a text bar's width.
    span = max(6, min(14, width // 2))
    lit = int(progress * span)
    body = "".join(filled_tile if i < lit else empty_tile for i in range(span))
    return f"{cap_left}{body}{cap_right} {int(progress * 100)}%"


def _diagnostic_flavour(phase: int) -> str:
    return _DIAGNOSTIC_FLAVOUR[(phase // 3) % len(_DIAGNOSTIC_FLAVOUR)]


# name -> (renderer, weight in the random pool, flavour provider)
# Weight 0.0 means the style is reserved and must be requested by name.
STYLE_TABLE: Dict[str, Tuple[Callable[..., str], float, Optional[Callable[[int], str]]]] = {
    "classic": (_render_classic, 0.20, None),
    "drift": (_render_drift, 0.14, None),
    "rich": (_render_rich, 0.14, None),
    "equalizer": (_render_equalizer, 0.13, None),
    "braille": (_render_braille, 0.13, None),
    "diagnostic": (_render_diagnostic, 0.13, _diagnostic_flavour),
    "seek": (_render_seek, 0.13, None),
    # Guild-scoped assets: weighted into the pool only where installed.
    "emoji_head": (_render_emoji_head, 0.07, None),
    "emoji_tiles": (_render_emoji_tiles, 0.06, None),
    # Reserved: these assert something about the work, so they are never rolled.
    "barberpole": (_render_barberpole, 0.0, None),
    "checklist": (_render_classic, 0.0, None),  # rendered via render_stage_checklist
}

DEFAULT_STYLE = "classic"
RESERVED_STYLES = frozenset({"barberpole", "checklist"})


def _configured_weights() -> Dict[str, float]:
    """Live-tunable weights via the behaviour registry, falling back to table."""
    defaults = {name: spec[1] for name, spec in STYLE_TABLE.items()}
    try:
        from services.behavior_registry import get_runtime_setting

        override = get_runtime_setting("progress.style.weights", None)
    except Exception:
        override = None
    if isinstance(override, dict):
        for name, value in override.items():
            if name in defaults:
                try:
                    defaults[name] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
    if not _emoji_styles_available():
        defaults["emoji_head"] = 0.0
        defaults["emoji_tiles"] = 0.0
    return defaults


def pick_style(seed: int | None = None) -> str:
    """Draw a style from the weighted pool.

    Seeded on the triggering message id so a retry renders identically and the
    choice is reproducible in tests. Reserved styles are never returned.
    """
    weights = _configured_weights()
    pool = [
        (name, weight)
        for name, weight in weights.items()
        if weight > 0 and name not in RESERVED_STYLES
    ]
    total = sum(weight for _, weight in pool)
    if not pool or total <= 0:
        return DEFAULT_STYLE
    rng = random.Random(seed) if seed is not None else random
    roll = rng.random() * total
    for name, weight in pool:
        roll -= weight
        if roll <= 0:
            return name
    return pool[-1][0]


def resolve_style(style: str | None, *, indeterminate: bool = False) -> str:
    """Normalise a requested style name, falling back to the default."""
    if indeterminate and not style:
        return "barberpole"
    if style in STYLE_TABLE:
        return style
    return DEFAULT_STYLE


def build_progress_bar(
    progress: float,
    width: int = 24,
    fancy: bool = True,
    *,
    phase: int = 0,
    style: str | None = None,
    elapsed: float | None = None,
) -> str:
    """Render one bar. Defaults to the original shaded 24-cell treatment."""
    progress = max(0.0, min(1.0, float(progress)))
    width = max(4, int(width))
    name = resolve_style(style)
    renderer = STYLE_TABLE[name][0]
    if not fancy and name in {"classic", "drift"}:
        # Completed bars should settle instead of shimmering.
        filled = int(progress * width)
        cells = [FULL_BLOCK if i < filled else "░" for i in range(width)]
        return f"[{''.join(cells)}]"
    try:
        return renderer(progress, int(phase), float(elapsed or 0.0), width)
    except Exception:
        return _render_classic(progress, int(phase), float(elapsed or 0.0), width)


def render_stage_checklist(
    stages: Sequence[str],
    current: int,
    *,
    phase: int = 0,
    failed: bool = False,
) -> str:
    """Discrete stages as a checklist.

    A smooth bar over a handful of genuinely discrete steps invents continuity
    that is not there, so the preflight path renders its real stages instead.
    """
    lines = []
    for index, stage in enumerate(stages):
        if index < current:
            lines.append(f"{STAGE_DONE} {stage}")
        elif index == current:
            if failed:
                lines.append(f"{STAGE_FAILED} {stage}")
            else:
                lines.append(f"{STAGE_ACTIVE[phase % len(STAGE_ACTIVE)]} {stage}")
        else:
            lines.append(f"{STAGE_PENDING} {stage}")
    return "\n".join(lines)


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
    width: int = 24,
    style: str | None = None,
) -> str:
    label = _resolve_label(action_label)
    name = resolve_style(style)
    bar = build_progress_bar(
        1.0 if done and not failed else progress,
        width=width,
        fancy=not done,
        phase=phase,
        style=name,
        elapsed=elapsed,
    )
    render = f"{emoji} {label} {bar}"
    if failed:
        render += " ⚠"

    if detail is None and not done and not failed:
        flavour = STYLE_TABLE[name][2]
        if flavour is not None:
            try:
                detail = flavour(int(phase))
            except Exception:
                detail = None

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
    style: str | None = None,
    preview_sink: dict | None = None,
):
    """Animate one Discord message without exceeding a conservative edit rate.

    The style is resolved once here, never inside the loop: rolling per frame
    would restyle the bar every 1.5s.
    """
    animation_update_interval = 0.1
    discord_edit_interval = 1.5
    loop = asyncio.get_running_loop()
    last_discord_edit = -discord_edit_interval
    start_time = loop.time()
    phase = 0
    last_progress = 0.0
    last_preview_seq = 0
    resolved_style = resolve_style(style)

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
                    style=resolved_style,
                )
                # A preview sink lets a provider stream frames of the actual
                # work in progress. Kept provider-agnostic: the caller hands
                # back ready-made attachments, so this module never imports
                # discord. Only fires when the sequence advances, so a stalled
                # render costs no re-uploads.
                attachments = None
                if preview_sink:
                    seq = int(preview_sink.get("seq") or 0)
                    if seq > last_preview_seq:
                        try:
                            attachments = preview_sink["build"]()
                            last_preview_seq = seq
                        except Exception:
                            attachments = None
                try:
                    if attachments is not None:
                        await message.edit(content=render, attachments=attachments)
                    else:
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
            style=resolved_style,
        )
        try:
            await message.edit(content=final_render)
        except Exception:
            pass
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
