"""Provider-transparent reverse-image lookup.

Google Cloud Vision Web Detection is the primary path because Discord images
are collected as data URLs. It prefers a dedicated Vision credential so Google
Custom Search restrictions can remain isolated, with the legacy shared key as
a compatibility fallback. When Vision completes but finds no matching image or
source page, auto mode escalates public image URLs to SerpApi Google Lens. No
provider result is described as a match unless that provider returned explicit
match evidence; visual Lens results remain candidates until corroborated.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

import httpx

from config import GOOGLE_VISION_API_KEY, SERPAPI_API_KEY


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


def _provider_attempt(provider: str, result: dict[str, Any]) -> dict[str, Any]:
    error = str(result.get("error") or "")
    status = "completed" if result.get("ok") else "failed"
    if error.endswith("_not_configured") or error.endswith("_requires_public_image_url"):
        status = "unavailable"
    return {
        "provider": str(result.get("provider") or provider),
        "status": status,
        "match_found": bool(result.get("match_found")),
        "candidate_found": bool(result.get("candidate_found")),
        "result_counts": dict(result.get("result_counts") or {}),
        **({"error": error[:120]} if error else {}),
        **({"http_status": result.get("status")} if result.get("status") else {}),
    }


def _apply_mode(result: dict[str, Any], mode: str) -> dict[str, Any]:
    filtered = dict(result)
    if mode == "exact":
        filtered.pop("visually_similar_images", None)
        filtered.pop("visual_matches", None)
    elif mode == "visual":
        filtered.pop("exact_or_partial_matches", None)
        filtered.pop("pages_with_matches", None)
        filtered.pop("exact_matches", None)
    return filtered


def _vision_context(result: dict[str, Any]) -> dict[str, Any]:
    """Keep useful primary-provider context without duplicating its full payload."""
    return {
        "provider": result.get("provider"),
        "best_guess_labels": list(result.get("best_guess_labels") or []),
        "entities": list(result.get("entities") or []),
        "visually_similar_images": list(result.get("visually_similar_images") or []),
        "result_counts": dict(result.get("result_counts") or {}),
        "match_found": bool(result.get("match_found")),
    }


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
    if not GOOGLE_VISION_API_KEY:
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
            headers={"x-goog-api-key": GOOGLE_VISION_API_KEY},
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
        "type": "all",
        "url": image_url,
        "hl": os.getenv("SERPAPI_LENS_LANGUAGE", "en"),
        "country": os.getenv("SERPAPI_LENS_COUNTRY", "us"),
        "api_key": SERPAPI_API_KEY,
    }
    safe = os.getenv("SERPAPI_LENS_SAFE", "").strip().lower()
    if safe in {"active", "off"}:
        params["safe"] = safe
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(_SERPAPI_URL, params=params)
    if response.status_code != 200:
        return {
            "ok": False,
            "error": "serpapi_lens_http_error",
            "status": response.status_code,
        }
    data = response.json()
    if data.get("error"):
        return {
            "ok": False,
            "error": "serpapi_lens_provider_error",
            "detail": str(data.get("error") or "")[:240],
        }

    exact_matches = []
    for item in data.get("exact_matches") or []:
        exact_matches.append(
            {
                "url": item.get("link") or item.get("source"),
                "title": item.get("title") or "",
                "source": item.get("source") or "",
                "thumbnail": item.get("thumbnail") or "",
                "match_type": "lens_exact_match",
            }
        )

    visual_matches = []
    for item in data.get("visual_matches") or []:
        visual_matches.append(
            {
                "url": item.get("link") or item.get("source"),
                "title": item.get("title") or "",
                "source": item.get("source") or "",
                "thumbnail": item.get("thumbnail") or "",
                "match_type": (
                    "lens_exact_candidate" if item.get("exact_matches") else "visual_match"
                ),
                "exact_candidate": bool(item.get("exact_matches")),
            }
        )
    exact_matches = _dedupe(exact_matches, limit)
    visual_matches = _dedupe(visual_matches, limit)
    related_queries = [
        str(item.get("query") or "").strip()
        for item in data.get("related_content") or []
        if str(item.get("query") or "").strip()
    ][:limit]
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
        "exact_matches": exact_matches,
        "visual_matches": visual_matches,
        "related_queries": related_queries,
        "match_found": bool(exact_matches),
        "candidate_found": bool(visual_matches),
        "result_counts": {
            "exact_matches": len(exact_matches),
            "visual_matches": len(visual_matches),
            "related_queries": len(related_queries),
        },
    }


async def reverse_image_search(
    image_input: str,
    *,
    public_image_url: str | None = None,
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
    lens_image_url = (
        public_image_url
        if str(public_image_url or "").startswith(("http://", "https://"))
        else image_input
    )
    attempts: list[dict[str, Any]] = []
    provider_chain: list[dict[str, Any]] = []
    vision_no_match: dict[str, Any] | None = None

    if selected in {"auto", "google", "google_vision", "vision"}:
        try:
            result = await _google_vision(image_input, limit)
        except Exception as exc:
            logger.warning("Google Vision reverse lookup failed", exc_info=True)
            result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
        provider_chain.append(_provider_attempt("google_cloud_vision_web_detection", result))
        if result.get("ok") and result.get("match_found"):
            return _apply_mode({**result, "provider_chain": provider_chain}, mode)
        if result.get("ok"):
            vision_no_match = result
            if selected != "auto":
                return _apply_mode({**result, "provider_chain": provider_chain}, mode)
        else:
            attempts.append({"provider": "google_cloud_vision_web_detection", **result})
            if selected != "auto":
                return {
                    "ok": False,
                    "lookup_type": "reverse_image_search",
                    "attempts": attempts,
                    "provider_chain": provider_chain,
                }

    if selected in {"auto", "serpapi", "lens", "google_lens"}:
        try:
            result = await _serpapi_lens(lens_image_url, limit)
        except Exception as exc:
            logger.warning("SerpApi Lens reverse lookup failed", exc_info=True)
            result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
        provider_chain.append(_provider_attempt("serpapi_google_lens", result))
        if result.get("ok"):
            enriched = {**result, "provider_chain": provider_chain}
            if vision_no_match is not None:
                enriched["fallback_reason"] = "google_vision_no_match"
                enriched["primary_provider_context"] = _vision_context(vision_no_match)
            elif provider_chain and provider_chain[0].get("provider") != "serpapi_google_lens":
                enriched["fallback_reason"] = "google_vision_failed"
            return _apply_mode(enriched, mode)
        attempts.append({"provider": "serpapi_google_lens", **result})

    if vision_no_match is not None:
        return _apply_mode(
            {
                **vision_no_match,
                "provider_chain": provider_chain,
                "fallback_reason": "google_vision_no_match",
                "fallback_succeeded": False,
            },
            mode,
        )

    return {
        "ok": False,
        "lookup_type": "reverse_image_search",
        "error": "no_reverse_image_provider_succeeded",
        "attempts": attempts,
        "provider_chain": provider_chain,
        "configuration": {
            "google_vision_available": bool(GOOGLE_VISION_API_KEY),
            "serpapi_lens_available": bool(SERPAPI_API_KEY),
        },
    }
