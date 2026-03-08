from __future__ import annotations

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

