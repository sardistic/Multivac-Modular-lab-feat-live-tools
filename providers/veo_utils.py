from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from providers.gemini_client import PILImage, get_gemini_client, types

logger = logging.getLogger("gemini_utils")

DEFAULT_VEO_MODEL = os.getenv("GEMINI_VEO_MODEL", "veo-3.1-generate-preview")
DEFAULT_VEO_FAST_MODEL = os.getenv("GEMINI_VEO_FAST_MODEL", "veo-3.1-fast-generate-preview")
DEFAULT_VEO_RESOLUTION = os.getenv("GEMINI_VEO_RESOLUTION", "720p")

VEO_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    DEFAULT_VEO_MODEL: {
        "label": "Veo 3.1",
        "emoji": "🎬",
        "cost_per_second": 0.20,
        "durations": (4, 6, 8),
        "supports_audio": True,
    },
    DEFAULT_VEO_FAST_MODEL: {
        "label": "Veo 3.1 Fast",
        "emoji": "⚡",
        "cost_per_second": 0.10,
        "durations": (4, 6, 8),
        "supports_audio": True,
    },
}


def veo_is_available() -> bool:
    return get_gemini_client() is not None and types is not None


def get_veo_model_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for model, config in VEO_MODEL_CONFIGS.items():
        for seconds in config["durations"]:
            options.append(
                {
                    "provider": "veo",
                    "model": model,
                    "provider_label": config["label"],
                    "seconds": seconds,
                    "emoji": config["emoji"],
                    "cost": estimate_veo_cost(model, seconds),
                    "generate_audio": False,
                }
            )
    return options


def get_veo_model_label(model: str) -> str:
    config = VEO_MODEL_CONFIGS.get(model)
    if config:
        return str(config["label"])
    return model


def estimate_veo_cost(model: str, seconds: int, *, generate_audio: bool = False) -> float:
    config = VEO_MODEL_CONFIGS.get(model, {})
    rate = float(config.get("cost_per_second", 0.20))
    if generate_audio and config.get("supports_audio"):
        rate += 0.15 if "fast" not in model else 0.05
    return round(rate * int(seconds), 2)


def estimate_veo_runtime(model: str, seconds: int) -> int:
    per_second = 12 if "fast" in (model or "").lower() else 18
    return max(45, int(seconds) * per_second)


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_error_message(error: Any) -> str:
    if error is None:
        return "Unknown error"
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        return error.get("message") or error.get("code") or str(error)
    message = getattr(error, "message", None)
    if message:
        return str(message)
    code = getattr(error, "code", None)
    if code:
        return f"{code}: {error}"
    return str(error)


def _extract_generated_video(operation: Any) -> Any:
    for container_name in ("response", "result"):
        container = _safe_attr(operation, container_name)
        generated = _safe_attr(container, "generated_videos")
        if generated:
            return generated[0]
    return None


def _select_veo_aspect_ratio(image_data: bytes | None) -> str:
    if not image_data or not PILImage:
        return "16:9"
    try:
        from io import BytesIO

        with PILImage.open(BytesIO(image_data)) as image:
            width, height = image.size
            if width > 0 and height > 0 and height > width:
                return "9:16"
    except Exception as e:
        logger.warning("Failed to inspect Veo reference image size: %s", e)
    return "16:9"


async def generate_veo_video(
    prompt: str,
    *,
    model: str = DEFAULT_VEO_MODEL,
    seconds: int = 8,
    image_data: bytes | None = None,
    image_content_type: Optional[str] = None,
    generate_audio: bool = False,
    progress_state: Optional[dict[str, Any]] = None,
) -> tuple[bytes | None, str | None]:
    client = get_gemini_client()
    if not client or not types:
        return None, "GEMINI_API_KEY is not configured for Veo video generation."

    if progress_state is not None:
        progress_state["status"] = "Submitting Veo job"
        progress_state["progress"] = 0.05

    image = None
    if image_data:
        image = types.Image(
            image_bytes=image_data,
            mime_type=image_content_type or "image/png",
        )

    config = types.GenerateVideosConfig(
        duration_seconds=int(seconds),
        aspect_ratio=_select_veo_aspect_ratio(image_data),
        resolution=DEFAULT_VEO_RESOLUTION,
        generate_audio=bool(generate_audio),
    )

    try:
        operation = await asyncio.to_thread(
            client.models.generate_videos,
            model=model,
            prompt=prompt,
            image=image,
            config=config,
        )
    except Exception as e:
        logger.exception("Veo generation request failed (model=%s): %s", model, e)
        return None, f"Failed to start Veo generation: {e}"

    started_at = asyncio.get_running_loop().time()
    expected_runtime = estimate_veo_runtime(model, seconds)

    while not bool(_safe_attr(operation, "done", False)):
        await asyncio.sleep(8)
        if progress_state is not None:
            elapsed = asyncio.get_running_loop().time() - started_at
            progress_state["status"] = "Waiting on Veo"
            progress_state["progress"] = min(0.92, max(0.10, elapsed / expected_runtime))
        try:
            operation = await asyncio.to_thread(client.operations.get, operation)
        except Exception as e:
            logger.warning("Veo poll failed: %s", e)

        if asyncio.get_running_loop().time() - started_at > 900:
            return None, "Timeout waiting for Veo video generation."

    error = _safe_attr(operation, "error")
    if error:
        return None, _extract_error_message(error)

    generated_video = _extract_generated_video(operation)
    if not generated_video:
        return None, "Veo did not return a generated video."

    video_file = _safe_attr(generated_video, "video", generated_video)
    try:
        content = await asyncio.to_thread(client.files.download, file=video_file)
    except Exception as e:
        logger.exception("Veo video download failed: %s", e)
        return None, f"Failed to download Veo video: {e}"

    if progress_state is not None:
        progress_state["status"] = "Downloading video"
        progress_state["progress"] = 1.0
    return content, None


__all__ = [
    "DEFAULT_VEO_FAST_MODEL",
    "DEFAULT_VEO_MODEL",
    "estimate_veo_cost",
    "estimate_veo_runtime",
    "generate_veo_video",
    "get_veo_model_label",
    "get_veo_model_options",
    "veo_is_available",
]
