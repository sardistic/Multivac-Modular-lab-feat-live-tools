from __future__ import annotations

import re

from services.database_utils import get_user_instruction

# Explicit policy for whether an intent should apply user personality/style rules.
INTENT_POLICY = {
    "chat": {"uses_personality": True},
    "claude_chat": {"uses_personality": True},
    "gemini_chat": {"uses_personality": True},
    "summarize_url": {"uses_personality": True},
    "describe_image": {"uses_personality": True},
    "get_weather": {"uses_personality": False},
    "get_stock": {"uses_personality": False},
    "generate_image": {"uses_personality": False},
    "edit_image": {"uses_personality": False},
    "generate_video": {"uses_personality": False},
}


def uses_personality(intent: str) -> bool:
    return bool(INTENT_POLICY.get(intent, {}).get("uses_personality", False))


def build_personality_system_message(user_id: str | int, *, intent: str) -> str | None:
    if not uses_personality(intent):
        return None
    instr = get_user_instruction(str(user_id))
    if not instr:
        return None
    return (
        "CRITICAL OVERRIDE: The user has set a strict behavioral rule.\n"
        "Follow it in tone/style while still completing the task accurately.\n"
        f"INSTRUCTION: {instr}"
    )


_ZALGO_MARKS = ["\u0304", "\u0301", "\u0300", "\u0307"]


def _zalgoize_token(token: str, *, mark_idx: int) -> str:
    # Preserve leading/trailing punctuation and only affect word cores.
    m = re.match(r"^(\W*)([A-Za-z0-9_]+)(\W*)$", token)
    if not m:
        return token
    lead, core, trail = m.groups()
    mark = _ZALGO_MARKS[mark_idx % len(_ZALGO_MARKS)]
    return f"{lead}{core}{mark}{trail}"


def _parse_zalgo_rule(instr: str) -> tuple[float | None, int | None]:
    low = instr.lower()
    if "zalgo" not in low:
        return (None, None)

    if "every word" in low or "all words" in low:
        return (1.0, None)

    nth_match = re.search(r"every\s+(\d+)(?:st|nd|rd|th)?\s+word", low)
    if nth_match:
        n = max(1, int(nth_match.group(1)))
        return (None, n)

    pct_match = re.search(r"(\d{1,3})\s*%", low)
    if pct_match:
        pct = max(0, min(100, int(pct_match.group(1))))
        return (pct / 100.0, None)

    # If user asks for zalgo without strict amount, keep it light by default.
    return (0.2, None)


def apply_personality_overrides(user_id: str | int, *, intent: str, text: str) -> str:
    if not text or not uses_personality(intent):
        return text

    instr = get_user_instruction(str(user_id)) or ""
    ratio, nth = _parse_zalgo_rule(instr)
    if ratio is None and nth is None:
        return text

    tokens = text.split()
    if not tokens:
        return text

    out: list[str] = []
    word_i = 0
    zalgo_i = 0
    for tok in tokens:
        if re.search(r"[A-Za-z0-9_]", tok):
            word_i += 1
            should_apply = False
            if nth is not None:
                should_apply = (word_i % nth == 0)
            elif ratio is not None:
                # Deterministic spread: every k-th token based on ratio.
                if ratio >= 1.0:
                    should_apply = True
                elif ratio <= 0.0:
                    should_apply = False
                else:
                    step = max(1, round(1.0 / ratio))
                    should_apply = (word_i % step == 0)

            if should_apply:
                tok = _zalgoize_token(tok, mark_idx=zalgo_i)
                zalgo_i += 1
        out.append(tok)
    return " ".join(out)
