"""Rendered completion cards for long-running work.

Animating with Pillow is a trap: an edited Discord message cannot swap its
attachment without re-uploading, so a per-frame image blows past the edit rate
limit. These cards fire exactly once, when the work is already done, and carry
what the run actually cost.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Iterable, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

logger = logging.getLogger("discord_bot")

# Sized for Discord's inline render without forcing a lightbox click.
CARD_WIDTH = 720
CARD_PADDING = 28
ROW_HEIGHT = 34
HEADER_HEIGHT = 62

# Tuned against Discord's dark theme rather than a generic palette.
INK = (228, 231, 238)
INK_SOFT = (145, 153, 168)
GROUND = (30, 33, 41)
RULE = (51, 56, 68)
ACCENT = (86, 201, 198)
WARN = (224, 164, 95)


def _font(size: int, bold: bool = False):
    """Best available face, degrading to Pillow's bitmap default.

    The container has no guaranteed font set, so every candidate is optional
    and a miss costs legibility, not a crash.
    """
    candidates = (
        ("DejaVuSansMono-Bold.ttf", "DejaVuSans-Bold.ttf") if bold
        else ("DejaVuSansMono.ttf", "DejaVuSans.ttf")
    )
    from PIL import ImageFont

    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _format_cost(cost: float) -> str:
    if cost <= 0:
        return "—"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.3f}"


def _format_tokens(tokens: float) -> str:
    count = int(tokens or 0)
    if count <= 0:
        return "—"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def receipts_enabled(path: str) -> bool:
    """Whether the completion card should be attached on a given path.

    Defaults to video only. On the image path the generated picture is the
    payload, and a second attachment makes Discord render a two-up grid that
    shrinks it -- so that one is opt-in. Tune live via ``progress.receipt``:
    "off", "video" (default), or "all".
    """
    try:
        from services.behavior_registry import get_runtime_setting

        mode = get_runtime_setting("progress.receipt", "video")
    except Exception:
        mode = "video"
    mode = str(mode or "video").strip().lower()
    if mode in {"off", "false", "none", "0"}:
        return False
    if mode in {"all", "true", "1"}:
        return True
    return path == "video"


def requester_label(message) -> str:
    """Best available display name, or empty. Never raises.

    The receipt is decoration attached to real output, so nothing about it may
    take down the delivery of the image or video it describes.
    """
    author = getattr(message, "author", None)
    for attribute in ("display_name", "name", "global_name"):
        value = getattr(author, attribute, None)
        if isinstance(value, str) and value.strip():
            return f"@{value.strip()}"
    return ""


def build_receipt_card(
    title: str,
    rows: Sequence[Tuple[str, str]],
    *,
    subtitle: str = "",
    accent: Tuple[int, int, int] = ACCENT,
) -> Optional[BytesIO]:
    """Render a compact summary card. Returns None if rendering fails."""
    try:
        rows = [(str(k), str(v)) for k, v in rows if v not in (None, "")]
        height = HEADER_HEIGHT + CARD_PADDING + max(1, len(rows)) * ROW_HEIGHT + CARD_PADDING
        image = Image.new("RGB", (CARD_WIDTH, height), GROUND)
        draw = ImageDraw.Draw(image)

        title_font = _font(24, bold=True)
        label_font = _font(17)
        value_font = _font(17, bold=True)

        # Accent rail: one saturated edge against an otherwise quiet card.
        draw.rectangle([(0, 0), (5, height)], fill=accent)

        draw.text((CARD_PADDING, 22), title, font=title_font, fill=INK)
        if subtitle:
            width = draw.textlength(subtitle, font=label_font)
            draw.text(
                (CARD_WIDTH - CARD_PADDING - width, 28),
                subtitle,
                font=label_font,
                fill=INK_SOFT,
            )

        draw.line(
            [(CARD_PADDING, HEADER_HEIGHT), (CARD_WIDTH - CARD_PADDING, HEADER_HEIGHT)],
            fill=RULE,
            width=1,
        )

        y = HEADER_HEIGHT + CARD_PADDING - 8
        for label, value in rows:
            draw.text((CARD_PADDING, y), label, font=label_font, fill=INK_SOFT)
            width = draw.textlength(value, font=value_font)
            draw.text(
                (CARD_WIDTH - CARD_PADDING - width, y),
                value,
                font=value_font,
                fill=INK,
            )
            y += ROW_HEIGHT

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return buffer
    except Exception as e:
        logger.warning("Failed to render receipt card: %s", e)
        return None


def build_run_receipt(
    title: str,
    *,
    elapsed: float,
    model: str = "",
    requested_by: str = "",
    extra_rows: Iterable[Tuple[str, str]] = (),
) -> Optional[BytesIO]:
    """Completion card backed by the in-flight request's real usage totals."""
    try:
        from services import usage_costs

        totals = usage_costs.get_request_totals()
    except Exception:
        totals = {}

    rows = [("elapsed", format_elapsed(elapsed))]
    rows.extend(extra_rows)
    if model:
        rows.append(("model", model))
    tokens = totals.get("tokens", 0)
    if tokens:
        rows.append(("tokens", _format_tokens(tokens)))
    cost = totals.get("cost_usd", 0)
    if cost:
        rows.append(("cost", _format_cost(cost)))
    if requested_by:
        rows.append(("requested by", requested_by))

    return build_receipt_card(title, rows, subtitle=model or "")
