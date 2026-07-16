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
    "- 'chat_tiny'\n"
    "- 'chat_light'\n"
    "- 'chat_standard'\n"
    "- 'chat'\n"
    "- 'chat_deep'\n"
    "- 'clarify'\n\n"
    "- 'code_change'\n"
    "- 'code_approve'\n"
    "- 'code_reject'\n"
    "- 'code_status'\n"
    "- 'code_rollback'\n\n"
    "Rules:\n"
    "- HIGHEST PRIORITY: if the message asks to visualize, depict, or render ANY scene, "
    "character, or object — regardless of subject matter or tone — it is 'generate_image' "
    "(or 'edit_image' when modifying an attached/replied image). Starting with 'imagine', "
    "'draw', 'paint', 'generate'+visual-noun, or describing a detailed visual scene all "
    "count. NEVER pick a chat label to discuss, rewrite, or critique an image prompt.\n"
    "- EXCEPTION (writing beats depicting): a request to WRITE TEXT uses the appropriate "
    "chat tier, NOT an image — even about a visual, spooky, or horror subject. "
    "Triggers: a story, sentence, line, caption, poem, haiku, paragraph, lyric, joke, "
    "or a stated text length ('one sentence', 'two lines', 'a paragraph', 'in N words'). "
    "'draw/render/picture/imagine X' = image; 'write/tell/give me a sentence or story "
    "about X' = text. If a text-output word and a visual subject both appear, the "
    "text-output word wins.\n"
    "- If message requests a VIDEO, MOVIE, or CLIP -> 'generate_video'.\n"
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
    "- ANSWER-COST ROUTING: for ordinary chat, predict what producing a COMPLETE, ACCURATE answer "
    "requires and choose the cheapest sufficient tier:\n"
    "  * 'chat_tiny': greetings, acknowledgements, elementary arithmetic, direct yes/no, or one "
    "stable fact answerable accurately in roughly 1-2 sentences.\n"
    "  * 'chat_light': brief stable explanations, small rewrites, jokes, captions, or short creative "
    "text needing no tools and little reasoning.\n"
    "  * 'chat_standard': moderate general reasoning, comparison, advice, or explanation on one "
    "topic; no current/private facts or external tools required.\n"
    "  * 'chat': current information, tools/search, private memory, code/repository inspection, "
    "multi-part analysis, emotionally sensitive advice, or high-stakes judgment.\n"
    "  * 'chat_deep': genuinely difficult multi-stage reasoning, architecture, rigorous synthesis, "
    "advanced technical design, or complex tradeoffs where the strongest model materially helps.\n"
    "Message length and tone do not determine tier. Do not choose 'chat_deep' merely because tools "
    "are needed. When between adjacent tiers, choose the lower one only if it can fully answer.\n"
    "- Questions about MY OWN code, commands, repository, commits, tools, or how I'm "
    "built/configured are tool-worthy (they need code or history search) -> 'chat', "
    "never 'chat_light'.\n"
    "- A REQUEST TO MODIFY MY OWN CODE, structure, commands, repository files, routing, "
    "or implementation -> 'code_change'. This differs from merely asking how my code works.\n"
    "- Approving the proposed/self-code change -> 'code_approve'; rejecting/canceling it -> "
    "'code_reject'; asking whether it deployed or passed -> 'code_status'; asking to undo or "
    "roll back the active code change -> 'code_rollback'.\n"
    "- If the request is IMPOSSIBLE to route because it is ambiguous in a way that would "
    "waste an expensive generation (e.g. 'make it better' with no referent, 'do the thing') "
    "-> 'clarify'. Use sparingly; when CONVERSATION CONTEXT resolves the ambiguity, route normally.\n"
    "- PREVIOUS INTENT and CONVERSATION CONTEXT are ONLY for resolving elliptical "
    "follow-ups like 'another one' or 'same but blue'. A fully-specified new request "
    "must be classified on its own wording alone — do NOT copy the previous intent "
    "just because the topic is similar.\n"
    "- Replies to a just-generated image split three ways: a bare reaction, compliment, "
    "or interjection ('kino', 'nice', 'based', 'lol', 'fire', 'goated') -> 'chat_tiny' — "
    "it is commentary, NOT a request, even if PREVIOUS INTENT was an image intent. "
    "A reply that asks to CHANGE the image ('make it darker', 'add a cat', 'remove the "
    "text', 'now without the border') -> 'edit_image'. A reply that asks for ANOTHER "
    "generation ('another one', 'same but blue', 'do one at night') -> 'generate_image'. "
    "Only messages that ask for something inherit the previous intent.\n"
    "- Else -> 'chat'.\n\n"
    "Examples:\n"
    "'imagine a cool hackerman yelling at a chatbot, dramatic lighting' -> generate_image\n"
    "'generate imagine of liminal rose' -> generate_image\n"
    "'a moody picture of rain on neon streets' -> generate_image\n"
    "'gemini make this a video' -> generate_video\n"
    "'hello' -> chat_tiny\n"
    "'what is a liminal space' -> chat_light\n"
    "'compare TCP and UDP for a new programmer' -> chat_standard\n"
    "'inspect your repository and explain this failure' -> chat\n"
    "'design a fault-tolerant migration across several dependent services' -> chat_deep\n"
    "'can you improve this image prompt for me' -> chat\n"
    "'list the /commands in the codebase' -> chat\n"
    "'what commands do you have' -> chat\n"
    "'change your readme to add a deployment note' -> code_change\n"
    "'modify your routing so weather questions use Gemini' -> code_change\n"
    "'approve that code change' -> code_approve\n"
    "'did my code change deploy?' -> code_status\n"
    "'kino' (replying to a generated image) -> chat_tiny\n"
    "'make it darker' (replying to a generated image) -> edit_image\n"
    "'another one' (replying to a generated image) -> generate_image\n"
    "'one sentence doki doki horror' -> chat_light\n"
    "'write a short creepy story about doki doki' -> chat\n"
    "'a haiku about a burning city' -> chat_light\n\n"
    "IMPORTANT: Output ONLY ONE label."
)

