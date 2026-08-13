from __future__ import annotations

import re
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from services.memory_client import OPENSEARCH_INDEX, conversation_key, init_es_client, search_raw, runtime, _now_iso, _now_utc
from config import ALLOW_CROSS_CHANNEL_USER_CONTEXT, ALLOW_CROSS_GUILD_USER_CONTEXT

# Keep sorting compatible with older indices where message_id may be mapped as text.
_SORT_RECENT = [{"timestamp": {"order": "desc"}}]
_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"

_USER_RECALL_RE = re.compile(
    r"\b(?:"
    r"when did i|what did i|did i|have i|"
    r"i said|i say|i mentioned|i mention|"
    r"my message|my messages"
    r")\b"
)
_ASSISTANT_RECALL_RE = re.compile(
    r"\b(?:"
    r"when did you|what did you|did you|have you|"
    r"you said|you say|you mentioned|you mention|you told me|"
    r"your message|your messages|assistant|bot"
    r")\b"
)
_RECALL_NOISE_PATTERNS = [
    r"\bwhen did (?:i|you|we)\b",
    r"\bwhat did (?:i|you|we)\b",
    r"\bdid (?:i|you|we)\b",
    r"\bhave (?:i|you|we)\b",
    r"\bcan you\b",
    r"\bplease\b",
    r"\b(?:find|search|look up|lookup)\b",
    r"\b(?:the )?last time\b",
    r"\blast\b",
    r"\bmost recent(?:ly)?\b",
    r"\brecently\b",
    r"\bprevious(?:ly)?\b",
    r"\bearliest\b",
    r"\bfirst(?: message| thing)?\b",
    r"\bhistory\b",
    r"\bmessage(?:s)?\b",
    r"\bmention(?:ed|ing)?\b",
    r"\b(?:say|said)\b",
    r"\btalk(?:ed)? about\b",
    r"\bbring(?:ing)? up\b",
    r"\bbrought up\b",
    r"\btold you\b",
    r"\babout\b",
    r"\byesterday\b",
    r"\blast (?:week|month|year)\b",
    r"\b\d+\s+(?:sec|second|min|minute|hour|hr|day|week|month|year)s?\s+ago\b",
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:sec|second|min|minute|hour|hr|day|week|month|year)s?\s+ago\b",
]
_PRONOUN_NOISE_RE = re.compile(r"\b(?:i|you|we|me|my|your|our|the)\b")


def _infer_role_filter(query_text: str) -> Optional[str]:
    low = (query_text or "").lower()
    if _USER_RECALL_RE.search(low):
        return _ROLE_USER
    if _ASSISTANT_RECALL_RE.search(low):
        return _ROLE_ASSISTANT
    return None


def _extract_recall_search_terms(query_text: str) -> str:
    cleaned = (query_text or "").lower()
    cleaned = re.sub(r"[\"'`“”‘’]", " ", cleaned)
    cleaned = re.sub(r"[?!.:,/\\()\[\]{}<>]", " ", cleaned)
    for pattern in _RECALL_NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned)
    cleaned = _PRONOUN_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _build_scope_filters(
    *,
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    target_user_id: str | int | None = None,
    strict_scope: bool = False,
) -> List[Dict[str, Any]]:
    active_user_id = str(target_user_id) if target_user_id is not None else str(user_id)
    filters: List[Dict[str, Any]] = [{"term": {"user_id": active_user_id}}]

    if strict_scope or not ALLOW_CROSS_GUILD_USER_CONTEXT:
        filters.append({"term": {"guild_id": str(guild_id)}})

    if strict_scope or not ALLOW_CROSS_CHANNEL_USER_CONTEXT:
        filters.append({"term": {"channel_id": str(channel_id)}})

    return filters


def build_message_window(
    *,
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    target_user_id: str | int | None = None,
    limit_msgs: int = 24,
) -> List[Dict[str, str]]:
    scope_filters = _build_scope_filters(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        target_user_id=target_user_id,
    )
    resp = search_raw(
        {"bool": {"filter": scope_filters}},
        size=limit_msgs,
        source=["role", "content", "timestamp", "user_id", "message_id"],
        sort=[{"timestamp": {"order": "desc"}}],
    )
    hits = [h.get("_source", {}) for h in resp.get("hits", {}).get("hits", [])]
    hits.reverse()
    return [{"role": (h.get("role") or "user"), "content": (h.get("content") or "")} for h in hits]


