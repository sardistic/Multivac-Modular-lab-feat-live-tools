# youtube_utils.py
import logging
import re
from typing import Optional

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

logger = logging.getLogger("discord_bot")

_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|v/|embed/))([A-Za-z0-9_-]{11})"
)

def extract_youtube_id(url: str) -> Optional[str]:
    m = _YT_ID_RE.search(url)
    if m:
        return m.group(1)
    # fallback: try v= query param manually
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None

def fetch_youtube_transcript(video_id: str) -> Optional[str]:
    """
    Returns a plain-text transcript (no timestamps), or None if unavailable.
    Supports both youtube-transcript-api <1.0 (classmethod get_transcript
    returning dicts) and >=1.0 (instance .fetch returning snippet objects).
    """
    languages = ["en", "en-US"]
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            chunks = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            return " ".join(c.get("text", "").strip() for c in chunks if c.get("text"))
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
        return " ".join(s.text.strip() for s in fetched.snippets if s.text)
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        logger.info("No transcript available for video %s", video_id)
        return None
    except Exception:
        logger.warning("Transcript fetch failed for video %s", video_id, exc_info=True)
        return None
