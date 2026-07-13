# discord_bot.py
# Discord bot triggered by mentions/replies: classifies intent, then routes to
# chat (ES-backed per-user context), search, weather/stock, URL summarize,
# image/video generation and editing. Replies use a live progress bar and
# expand/collapse UI.

from __future__ import annotations

import os
import re
import io
import logging
import mimetypes
import asyncio
import contextlib
import collections
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from datetime import datetime, timezone

import discord
from discord.ext import commands

# IMPORTANT: importing config mirrors GCE metadata → os.environ for all keys
# Importing GOOGLE_* here ensures that side-effect runs even if this file
# doesn't directly use the variables. (We also log simple configured yes/no checks.)
from config import DISCORD_TOKEN, GOOGLE_API_KEY, GOOGLE_CSE_ID

# Memory / Elasticsearch helpers
from services.memory_utils import (
    index_message,                  # (message_id, guild_id, channel_id, user_id, role, content, timestamp?, reply_to_id?)
)

# OpenAI helpers
from providers.openai_utils import (
    classify_intent,
    image_url_to_base64,
)


# Optional features (your existing utilities)
from services.weather_utils import get_location_details, get_weather_data
from services.database_utils import (
    activate_behavior_change,
    get_message_expansion,
    get_channel_last_seen,
    set_channel_last_seen,
    get_user_seen,
    set_user_seen,
    list_user_facts,
    delete_user_facts_matching,
    get_user_profile,
    get_behavior_change,
    list_behavior_changes,
    propose_behavior_change,
    rollback_behavior_change,
    create_code_proposal,
    get_code_proposal,
    get_any_code_proposal,
    get_code_deployment,
    list_code_proposals,
    list_all_code_proposals,
    review_code_proposal,
    review_any_code_proposal,
    set_code_proposal_patch,
    set_code_proposal_validation,
    request_code_rollback,
    request_any_code_rollback,
)
from services.code_changes import MAX_PATCH_BYTES, get_baseline_sha, validate_patch
from services.code_generator import generate_code_patch
from services import usage_costs
from services.user_profile import maybe_refresh_profile
from providers.claude_utils import ANTHROPIC_API_KEY
from bot.intent_dispatcher import DispatchContext, dispatch_intent, resolve_keyword_intent
from bot.message_inputs import (
    collect_gemini_parts,
    collect_image_inputs,
    extract_search_query,
    has_google_search,
    has_visual_inputs,
    looks_like_search,
    resolve_reference_message,
    strip_mention_and_trigger,
)
from bot.moderation_view import ModerationFallbackView
from bot.ui_messages import (
    EXPAND_EMOJI,
    COLLAPSE_EMOJI,
    handle_expansion_reaction,
    send_or_edit_with_truncation as ui_send_or_edit_with_truncation,
    live_status_with_progress as ui_live_status_with_progress,
)

# NEW: direct search fast-path (kept, but now properly gated)
try:
    from services.search_utils import web_search
except Exception:
    web_search = None

# Streaming niceties (optional)
try:
    from services.stream_utils import ThrottledEditor
    STREAM_OK = True
except Exception:
    STREAM_OK = False

# ---- Logging ----
# Configured centrally by services.logging_config (called from main.py).
logger = logging.getLogger("discord_bot")


def _configured(value: Optional[str]) -> str:
    return "yes" if bool(value) else "no"


logger.info(
    "Google CSE configured: GOOGLE_API_KEY=%s, GOOGLE_CSE_ID=%s",
    _configured(GOOGLE_API_KEY),
    _configured(GOOGLE_CSE_ID),
)
logger.info("Anthropic configured: ANTHROPIC_API_KEY=%s", _configured(ANTHROPIC_API_KEY))

# ---- Discord ----
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ---- Backfill high-water marks (per guild:channel), stored in SQLite ----
def _state_key(guild_id: int | None, channel_id: int) -> str:
    g = str(guild_id) if guild_id else "DM"
    return f"{g}:{channel_id}"

# ---- Multi-Image Selection ----
# Track messages recently processed to prevent gateway replays / feedback loops
_processed_msg_ids: "collections.OrderedDict[int, None]" = collections.OrderedDict()
_PROCESSED_CACHE_SIZE = 1000

def _already_processed(message_id: int) -> bool:
    """Record message_id; return True if it was already seen recently."""
    if message_id in _processed_msg_ids:
        return True
    _processed_msg_ids[message_id] = None
    while len(_processed_msg_ids) > _PROCESSED_CACHE_SIZE:
        _processed_msg_ids.popitem(last=False)
    return False

# Track users currently being prompted (to prevent on_message from double-processing)
_pending_image_selection: set[int] = set()  # user IDs awaiting reply

# Track message IDs currently being expanded to prevent race conditions
_expansion_locks: set[int] = set()
# Track preflight status messages so progress can start immediately.
_preflight_status_by_message_id: dict[int, discord.Message] = {}


