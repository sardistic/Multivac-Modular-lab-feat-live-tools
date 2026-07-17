from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import re

import discord

from providers.claude_utils import CLAUDE_MODEL, generate_claude_response, image_input_to_block
from providers.gemini_utils import GeminiModerationError, generate_gemini_text
from providers.openai_utils import (
    OpenAIModerationError,
    TOOLS_DEF,
    generate_openai_messages_response,
    generate_openai_messages_response_with_tools,
)
from bot.response_policy import apply_personality_overrides, build_personality_system_message
from services.memory_utils import build_message_window
from services.url_utils import extract_main_text, fetch_url_content, reduce_text_length
from services.youtube_utils import extract_youtube_id, fetch_youtube_transcript

logger = logging.getLogger("discord_bot")


async def _youtube_transcript_for(text_with_url: str, max_chars: int = 9000) -> str | None:
    """If the text contains a YouTube URL, fetch the actual spoken transcript.
    Without this, summaries get built from the video description/metadata,
    which often covers 20 seconds of a 40-minute video.

    Long transcripts are map-reduce condensed through the cheap model tiers
    (so the whole video is represented, not just the first 9k chars) and the
    condensed form is cached per video id — one condensation cost per video,
    ever, no matter how many users ask about it."""
    vid = extract_youtube_id(text_with_url or "")
    if not vid:
        return None

    from services.database_utils import (
        get_cached_transcript_summary,
        set_cached_transcript_summary,
    )

    try:
        cached = await asyncio.to_thread(get_cached_transcript_summary, vid)
        if cached:
            return cached
    except Exception:
        logger.warning("transcript cache read failed", exc_info=True)

    try:
        transcript = await asyncio.to_thread(fetch_youtube_transcript, vid)
    except Exception:
        logger.warning("YouTube transcript fetch failed for %s", vid, exc_info=True)
        return None
    if not transcript:
        return None
    if len(transcript) <= max_chars:
        return transcript

    from services.condense import condense_long_text

    condensed = await condense_long_text(transcript, target_chars=max_chars)
    try:
        await asyncio.to_thread(set_cached_transcript_summary, vid, condensed)
    except Exception:
        logger.warning("transcript cache write failed", exc_info=True)
    return condensed


async def handle_claude_chat_intent(
    *,
    message,
    prompt: str,
    stream_ok: bool,
    live_status_with_progress,
    send_or_edit_with_truncation,
    image_urls=None,
    ref_msg=None,
):
    clean_prompt = re.sub(r"^(claude|hey claude)\s*", "", prompt, flags=re.IGNORECASE).strip()

    # When replying to another message, that message is usually the subject
    # ("claude fix why this happened" under a bot error). Quote it so Claude
    # sees what "this" is even if it has scrolled out of the history window.
    ref_content = (getattr(ref_msg, "content", None) or "").strip()
    if ref_content:
        ref_author = getattr(getattr(ref_msg, "author", None), "display_name", None) or "earlier message"
        clean_prompt = f'[Replying to {ref_author}: "{ref_content[:1500]}"]\n\n{clean_prompt}'

    personality_msg = build_personality_system_message(message.author.id, intent="claude_chat")
    context_msgs = build_message_window(
        guild_id=message.guild.id if message.guild else "DM",
        channel_id=message.channel.id,
        user_id=message.author.id,
        limit_msgs=20,
    )
    context_msgs = [
        m for m in context_msgs
        if not (m.get("role") == "user" and "gemini imagine" in m.get("content", "").lower())
    ]
    claude_messages = [{"role": "system", "content": "You are Claude, a helpful AI assistant."}]
    if personality_msg:
        claude_messages.append({"role": "system", "content": personality_msg})
    claude_messages.extend(context_msgs)

    # Attached/replied-to images become Anthropic image blocks so Claude can
    # actually see screenshots instead of asking for one that's already there.
    image_blocks = []
    for src in (image_urls or [])[:5]:
        block = image_input_to_block(src)
        if block:
            image_blocks.append(block)
    if image_blocks:
        user_content = image_blocks + ([{"type": "text", "text": clean_prompt}] if clean_prompt else [])
    else:
        user_content = clean_prompt
    claude_messages.append({"role": "user", "content": user_content})

    status_msg, response = await live_status_with_progress(
        message,
        action_label="Thinking (Claude)",
        emoji="🧠",
        coro=generate_claude_response(claude_messages),
        duration_estimate=5,
        summarizer=(lambda: "Queries Anthropic API...") if stream_ok else None,
    )

    if response:
        response = apply_personality_overrides(message.author.id, intent="claude_chat", text=response)
        await send_or_edit_with_truncation(
            response,
            target_msg=status_msg,
            original_message=message,
            model=CLAUDE_MODEL,
        )
    else:
        await status_msg.edit(content="❌ Claude returned no response.")


