from __future__ import annotations

import base64
import logging
import mimetypes
import re
from typing import List, Optional, Tuple

from services.url_utils import DEFAULT_MEDIA_BYTES, fetch_url_bytes_async

DEFAULT_VISION_DETAIL = "high"
MAX_IMAGE_INPUT_BYTES = DEFAULT_MEDIA_BYTES


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
        encoded = url.split(",", 1)[1] if "," in url else ""
        if len(encoded) * 3 // 4 > MAX_IMAGE_INPUT_BYTES:
            return None
        return url
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", url) and 200 < len(url) <= (MAX_IMAGE_INPUT_BYTES * 4 // 3 + 8):
        return _ensure_data_url(url)
    try:
        fetched = await fetch_url_bytes_async(
            url,
            timeout=timeout,
            max_bytes=MAX_IMAGE_INPUT_BYTES,
            allowed_content_types=("image/",),
            headers={"User-Agent": "DiscordBot/1.0"},
        )
        ctype = fetched.content_type
        raw = fetched.body
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
            if len(encoded) * 3 // 4 > MAX_IMAGE_INPUT_BYTES:
                return None
            mime = header.split(":", 1)[1].split(";", 1)[0]
            raw = base64.b64decode(encoded)
        elif re.fullmatch(r"[A-Za-z0-9+/=\s]+", src) and len(src) > 200:
            if len(src) * 3 // 4 > MAX_IMAGE_INPUT_BYTES:
                return None
            raw = base64.b64decode(src)
            mime = _guess_mime_from_bytes(raw[:16])
        elif src.startswith("http://") or src.startswith("https://"):
            fetched = await fetch_url_bytes_async(
                src,
                timeout=timeout,
                max_bytes=MAX_IMAGE_INPUT_BYTES,
                allowed_content_types=("image/",),
                headers={"User-Agent": "DiscordBot/1.0"},
            )
            ctype = fetched.content_type
            raw = fetched.body
            mime = ctype if ctype and ctype.startswith("image/") else _guess_mime_from_bytes(raw[:16])
        else:
            raw = base64.b64decode(src)
            mime = _guess_mime_from_bytes(raw[:16])

        if not raw:
            return None
        if len(raw) > MAX_IMAGE_INPUT_BYTES:
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
