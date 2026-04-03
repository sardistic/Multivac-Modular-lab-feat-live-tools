from __future__ import annotations

import os

from config import GEMINI_API_KEY

try:
    from google import genai
    from google.genai import types
    from PIL import Image as PILImage

    SDK_AVAILABLE = True
except ImportError:
    genai = None
    types = None
    PILImage = None
    SDK_AVAILABLE = False


def get_gemini_client():
    if not SDK_AVAILABLE or not GEMINI_API_KEY:
        return None

    # google-genai prioritizes GOOGLE_API_KEY when both env vars are present,
    # even if api_key= is passed explicitly. This repo also uses GOOGLE_API_KEY
    # for CSE, so mask it briefly while constructing the Gemini client.
    google_api_key = os.environ.get("GOOGLE_API_KEY")
    restore_google_api_key = bool(google_api_key) and google_api_key != GEMINI_API_KEY
    if restore_google_api_key:
        os.environ.pop("GOOGLE_API_KEY", None)

    try:
        return genai.Client(
            vertexai=False,
            api_key=GEMINI_API_KEY,
            http_options={"api_version": "v1beta"},
        )
    finally:
        if restore_google_api_key and google_api_key:
            os.environ["GOOGLE_API_KEY"] = google_api_key
