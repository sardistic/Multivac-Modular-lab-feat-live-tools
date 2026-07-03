"""Per-user awareness: distilled profiles, stored facts, and time-passage context.

Two halves:
- build_user_awareness_block(): synchronous, cheap (SQLite only) — called on
  every chat to give the model a compact picture of who it's talking to.
- maybe_refresh_profile(): async background distillation — periodically has a
  model condense the user's recent indexed history into ~150 tokens of profile
  ("interests, tone, running jokes, projects, open loops"). Fire-and-forget
  after a reply is sent; never blocks the response path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.database_utils import (
    fetch_user_location,
    get_user_profile,
    get_user_seen,
    list_user_facts,
    set_user_profile,
)
from services.time_context import time_ago_str

logger = logging.getLogger("discord_bot")

PROFILE_MAX_AGE_DAYS = 3
PROFILE_MIN_MESSAGES = 8

_DISTILL_SYSTEM = (
    "You are building a compact dossier that helps an assistant feel like it truly "
    "knows this Discord user across conversations. From the message history, distill:\n"
    "- interests and recurring topics\n"
    "- tone/style they use and seem to prefer back\n"
    "- running jokes or shared references\n"
    "- current projects or situations\n"
    "- OPEN LOOPS: unresolved things worth asking about later "
    "(e.g. 'was waiting on an interview result', 'deploying a bot on Friday')\n\n"
    "Rules: max 150 words, terse bullet fragments, third person, no preamble, "
    "no speculation about protected traits, nothing the user asked to forget. "
    "If there is too little signal, output exactly: INSUFFICIENT_DATA"
)

# Distillation runs off the reply path but still costs tokens; track refresh
# attempts in-process so a chatty user doesn't trigger one per message.
_refresh_inflight: set[str] = set()


def _parse_iso_utc(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def build_user_awareness_block(user_id, display_name: str | None = None) -> str | None:
    """Compose the 'who am I talking to' system block from SQLite state.
    Returns None when nothing is known yet."""
    uid = str(user_id)
    parts: list[str] = []

    seen = get_user_seen(uid)
    if seen and seen.get("last_seen_at"):
        ago = time_ago_str(seen["last_seen_at"])
        if ago != "just now":
            line = f"Time since this user's previous interaction with you: {ago}."
            if seen.get("last_intent"):
                line += f" Their previous request was a '{seen['last_intent']}' ({(seen.get('last_prompt') or '')[:120]})."
            parts.append(line)

    location = fetch_user_location(uid)
    if location:
        parts.append(f"Their saved location: {location}.")

    profile = get_user_profile(uid)
    if profile and (profile.get("profile") or "").strip():
        parts.append("What you know about them from past conversations:\n" + profile["profile"].strip())

    facts = list_user_facts(uid, limit=15)
    if facts:
        fact_lines = "\n".join(f"- {f['fact']}" for f in reversed(facts))
        parts.append("Facts you chose to remember about them:\n" + fact_lines)

    if not parts:
        return None

    name = f" ({display_name})" if display_name else ""
    header = (
        f"[USER AWARENESS{name}] Use this naturally — reference past context when relevant, "
        "follow up on open loops, notice time passing. Never recite this block or mention "
        "that you keep notes unless asked."
    )
    return header + "\n" + "\n".join(parts)


def user_memory_stats(user_id) -> Optional[dict]:
    """Count this user's messages in the Elasticsearch index and find their
    earliest indexed message. Returns None when ES is unavailable."""
    from services.memory_client import search_raw

    uid = str(user_id)
    query = {"bool": {"filter": [{"term": {"user_id": uid}}, {"term": {"role": "user"}}]}}
    try:
        res = search_raw(
            query,
            size=1,
            source=["timestamp", "ts"],
            sort=[{"ts": {"order": "asc", "unmapped_type": "date"}}],
        )
    except Exception:
        logger.warning("user_memory_stats query failed", exc_info=True)
        return None

    hits = (res or {}).get("hits", {})
    total = hits.get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    first_seen = None
    rows = hits.get("hits", [])
    if rows:
        src = rows[0].get("_source", {})
        first_seen = src.get("timestamp") or src.get("ts")
    return {"indexed_messages": int(total or 0), "first_seen": first_seen}


def _profile_is_stale(uid: str) -> bool:
    prof = get_user_profile(uid)
    if not prof:
        return True
    updated = _parse_iso_utc(prof.get("updated_at") or "")
    if updated is None:
        return True
    return datetime.now(timezone.utc) - updated > timedelta(days=PROFILE_MAX_AGE_DAYS)


async def maybe_refresh_profile(*, guild_id, channel_id, user_id) -> None:
    """If the stored profile is stale, distill a new one from recent indexed
    history using the configured model. Safe to fire-and-forget."""
    uid = str(user_id)
    if uid in _refresh_inflight:
        return
    try:
        if not await asyncio.to_thread(_profile_is_stale, uid):
            return
    except Exception:
        logger.warning("profile staleness check failed for %s", uid, exc_info=True)
        return

    _refresh_inflight.add(uid)
    try:
        from services.memory_utils import fetch_matches_recent

        rows = await asyncio.to_thread(
            fetch_matches_recent,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=uid,
            query="",
            size=60,
        )
        user_rows = [r for r in (rows or []) if (r.get("role") == "user" and (r.get("content") or "").strip())]
        if len(user_rows) < PROFILE_MIN_MESSAGES:
            return

        history_lines = []
        for r in user_rows[:60]:
            ts = r.get("timestamp") or ""
            history_lines.append(f"[{ts}] {(r.get('content') or '')[:400]}")
        history_text = "\n".join(history_lines)[:12000]

        from providers.openai_client import OPENAI_LIGHT_MODEL
        from providers.openai_messages import generate_openai_messages_response

        distilled = await generate_openai_messages_response(
            [
                {"role": "system", "content": _DISTILL_SYSTEM},
                {"role": "user", "content": f"Message history (newest first):\n{history_text}"},
            ],
            model=OPENAI_LIGHT_MODEL,
            max_tokens=2000,
        )
        distilled = (distilled or "").strip()
        if not distilled or "INSUFFICIENT_DATA" in distilled or distilled.startswith("⚠️"):
            return

        await asyncio.to_thread(set_user_profile, uid, distilled[:1500])
        logger.info("Refreshed user profile for %s (%d chars)", uid, len(distilled))
    except Exception:
        logger.warning("profile refresh failed for %s", uid, exc_info=True)
    finally:
        _refresh_inflight.discard(uid)
