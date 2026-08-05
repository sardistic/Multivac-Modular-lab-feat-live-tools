"""Provider-transparent reverse-image lookup.

Google Cloud Vision Web Detection is the primary path because Multivac already
has a Google API credential and Discord images are collected as data URLs. An
optional SerpApi Google Lens path is used only when configured and the image is
available as a public HTTP(S) URL. No provider result is described as a match
unless the provider returned a matching image or page.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

import httpx

from config import GOOGLE_API_KEY, SERPAPI_API_KEY


logger = logging.getLogger("reverse_image_search")

_DATA_URL_RE = re.compile(
    r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
    flags=re.IGNORECASE | re.DOTALL,
)
_VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
_SERPAPI_URL = "https://serpapi.com/search.json"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _limit(value: Any, *, default: int = 10, maximum: int = 20) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _dedupe(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def _image_base64(image_input: str) -> str:
    value = (image_input or "").strip()
    match = _DATA_URL_RE.match(value)
    if match:
        data = re.sub(r"\s+", "", match.group(2))
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise ValueError("invalid_image_data_url") from exc
    elif value.startswith(("http://", "https://")):
        timeout = _env_float("REVERSE_IMAGE_HTTP_TIMEOUT_SECONDS", 15.0)
        max_bytes = int(_env_float("REVERSE_IMAGE_MAX_BYTES", 10_000_000))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(value, headers={"User-Agent": "Multivac/1.0"})
            response.raise_for_status()
            raw = response.content
            content_type = (response.headers.get("content-type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError("image_url_did_not_return_an_image")
            if len(raw) > max_bytes:
                raise ValueError("image_exceeds_reverse_search_size_limit")
    else:
        raise ValueError("unsupported_image_input")

    if not raw:
        raise ValueError("empty_image_input")
    return base64.b64encode(raw).decode("ascii")


def _vision_result(data: dict[str, Any], limit: int) -> dict[str, Any]:
    responses = data.get("responses") or []
    first = responses[0] if responses else {}
    if first.get("error"):
        message = first.get("error", {}).get("message") or "vision_web_detection_failed"
        return {"ok": False, "error": str(message)}

    web = first.get("webDetection") or {}
    exact: list[dict[str, Any]] = []
    for key, kind in (
        ("fullMatchingImages", "full_image_match"),
        ("partialMatchingImages", "partial_image_match"),
    ):
        for item in web.get(key) or []:
            exact.append({"url": item.get("url"), "match_type": kind})

    pages = [
        {
            "url": item.get("url"),
            "title": item.get("pageTitle") or "",
            "match_type": "page_with_matching_image",
        }
        for item in web.get("pagesWithMatchingImages") or []
    ]
    similar = [
        {"url": item.get("url"), "match_type": "visually_similar"}
        for item in web.get("visuallySimilarImages") or []
    ]
    labels = [
        str(item.get("label") or "").strip()
        for item in web.get("bestGuessLabels") or []
        if str(item.get("label") or "").strip()
    ]
    entities = [
        {
            "description": item.get("description") or "",
            "score": item.get("score"),
        }
        for item in web.get("webEntities") or []
        if item.get("description")
    ][:limit]
    exact = _dedupe(exact, limit)
    pages = _dedupe(pages, limit)
    similar = _dedupe(similar, limit)
    return {
        "ok": True,
        "provider": "google_cloud_vision_web_detection",
        "lookup_type": "reverse_image_search",
        "best_guess_labels": labels[:5],
        "entities": entities,
        "exact_or_partial_matches": exact,
        "pages_with_matches": pages,
        "visually_similar_images": similar,
        "match_found": bool(exact or pages),
        "result_counts": {
            "exact_or_partial": len(exact),
            "pages": len(pages),
            "visually_similar": len(similar),
        },
    }


async def _google_vision(image_input: str, limit: int) -> dict[str, Any]:
    if not GOOGLE_API_KEY:
        return {"ok": False, "error": "google_api_key_not_configured"}
    content = await _image_base64(image_input)
    payload = {
        "requests": [
            {
                "image": {"content": content},
                "features": [{"type": "WEB_DETECTION", "maxResults": limit}],
            }
        ]
    }
    timeout = _env_float("REVERSE_IMAGE_HTTP_TIMEOUT_SECONDS", 15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            _VISION_URL,
            headers={"x-goog-api-key": GOOGLE_API_KEY},
            json=payload,
        )
    if response.status_code != 200:
        try:
            detail = response.json().get("error", {}).get("message")
        except Exception:
            detail = None
        return {
            "ok": False,
            "error": "google_vision_http_error",
            "status": response.status_code,
            "detail": str(detail or "")[:240],
        }
    result = _vision_result(response.json(), limit)
    if result.get("ok"):
        from services import usage_costs

        usage_costs.record_metered(
            "google-cloud-vision-web-detection",
            _env_float("GOOGLE_VISION_WEB_COST_PER_CALL_USD", 0.0035),
            label="reverse_image_search",
            meta={"pricing_basis": "list_price_before_free_tier"},
        )
    return result


async def _serpapi_lens(image_url: str, limit: int) -> dict[str, Any]:
    if not SERPAPI_API_KEY:
        return {"ok": False, "error": "serpapi_api_key_not_configured"}
    if not image_url.startswith(("http://", "https://")):
        return {"ok": False, "error": "serpapi_lens_requires_public_image_url"}
    timeout = _env_float("REVERSE_IMAGE_HTTP_TIMEOUT_SECONDS", 15.0)
    params = {
        "engine": "google_lens",
        "url": image_url,
        "api_key": SERPAPI_API_KEY,
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(_SERPAPI_URL, params=params)
    if response.status_code != 200:
        return {
            "ok": False,
            "error": "serpapi_lens_http_error",
            "status": response.status_code,
        }
    data = response.json()
    matches = []
    for item in data.get("visual_matches") or []:
        matches.append(
            {
                "url": item.get("link") or item.get("source"),
                "title": item.get("title") or "",
                "source": item.get("source") or "",
                "thumbnail": item.get("thumbnail") or "",
                "match_type": "visual_match",
            }
        )
    matches = _dedupe(matches, limit)
    from services import usage_costs

    usage_costs.record_metered(
        "serpapi-google-lens",
        _env_float("SERPAPI_LENS_COST_PER_CALL_USD", 0.025),
        label="reverse_image_search",
        meta={"pricing_basis": "configurable_plan_estimate"},
    )
    return {
        "ok": True,
        "provider": "serpapi_google_lens",
        "lookup_type": "reverse_image_search",
        "visual_matches": matches,
        "match_found": bool(matches),
        "result_counts": {"visual_matches": len(matches)},
    }


async def reverse_image_search(
    image_input: str,
    *,
    mode: str = "all",
    max_results: int = 10,
    provider: str | None = None,
) -> dict[str, Any]:
    """Run a genuine content-based reverse-image lookup.

    ``mode`` controls which result families are returned, but the provider name
    and match semantics always remain visible to the model and user.
    """
    limit = _limit(max_results)
    selected = (provider or os.getenv("REVERSE_IMAGE_PROVIDER", "auto")).strip().lower()
    attempts: list[dict[str, Any]] = []

    if selected in {"auto", "google", "google_vision", "vision"}:
        try:
            result = await _google_vision(image_input, limit)
        except Exception as exc:
            logger.warning("Google Vision reverse lookup failed", exc_info=True)
            result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
        if result.get("ok"):
            if mode == "exact":
                result.pop("visually_similar_images", None)
            elif mode == "visual":
                result.pop("exact_or_partial_matches", None)
                result.pop("pages_with_matches", None)
            return result
        attempts.append({"provider": "google_cloud_vision_web_detection", **result})
        if selected != "auto":
            return {"ok": False, "lookup_type": "reverse_image_search", "attempts": attempts}

    if selected in {"auto", "serpapi", "lens", "google_lens"}:
        try:
            result = await _serpapi_lens(image_input, limit)
        except Exception as exc:
            logger.warning("SerpApi Lens reverse lookup failed", exc_info=True)
            result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
        if result.get("ok"):
            return result
        attempts.append({"provider": "serpapi_google_lens", **result})

    return {
        "ok": False,
        "lookup_type": "reverse_image_search",
        "error": "no_reverse_image_provider_succeeded",
        "attempts": attempts,
        "configuration": {
            "google_vision_available": bool(GOOGLE_API_KEY),
            "serpapi_lens_available": bool(SERPAPI_API_KEY),
        },
    }
