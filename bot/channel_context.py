"""Bounded, ephemeral multi-speaker Discord context."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger("discord_bot")


def _clean_label(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    return (text or fallback)[:80]


def _message_content(message: Any, *, max_chars: int) -> str:
    content = str(getattr(message, "content", "") or "").strip()
    if not content:
        attachments = list(getattr(message, "attachments", None) or [])
        if attachments:
            content = f"[shared {len(attachments)} attachment(s)]"
    return content[:max_chars]


def _speaker_turn(message: Any, bot_user: Any, *, max_chars: int) -> dict[str, str] | None:
    author = getattr(message, "author", None)
    if author is None:
        return None
    author_id = str(getattr(author, "id", "unknown"))
    bot_id = str(getattr(bot_user, "id", "")) if bot_user is not None else ""
    is_multivac = bool(bot_id and author_id == bot_id)
    if bool(getattr(author, "bot", False)) and not is_multivac:
        return None

    content = _message_content(message, max_chars=max_chars)
    if not content:
        return None
    reference = getattr(message, "reference", None)
    reply_to = getattr(reference, "message_id", None) if reference else None
    reply_suffix = f"; reply_to_message_id={reply_to}" if reply_to else ""
    if is_multivac:
        return {
            "role": "assistant",
            "content": f"[Multivac in shared channel{reply_suffix}]\n{content}",
        }

    display_name = _clean_label(
        getattr(author, "display_name", None) or getattr(author, "name", None),
        f"user-{author_id}",
    )
    return {
        "role": "user",
        "content": (
            f"[Channel speaker: {display_name}; user_id={author_id}{reply_suffix}]\n"
            f"{content}"
        ),
    }


async def fetch_recent_channel_context(
    message: Any,
    bot_user: Any,
    *,
    limit: int = 24,
    max_total_chars: int = 12_000,
    max_message_chars: int = 1_200,
) -> list[dict[str, str]]:
    """Read bounded messages before the invocation without persisting them.

    The latest messages win when the character budget is reached. Each human
    turn carries an explicit stable speaker ID so models do not collapse a
    shared channel into a one-to-one conversation.
    """
    channel = getattr(message, "channel", None)
    history = getattr(channel, "history", None)
    if not callable(history):
        return []

    newest_first: list[dict[str, str]] = []
    used = 0
    try:
        async for prior in history(
            limit=max(1, min(int(limit), 50)),
            before=message,
            oldest_first=False,
        ):
            turn = _speaker_turn(prior, bot_user, max_chars=max_message_chars)
            if not turn:
                continue
            remaining = max_total_chars - used
            if remaining <= 0:
                break
            if len(turn["content"]) > remaining:
                turn = {**turn, "content": turn["content"][:remaining]}
            newest_first.append(turn)
            used += len(turn["content"])
            if used >= max_total_chars:
                break
    except Exception:
        logger.warning("Unable to fetch ephemeral channel conversation context", exc_info=True)
        return []

    newest_first.reverse()
    return newest_first


__all__ = ["fetch_recent_channel_context"]