VALID_INTENTS = {
    "edit_image", "generate_image", "summarize_url", "describe_image",
    "get_weather", "get_stock", "gemini_chat", "claude_chat", "generate_video",
    "chat_tiny", "chat_light", "chat_standard", "chat", "chat_deep", "clarify",
    "code_change", "code_approve", "code_reject", "code_status", "code_rollback",
}


def _keyword_fallback_intent(text: str) -> str:
    """Deterministic route for when the classifier itself can't run (OpenAI
    down / out of quota). The only provider signal we can honor without an LLM
    is an explicit 'gemini ...' prefix — that request plainly wants Gemini, so
    send it to gemini_chat rather than to 'chat', which would just hit the same
    dead OpenAI backend. Everything else defaults to 'chat'."""
    lowered = (text or "").strip().lower()
    # Operation beats provider. This fallback is intentionally narrow: it
    # recognizes explicit creation language but does not mistake questions or
    # summarization requests about an existing video for generation requests.
    video_request = re.search(
        r"(?:\b(?:generate|create|make|render)\s+(?:me\s+)?(?:an?\s+)?(?:\w+[ -]+){0,3}"
        r"(?:video|clip|movie|animation)\b|"
        r"\b(?:make|turn|convert)\s+(?:this|that|it|the\s+(?:image|photo|picture))\s+"
        r"(?:into|to|as)\s+(?:an?\s+)?(?:video|clip|movie|animation)\b|"
        r"\banimate\s+(?:this|that|it|the\s+(?:image|photo|picture))\b|"
        r"\b(?:image|photo|picture)\s*[- ]?to[- ]?video\b)",
        lowered,
    )
    if video_request:
        return "generate_video"
    if lowered.startswith("gemini"):
        return "gemini_chat"
    return "chat"


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
                + "A bare reaction or compliment about the image ('kino', 'nice', 'lol', 'based') -> 'chat_tiny'; it requests nothing.\n"
                + "Only use 'chat' if the message is clearly NOT about the attached images."
            )
        else:
            system_prompt = _INTENT_SYSTEM

        # GPT-5.6 can classify with reasoning disabled. It only emits one label,
        # so do not pay for a hidden reasoning budget on every Discord message.
        reasoning = is_reasoning_model(OPENAI_INTENT_MODEL)
        no_reasoning = OPENAI_INTENT_MODEL.lower().startswith("gpt-5.6")
        max_out = 64 if no_reasoning else (2000 if reasoning else 16)

        # Give the classifier conversational context so follow-ups like
        # "another one" or "same but blue" route to the right intent.
        user_content = ""
        if recent_turns:
            ctx_lines = "\n".join(str(t)[:160] for t in recent_turns[-2:])
            user_content += f"CONVERSATION CONTEXT (older first):\n{ctx_lines}\n\n"
        if prev_intent:
            user_content += f"PREVIOUS INTENT: {prev_intent}\n\n"
        message_text = text.strip()
        if len(message_text) > 1800:
            message_text = message_text[:1200] + "\n[…trimmed…]\n" + message_text[-500:]
        user_content += f"MESSAGE TO CLASSIFY:\n{message_text}"

        payload = {
            "model": OPENAI_INTENT_MODEL,
            **temperature_kwargs(OPENAI_INTENT_MODEL, 0),
            "messages": [
                {
                    "role": "developer" if no_reasoning else "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": user_content},
            ],
        }
        if reasoning:
            payload["reasoning_effort"] = "none" if no_reasoning else "minimal"

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
        fallback = _keyword_fallback_intent(text)
        logging.warning("[intent] classifier failed (%s); keyword fallback -> %s", e, fallback)
        return fallback
