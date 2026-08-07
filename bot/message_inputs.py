from __future__ import annotations

import base64
import logging
import mimetypes
import re
from typing import Any, List
from urllib.parse import urlparse

from google.genai import types

logger = logging.getLogger("discord_bot")

URL_RE = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".avif", ".heic", ".heif")


def strip_mention_and_trigger(raw: str, bot_user_id: int | None) -> str:
    s = raw
    if bot_user_id:
        s = re.sub(f"<@!?{bot_user_id}>", "", s).strip()
    return s


async def resolve_reference_message(message, bot_user):
    is_reply_to_bot = False
    ref_msg = None
    if message.reference:
        try:
            ref_msg = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            ref_msg = None
        if ref_msg and bot_user and ref_msg.author.id == bot_user.id:
            is_reply_to_bot = True
    return ref_msg, is_reply_to_bot


def _forward_snapshots(message) -> List[Any]:
    """Forwarded messages carry their content/attachments/embeds in message
    snapshots, not on the message itself."""
    if message is None:
        return []
    snaps = getattr(message, "message_snapshots", None) or getattr(message, "snapshots", None)
    return list(snaps or [])


def _embed_image_candidates(embeds) -> List[str]:
    candidates: List[str] = []
    for embed in embeds or []:
        if getattr(embed, "image", None) and embed.image.url:
            candidates.append(embed.image.url)
        if getattr(embed, "thumbnail", None) and embed.thumbnail.url:
            candidates.append(embed.thumbnail.url)
    return candidates


def _clean_url_token(url: str) -> str:
    return (url or "").strip().rstrip(").,!?:;]}'\"")


def _extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    return [_clean_url_token(m.group(0)) for m in URL_RE.finditer(text)]


def _looks_like_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        if path.endswith(IMAGE_EXTS):
            return True
        if "cdn.discordapp.com/attachments/" in url.lower():
            return True
        if "media.discordapp.net/attachments/" in url.lower():
            return True
    except Exception:
        return False
    return False


def has_visual_inputs(message, ref_msg=None) -> bool:
    if message.attachments or (ref_msg and ref_msg.attachments):
        return True
    if _embed_image_candidates(getattr(message, "embeds", None)):
        return True
    if ref_msg and _embed_image_candidates(getattr(ref_msg, "embeds", None)):
        return True
    for snap in _forward_snapshots(message) + _forward_snapshots(ref_msg):
        if getattr(snap, "attachments", None):
            return True
        if _embed_image_candidates(getattr(snap, "embeds", None)):
            return True
    for url in _extract_urls_from_text(message.content):
        if _looks_like_image_url(url):
            return True
    if ref_msg:
        for url in _extract_urls_from_text(getattr(ref_msg, "content", "")):
            if _looks_like_image_url(url):
                return True
    return False


async def collect_image_inputs(
    message,
    ref_msg,
    image_url_to_base64,
    source_image_urls: List[str] | None = None,
) -> List[str]:
    image_urls: List[str] = []
    source_urls: List[str] = []

    async def append_downloaded(url: str) -> None:
        b64 = await image_url_to_base64(url)
        if b64:
            image_urls.append(b64)
            source_urls.append(url)

    def append_public(url: str) -> None:
        image_urls.append(url)
        source_urls.append(url)

    if ref_msg and ref_msg.attachments:
        for attachment in ref_msg.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                await append_downloaded(attachment.url)

    if ref_msg and ref_msg.embeds:
        for url in _embed_image_candidates(ref_msg.embeds):
            await append_downloaded(url)

    if message.attachments:
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                await append_downloaded(attachment.url)

    if message.embeds:
        for url in _embed_image_candidates(message.embeds):
            await append_downloaded(url)

    # Forwarded posts: images live in message snapshots.
    for snap in _forward_snapshots(message) + _forward_snapshots(ref_msg):
        for attachment in getattr(snap, "attachments", None) or []:
            ctype = getattr(attachment, "content_type", None)
            if ctype and ctype.startswith("image/"):
                await append_downloaded(attachment.url)
        for url in _embed_image_candidates(getattr(snap, "embeds", None)):
            await append_downloaded(url)

    text_candidates: List[str] = []
    if ref_msg:
        text_candidates.extend(_extract_urls_from_text(getattr(ref_msg, "content", "")))
    text_candidates.extend(_extract_urls_from_text(message.content))

    for raw_url in text_candidates:
        if not _looks_like_image_url(raw_url):
            continue
        lowered = raw_url.lower()
        if "cdn.discordapp.com" in lowered or "media.discordapp.net" in lowered:
            await append_downloaded(raw_url)
            continue
        append_public(raw_url)

    seen = set()
    unique_urls = []
    unique_sources = []
    for url, source_url in zip(image_urls, source_urls):
        if url not in seen:
            unique_urls.append(url)
            unique_sources.append(source_url)
            seen.add(url)
    if source_image_urls is not None:
        source_image_urls.extend(unique_sources)
    return unique_urls


async def collect_gemini_parts(message, ref_msg, image_urls) -> List[Any]:
    gemini_parts = []
    text_exts = (
        ".txt",
        ".md",
        ".py",
        ".js",
        ".ts",
        ".json",
        ".csv",
        ".c",
        ".cpp",
        ".h",
        ".java",
        ".go",
        ".rs",
        ".sql",
        ".yaml",
        ".yml",
        ".html",
        ".css",
    )

    async def _append_attachment_parts(attachment, label: str, prefix: str) -> None:
        try:
            data = await attachment.read()
            mime = attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"
            if mime.startswith("image/"):
                gemini_parts.append(types.Part.from_bytes(data=data, mime_type=mime))
                logger.info("Added %s image part: %s", label, attachment.filename)
                return
            if mime.startswith("text/") or "/json" in mime or attachment.filename.lower().endswith(text_exts):
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    content = data.decode("latin-1")
                if len(content) > 150_000:
                    content = content[:150_000] + "\n... [TRUNCATED] ..."
                gemini_parts.append(types.Part(text=f"--- {prefix}: {attachment.filename} ---\n{content}\n"))
                logger.info("Added %s text part: %s", label, attachment.filename)
        except Exception as e:
            logger.error("Failed to process %s attachment: %s", label, e)

    if ref_msg and ref_msg.attachments:
        for attachment in ref_msg.attachments:
            await _append_attachment_parts(attachment, "replied", "REPLIED FILE")

    if message.attachments:
        for attachment in message.attachments:
            await _append_attachment_parts(attachment, "current", "FILE")

    for url in image_urls:
        if not url.startswith("data:image/"):
            continue
        try:
            header, encoded = url.split(",", 1)
            mime = header.split(":", 1)[1].split(";", 1)[0]
            gemini_parts.append(types.Part.from_bytes(data=base64.b64decode(encoded), mime_type=mime))
        except Exception as e:
            logger.error("Failed to decode data URI: %s", e)

    return gemini_parts