def build_channel_message_window(
    *,
    guild_id: str | int,
    channel_id: str | int,
    current_user_id: str | int,
    current_display_name: str | None = None,
    exclude_message_id: str | int | None = None,
    limit_msgs: int = 24,
) -> List[Dict[str, str]]:
    """Return indexed shared-channel turns with stable speaker attribution.

    This is intentionally separate from ``build_message_window``. The latter
    remains user-scoped for personal recall/profile behavior; this function is
    only the fallback conversational window when live Discord history cannot
    be read.
    """
    filters = [
        {"term": {"guild_id": str(guild_id)}},
        {"term": {"channel_id": str(channel_id)}},
    ]
    resp = search_raw(
        {"bool": {"filter": filters}},
        size=max(1, min(int(limit_msgs), 50)),
        source=["role", "content", "timestamp", "user_id", "message_id", "reply_to_id"],
        sort=[{"timestamp": {"order": "desc"}}],
    )
    hits = [h.get("_source", {}) for h in resp.get("hits", {}).get("hits", [])]
    hits.reverse()
    current_id = str(current_user_id)
    current_name = " ".join(str(current_display_name or "").split())[:80]
    turns: List[Dict[str, str]] = []
    for source in hits:
        if exclude_message_id is not None and str(source.get("message_id") or "") == str(exclude_message_id):
            continue
        content = str(source.get("content") or "").strip()
        if not content:
            continue
        speaker_id = str(source.get("user_id") or "unknown")
        reply_to = source.get("reply_to_id")
        reply_suffix = f"; reply_to_message_id={reply_to}" if reply_to else ""
        role = str(source.get("role") or "user")
        if role == _ROLE_ASSISTANT:
            labelled = (
                f"[Multivac in shared channel; response_scope_user_id={speaker_id}"
                f"{reply_suffix}]\n{content}"
            )
            turns.append({"role": "assistant", "content": labelled})
            continue
        label = current_name if speaker_id == current_id and current_name else f"user-{speaker_id}"
        turns.append(
            {
                "role": "user",
                "content": (
                    f"[Channel speaker: {label}; user_id={speaker_id}{reply_suffix}]\n"
                    f"{content}"
                ),
            }
        )
    return turns


def _quantize_ago(dt: datetime, now: Optional[datetime] = None) -> str:
    now = now or _now_utc()
    delta = now - dt
    sec = int(delta.total_seconds())
    if sec < 45:
        return "just now"
    if sec < 90:
        return "1m ago"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 18:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def fetch_recent_timeline(
    *,
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    target_user_id: str | int | None = None,
    max_items: int = 12,
) -> List[Tuple[str, str]]:
    scope_filters = _build_scope_filters(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        target_user_id=target_user_id,
    )
    resp = search_raw(
        {"bool": {"filter": scope_filters}},
        size=max_items,
        source=["role", "content", "timestamp"],
        sort=[{"timestamp": {"order": "desc"}}],
    )

    now = _now_utc()
    items: List[Tuple[str, str]] = []
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        ts = src.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            dt = now
        ago = _quantize_ago(dt, now)
        who = src.get("role", "user")
        content = (src.get("content") or "").strip()
        if len(content) > 140:
            content = content[:137].rstrip() + "…"
        items.append((ago, f"{who}: {content}"))
    return items


def build_timeline_prompt_block(
    *,
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    target_user_id: str | int | None = None,
    max_items: int = 12,
) -> str:
    lines = [
        "Use temporal context when helpful. For example, if asked 'when did I say that?', reference how long ago an item occurred using the recent timeline below.",
        f"Current UTC time: {_now_iso()}",
        "",
    ]
    timeline = fetch_recent_timeline(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        target_user_id=target_user_id,
        max_items=max_items,
    )
    if not timeline:
        lines.append("Recent timeline: (no recent messages found)")
        return "\n".join(lines)
    lines.append("Recent timeline (newest first):")
    for ago, pretty in timeline:
        lines.append(f"- [{ago}] {pretty}")
    return "\n".join(lines)


