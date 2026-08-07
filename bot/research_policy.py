from __future__ import annotations

import re
from datetime import date, datetime, timezone

_EXPLICIT_FRESHNESS_RE = re.compile(
    r"\b(?:latest|current(?:ly)?|today|tonight|tomorrow|now|recent(?:ly)?|"
    r"breaking|news|live|up[- ]?to[- ]?date|as of|this (?:week|month|year|season)|"
    r"verify|fact[- ]?check|look up|search)\b",
    re.IGNORECASE,
)
_DYNAMIC_FACT_RE = re.compile(
    r"(?:\b(?:what(?:'s| is)|tell me|show me|check)\b.{0,45}\b"
    r"(?:availability|release date|version|schedule|standings?|score|results?|"
    r"polls?|odds|forecast)\b|"
    r"\b(?:what(?:'s| is)|tell me|show me|check)\b.{0,45}\b"
    r"(?:price|cost)\s+(?:of|for)\b|"
    r"\bhow much (?:is|are|does|do)\b)",
    re.IGNORECASE,
)
_RECURRING_RESULT_RE = re.compile(
    r"\b(?:who won|winner of|won the|champion of|champions of)\b.{0,90}\b"
    r"(?:world cup|super bowl|championship|tournament|league|season|election|"
    r"award|oscars?|grammys?|emmys?|game|match|race|grand prix)\b",
    re.IGNORECASE,
)
_CURRENT_ROLE_RE = re.compile(
    r"\b(?:who is|who(?:'s| is) the|name the)\b.{0,60}\b"
    r"(?:president|prime minister|premier|governor|mayor|senator|representative|"
    r"secretary|minister|pope|monarch|king|queen|ceo|chief executive|"
    r"chair(?:man|woman|person)?|head coach|manager)\b",
    re.IGNORECASE,
)
_EXPLICIT_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_REVERSE_IMAGE_RE = re.compile(
    r"(?:\breverse(?:[- ]image)?\s+search\b|"
    r"\bsearch\s+(?:for\s+)?(?:this|that|the)\s+(?:image|picture|photo|panel)\b|"
    r"\bfind\s+(?:me\s+)?(?:the\s+)?(?:source|origin|match)\b|"
    r"\bwhere\s+(?:is|was|did)\s+(?:this|that|the\s+(?:image|picture|photo|panel))\b.{0,25}\bfrom\b|"
    r"\bidentify\s+(?:the\s+)?(?:source|manga|comic|anime|art(?:ist|work)?)\b)",
    re.IGNORECASE,
)


def is_reverse_image_request(prompt: str, *, has_images: bool) -> bool:
    """Require both content-search wording and an image in this request."""
    return bool(has_images and _REVERSE_IMAGE_RE.search(prompt or ""))


def requires_fresh_web(prompt: str) -> bool:
    """Return true for high-confidence requests whose answer can go stale.

    The semantic intent classifier handles broad meaning. This narrow,
    deterministic safety net catches costly misses without turning every fact
    question into a search. A named historical year stays on the normal route
    unless the user also explicitly requests a current verification.
    """
    text = " ".join((prompt or "").split())
    if not text:
        return False
    explicit_freshness = bool(_EXPLICIT_FRESHNESS_RE.search(text))
    if explicit_freshness:
        return True
    if _EXPLICIT_YEAR_RE.search(text):
        return False
    return bool(
        _DYNAMIC_FACT_RE.search(text)
        or _RECURRING_RESULT_RE.search(text)
        or _CURRENT_ROLE_RE.search(text)
    )


def build_fresh_search_query(prompt: str, *, today: date | None = None) -> str:
    """Make the mandatory first lookup explicit enough to avoid stale results."""
    text = " ".join((prompt or "").split()).strip()
    current = today or datetime.now(timezone.utc).date()
    if not text:
        return f"latest verified information as of {current.isoformat()}"
    if _EXPLICIT_YEAR_RE.search(text):
        return f"{text} verified result"
    if _RECURRING_RESULT_RE.search(text):
        return f"{text} {current.year} final result winner"
    if _CURRENT_ROLE_RE.search(text):
        return f"{text} as of {current.isoformat()} official"
    return f"{text} latest as of {current.isoformat()}"