def _preflight_bar(step: int, total: int = 3, width: int = 10) -> str:
    total = max(1, total)
    step = max(0, min(step, total))
    filled = int((step / total) * width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"

async def prompt_for_image_selection(message, image_count: int, timeout: float = 30.0):
    """
    Ask user which image to process when multiple are present.
    Returns: int (0-based index), "all", or 0 on timeout.

    Only messages that look like a selection (a number or "all") are consumed;
    anything else the user says in the meantime is left for normal handling.
    """
    user_id = message.author.id
    _pending_image_selection.add(user_id)

    try:
        prompt_msg = await message.reply(
            f"📷 I see **{image_count} images**. Which one should I edit?\n"
            "Reply with a number (1, 2, ...) or **all**."
        )

        def check(m):
            if m.author.id != user_id or m.channel != message.channel:
                return False
            text = m.content.strip().lower()
            return text == "all" or text.isdigit()

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                reply = await bot.wait_for("message", check=check, timeout=remaining)
            except asyncio.TimeoutError:
                await prompt_msg.edit(content="⏰ Timed out. Using the first image.")
                return 0
            text = reply.content.strip().lower()
            if text == "all":
                return "all"
            idx = int(text) - 1
            if 0 <= idx < image_count:
                return idx
            await message.channel.send(
                f"⚠️ Pick a number between 1 and {image_count}, or **all**."
            )
    except asyncio.TimeoutError:
        return 0
    finally:
        _pending_image_selection.discard(user_id)

# --------------------------
# Helpers
# --------------------------

def is_probably_image(url: str) -> bool:
    path = urlparse(url).path
    mime, _ = mimetypes.guess_type(path)
    return bool(mime and mime.startswith("image/"))

async def _index_message_async(**kwargs) -> Optional[str]:
    """Run the synchronous OpenSearch indexing call off the event loop."""
    return await asyncio.to_thread(index_message, **kwargs)


async def _auto_index_bot_message(sent_message, full_text: str, *, original_message=None, reply_to=None, model: Optional[str] = None):
    try:
        src_msg = original_message or reply_to
        if src_msg:
            await _index_message_async(
                message_id=str(sent_message.id),
                guild_id=str(src_msg.guild.id) if src_msg.guild else "DM",
                channel_id=str(src_msg.channel.id),
                user_id=str(src_msg.author.id),
                role="assistant",
                content=full_text,
                timestamp=_now_iso(),
                reply_to_id=str(src_msg.id),
                model=model or "unknown",
            )
    except Exception as e:
        logger.warning(f"Failed to auto-index bot message: {e}")


async def send_or_edit_with_truncation(*args, **kwargs):
    kwargs.setdefault("index_callback", _auto_index_bot_message)
    return await ui_send_or_edit_with_truncation(*args, **kwargs)


async def live_status_with_progress(*args, **kwargs):
    base_message = args[0] if args else kwargs.get("message")
    if isinstance(base_message, discord.Message):
        explicit_status_msg = kwargs.get("existing_status_msg")
        existing = _preflight_status_by_message_id.pop(base_message.id, None)
        if existing is not None:
            if explicit_status_msg is not None:
                with contextlib.suppress(Exception):
                    await existing.delete()
            else:
                kwargs.setdefault("existing_status_msg", existing)
    kwargs.setdefault("stream_ok", STREAM_OK)
    if STREAM_OK:
        kwargs.setdefault("editor_factory", lambda status_msg: ThrottledEditor(status_msg, min_interval_s=1.5, max_len=1300))
    return await ui_live_status_with_progress(*args, **kwargs)

# --------------------------
# RECENT backfill (new behavior)
# --------------------------

async def backfill_recent_channel_history_to_es(
    guild_id: int | None,
    channel_id: int,
    chunk: int = 200,
) -> int:
    """
    Fetch ONLY recent messages:
      - Uses a channel-scoped 'last_seen_id' (stored in SQLite) as a high-water mark.
      - If first run (no last_seen), grab the latest <chunk> messages.
      - On subsequent runs, fetch messages strictly AFTER last_seen (i.e., newer).
    Indexes each message by snowflake id; duplicates are naturally ignored by ES.
    Returns the number of messages we attempted to index.
    """
    # Resolve channel
    guild = bot.get_guild(int(guild_id)) if guild_id else None
    channel = None
    if guild:
        channel = guild.get_channel(int(channel_id))
    if channel is None:
        channel = bot.get_channel(int(channel_id))
    if channel is None:
        raise RuntimeError(f"Channel {channel_id} not found")

    # Read the high-water mark
    key = _state_key(guild_id, channel_id)
    last_seen_id = await asyncio.to_thread(get_channel_last_seen, key)

    # Build history kwargs to fetch RECENT, not last
    kwargs: Dict[str, Any] = dict(limit=int(chunk), oldest_first=False)
    if last_seen_id:
        kwargs["after"] = discord.Object(id=int(last_seen_id))

    indexed = 0
    max_id_seen = int(last_seen_id) if last_seen_id else 0

    async for msg in channel.history(**kwargs):
        content = (msg.content or "").strip()
        if not content and not msg.reference:
            continue

        role = "assistant" if bot.user and msg.author.id == bot.user.id else "user"
        reply_to = str(msg.reference.message_id) if msg.reference else None

        try:
            await _index_message_async(
                message_id=str(msg.id),
                guild_id=str(msg.guild.id) if msg.guild else "DM",
                channel_id=str(channel_id),
                user_id=str(msg.author.id),
                role=role,
                content=content,
                timestamp=msg.created_at.isoformat(),
                reply_to_id=reply_to,
            )
            indexed += 1
            if msg.id > max_id_seen:
                max_id_seen = msg.id
        except Exception:
            logger.exception("Recent backfill index error")
            continue

    if max_id_seen and max_id_seen != (int(last_seen_id) if last_seen_id else 0):
        await asyncio.to_thread(set_channel_last_seen, key, str(max_id_seen))

    return indexed

# --------------------------
# Events
# --------------------------

_app_commands_synced = False


@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Graphs Go BRRR 📈")
    )
    logger.info("Bot is online and ready!")

    # Register slash (application) commands with Discord. Global sync; if the
    # commands don't appear, the bot needs re-inviting with the
    # 'applications.commands' OAuth scope.
    global _app_commands_synced
    if not _app_commands_synced:
        try:
            synced = await bot.tree.sync()
            _app_commands_synced = True
            logger.info("Synced %d application commands: %s", len(synced), [c.name for c in synced])
        except Exception:
            logger.exception("Application command sync failed")

