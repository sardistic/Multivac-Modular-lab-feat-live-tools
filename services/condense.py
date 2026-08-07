"""Map-reduce condensation of long texts (transcripts, articles) using the
cheap model tiers, so full content fits in a prompt without full-model cost.

Cost shape: a 40-minute video transcript (~37k chars ≈ 10k tokens) costs about
$0.0005 through the nano tier for the map pass, plus a fraction of a cent for
the reduce pass — versus feeding raw text to the main model on every question.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("discord_bot")

# ~6k tokens per map chunk; comfortably inside the nano tier's context.
CHUNK_CHARS = 24_000
# Hard ceiling: beyond this we condense only the first N chars (~8 hours of speech).
MAX_INPUT_CHARS = 200_000

_MAP_SYSTEM = (
    "Condense this transcript/document section into dense notes. Preserve every "
    "distinct topic, claim, name, number, and recommendation, in order. Terse "
    "fragments are fine. No preamble, no meta-commentary."
)

_REDUCE_SYSTEM = (
    "Merge these sequential section notes into one coherent condensed outline of "
    "the full content. Preserve all distinct topics in order and their key "
    "details. No preamble."
)


async def condense_long_text(text: str, *, target_chars: int = 9000) -> str:
    """Return text condensed to <= target_chars. Short input passes through
    untouched (zero cost). Falls back to truncation if the model calls fail."""
    if len(text) <= target_chars:
        return text

    from providers.openai_client import OPENAI_INTENT_MODEL, OPENAI_LIGHT_MODEL
    from providers.openai_messages import generate_openai_messages_response

    text = text[:MAX_INPUT_CHARS]
    chunks = [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]

    async def _map(idx: int, chunk: str) -> str:
        try:
            notes = await generate_openai_messages_response(
                [
                    {"role": "system", "content": _MAP_SYSTEM},
                    {"role": "user", "content": chunk},
                ],
                model=OPENAI_INTENT_MODEL,
                max_tokens=2000,
            )
            if notes and notes.strip() and not notes.startswith("⚠️"):
                return f"[Part {idx}/{len(chunks)}]\n{notes.strip()}"
        except Exception:
            logger.warning("condense map pass failed for chunk %d", idx, exc_info=True)
        # Fallback: keep the head of the raw chunk rather than dropping it.
        return f"[Part {idx}/{len(chunks)} — raw excerpt]\n{chunk[:2000]}"

    partials = await asyncio.gather(*(_map(i + 1, c) for i, c in enumerate(chunks)))
    combined = "\n\n".join(partials)

    if len(combined) > target_chars:
        try:
            reduced = await generate_openai_messages_response(
                [
                    {"role": "system", "content": _REDUCE_SYSTEM},
                    {"role": "user", "content": combined[:MAX_INPUT_CHARS]},
                ],
                model=OPENAI_LIGHT_MODEL,
                max_tokens=2500,
            )
            if reduced and reduced.strip() and not reduced.startswith("⚠️"):
                combined = reduced.strip()
        except Exception:
            logger.warning("condense reduce pass failed", exc_info=True)

    return combined[:target_chars]
