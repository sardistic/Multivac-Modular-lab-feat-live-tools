"""Shared redaction and user-safe diagnostic helpers."""

from __future__ import annotations

import re


_SECRET_RE = re.compile(
    r"(?i)("
    r"(?:api[_-]?key|token|password|secret|authorization|cookie)\s*[=:]\s*\S+|"
    r"bearer\s+\S+|"
    r"(?:sk|ghp|ghu|xox[a-z]?|AIza)[-_A-Za-z0-9]{8,}"
    r")"
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
_SNOWFLAKE_RE = re.compile(r"\b\d{15,22}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_WINDOWS_USER_PATH_RE = re.compile(r"(?i)\b([a-z]:\\users\\)[^\\\s]+")
_UNIX_PRIVATE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:home|srv|app|etc|var|root|run|tmp)/\S+")


def sanitize_diagnostic_text(text: str, *, max_chars: int = 800) -> str:
    value = _SECRET_RE.sub("[REDACTED]", text or "")
    value = _URL_RE.sub("[URL]", value)
    value = _EMAIL_RE.sub("[EMAIL]", value)
    value = _MENTION_RE.sub("[MENTION]", value)
    value = _SNOWFLAKE_RE.sub("[ID]", value)
    value = _UUID_RE.sub("[UUID]", value)
    value = _WINDOWS_USER_PATH_RE.sub(r"\1[USER]", value)
    value = _UNIX_PRIVATE_PATH_RE.sub("[PATH]", value)
    return " ".join(value.split())[: max(1, int(max_chars))]


def public_error_detail(exc: BaseException | str) -> str:
    """Return useful operational context without exposing raw exception text."""
    raw = str(exc or "")
    lowered = raw.lower()
    status = re.search(r"\b(?:http\s*)?(4\d\d|5\d\d)\b", lowered)
    suffix = f" (HTTP {status.group(1)})" if status else ""
    if "rate limit" in lowered or "quota" in lowered or (status and status.group(1) == "429"):
        return "the provider is rate-limited or out of quota" + suffix
    if "provider queue" in lowered or "concurren" in lowered or "queue is currently full" in lowered:
        return "provider capacity is temporarily busy"
    if "timeout" in lowered or "timed out" in lowered:
        return "the upstream request timed out" + suffix
    if "content_filter" in lowered or "safety" in lowered or "moderation" in lowered:
        return "the provider safety filter rejected the request" + suffix
    if "auth" in lowered or "permission" in lowered or "forbidden" in lowered or (
        status and status.group(1) in {"401", "403"}
    ):
        return "the upstream service rejected authorization" + suffix
    if "connect" in lowered or "dns" in lowered or "resolve" in lowered:
        return "the upstream service could not be reached" + suffix
    if status:
        return "an upstream request failed" + suffix
    if isinstance(exc, (ValueError, TypeError)):
        return "the request contained an invalid value"
    return f"an internal {type(exc).__name__} occurred" if not isinstance(exc, str) else "an internal error occurred"


__all__ = ["public_error_detail", "sanitize_diagnostic_text"]