@bot.event
async def on_raw_reaction_add(payload):
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    # Ignore ANY bot reaction (if member is available, e.g. in guild)
    if payload.member and payload.member.bot:
        return
    
    # If member not in payload (DM? or uncached), try to fetch user
    if not payload.member:
        try:
            u = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
            if u.bot:
                return
        except Exception:
            # Can't tell if it's a bot; process anyway rather than drop the reaction.
            logger.warning("Could not resolve reacting user %s; processing anyway", payload.user_id)

    # Check if this message is a truncatable message (fast DB check)
    rec = await asyncio.to_thread(get_message_expansion, payload.message_id)
    if not rec:
        return

    # Get channel and message
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    except Exception as e:
        logger.error(f"Failed to fetch message for expansion: {e}")
        return

    # Ensure we only care about reactions to the bot's messages
    if msg.author.id != bot.user.id:
        return

    emoji = str(payload.emoji)

    user = bot.get_user(payload.user_id) 
    # If user is None (not in cache), we can't easily pass 'user' to remove_reaction 
    # unless we fetch member. But remove_reaction accepts Member or User.
    # We can use payload.member if in guild.
    member = payload.member or user

    # Expansion lock check
    if payload.message_id in _expansion_locks:
        return
    _expansion_locks.add(payload.message_id)

    try:
        await handle_expansion_reaction(msg, emoji, rec, member=member)
    finally:
        _expansion_locks.discard(payload.message_id)

@bot.event
async def on_command_error(ctx, error):
    """Surface command failures to the user instead of hanging a deferred
    interaction on silent 'thinking'."""
    root = getattr(error, "original", None) or error.__cause__ or error
    logger.error(
        "Command %s failed: %r",
        getattr(ctx.command, "name", "?"),
        root,
        exc_info=root,
    )
    with contextlib.suppress(Exception):
        await ctx.reply(f"❌ `{getattr(ctx.command, 'name', '?')}` failed: {type(root).__name__}: {str(root)[:150]}")


# --------------------------
# Commands
# --------------------------

@bot.hybrid_command(name="ping", description="Check that the bot is alive.")
async def ping(ctx):
    await ctx.reply("pong")


def _behavior_change_summary(change: dict) -> str:
    instruction = change.get("instruction") or "_(baseline behavior; no custom instruction)_"
    if len(instruction) > 900:
        instruction = instruction[:897] + "..."
    parent = f" · parent `#{change['parent_id']}`" if change.get("parent_id") else ""
    return (
        f"**Behavior change `#{change['id']}`** · `{change['status']}`{parent}\n"
        f"> {instruction}"
    )


@bot.hybrid_command(name="behavior_propose", description="Draft a personal behavior change without activating it.")
async def behavior_propose(ctx, *, instruction: str):
    """Create an auditable draft. Use --clear to propose returning to baseline."""
    instruction = instruction.strip()
    if instruction.lower() == "--clear":
        instruction = ""
    change_id = await asyncio.to_thread(
        propose_behavior_change,
        str(ctx.author.id),
        instruction,
        created_by=str(ctx.author.id),
    )
    change = await asyncio.to_thread(get_behavior_change, str(ctx.author.id), change_id)
    await ctx.reply(
        f"🧪 Drafted, but **not active**.\n{_behavior_change_summary(change)}\n"
        f"Activate with `/behavior_activate {change_id}` or leave it as a draft."
    )


@bot.hybrid_command(name="behavior_show", description="Show one of your proposed behavior changes.")
async def behavior_show(ctx, change_id: int):
    change = await asyncio.to_thread(get_behavior_change, str(ctx.author.id), change_id)
    if not change:
        await ctx.reply("That behavior change does not exist in your personal history.")
        return
    await ctx.reply(_behavior_change_summary(change))


@bot.hybrid_command(name="behavior_history", description="List your recent behavior changes and their states.")
async def behavior_history(ctx, limit: int = 10):
    changes = await asyncio.to_thread(list_behavior_changes, str(ctx.author.id), limit)
    if not changes:
        await ctx.reply("You have no versioned behavior changes yet.")
        return
    lines = ["🧾 **Your behavior-change history**"]
    for change in changes:
        preview = (change.get("instruction") or "baseline behavior").replace("\n", " ")
        if len(preview) > 90:
            preview = preview[:87] + "..."
        lines.append(f"`#{change['id']}` **{change['status']}** — {preview}")
    await ctx.reply("\n".join(lines)[:1990])


@bot.hybrid_command(name="behavior_activate", description="Activate one of your drafted behavior changes.")
async def behavior_activate(ctx, change_id: int):
    try:
        change = await asyncio.to_thread(
            activate_behavior_change, str(ctx.author.id), change_id
        )
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    await ctx.reply(f"✅ Activated immediately.\n{_behavior_change_summary(change)}")


@bot.hybrid_command(name="behavior_rollback", description="Roll back your active behavior change to its parent.")
async def behavior_rollback(ctx):
    restored = await asyncio.to_thread(rollback_behavior_change, str(ctx.author.id))
    if restored:
        await ctx.reply(f"↩️ Rolled back and restored:\n{_behavior_change_summary(restored)}")
    else:
        await ctx.reply("↩️ Rolled back to baseline behavior. No custom instruction is active.")


def _code_proposal_summary(proposal: dict) -> str:
    request = proposal["request"].replace("\n", " ")
    if len(request) > 600:
        request = request[:597] + "..."
    validation = proposal.get("validation")
    checks = "not run"
    if validation:
        checks = "passed" if validation.get("ok") else f"failed ({len(validation.get('errors', []))} errors)"
    return (
        f"**Code proposal `#{proposal['id']}`** · `{proposal['status']}`\n"
        f"Baseline: `{proposal['baseline_sha'][:12]}` · validation: **{checks}**\n"
        f"> {request}"
    )


