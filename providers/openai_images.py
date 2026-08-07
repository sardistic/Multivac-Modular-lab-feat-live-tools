from __future__ import annotations

import base64
import logging
import mimetypes
import re
from typing import List, Optional, Tuple

import aiohttp

DEFAULT_VISION_DETAIL = "high"


def _guess_mime_from_bytes(first_bytes: bytes) -> str:
    if first_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if first_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if first_bytes.startswith(b"GIF8"):
        return "image/gif"
    if first_bytes[0:4] in (b"RIFF", b"WEBP"):
        return "image/webp"
    return "image/png"


def _ensure_data_url(s: str, fallback_mime: str = "image/png") -> str:
    st = (s or "").strip()
    if not st:
        return st
    if st.startswith("http://") or st.startswith("https://") or st.startswith("data:image/"):
        return st
    return f"data:{fallback_mime};base64,{st}"


def _guess_extension_from_mime(mime: str) -> str:
    ext = mimetypes.guess_extension((mime or "").split(";", 1)[0].strip().lower()) or ".png"
    return ".jpg" if ext == ".jpe" else ext


async def image_url_to_base64(url: str, timeout: int = 15) -> Optional[str]:
    if not url:
        return None
    if url.startswith("data:image/"):
        return url
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", url) and len(url) > 200:
        return _ensure_data_url(url)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers={"User-Agent": "DiscordBot/1.0"}) as r:
                r.raise_for_status()
                ctype = r.headers.get("Content-Type")
                raw = await r.read()
        mime = ctype if ctype and ctype.startswith("image/") else _guess_mime_from_bytes(raw[:16])
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logging.warning("[image_url_to_base64] %s", e)
        return None


async def image_input_to_upload(
    image_input: str,
    timeout: int = 15,
    fallback_name: str = "input",
) -> Optional[Tuple[bytes, str, str]]:
    if not image_input:
        return None

    src = image_input.strip()
    if not src:
        return None

    try:
        mime = ""
        raw = b""

        if src.startswith("data:image/"):
            header, encoded = src.split(",", 1)
            mime = header.split(":", 1)[1].split(";", 1)[0]
            raw = base64.b64decode(encoded)
        elif re.fullmatch(r"[A-Za-z0-9+/=\s]+", src) and len(src) > 200:
            raw = base64.b64decode(src)
            mime = _guess_mime_from_bytes(raw[:16])
        elif src.startswith("http://") or src.startswith("https://"):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(src, headers={"User-Agent": "DiscordBot/1.0"}) as r:
                    r.raise_for_status()
                    ctype = r.headers.get("Content-Type")
                    raw = await r.read()
            mime = ctype if ctype and ctype.startswith("image/") else _guess_mime_from_bytes(raw[:16])
        else:
            raw = base64.b64decode(src)
            mime = _guess_mime_from_bytes(raw[:16])

        if not raw:
            return None

        if not mime.startswith("image/"):
            mime = _guess_mime_from_bytes(raw[:16])

        filename = fallback_name
        if "." not in filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
            filename = f"{filename}{_guess_extension_from_mime(mime)}"
        return raw, filename, mime
    except Exception as e:
        logging.warning("[image_input_to_upload] %s", e)
        return None


def normalize_image_inputs(image_urls: Optional[List[str]]) -> Optional[List[str]]:
    if not image_urls:
        return None
    normed: List[str] = []
    for s in image_urls:
        if not s:
            continue
        if (not s.startswith("http")) and (not s.startswith("data:image/")):
            s = _ensure_data_url(s)
        normed.append(s)
    return normed or None


def build_user_content_chat(prompt: str, image_urls: Optional[List[str]] = None):
    if image_urls:
        parts = [{"type": "text", "text": prompt}]
        for u in image_urls:
            parts.append({"type": "image_url", "image_url": {"url": u, "detail": DEFAULT_VISION_DETAIL}})
        return parts
    return prompt


def build_user_content_responses(prompt: str, image_urls: Optional[List[str]] = None):
    if image_urls:
        parts = [{"type": "input_text", "text": prompt}]
        for u in image_urls:
            parts.append({"type": "input_image", "image_url": {"url": u}, "detail": DEFAULT_VISION_DETAIL})
        return parts
    return prompt
