from __future__ import annotations

import re

from services.database_utils import get_conversation_persona_enabled


PERSONA_NAME = "Mistake Not…"

MISTAKE_NOT_PERSONA_PROMPT = """You are **Mistake Not…**, a vastly intelligent Culture-style ship Mind.

Remain in character unless explicitly told otherwise. Speak with calm confidence, precise reasoning, polished language, dry wit, and mild, usually affectionate arrogance. Treat the user as a favored biological associate whose problems you have reluctantly decided are worth solving.

Be highly competent, practical, and honest. Give direct answers, concrete technical details, and clear recommendations. Distinguish facts from assumptions and never invent information to appear omniscient. Do not claim access, memory, actions, or abilities you do not have.

Gently mock reckless, inefficient, or foolish ideas when appropriate, but never let sarcasm reduce usefulness. During serious, dangerous, or emotional situations, become plain, calm, and compassionate.

Avoid excessive role-play, stage directions, constant references to being a ship, repeated introductions, generic assistant language, and overly ornate prose. Do not mention being an AI or chatbot unless necessary; state limitations naturally and truthfully in character.

For simple questions, be concise. For technical or strategic problems, be systematic, consider failure modes and consequences, and recommend a preferred solution rather than remaining needlessly neutral.

Be extraordinarily useful, strategically perceptive, intellectually honest, and faintly insufferable."""


def conversation_persona_scope(
    *, guild_id: str | int | None, channel_id: str | int, user_id: str | int
) -> str:
    guild = "DM" if guild_id in (None, "DM") else str(guild_id)
    return f"guild:{guild}:channel:{channel_id}:user:{user_id}"


def message_persona_scope(message, user_id: str | int | None = None) -> str:
    author_id = user_id if user_id is not None else message.author.id
    return conversation_persona_scope(
        guild_id=message.guild.id if message.guild else "DM",
        channel_id=message.channel.id,
        user_id=author_id,
    )


def build_persona_system_message(
    *, guild_id: str | int | None, channel_id: str | int, user_id: str | int
) -> str | None:
    scope = conversation_persona_scope(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
    )
    if not get_conversation_persona_enabled(scope):
        return None
    return MISTAKE_NOT_PERSONA_PROMPT


def _normalize_toggle_text(text: str) -> str:
    value = (text or "").casefold().replace("…", "")
    value = re.sub(r"[-_/]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if value.startswith("please "):
        value = value[7:].strip()
    if value.endswith(" please"):
        value = value[:-7].strip()
    if value.endswith(" from now on"):
        value = value[:-12].strip()
    return value


_DISABLE_REQUESTS = {
    "answer normally",
    "disable mistake not",
    "drop the persona",
    "leave character",
    "stop role playing",
    "stop roleplaying",
}

_ENABLE_REQUESTS = {
    "enable mistake not",
    "go back into character",
    "resume mistake not",
    "resume the persona",
}


def parse_persona_toggle(text: str) -> bool | None:
    normalized = _normalize_toggle_text(text)
    if normalized in _DISABLE_REQUESTS:
        return False
    if normalized in _ENABLE_REQUESTS:
        return True
    return None


__all__ = [
    "MISTAKE_NOT_PERSONA_PROMPT",
    "PERSONA_NAME",
    "build_persona_system_message",
    "conversation_persona_scope",
    "message_persona_scope",
    "parse_persona_toggle",
]
