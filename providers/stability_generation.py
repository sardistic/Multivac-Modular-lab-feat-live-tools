from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
import re
from io import BytesIO
from typing import Optional

import requests
from PIL import Image

from providers.gemini_utils import edit_gemini_image, generate_gemini_image, generate_gemini_with_references
from providers.stability_client import (
    STABILITY_AVAILABLE,
    STABILITY_KEY,
    generation,
    get_openai_image_client,
    stability_client,
)

logger = logging.getLogger("stability_utils")
_REPLY_PROMPT_MAX_CHARS = 1800

# Friendly, user-facing model labels shown in the live status / ✅ line.
IMG_MODEL_OPENAI = "GPT Image 1.5"
IMG_MODEL_GEMINI = "Gemini 3 Pro Image"
IMG_MODEL_STABILITY = "Stable Diffusion 1.5"


def _compose_reply_aware_image_prompt(prompt: str, reply_msg=None, retry_context: str = "") -> str:
    base_prompt = (prompt or "").strip()
    retry_text = re.sub(r"\s+", " ", (retry_context or "").strip())
    if retry_text:
        if len(retry_text) > _REPLY_PROMPT_MAX_CHARS:
            retry_text = retry_text[:_REPLY_PROMPT_MAX_CHARS].rstrip() + "..."
        if base_prompt:
            return (
                "Create a new image that follows the current revision while preserving the "
                "subject and requirements from the prior image context. Do not substitute an "
                "unrelated subject.\n\n"
                f"Current revision request:\n{base_prompt}\n\n"
                "Previous image request/revision context (oldest to newest):\n"
                f"{retry_text}"
            )
        return f"Previous image request/revision context (oldest to newest):\n{retry_text}"

    reply_text = (getattr(reply_msg, "content", "") or "").strip()
    if not reply_text:
        return base_prompt

    reply_text = re.sub(r"\s+", " ", reply_text)
    if len(reply_text) > _REPLY_PROMPT_MAX_CHARS:
        reply_text = reply_text[:_REPLY_PROMPT_MAX_CHARS].rstrip() + "..."

    author = getattr(getattr(reply_msg, "author", None), "display_name", None) or getattr(
        getattr(reply_msg, "author", None), "name", None
    )
    context_label = f"Replied message context from {author}" if author else "Replied message context"
    if base_prompt:
        return f"{base_prompt}\n\n{context_label}:\n{reply_text}"
    return f"{context_label}:\n{reply_text}"


