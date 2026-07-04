from __future__ import annotations

import logging
import re

from providers.openai_client import (
    OPENAI_INTENT_MODEL,
    get_openai_client,
    is_reasoning_model,
    temperature_kwargs,
)
from providers.openai_messages import OpenAIModerationError

_INTENT_SYSTEM = (
    "You are a fast, lightweight intent classifier.\n"
    "Classify a user's message into one of:\n"
    "- 'edit_image'\n"
    "- 'generate_image'\n"
    "- 'summarize_url'\n"
    "- 'describe_image'\n"
    "- 'get_weather'\n"
    "- 'get_stock'\n"
    "- 'gemini_chat'\n"
    "- 'claude_chat'\n"
    "- 'generate_video'\n"
    "- 'chat_light'\n"
    "- 'chat'\n"
    "- 'clarify'\n\n"
    "Rules:\n"
    "- If message requests a VIDEO, MOVIE, or CLIP -> 'generate_video'.\n"
    "- If message starts with \"imagine\", \"generate\", \"draw\", \"create\", \"paint\" AND is NOT about video -> 'generate_image'.\n"
    "- If user says \"transparent background\" -> 'generate_image'.\n"
    "- If replying to an image and mentions \"change\", \"edit\", \"make transparent\", \"fix\" -> 'edit_image'.\n"
    "- ONLY a pure weather request (nothing else asked) -> 'get_weather'. Weather mixed with anything else -> 'chat' (it has a weather tool).\n"
    "- If a URL is present and they want a summary -> 'summarize_url'.\n"
    "- If they ask to describe an image -> 'describe_image'.\n"
    "- ONLY a pure quote request like 'stock <TICKER>' -> 'get_stock'. Stocks mixed with discussion -> 'chat'.\n"
    "- Provider names ('gemini', 'claude') select WHICH backend, NOT the intent: "
    "'gemini generate an image of X' -> 'generate_image'; 'gemini make this a video' -> "
    "'generate_video'; 'gemini edit this' with an image -> 'edit_image'. Only when a "
    "gemini-prefixed message is plain conversation -> 'gemini_chat'.\n"
    "- If message starts with 'claude' or user explicitly asks for 'Claude' -> 'claude_chat'.\n"
    "- Casual banter, greetings, reactions, one-liners, jokes, simple factual questions "
    "that need no tools or deep reasoning -> 'chat_light'.\n"
    "- Anything substantive, multi-part, tool-worthy, or emotionally weighty -> 'chat'.\n"
    "- If the request is IMPOSSIBLE to route because it is ambiguous in a way that would "
    "waste an expensive generation (e.g. 'make it better' with no referent, 'do the thing') "
    "-> 'clarify'. Use sparingly; when CONVERSATION CONTEXT resolves the ambiguity, route normally.\n"
    "- PREVIOUS INTENT and CONVERSATION CONTEXT are ONLY for resolving elliptical "
    "follow-ups like 'another one' or 'same but blue'. A fully-specified new request "
    "must be classified on its own wording alone — do NOT copy the previous intent "
    "just because the topic is similar.\n"
    "- Else -> 'chat'.\n\n"
    "IMPORTANT: Output ONLY ONE label."
)

VALID_INTENTS = {
    "edit_image", "generate_image", "summarize_url", "describe_image",
    "get_weather", "get_stock", "gemini_chat", "claude_chat", "generate_video",
    "chat", "chat_light", "clarify",
}


async def classify_intent(
    text: str,
    has_images: bool = False,
    *,
    recent_turns: list | None = None,
    prev_intent: str | None = None,
) -> str:
    try:
        if not (text or "").strip():
            return "chat"

        if has_images:
            system_prompt = (
                _INTENT_SYSTEM
                + "\n\n"
                + "CRITICAL: The user has attached one or more IMAGES with their message.\n"
                + "When images are present, assume the user's request is ABOUT those images unless explicitly stated otherwise.\n\n"
                + "Choose the intent:\n"
                + "- 'generate_image' = User wants to CREATE a NEW image from scratch (imagine, generate, draw, paint, create)\n\n"
                + "IMPORTANT: If the user says 'edit', 'change', 'make', 'transform' -> 'edit_image' (even if it involves text).\n"
                + "Only use 'describe_image' if they specifically ask what is in the image, or to transcribe/translate text WITHOUT modifying the image.\n"
                + "Only use 'chat' if the message is clearly NOT about the attached images."
            )
        else:
            system_prompt = _INTENT_SYSTEM

        # Reasoning models (gpt-5+/o-series) burn hidden reasoning tokens before
        # emitting the label, so a tiny budget of 16 gets exhausted mid-reasoning
        # and returns a 400. Give them room and keep reasoning effort minimal.
        reasoning = is_reasoning_model(OPENAI_INTENT_MODEL)
        max_out = 2000 if reasoning else 16

        # Give the classifier conversational context so follow-ups like
        # "another one" or "same but blue" route to the right intent.
        user_content = ""
        if recent_turns:
            ctx_lines = "\n".join(str(t)[:200] for t in recent_turns[-3:])
            user_content += f"CONVERSATION CONTEXT (older first):\n{ctx_lines}\n\n"
        if prev_intent:
            user_content += f"PREVIOUS INTENT: {prev_intent}\n\n"
        user_content += f"MESSAGE TO CLASSIFY:\n{text.strip()}"

        payload = {
            "model": OPENAI_INTENT_MODEL,
            **temperature_kwargs(OPENAI_INTENT_MODEL, 0),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if reasoning:
            payload["reasoning_effort"] = "minimal"

        try:
            resp = await get_openai_client().chat.completions.create(
                **payload,
                max_completion_tokens=max_out,
            )
        except Exception as e:
            msg = str(e).lower()
            # Drop reasoning_effort if this model/endpoint doesn't accept it.
            if "reasoning_effort" in msg and "reasoning_effort" in payload:
                payload.pop("reasoning_effort", None)
                resp = await get_openai_client().chat.completions.create(
                    **payload,
                    max_completion_tokens=max_out,
                )
            elif "max_completion_tokens" in msg and ("unsupported" in msg or "unknown" in msg):
                resp = await get_openai_client().chat.completions.create(
                    **payload,
                    max_tokens=max_out,
                )
            else:
                raise
        from services import usage_costs
        usage_costs.record_response(OPENAI_INTENT_MODEL, resp, label="intent_classify")

        label = (resp.choices[0].message.content or "").strip().lower()
        label = re.sub(r"[^a-z_]", "", label)
        return label if label in VALID_INTENTS else "chat"
    except Exception as e:
        if isinstance(e, OpenAIModerationError):
            raise
        logging.warning("[intent] fallback to chat due to: %s", e)
        return "chat"
