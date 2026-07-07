from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from bot.chat_handler import handle_chat_intent
from bot.image_handler import (
    handle_describe_image_intent,
    handle_edit_image_intent,
    handle_generate_image_intent,
    send_debug_context,
)
from bot.provider_intents import (
    handle_claude_chat_intent,
    handle_gemini_chat_intent,
    handle_summarize_url_intent,
)
from bot.video_handler import handle_generate_video_intent
from services.stock_utils import handle_stock_command
from services.weather_utils import handle_weather_request

def resolve_keyword_intent(raw_prompt: str, prompt: str, has_attachments: bool) -> Optional[str]:
    """Keywords decide the PROVIDER, the LLM classifier decides the INTENT.

    The only intent shortcut left is an explicit "claude ..." prefix (Claude
    only does chat here). Everything else — including "gemini ..." messages —
    goes to the classifier, which understands 'gemini generate an image of X'
    means image generation. Which backend renders it is decided separately
    (wants_gemini)."""
    lowered_raw = raw_prompt.lower().strip()
    lowered_prompt = prompt.lower().strip()

    if lowered_prompt.startswith("claude") or lowered_raw.startswith("claude"):
        return "claude_chat"

    # "imagine ..." is this bot's established image command word — always an
    # image request, with or without a leading provider name. (Provider is
    # still chosen separately via wants_gemini.)
    without_provider = re.sub(r"^gemini[\s,:]+", "", lowered_prompt)
    if without_provider.startswith(("imagine ", "imagine:")):
        return "generate_image"

    return None


def wants_gemini(prompt: str) -> bool:
    """Provider selection: the user said gemini -> use gemini; otherwise the
    default provider (OpenAI). Word-boundary aware so 'the gemini zodiac
    sign' in the middle of a scene description doesn't count, but leading
    'gemini ...' or 'with/using gemini' does."""
    lowered = (prompt or "").lower().strip()
    return (
        lowered.startswith("gemini")
        or "with gemini" in lowered
        or "using gemini" in lowered
        or "use gemini" in lowered
    )


def get_duration_estimate(intent: str) -> int:
    return {
        "generate_image": 40,
        "edit_image": 40,
        "summarize_url": 10,
        "describe_image": 8,
        "chat": 6,
        "chat_light": 4,
        "clarify": 3,
        "get_weather": 5,
        "get_stock": 5,
    }.get(intent, 12)


@dataclass
class DispatchContext:
    """Everything a single triggered message needs to be routed and answered."""

    intent: str
    message: Any
    prompt: str
    raw_prompt: str
    user_id: Any
    bot_user: Any
    ref_msg: Any = None
    is_reply_to_bot: bool = False
    image_urls: List[Any] = field(default_factory=list)
    gemini_parts: List[Any] = field(default_factory=list)
    general_url_match: Any = None
    stream_ok: bool = False
    # Injected collaborators (Discord-side helpers owned by discord_bot.py)
    get_location_details: Optional[Callable] = None
    get_weather_data: Optional[Callable] = None
    live_status_with_progress: Optional[Callable] = None
    send_or_edit_with_truncation: Optional[Callable] = None
    prompt_for_image_selection: Optional[Callable] = None
    moderation_view_factory: Any = None