def get_newest_indexed_message_id(*, guild_id: str | int, channel_id: str | int, user_id: str | int) -> Optional[str]:
    scope_filters = _build_scope_filters(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
    resp = search_raw(
        {"bool": {"filter": scope_filters}},
        size=1,
        source=["message_id", "timestamp"],
        sort=[{"timestamp": {"order": "desc"}}],
    )
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return None
    return hits[0].get("_source", {}).get("message_id")


def get_oldest_indexed_message_id(*, guild_id: str | int, channel_id: str | int, user_id: str | int) -> Optional[str]:
    scope_filters = _build_scope_filters(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
    resp = search_raw(
        {"bool": {"filter": scope_filters}},
        size=1,
        source=["message_id", "timestamp"],
        sort=[{"timestamp": {"order": "asc"}}],
    )
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return None
    return hits[0].get("_source", {}).get("message_id")


def fetch_recent_page(
    *,
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    target_user_id: str | int | None = None,
    size: int = 24,
    after: List[Any] | None = None,
    source: List[str] | None = None,
) -> Tuple[List[Dict[str, Any]], List[Any] | None]:
    scope_filters = _build_scope_filters(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        target_user_id=target_user_id,
    )
    body: Dict[str, Any] = {
        "query": {"bool": {"filter": scope_filters}},
        "size": int(size),
        "sort": _SORT_RECENT,
    }
    if after:
        body["search_after"] = after
    if source:
        body["_source"] = source

    client = runtime.client or init_es_client()
    if client is None:
        return ([], None)

    resp = client.search(index=OPENSEARCH_INDEX, body=body)  # type: ignore[attr-defined]
    hits = resp.get("hits", {}).get("hits", [])
    docs = [h.get("_source", {}) for h in hits]
    next_after = hits[-1].get("sort") if hits else None
    return (docs, next_after)


def _parse_relative_time_search(query: str) -> Optional[Dict[str, Any]]:
    q = query.lower()
    now = datetime.now(timezone.utc)
    if "yesterday" in q:
        target_time = now - timedelta(days=1)
        return {
            "gte": (target_time - timedelta(days=1)).isoformat(),
            "lte": (target_time + timedelta(days=1)).isoformat(),
        }

    if "last week" in q:
        q = q.replace("last week", "1 week ago")
    if "last month" in q:
        q = q.replace("last month", "1 month ago")
    if "last year" in q:
        q = q.replace("last year", "1 year ago")

    q = re.sub(r"\b(a|an)\s+(sec|second|min|minute|hour|hr|day|week|month|year)s?\s+ago\b", r"1 \2 ago", q)

    word_to_num = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
    }
    for word, value in word_to_num.items():
        q = re.sub(
            rf"\b{word}\s+(sec|second|min|minute|hour|hr|day|week|month|year)s?\s+ago\b",
            rf"{value} \1 ago",
            q,
        )

    match = re.search(r"(\d+)\s+(sec|second|min|minute|hour|hr|day|week|month|year)s?\s+ago", q)
    if not match:
        return None

    val = int(match.group(1))
    unit = match.group(2)
    if "sec" in unit:
        delta = timedelta(seconds=val)
    elif "min" in unit:
        delta = timedelta(minutes=val)
    elif "hour" in unit or "hr" in unit:
        delta = timedelta(hours=val)
    elif "day" in unit:
        delta = timedelta(days=val)
    elif "week" in unit:
        delta = timedelta(weeks=val)
    elif "month" in unit:
        delta = timedelta(days=val * 30)
    elif "year" in unit:
        delta = timedelta(days=val * 365)
    else:
        return None

    target_time = now - delta
    if "month" in unit:
        window = timedelta(days=10)
    elif "year" in unit:
        window = timedelta(days=30)
    elif "week" in unit:
        window = timedelta(days=4)
    elif "day" in unit:
        window = timedelta(days=1)
    else:
        window = timedelta(minutes=15)

    return {
        "gte": (target_time - window).isoformat(),
        "lte": (target_time + window).isoformat(),
    }


def search_history_for_context(
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    query_text: str,
    target_user_id: str | int | None = None,
    target_role: str | None = None,
    limit: int = 5,
    oldest_first: bool = False,
    strict_scope: bool = False,
) -> str:
    scope_filters = _build_scope_filters(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        target_user_id=target_user_id,
        strict_scope=strict_scope,
    )
    effective_role = target_role or _infer_role_filter(query_text)
    if effective_role in {_ROLE_USER, _ROLE_ASSISTANT}:
        scope_filters.append({"term": {"role": effective_role}})
    if "random" in query_text.lower():
        query = {
            "function_score": {
                "query": {"bool": {"filter": scope_filters}},
                "functions": [{"random_score": {}}],
                "boost_mode": "replace",
            }
        }
        resp = search_raw(query, size=limit, source=["role", "content", "timestamp"], sort=None)
    else:
        time_range = _parse_relative_time_search(query_text)
        query = {"bool": {"filter": list(scope_filters), "must": []}}
        skip_content_filter = any(k in query_text.lower() for k in ["first message", "first thing", "history", "beginning", "start"])
        cleaned = _extract_recall_search_terms(query_text)
        if time_range:
            query["bool"]["filter"].append({"range": {"timestamp": time_range}})
        if not skip_content_filter and cleaned:
            query["bool"]["must"].append({"match": {"content": cleaned}})
        sort_order = "asc" if oldest_first else "desc"
        resp = search_raw(
            query,
            size=limit,
            source=["role", "content", "timestamp"],
            sort=[{"timestamp": {"order": sort_order}}],
        )

    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return ""

    lines = []
    for h in hits:
        src = h.get("_source", {})
        content = src.get("content", "").strip()
        if content:
            lines.append(f"[{src.get('timestamp', '')}] {src.get('role', 'unknown')}: {content}")
    return "\n".join(lines)


def fetch_matches_recent(
    *,
    guild_id: str | int,
    channel_id: str | int,
    user_id: str | int,
    target_user_id: str | int | None = None,
    target_role: str | None = None,
    query: str,
    size: int = 16,
    source: List[str] | None = None,
    strict_scope: bool = False,
) -> List[Dict[str, Any]]:
    scope_filters = _build_scope_filters(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        target_user_id=target_user_id,
        strict_scope=strict_scope,
    )
    effective_role = target_role or _infer_role_filter(query)
    if effective_role in {_ROLE_USER, _ROLE_ASSISTANT}:
        scope_filters.append({"term": {"role": effective_role}})
    cleaned_query = _extract_recall_search_terms(query) or (query or "").strip()

    bool_query: Dict[str, Any] = {
        "filter": list(scope_filters),
        "must": [],
    }
    if cleaned_query:
        bool_query["must"].append({"match": {"content": {"query": cleaned_query, "operator": "and"}}})

    # Use the shared safe search path to avoid bubbling ES 400s into tool failures.
    resp = search_raw(
        {"bool": bool_query},
        index=OPENSEARCH_INDEX,
        size=int(size),
        source=source,
        sort=_SORT_RECENT,
    )

    hits = resp.get("hits", {}).get("hits", [])
    docs = [h.get("_source", {}) for h in hits]
    if docs or not cleaned_query:
        return docs

    # Fallback: loosen matching if strict AND matching produced no results.
    fallback_query = {
        "bool": {
            "filter": list(scope_filters),
            "must": [{"match": {"content": cleaned_query}}],
        }
    }
    resp = search_raw(
        fallback_query,
        index=OPENSEARCH_INDEX,
        size=int(size),
        source=source,
        sort=_SORT_RECENT,
    )
    return [h.get("_source", {}) for h in resp.get("hits", {}).get("hits", [])]


def build_timeline_from_docs(docs: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    now = _now_utc()
    items: List[Tuple[str, str]] = []
    for src in docs:
        ts = src.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            dt = now
        ago = _quantize_ago(dt, now)
        who = src.get("role", "user")
        content = (src.get("content") or "").strip()
        if len(content) > 140:
            content = content[:137].rstrip() + "…"
        items.append((ago, f"{who}: {content}"))
    return items