def _proposal_id_from_text(text: str) -> int | None:
    match = re.search(r"(?:proposal\s*)?#?(\d+)\b", text or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


async def _natural_code_proposal(message, intent: str, prompt: str, status_msg=None, ref_msg=None) -> None:
    """Conversational front-end for the audited code-change pipeline."""
    is_app_owner = await bot.is_owner(message.author)
    requester_id = str(message.author.id)
    proposal_id = _proposal_id_from_text(prompt)
    if proposal_id is None and ref_msg is not None:
        proposal_id = _proposal_id_from_text(getattr(ref_msg, "content", ""))
    proposals = await asyncio.to_thread(list_code_proposals, requester_id, 30)
    review_pool = (
        await asyncio.to_thread(list_all_code_proposals, 50)
        if is_app_owner else proposals
    )

    def latest_with_status(*statuses):
        return next((p for p in review_pool if p["status"] in statuses), None)

    if intent == "code_change":
        request = re.sub(r"^/code_propose\s*", "", prompt or "", flags=re.IGNORECASE).strip()
        if not request:
            await message.reply("Tell me what you want changed in my code.")
            return
        open_proposal = next(
            (p for p in proposals if p["status"] in {"draft", "patch_uploaded", "reviewable"}),
            None,
        )
        if open_proposal:
            await message.reply(
                f"You already have open proposal `#{open_proposal['id']}`. Wait for review before proposing another change."
            )
            return
        baseline = await asyncio.to_thread(get_baseline_sha)
        proposal_id = await asyncio.to_thread(
            create_code_proposal, requester_id, request, baseline
        )
        if status_msg is not None:
            with contextlib.suppress(Exception):
                await status_msg.edit(content=f"[🧩 Proposal #{proposal_id}] Selecting relevant source and generating a patch…")
        try:
            patch, generation = await generate_code_patch(request, baseline)
            await asyncio.to_thread(set_code_proposal_patch, requester_id, proposal_id, patch)
            report = await asyncio.to_thread(validate_patch, baseline, patch)
            report["generation"] = generation
            proposal = await asyncio.to_thread(
                set_code_proposal_validation, requester_id, proposal_id, report
            )
        except (RuntimeError, ValueError) as exc:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    review_any_code_proposal,
                    proposal_id,
                    "rejected",
                    reviewer_id="generation-failure",
                )
            await message.reply(
                f"I recorded proposal `#{proposal_id}`, but patch generation failed: {str(exc)[:1200]}"
            )
            return
        payload = io.BytesIO(patch.encode("utf-8"))
        if not report["ok"]:
            errors = "; ".join(report.get("errors") or ["Unknown validation error"])
            await message.reply(
                f"⚠️ I recorded proposal `#{proposal_id}`, but it is **not ready for review** because validation failed.\n"
                f"{_code_proposal_summary(proposal)}\n"
                f"Reason: {errors[:1200]}\n"
                "You can submit a corrected proposal.",
                file=discord.File(payload, filename=f"proposal-{proposal_id}-failed.diff"),
            )
            return
        await message.reply(
            f"🧩 **Proposal `#{proposal_id}` is ready for review.**\n"
            f"{_code_proposal_summary(proposal)}\n"
            f"Files: {', '.join(report['files'])}\n"
            f"The bot owner can reply naturally with **approve this change** or **reject it**. You can ask **what is its status?**",
            file=discord.File(payload, filename=f"proposal-{proposal_id}.diff"),
        )
        return

    if proposal_id is None:
        if intent == "code_approve":
            selected = latest_with_status("reviewable")
        elif intent == "code_reject":
            selected = latest_with_status("draft", "patch_uploaded", "validation_failed", "reviewable")
        else:
            selected = review_pool[0] if review_pool else None
        proposal_id = selected["id"] if selected else None
    if proposal_id is None:
        await message.reply("I couldn't find one of your code proposals to act on.")
        return

    if intent in {"code_approve", "code_reject", "code_rollback"} and not is_app_owner:
        await message.reply("Anyone can propose a code change, but only my owner can approve, reject, deploy, or roll one back.")
        return

    if intent == "code_approve":
        try:
            proposal = await asyncio.to_thread(
                review_any_code_proposal,
                proposal_id,
                "approved",
                reviewer_id=requester_id,
            )
        except ValueError as exc:
            await message.reply(f"I couldn't approve proposal `#{proposal_id}`: {exc}")
            return
        await message.reply(
            f"✅ Approved proposal `#{proposal_id}`. The separate host supervisor will test and deploy it; I'll DM you the result.\n"
            f"{_code_proposal_summary(proposal)}"
        )
        return

    if intent == "code_reject":
        try:
            proposal = await asyncio.to_thread(
                review_any_code_proposal,
                proposal_id,
                "rejected",
                reviewer_id=requester_id,
            )
        except ValueError as exc:
            await message.reply(f"I couldn't reject proposal `#{proposal_id}`: {exc}")
            return
        await message.reply(f"🛑 Rejected proposal `#{proposal_id}`. No code was deployed.")
        return

    if intent == "code_rollback":
        if _proposal_id_from_text(prompt) is None:
            active = None
            for candidate in review_pool:
                deployment = await asyncio.to_thread(
                    get_code_deployment, candidate["owner_id"], candidate["id"]
                )
                if deployment and deployment["status"] == "active":
                    active = candidate
                    break
            proposal_id = active["id"] if active else None
        if proposal_id is None:
            await message.reply("You don't currently have an active code release to roll back.")
            return
        try:
            request_id = await asyncio.to_thread(
                request_any_code_rollback, requester_id, proposal_id
            )
        except ValueError as exc:
            await message.reply(f"I couldn't queue that rollback: {exc}")
            return
        await message.reply(
            f"↩️ Rollback request `#{request_id}` queued for proposal `#{proposal_id}`. I'll DM you when the previous release is healthy."
        )
        return

    proposal = (
        await asyncio.to_thread(get_any_code_proposal, proposal_id)
        if is_app_owner
        else await asyncio.to_thread(get_code_proposal, requester_id, proposal_id)
    )
    if not proposal:
        await message.reply(f"I couldn't find proposal `#{proposal_id}`.")
        return
    deployment = await asyncio.to_thread(
        get_code_deployment, proposal["owner_id"], proposal_id
    )
    if not deployment:
        await message.reply(
            f"{_code_proposal_summary(proposal)}\nIt has no deployment record yet."
        )
        return
    await message.reply(
        f"{_code_proposal_summary(proposal)}\n"
        f"Deployment: **{deployment['status']}** — {(deployment.get('detail') or 'in progress')[:900]}"
    )