async def dispatch_intent(ctx: DispatchContext) -> bool:
    duration_estimate = get_duration_estimate(ctx.intent)
    message = ctx.message

    if ctx.intent == "get_weather":
        response = await handle_weather_request(
            message,
            ctx.bot_user.id,
            ctx.get_location_details,
            ctx.get_weather_data,
            None,
            f"{message.guild.id}-{message.channel.id}",
            message.author.id,
        )
        if response:
            await ctx.send_or_edit_with_truncation(response, channel=message.channel, reply_to=message)
        return True

    if ctx.intent == "claude_chat":
        await handle_claude_chat_intent(
            message=message,
            prompt=ctx.prompt,
            stream_ok=ctx.stream_ok,
            live_status_with_progress=ctx.live_status_with_progress,
            send_or_edit_with_truncation=ctx.send_or_edit_with_truncation,
        )
        return True

    if ctx.intent == "gemini_chat":
        await handle_gemini_chat_intent(
            message=message,
            prompt=ctx.prompt,
            gemini_parts=ctx.gemini_parts,
            live_status_with_progress=ctx.live_status_with_progress,
            send_or_edit_with_truncation=ctx.send_or_edit_with_truncation,
            moderation_view_factory=ctx.moderation_view_factory,
        )
        return True

    if ctx.intent == "generate_image":
        if message.content.startswith("/debug_context"):
            await send_debug_context(message, ctx.bot_user)
            return True
        await handle_generate_image_intent(
            message=message,
            prompt=ctx.prompt,
            ref_msg=ctx.ref_msg,
            duration_estimate=duration_estimate,
            stream_ok=ctx.stream_ok,
            live_status_with_progress=ctx.live_status_with_progress,
            use_gemini=wants_gemini(ctx.prompt) or wants_gemini(ctx.raw_prompt),
        )
        return True

    if ctx.intent == "edit_image" and ctx.image_urls:
        use_gemini = wants_gemini(ctx.prompt) or wants_gemini(ctx.raw_prompt)
        await handle_edit_image_intent(
            message=message,
            prompt=ctx.prompt,
            image_urls=ctx.image_urls,
            prompt_for_image_selection=ctx.prompt_for_image_selection,
            live_status_with_progress=ctx.live_status_with_progress,
            use_gemini=use_gemini,
        )
        return True

    if ctx.intent == "summarize_url" and ctx.general_url_match and not ctx.image_urls:
        await handle_summarize_url_intent(
            message=message,
            url=ctx.general_url_match.group(0),
            duration_estimate=duration_estimate,
            stream_ok=ctx.stream_ok,
            live_status_with_progress=ctx.live_status_with_progress,
            send_or_edit_with_truncation=ctx.send_or_edit_with_truncation,
        )
        return True

    if ctx.intent == "describe_image" and ctx.image_urls:
        await handle_describe_image_intent(
            message=message,
            prompt=ctx.prompt,
            image_urls=ctx.image_urls,
            ref_msg=ctx.ref_msg,
            is_reply_to_bot=ctx.is_reply_to_bot,
            duration_estimate=duration_estimate,
            stream_ok=ctx.stream_ok,
            live_status_with_progress=ctx.live_status_with_progress,
            send_or_edit_with_truncation=ctx.send_or_edit_with_truncation,
        )
        return True

    if ctx.intent == "generate_video":
        await handle_generate_video_intent(
            message=message,
            prompt=ctx.prompt,
            user_id=ctx.user_id,
            live_status_with_progress=ctx.live_status_with_progress,
            stream_ok=ctx.stream_ok,
            image_urls=ctx.image_urls,
            ref_msg=ctx.ref_msg,
        )
        return True

    if ctx.intent == "get_stock" and ctx.prompt.lower().startswith("stock"):
        async with message.channel.typing():
            await handle_stock_command(message, ctx.prompt)
        return True

    # 'clarify' runs as normal chat WITH full conversation history plus a
    # nudge: resolve the ambiguity from context if possible ("do that" right
    # after the bot offered something), otherwise ask one short question.
    # A blind clarifying question without history made the bot forget its own
    # previous offer.
    from providers.openai_client import OPENAI_LIGHT_MODEL as _light_model

    await handle_chat_intent(
        message=message,
        prompt=ctx.prompt,
        raw_prompt=ctx.raw_prompt,
        user_id=ctx.user_id,
        ref_msg=ctx.ref_msg,
        is_reply_to_bot=ctx.is_reply_to_bot,
        image_urls=ctx.image_urls,
        gemini_parts=ctx.gemini_parts,
        duration_estimate=duration_estimate,
        stream_ok=ctx.stream_ok,
        live_status_with_progress=ctx.live_status_with_progress,
        send_or_edit_with_truncation=ctx.send_or_edit_with_truncation,
        moderation_view_factory=ctx.moderation_view_factory,
        default_model=_light_model if ctx.intent == "chat_light" else None,
        clarify_hint=(ctx.intent == "clarify"),
    )
    return True