def extract_width_height_from_prompt(prompt: str) -> tuple[int, int]:
    prompt_lower = prompt.lower()
    width = height = 1024
    if "portrait" in prompt_lower or "vertical" in prompt_lower:
        width, height = 768, 1024
    elif "landscape" in prompt_lower or "horizontal" in prompt_lower:
        width, height = 1024, 768
    match = re.search(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", prompt)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
    return width, height


async def generate_stability_image(image_prompt: str, width: int = 960, height: int = 768) -> Optional[BytesIO]:
    if not (STABILITY_AVAILABLE and STABILITY_KEY and stability_client and generation):
        logger.warning("generate_stability_image called but Stability is not configured.")
        return None
    try:
        api = stability_client.StabilityInference(
            key=STABILITY_KEY,
            verbose=True,
            engine="stable-diffusion-v1-5",
        )
        answers = api.generate(
            prompt=image_prompt,
            seed=random.randint(0, 2**32 - 1),
            steps=50,
            cfg_scale=11.0,
            width=width,
            height=height,
            samples=1,
            sampler=generation.SAMPLER_K_EULER_ANCESTRAL,
        )
        for resp in answers:
            for artifact in resp.artifacts:
                if artifact.type == generation.ARTIFACT_IMAGE:
                    img = Image.open(io.BytesIO(artifact.binary))
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    buf.seek(0)
                    return buf
    except Exception:
        logger.exception("Error generating Stability image")
    return None


class ImageModerationError(Exception):
    """The provider's safety system refused the image prompt."""


async def generate_gpt_image(prompt: str) -> Optional[BytesIO]:
    try:
        background_type = "transparent" if "transparent background" in prompt.lower() else "auto"
        result = await get_openai_image_client().images.generate(
            model="gpt-image-1.5",
            prompt=prompt,
            size="auto",
            background=background_type,
            quality="high",
            moderation="low",
            n=1,
        )
        _record_image_usage(result, label="image_generation")
        b64_image = result.data[0].b64_json if result and result.data else None
        if not b64_image:
            logger.warning("gpt-image returned no image data (prompt=%.80r, result=%r)", prompt, result)
            return None
        logger.info("gpt-image generated %d bytes (prompt=%.80r)", len(b64_image) * 3 // 4, prompt)
        return BytesIO(base64.b64decode(b64_image))
    except Exception as e:
        if "moderation_blocked" in str(e):
            logger.warning("gpt-image moderation block (prompt=%.80r)", prompt)
            raise ImageModerationError("OpenAI's safety system rejected this image prompt.") from e
        logger.exception("Error generating GPT image")
        return None


def _record_image_usage(result, *, label: str) -> None:
    """Ledger entry for a gpt-image call: token-based when the API reports
    usage, else a flat per-image estimate (OPENAI_IMAGE_COST_USD, default 0.06)."""
    try:
        import os

        from services import usage_costs

        if getattr(result, "usage", None) is not None:
            usage_costs.record_response("gpt-image-1.5", result, label=label)
        else:
            usage_costs.record(
                "gpt-image-1.5", None, float(os.getenv("OPENAI_IMAGE_COST_USD", "0.06")), label=label
            )
    except Exception:
        logger.warning("image usage recording failed", exc_info=True)


# "gemini imagine X", "gemini generate an image of X", "gemini draw a picture
# of Y", ... — match the routing prefix so it can be stripped from the actual
# image prompt.
_GEMINI_IMAGE_PREFIX_RE = re.compile(
    r"^gemini\s+(?:imagine|generate|create|draw|paint|make)\b"
    r"(?:\s+(?:an|a|the|me|us)\b)*"
    r"(?:\s+(?:image|picture|photo|pic|artwork|art|drawing|portrait|wallpaper|logo)\b)?"
    r"(?:\s+of\b|\s*:)?\s*",
    re.IGNORECASE,
)


async def handle_image_generation(
    message,
    prompt: str,
    reply_msg=None,
    retry_context: str = "",
    use_gemini: bool | None = None,
    provider_state: dict | None = None,
) -> Optional[BytesIO]:
    def _set_provider(name: str, model: str) -> None:
        if provider_state is not None:
            provider_state["provider"] = name
            provider_state["model"] = model

    async def _openai_then_gemini(image_prompt: str, width: int, height: int) -> Optional[BytesIO]:
        """Try OpenAI once, then always try Gemini for any OpenAI failure."""
        _set_provider("OpenAI", IMG_MODEL_OPENAI)
        try:
            img = await generate_gpt_image(image_prompt)
        except ImageModerationError:
            img = None
        if img:
            return img

        if message:
            await message.channel.send("⚠️ OpenAI image generation failed — trying **Gemini** instead…")
        _set_provider("Gemini", IMG_MODEL_GEMINI)
        img = await asyncio.to_thread(generate_gemini_image, image_prompt, width, height)
        if img:
            return img
        if message:
            await message.channel.send("❌ Gemini image generation failed too. Try again or rephrase the prompt.")
        return None

    try:
        prompt_with_reply_context = _compose_reply_aware_image_prompt(prompt, reply_msg, retry_context)
        width, height = extract_width_height_from_prompt(prompt_with_reply_context)
        if prompt.lower().startswith("stable imagine"):
            image_prompt = _compose_reply_aware_image_prompt(prompt[15:].strip(), reply_msg, retry_context)
            if STABILITY_AVAILABLE:
                _set_provider("Stability", IMG_MODEL_STABILITY)
                img = await generate_stability_image(image_prompt, width, height)
                if img:
                    return img
            return await _openai_then_gemini(image_prompt, width, height)

        # Provider selection is passed in by the dispatcher (the user said
        # "gemini" somewhere); the prefix regex is only used to strip routing
        # words like "gemini generate an image of" from the actual prompt.
        gemini_prefix = _GEMINI_IMAGE_PREFIX_RE.match(prompt)
        if use_gemini is None:
            use_gemini = bool(gemini_prefix)
        if use_gemini:
            if gemini_prefix:
                core_prompt = prompt[gemini_prefix.end():].strip()
            else:
                core_prompt = re.sub(r"^gemini[\s,:]*", "", prompt, flags=re.IGNORECASE).strip() or prompt
            image_prompt = _compose_reply_aware_image_prompt(core_prompt, reply_msg, retry_context)
            _set_provider("Gemini", IMG_MODEL_GEMINI)
            ref_images = []
            headers = {"User-Agent": "Mozilla/5.0"}
            if reply_msg:
                if reply_msg.attachments:
                    for att in reply_msg.attachments:
                        if att.content_type and att.content_type.startswith("image/"):
                            try:
                                r = requests.get(att.url, headers=headers, timeout=20)
                                if r.status_code == 200:
                                    ref_images.append(BytesIO(r.content))
                            except Exception as e:
                                logger.error("Failed to download reply attachment %s: %s", att.url, e)
                if reply_msg.embeds:
                    for embed in reply_msg.embeds:
                        if embed.image and embed.image.url:
                            try:
                                r = requests.get(embed.image.url, headers=headers, timeout=20)
                                if r.status_code == 200:
                                    ref_images.append(BytesIO(r.content))
                            except Exception as e:
                                logger.error("Failed to download reply embed %s: %s", embed.image.url, e)
            if message and message.attachments:
                for att in message.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        try:
                            r = requests.get(att.url, headers=headers, timeout=20)
                            if r.status_code == 200:
                                ref_images.append(BytesIO(r.content))
                        except Exception as e:
                            logger.error("Failed to download attachment %s: %s", att.url, e)
            for url in re.findall(r"(https?://\S+\.(?:png|jpg|jpeg|webp|gif))", prompt, re.IGNORECASE):
                try:
                    r = requests.get(url, headers=headers, timeout=20)
                    if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                        ref_images.append(BytesIO(r.content))
                except Exception as e:
                    logger.error("Failed to download URL %s: %s", url, e)
            if ref_images:
                img = await asyncio.to_thread(generate_gemini_with_references, image_prompt, ref_images)
                if img:
                    return img
            img = await asyncio.to_thread(generate_gemini_image, image_prompt, width, height)
            if img:
                return img
            if message:
                await message.channel.send("⚠️ **Gemini generation failed** (likely rate limit or error). Falling back to OpenAI... 🧠")
            _set_provider("OpenAI", IMG_MODEL_OPENAI)
            try:
                return await generate_gpt_image(image_prompt)
            except ImageModerationError:
                if message:
                    await message.channel.send("🚫 OpenAI's safety system also rejected this prompt. Try rephrasing.")
                return None

        return await _openai_then_gemini(prompt_with_reply_context, width, height)
    except Exception:
        logger.exception("Error in handle_image_generation")
        return None


async def edit_image_with_prompt(image_input: str | list[str], prompt: str) -> Optional[BytesIO]:
    try:
        urls = [image_input] if isinstance(image_input, str) else image_input
        if not urls:
            return None

        def decode_img(u):
            if u.startswith("data:image/"):
                _, b64 = u.split(",", 1)
                return BytesIO(base64.b64decode(b64))
            if u.startswith("http"):
                r = requests.get(u, timeout=30)
                r.raise_for_status()
                return BytesIO(r.content)
            return BytesIO(base64.b64decode(u))

        if prompt.lower().startswith("gemini edit"):
            base_img = decode_img(urls[0])
            return edit_gemini_image(base_img, prompt[11:].strip())

        base_img = decode_img(urls[0])
        result = await get_openai_image_client().images.edits(
            model="gpt-image-1.5",
            image=base_img,
            prompt=prompt,
            size="auto",
        )
        _record_image_usage(result, label="image_edit")
        b64_image = result.data[0].b64_json if result and result.data else None
        if not b64_image:
            return None
        return BytesIO(base64.b64decode(b64_image))
    except Exception:
        logger.exception("Error editing image")
        return None