async def handle_gemini_chat_intent(
    *,
    message,
    prompt: str,
    gemini_parts,
    live_status_with_progress,
    send_or_edit_with_truncation,
    moderation_view_factory,
):
    clean_prompt = re.sub(r"^gemini\s*", "", prompt, flags=re.IGNORECASE).strip()
    is_test_mode = False
    if clean_prompt.lower() == "test" or clean_prompt.lower().startswith("test "):
        is_test_mode = True
        clean_prompt = re.sub(r"^test\s*", "", clean_prompt, flags=re.IGNORECASE).strip()

    enable_code_execution = False
    if clean_prompt.lower().startswith("code "):
        enable_code_execution = True
        clean_prompt = clean_prompt[5:].strip()
    elif is_test_mode:
        enable_code_execution = True

    context_msgs = []
    if not enable_code_execution:
        personality_msg = build_personality_system_message(message.author.id, intent="gemini_chat")
        context_msgs = build_message_window(
            guild_id=message.guild.id if message.guild else "DM",
            channel_id=message.channel.id,
            user_id=message.author.id,
            limit_msgs=20,
        )
        context_msgs = [
            m for m in context_msgs
            if not (m.get("role") == "user" and "gemini imagine" in m.get("content", "").lower())
        ]
        if personality_msg:
            context_msgs.insert(0, {"role": "system", "content": personality_msg})

    # Ground YouTube links in the real transcript, not the description.
    transcript = await _youtube_transcript_for(clean_prompt)
    if transcript:
        clean_prompt += (
            "\n\n[VIDEO TRANSCRIPT — this is the linked video's actual spoken content. "
            "Base your answer on THIS, not the title/description/thumbnail:]\n" + transcript
        )

    status_tracker = {"text": ""}

    def _live_code_summarizer():
        return status_tracker["text"] or "Using Gemini 1.5 Flash..."

    search_ids = {
        "guild_id": str(message.guild.id) if message.guild else "DM",
        "channel_id": str(message.channel.id),
        "user_id": str(message.author.id),
    }

    async def _do_gemini_generation(model_name=None):
        selected_model = model_name or "gemini-3-flash-preview"

        async def _run_gen():
            if "gpt" in selected_model.lower():
                ctx = {
                    "guild_id": message.guild.id if message.guild else "DM",
                    "channel_id": message.channel.id,
                    "user_id": str(message.author.id),
                }
                msgs = list(context_msgs)
                msgs.append({"role": "user", "content": clean_prompt})
                txt = await generate_openai_messages_response_with_tools(
                    msgs,
                    tools=TOOLS_DEF,
                    tool_context=ctx,
                    model=selected_model,
                )
                return txt, []

            return await asyncio.to_thread(
                generate_gemini_text,
                clean_prompt,
                context=context_msgs,
                extra_parts=gemini_parts,
                status_tracker=status_tracker,
                enable_code_execution=enable_code_execution,
                search_ids=search_ids,
                model_name=selected_model,
            )

        try:
            status_msg, response = await live_status_with_progress(
                message,
                action_label=f"Thinking ({selected_model})",
                emoji="✨",
                coro=_run_gen(),
                duration_estimate=6,
                summarizer=_live_code_summarizer,
            )

            if not response:
                await status_msg.edit(content="❌ Gemini returned no response.")
                return

            if isinstance(response, tuple):
                text_resp, artifacts = response
            else:
                text_resp, artifacts = response, []

            files_to_send = []
            if artifacts:
                for i, (data, mime) in enumerate(artifacts):
                    ext = mimetypes.guess_extension(mime) or ".bin"
                    if "wav" in mime:
                        ext = ".wav"
                    files_to_send.append(discord.File(io.BytesIO(data), filename=f"artifact_{i}{ext}"))

            if text_resp:
                text_resp = apply_personality_overrides(message.author.id, intent="gemini_chat", text=text_resp)
                if is_test_mode:
                    if len(text_resp) > 1900:
                        try:
                            text_file = discord.File(io.BytesIO(text_resp.encode("utf-8")), filename="response.md")
                            await status_msg.edit(content="⚠️ **Test Result Too Long** -> Sent as file `response.md`")
                            all_files = [text_file, *files_to_send]
                            await status_msg.reply(files=all_files)
                        except Exception as e:
                            await status_msg.edit(content=f"❌ Test mode file send failed: {e}")
                    else:
                        try:
                            await status_msg.edit(content=f"```\n{text_resp[:1990]}\n```")
                            if files_to_send:
                                await status_msg.reply(files=files_to_send)
                        except Exception as e:
                            await status_msg.edit(content=f"❌ Test mode failed: {e}")
                else:
                    await send_or_edit_with_truncation(text_resp, target_msg=status_msg, extra_files=files_to_send)
            elif files_to_send:
                await status_msg.reply(files=files_to_send)
            else:
                await status_msg.edit(content="❌ Gemini returned no text or files.")
        except GeminiModerationError as e:
            logger.warning("Gemini moderation hit: %s", e)
            user_msg = f"⚠️ **Response Blocked by Safety Filters** (Reason: {str(e)})\nSelect a different model to retry:"
            view = moderation_view_factory(author_id=message.author.id, retry_callback=_do_gemini_generation)
            await message.reply(user_msg, view=view)
        except OpenAIModerationError as e:
            logger.warning("OpenAI moderation hit in Gemini fallback: %s", e)
            user_msg = f"⚠️ **Response Blocked by Safety Filters** (Reason: {str(e)})\nSelect a different model to retry:"
            view = moderation_view_factory(author_id=message.author.id, retry_callback=_do_gemini_generation)
            await message.reply(user_msg, view=view)
        except Exception as e:
            logger.exception("Gemini generation error")
            await message.reply(f"❌ Gemini Error: {e}")

    await _do_gemini_generation()