@bot.hybrid_command(name="code_propose", description="Owner: create a reviewed code-change request.", with_app_command=False)
@commands.is_owner()
async def code_propose(ctx, *, request: str):
    baseline = await asyncio.to_thread(get_baseline_sha)
    proposal_id = await asyncio.to_thread(
        create_code_proposal, str(ctx.author.id), request, baseline
    )
    proposal = await asyncio.to_thread(get_code_proposal, str(ctx.author.id), proposal_id)
    await ctx.reply(
        f"🧩 Created against the current committed baseline.\n{_code_proposal_summary(proposal)}\n"
        "Attach a unified `.diff` or `.patch` with `/code_patch`."
    )


@bot.hybrid_command(name="code_patch", description="Owner: attach a unified diff to a code proposal.", with_app_command=False)
@commands.is_owner()
async def code_patch(ctx, proposal_id: int, patch_file: discord.Attachment):
    if patch_file.size > MAX_PATCH_BYTES:
        await ctx.reply(f"❌ Patch exceeds the {MAX_PATCH_BYTES:,}-byte limit.")
        return
    if not patch_file.filename.lower().endswith((".diff", ".patch")):
        await ctx.reply("❌ Attach a `.diff` or `.patch` file.")
        return
    try:
        patch = (await patch_file.read()).decode("utf-8")
        proposal = await asyncio.to_thread(
            set_code_proposal_patch, str(ctx.author.id), proposal_id, patch
        )
    except UnicodeDecodeError:
        await ctx.reply("❌ Patch must be UTF-8 text.")
        return
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    await ctx.reply(
        f"📎 Patch attached ({len(patch.encode('utf-8')):,} bytes).\n"
        f"{_code_proposal_summary(proposal)}\nRun `/code_validate {proposal_id}` next."
    )


@bot.hybrid_command(name="code_generate", description="Owner: generate and validate a patch for a proposal.", with_app_command=False)
@commands.is_owner()
async def code_generate(ctx, proposal_id: int):
    proposal = await asyncio.to_thread(get_code_proposal, str(ctx.author.id), proposal_id)
    if not proposal or proposal["status"] in {"approved", "rejected"}:
        await ctx.reply("❌ Editable code proposal not found.")
        return
    if ctx.interaction:
        await ctx.defer()
    try:
        patch, generation = await generate_code_patch(
            proposal["request"], proposal["baseline_sha"]
        )
        await asyncio.to_thread(
            set_code_proposal_patch, str(ctx.author.id), proposal_id, patch
        )
        report = await asyncio.to_thread(
            validate_patch, proposal["baseline_sha"], patch
        )
        report["generation"] = generation
        proposal = await asyncio.to_thread(
            set_code_proposal_validation, str(ctx.author.id), proposal_id, report
        )
    except (RuntimeError, ValueError) as exc:
        await ctx.reply(f"❌ Patch generation failed: {str(exc)[:1200]}")
        return
    payload = io.BytesIO(patch.encode("utf-8"))
    files = ", ".join(report["files"])
    if not report["ok"]:
        errors = "; ".join(report.get("errors") or ["Unknown validation error"])
        await ctx.reply(
            f"⚠️ Generated with `{generation['model']}`, but validation failed.\n"
            f"{_code_proposal_summary(proposal)}\nReason: {errors[:1200]}",
            file=discord.File(payload, filename=f"proposal-{proposal_id}-failed.diff"),
        )
        return
    await ctx.reply(
        f"🤖 Generated with `{generation['model']}` and statically validated.\n"
        f"{_code_proposal_summary(proposal)}\nFiles: {files}\n"
        f"Review the attached diff, then use `/code_approve {proposal_id}`.",
        file=discord.File(payload, filename=f"proposal-{proposal_id}-generated.diff"),
    )


@bot.hybrid_command(name="code_validate", description="Owner: statically validate a proposal in a temporary snapshot.", with_app_command=False)
@commands.is_owner()
async def code_validate(ctx, proposal_id: int):
    proposal = await asyncio.to_thread(get_code_proposal, str(ctx.author.id), proposal_id)
    if not proposal or not proposal.get("patch"):
        await ctx.reply("❌ Proposal not found or it has no attached patch.")
        return
    if ctx.interaction:
        await ctx.defer()
    report = await asyncio.to_thread(
        validate_patch, proposal["baseline_sha"], proposal["patch"]
    )
    proposal = await asyncio.to_thread(
        set_code_proposal_validation, str(ctx.author.id), proposal_id, report
    )
    lines = [_code_proposal_summary(proposal)]
    lines.append("Files: " + (", ".join(report["files"]) or "none"))
    if report["syntax_checked"]:
        lines.append("Python syntax: " + ", ".join(report["syntax_checked"]))
    lines.extend(f"❌ {error}" for error in report["errors"][:5])
    lines.extend(f"⚠️ {warning}" for warning in report["warnings"][:5])
    if report["ok"]:
        lines.append("✅ Static validation passed. This patch has **not** been deployed or executed.")
    await ctx.reply("\n".join(lines)[:1990])


@bot.hybrid_command(name="code_show", description="Owner: show a code proposal and its review state.", with_app_command=False)
@commands.is_owner()
async def code_show(ctx, proposal_id: int):
    proposal = await asyncio.to_thread(get_code_proposal, str(ctx.author.id), proposal_id)
    if not proposal:
        await ctx.reply("Code proposal not found.")
        return
    await ctx.reply(_code_proposal_summary(proposal))


@bot.hybrid_command(name="code_diff", description="Owner: download the patch attached to a code proposal.", with_app_command=False)
@commands.is_owner()
async def code_diff(ctx, proposal_id: int):
    proposal = await asyncio.to_thread(get_code_proposal, str(ctx.author.id), proposal_id)
    if not proposal or not proposal.get("patch"):
        await ctx.reply("Proposal not found or it has no attached patch.")
        return
    payload = io.BytesIO(proposal["patch"].encode("utf-8"))
    await ctx.reply(
        _code_proposal_summary(proposal),
        file=discord.File(payload, filename=f"proposal-{proposal_id}.diff"),
    )


