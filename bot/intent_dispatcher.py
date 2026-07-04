from __future__ import annotations

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

_VIDEO_KEYWORDS = ("video", "movie", "clip", "animate")
_EDIT_KEYWORDS = (
    "edit", "change", "make", "turn", "transform",
    "fix", "remove", "add", "replace", "redraw",
)
_IMAGE_GEN_VERBS = ("imagine", "generate", "create", "draw", "paint", "make")
_IMAGE_NOUNS = (
    "image", "picture", "photo", "pic", "art", "artwork",
    "drawing", "portrait", "wallpaper", "logo",
)


def resolve_keyword_intent(raw_prompt: str, prompt: str, has_attachments: bool) -> Optional[str]:
    """Route explicit provider prefixes ("claude ...", "gemini ...") and other
    unambiguous keyword patterns to an intent.

    Returns None when nothing matches, meaning the LLM classifier should decide.
    """
    lowered_raw = raw_prompt.lower().strip()
    lowered_prompt = prompt.lower().strip()

    # "gemini imagine" would otherwise be grabbed by the chat intent.
    # The "gemini" prefix is kept in the prompt because stability_utils
    # checks content.startswith("gemini") to pick the backend.
    if lowered_raw.startswith("gemini imagine"):
        return "generate_image"

    if lowered_prompt.startswith("claude") or lowered_raw.startswith("claude"):
        return "claude_chat"

    if lowered_prompt.startswith("gemini") or lowered_raw.startswith("gemini"):
        # "gemini make this a video" must route to video, not image editing or chat.
        if any(k in lowered_prompt for k in _VIDEO_KEYWORDS):
            return "generate_video"
        # "gemini edit this image..." must route to image editing, not chat.
        if has_attachments and any(k in lowered_prompt for k in _EDIT_KEYWORDS):
            return "edit_image"
        # "gemini generate an image of X" / "gemini draw a picture of Y" must
        # route to image generation, not text chat (which apologizes that it
        # can't render images).
        if any(v in lowered_prompt for v in _IMAGE_GEN_VERBS) and any(
            n in lowered_prompt for n in _IMAGE_NOUNS
        ):
            return "generate_image"
        return "gemini_chat"

    if "generate" in lowered_prompt and any(
        k in lowered_prompt for k in ("video", "movie", "clip", "sora")
    ):
        return "generate_video"

    return None


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
        )
        return True

    if ctx.intent == "edit_image" and ctx.image_urls:
        use_gemini = (
            ctx.raw_prompt.lower().strip().startswith("gemini")
            or ctx.prompt.lower().strip().startswith("gemini")
        )
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