async def handle_summarize_url_intent(
    *,
    message,
    url: str,
    duration_estimate: int,
    stream_ok: bool,
    live_status_with_progress,
    send_or_edit_with_truncation,
):
    async def _do_summarize():
        # YouTube: summarize the actual transcript, not the page metadata.
        transcript = await _youtube_transcript_for(url)
        if transcript:
            title = "YouTube video"
            condensed = reduce_text_length(transcript, max_chars=9000)
            source_note = "This is the video's spoken transcript."
        else:
            html = await asyncio.to_thread(fetch_url_content, url)
            title, text = extract_main_text(html)
            condensed = reduce_text_length(text, max_chars=3000)
            source_note = ""
        personality_msg = build_personality_system_message(message.author.id, intent="summarize_url")
        msgs = [
            {"role": "system", "content": "Summarize crisply (bullets ok) and extract key facts/figures. " + source_note},
            {"role": "user", "content": f"Title: {title or ''}\n\n{condensed}"},
        ]
        if personality_msg:
            msgs.insert(0, {"role": "system", "content": personality_msg})
        summary = await generate_openai_messages_response(msgs)
        summary = apply_personality_overrides(message.author.id, intent="summarize_url", text=summary)
        return f"**{title or 'Summary'}**\n{summary}"

    status_msg, summary = await live_status_with_progress(
        message,
        action_label="Summarizing",
        emoji="📰",
        coro=_do_summarize(),
        duration_estimate=duration_estimate,
        summarizer=(lambda: f"Fetching page…\nURL: {url}\nExtracting main content…") if stream_ok else None,
    )
    if summary:
        await send_or_edit_with_truncation(summary, target_msg=status_msg)
    else:
        await status_msg.edit(content="❌ Summary failed.")