@bot.hybrid_command(name="code_history", description="Owner: list recent code proposals.", with_app_command=False)
@commands.is_owner()
async def code_history(ctx, limit: int = 10):
    proposals = await asyncio.to_thread(list_code_proposals, str(ctx.author.id), limit)
    if not proposals:
        await ctx.reply("No code proposals have been recorded.")
        return
    lines = ["🧾 **Code proposal history**"]
    for proposal in proposals:
        preview = proposal["request"].replace("\n", " ")
        if len(preview) > 90:
            preview = preview[:87] + "..."
        lines.append(
            f"`#{proposal['id']}` **{proposal['status']}** · "
            f"`{proposal['baseline_sha'][:8]}` — {preview}"
        )
    await ctx.reply("\n".join(lines)[:1990])


@bot.hybrid_command(name="code_approve", description="Owner: approve a successfully validated code proposal.", with_app_command=False)
@commands.is_owner()
async def code_approve(ctx, proposal_id: int):
    try:
        proposal = await asyncio.to_thread(
            review_code_proposal,
            str(ctx.author.id),
            proposal_id,
            "approved",
            reviewer_id=str(ctx.author.id),
        )
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    await ctx.reply(
        f"✅ Review approval recorded. The patch is still **not deployed**.\n"
        f"{_code_proposal_summary(proposal)}"
    )


@bot.hybrid_command(name="code_reject", description="Owner: reject a code proposal without deploying it.", with_app_command=False)
@commands.is_owner()
async def code_reject(ctx, proposal_id: int):
    try:
        proposal = await asyncio.to_thread(
            review_code_proposal,
            str(ctx.author.id),
            proposal_id,
            "rejected",
            reviewer_id=str(ctx.author.id),
        )
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    await ctx.reply(f"🛑 Rejected.\n{_code_proposal_summary(proposal)}")


@bot.hybrid_command(name="code_deployment", description="Owner: show deployment and health-check results.", with_app_command=False)
@commands.is_owner()
async def code_deployment(ctx, proposal_id: int):
    deployment = await asyncio.to_thread(
        get_code_deployment, str(ctx.author.id), proposal_id
    )
    if not deployment:
        await ctx.reply("No deployment record exists for that proposal yet.")
        return
    detail = deployment.get("detail") or "No detail recorded."
    await ctx.reply(
        f"🚦 **Deployment `#{proposal_id}`** · `{deployment['status']}`\n"
        f"Release: `{deployment['release_path']}`\n"
        f"Patch: `{(deployment.get('patch_sha256') or '')[:12]}`\n"
        f"Detail: {detail[:900]}"
    )


@bot.hybrid_command(name="code_rollback", description="Owner: request rollback of the active code release.", with_app_command=False)
@commands.is_owner()
async def code_rollback(ctx, proposal_id: int):
    try:
        request_id = await asyncio.to_thread(
            request_code_rollback, str(ctx.author.id), proposal_id
        )
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    await ctx.reply(
        f"↩️ Rollback request `#{request_id}` queued. The host supervisor will process it within one minute."
    )

@bot.hybrid_command(name="memory_fetch_more", description="Index recent channel messages into long-term memory (mods only).")
@commands.has_permissions(manage_messages=True)
async def memory_fetch_more(ctx, chunk: int = 200):
    """
    Fetch RECENT messages for this channel only (not old history).
    Uses a per-channel 'last_seen_id' high-water mark; first run grabs latest <chunk>.
    """
    try:
        if ctx.interaction:
            await ctx.defer()
        count = await backfill_recent_channel_history_to_es(
            ctx.guild.id if ctx.guild else None, ctx.channel.id, chunk=chunk
        )
        await ctx.reply(f"Indexed ~{count} recent message(s) for this channel.")
    except Exception as e:
        await ctx.reply(f"❌ {e}")

@bot.hybrid_command(name="memories", description="See what the bot remembers about you.")
async def memories(ctx):
    """Show remembered facts, profile freshness, and indexed-history stats."""
    if ctx.interaction:
        await ctx.defer(ephemeral=True)
    uid = str(ctx.author.id)
    facts = await asyncio.to_thread(list_user_facts, uid, 25)
    profile = await asyncio.to_thread(get_user_profile, uid)

    lines = [f"🧠 **What I remember about {ctx.author.display_name}:**"]
    if facts:
        for f in reversed(facts):
            lines.append(f"`#{f['id']}` {f['fact']}")
    else:
        lines.append("_No saved facts yet — tell me things worth remembering._")

    from services.time_context import time_ago_str
    if profile and profile.get("updated_at"):
        lines.append(f"\n_Profile last distilled {time_ago_str(profile['updated_at'])} ago — see `/profile`._")

    # Long-term history stats straight from Elasticsearch.
    try:
        from services.user_profile import user_memory_stats
        stats = await asyncio.to_thread(user_memory_stats, uid)
        if stats and stats.get("indexed_messages"):
            since = f" since {stats['first_seen'][:10]}" if stats.get("first_seen") else ""
            lines.append(f"_{stats['indexed_messages']:,} of your messages indexed in long-term memory{since}._")
    except Exception:
        logger.warning("user_memory_stats failed", exc_info=True)

    lines.append("_Use `/forget <text>` to remove anything._")
    await ctx.reply("\n".join(lines)[:1990])


@bot.hybrid_command(name="profile", description="Show the bot's distilled profile of you.")
async def profile(ctx):
    if ctx.interaction:
        await ctx.defer(ephemeral=True)
    prof = await asyncio.to_thread(get_user_profile, str(ctx.author.id))
    if not prof or not (prof.get("profile") or "").strip():
        await ctx.reply("_No distilled profile yet — chat with me a bit more and one will form._")
        return
    from services.time_context import time_ago_str
    when = f" _(refreshed {time_ago_str(prof['updated_at'])} ago)_" if prof.get("updated_at") else ""
    await ctx.reply(f"📋 **My read on you**{when}:\n{prof['profile']}"[:1990])


@bot.hybrid_command(name="forget", description="Delete remembered facts matching some text.")
async def forget(ctx, *, text: str = ""):
    """Delete remembered facts matching the given text."""
    if not text.strip():
        await ctx.reply("Usage: `/forget <text to match>`")
        return
    deleted = await asyncio.to_thread(delete_user_facts_matching, str(ctx.author.id), text.strip())
    if deleted:
        await ctx.reply(f"🗑️ Forgot {deleted} fact{'s' if deleted != 1 else ''} matching “{text.strip()}”.")
    else:
        await ctx.reply(f"Nothing stored matches “{text.strip()}”.")


@bot.hybrid_command(name="usage", description="Show API usage and costs (yours + totals).")
async def usage(ctx):
    """Show API usage: yours today, plus totals (mods see per-user breakdown)."""
    if ctx.interaction:
        await ctx.defer()
    uid = str(ctx.author.id)
    mine = await asyncio.to_thread(usage_costs.today_for_user, uid)
    day = await asyncio.to_thread(usage_costs.today)
    month = await asyncio.to_thread(usage_costs.month_to_date)
    year = await asyncio.to_thread(usage_costs.year_to_date)
    total = await asyncio.to_thread(usage_costs.all_time)
    my_break = await asyncio.to_thread(usage_costs.today_breakdown, uid, 8)

    def _fmt_row(b):
        tokens = f", {b['total_tokens']:,} tok" if b["total_tokens"] else ""
        return f"  `{b['model']}` {b['label']}: {b['calls']}×{tokens}, ${b['cost']:.4f}"

    lines = [
        "📊 **API usage**",
        f"**You today:** {mine['calls']} calls, {mine['total_tokens']:,} tokens, ${mine['cost']:.4f}",
    ]
    lines.extend(_fmt_row(b) for b in my_break)
    lines.append(f"**Everyone today:** {day['calls']} calls, {day['total_tokens']:,} tokens, ${day['cost']:.4f}")

    perms = getattr(ctx.author, "guild_permissions", None)
    is_mod = bool(perms and perms.manage_messages)
    if is_mod:
        all_break = await asyncio.to_thread(usage_costs.today_breakdown, None, 8)
        lines.extend(_fmt_row(b) for b in all_break)

    lines.append(f"**Month to date:** {month['calls']} calls, {month['total_tokens']:,} tokens, ${month['cost']:.2f}")
    lines.append(f"**Year to date:** {year['calls']} calls, {year['total_tokens']:,} tokens, ${year['cost']:.2f}")
    lines.append(f"**All time:** {total['calls']} calls, {total['total_tokens']:,} tokens, ${total['cost']:.2f}")

    if is_mod:
        top = await asyncio.to_thread(usage_costs.top_users_today, 5)
        if top:
            lines.append("\n**Top spenders today:**")
            for t in top:
                lines.append(f"<@{t['user_id']}>: {t['calls']} calls, ${t['cost']:.4f}")

        top_month = await asyncio.to_thread(usage_costs.top_users_month, 5)
        if top_month:
            lines.append("\n**Top spenders this month:**")
            for t in top_month:
                lines.append(f"<@{t['user_id']}>: {t['calls']} calls, ${t['cost']:.4f}")
    await ctx.reply("\n".join(lines)[:1990])


# --------------------------
# Main message handler
# --------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    
    if _already_processed(message.id):
        return

    # Pass-through slash/! commands
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    # Trigger: direct mention or reply to bot
    raw_prompt = strip_mention_and_trigger(message.content, bot.user.id if bot.user else None)
    prompt = raw_prompt
    user_id = message.author.id

    # Only an EXPLICIT @bot mention in the message text counts as a trigger.
    # mentioned_in() is also true for role mentions (any @role the bot holds),
    # which made the bot jump into conversations nobody addressed to it.
    is_direct_mention = bool(bot.user) and (
        f"<@{bot.user.id}>" in (message.content or "")
        or f"<@!{bot.user.id}>" in (message.content or "")
    )
    ref_msg, is_reply_to_bot = await resolve_reference_message(message, bot.user)

    if not (is_direct_mention or is_reply_to_bot) or message.mention_everyone:
        return
    
    # Skip if user is currently responding to an image selection prompt
    if message.author.id in _pending_image_selection:
        return

    preflight_status = None
    try:
        preflight_status = await message.reply(f"[🧠 Preparing {_preflight_bar(0)}]")
    except Exception:
        try:
            preflight_status = await message.channel.send(f"[🧠 Preparing {_preflight_bar(0)}]")
        except Exception:
            preflight_status = None

    if preflight_status is not None:
        _preflight_status_by_message_id[message.id] = preflight_status

    # Pre-log the user's message (live indexing)
    try:
        await _index_message_async(
            message_id=str(message.id),
            guild_id=str(message.guild.id) if message.guild else "DM",
            channel_id=str(message.channel.id),
            user_id=str(message.author.id),
            role="user",
            content=message.content or "",
            timestamp=message.created_at.isoformat(),
            reply_to_id=(str(message.reference.message_id) if message.reference else None),
        )
    except Exception:
        logger.warning("Live indexing of user message %s failed", message.id, exc_info=True)
    finally:
        if preflight_status is not None:
            with contextlib.suppress(Exception):
                await preflight_status.edit(content=f"[🧠 Preparing {_preflight_bar(1)}]\nIndexing context…")

    # ---- Search: fast-path ONLY if fully configured; else fall through to tools ----
    if looks_like_search(prompt) and web_search is not None and has_google_search(GOOGLE_API_KEY, GOOGLE_CSE_ID, os.environ):
        if preflight_status is not None:
            with contextlib.suppress(Exception):
                await preflight_status.edit(content=f"[🧠 Preparing {_preflight_bar(2)}]\nRunning search…")
        q = extract_search_query(prompt)
        try:
            results = await asyncio.to_thread(web_search, q, max_results=5)
        except Exception:
            results = []
            logger.exception("web_search failed")
        if results:
            lines = ["**Top results:**"]
            for r in results:
                title = r.get("title") or "(untitled)"
                url = r.get("url") or ""
                snippet = (r.get("snippet") or "").strip()
                if snippet:
                    snippet = snippet[:300]
                lines.append(f"- [{title}]({url}) — {snippet}")
            await send_or_edit_with_truncation("\n".join(lines), target_msg=preflight_status, channel=message.channel, reply_to=message)
            _preflight_status_by_message_id.pop(message.id, None)
            return
        # If configured but the query returned nothing, say so (this path is “real”)
        await send_or_edit_with_truncation("No results found.", target_msg=preflight_status, channel=message.channel, reply_to=message)
        _preflight_status_by_message_id.pop(message.id, None)
        return
    # If not configured, we do NOT send “No results found” — we let the model’s web_search tool handle it.

    image_urls = await collect_image_inputs(message, ref_msg, image_url_to_base64)
    gemini_parts = await collect_gemini_parts(message, ref_msg, image_urls)
    has_attachments = has_visual_inputs(message, ref_msg) or bool(image_urls or gemini_parts)
    if preflight_status is not None:
        with contextlib.suppress(Exception):
            await preflight_status.edit(content=f"[🧠 Preparing {_preflight_bar(2)}]\nClassifying intent…")
    
    # Attribute all API spend during this message to the requesting user.
    usage_costs.set_request_context(
        user_id=str(user_id),
        guild_id=str(message.guild.id) if message.guild else "DM",
        channel_id=str(message.channel.id),
    )

    # Intent: explicit keyword routing first, LLM classifier otherwise.
    # The classifier sees the user's previous request so follow-ups like
    # "another one" route to the same intent.
    seen = None
    try:
        seen = await asyncio.to_thread(get_user_seen, str(user_id))
    except Exception:
        logger.warning("get_user_seen failed", exc_info=True)

    intent = resolve_keyword_intent(raw_prompt, prompt, has_attachments)
    if intent is None:
        # Give the classifier the last few real turns (user AND assistant) so
        # "do that" right after the bot offered something routes to chat, not
        # to a blind clarification.
        recent_turns = None
        try:
            from services.memory_utils import build_message_window

            window = await asyncio.to_thread(
                build_message_window,
                guild_id=message.guild.id if message.guild else "DM",
                channel_id=message.channel.id,
                user_id=user_id,
                limit_msgs=4,
            )
            recent_turns = [
                f"{m.get('role', 'user')}: {(m.get('content') or '')[:200]}"
                for m in (window or [])
                if (m.get("content") or "").strip()
            ] or None
        except Exception:
            logger.warning("classifier context window failed", exc_info=True)
        if not recent_turns and seen and seen.get("last_prompt"):
            recent_turns = [f"user: {seen['last_prompt']}"]

        intent = await classify_intent(
            prompt,
            has_images=has_attachments,
            recent_turns=recent_turns,
            prev_intent=seen.get("last_intent") if seen else None,
        )

    usage_costs.set_request_context(
        user_id=str(user_id),
        guild_id=str(message.guild.id) if message.guild else "DM",
        channel_id=str(message.channel.id),
        intent=intent,
    )

    logger.info(f"Intent identified as: {intent} (has_attachments={has_attachments}, image_inputs={len(image_urls)})")
    if preflight_status is not None:
        with contextlib.suppress(Exception):
            await preflight_status.edit(content=f"[🧠 Preparing {_preflight_bar(3)}]\nDispatching…")

    # Any URL (for summarize)
    general_url_match = re.search(r"https?://[^\s]+", message.content)

    try:
        if intent in {
            "code_change", "code_approve", "code_reject", "code_status", "code_rollback"
        }:
            await _natural_code_proposal(
                message, intent, prompt, preflight_status, ref_msg=ref_msg
            )
        else:
            await dispatch_intent(
                DispatchContext(
                intent=intent,
                message=message,
                prompt=prompt,
                raw_prompt=raw_prompt,
                user_id=user_id,
                bot_user=bot.user,
                ref_msg=ref_msg,
                is_reply_to_bot=is_reply_to_bot,
                image_urls=image_urls,
                gemini_parts=gemini_parts,
                general_url_match=general_url_match,
                stream_ok=STREAM_OK,
                get_location_details=get_location_details,
                get_weather_data=get_weather_data,
                live_status_with_progress=live_status_with_progress,
                send_or_edit_with_truncation=send_or_edit_with_truncation,
                prompt_for_image_selection=prompt_for_image_selection,
                moderation_view_factory=ModerationFallbackView,
                )
            )

    except Exception as e:
        logger.exception("Critical error in on_message dispatch")
        with contextlib.suppress(Exception):
            await message.reply(f"❌ Critical failure: {str(e)[:150]}...")
        _preflight_status_by_message_id.pop(message.id, None)

    leftover_status = _preflight_status_by_message_id.pop(message.id, None)
    if leftover_status is not None:
        with contextlib.suppress(Exception):
            await leftover_status.delete()

    # Track last interaction (time-passage awareness + intent continuity) and
    # opportunistically refresh the distilled user profile off the reply path.
    try:
        await asyncio.to_thread(set_user_seen, str(user_id), intent=intent, prompt=prompt)
    except Exception:
        logger.warning("set_user_seen failed", exc_info=True)
    asyncio.create_task(
        maybe_refresh_profile(
            guild_id=str(message.guild.id) if message.guild else "DM",
            channel_id=str(message.channel.id),
            user_id=str(user_id),
        )
    )

    # let other cogs/commands run too
    await bot.process_commands(message)

# ---- Entrypoint (used by main.py) ----
def run_bot():
    bot.run(DISCORD_TOKEN)
