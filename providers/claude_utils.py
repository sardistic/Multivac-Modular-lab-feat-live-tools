import logging
import re
import anthropic
from typing import List, Dict, Any, Optional

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger("discord_bot")

CLAUDE_MODEL = ANTHROPIC_MODEL

_DATA_URL_RE = re.compile(r"^data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)$")


def _supports_temperature(model: str) -> bool:
    """Fable/Mythos 5 use always-on adaptive thinking and reject temperature."""
    return not model.startswith(("claude-fable-5", "claude-mythos-5"))


def image_input_to_block(src: str) -> Optional[Dict[str, Any]]:
    """Turn one bot-internal image input (data URL or plain http URL — the two
    shapes collect_image_inputs produces) into an Anthropic image block."""
    src = (src or "").strip()
    m = _DATA_URL_RE.match(src)
    if m:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": m.group(1),
                "data": re.sub(r"\s+", "", m.group(2)),
            },
        }
    if src.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": src}}
    return None


def _content_blocks(content: Any) -> List[Dict[str, Any]]:
    """Normalize a message's content (str or block list) to a block list."""
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "text", "text": text}] if text else []
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []

async def generate_claude_response(
    messages: List[Dict[str, Any]], 
    model: str | None = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024
) -> str:
    """
    Generate a response from Claude.
    
    Args:
        messages: List of message dicts (role, content). 
                  Note: Claude API requires 'user' and 'assistant' roles in specific order. 
                  System prompts are passed separately in the API, so we handle extraction here.
        model: The model string to use.
        tools: List of tool definitions (not yet fully implemented in this minimal wrapper).
    
    Returns:
        The text response content.
    """
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY is missing.")
        return "❌ Error: `ANTHROPIC_API_KEY` is not set in the environment."

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    selected_model = model or CLAUDE_MODEL

    # 1. Extract System Prompt(s)
    system_prompt_parts = []
    raw_messages = []
    
    for msg in messages:
        if msg["role"] == "system":
            if msg.get("content"):
                system_prompt_parts.append(msg["content"])
        else:
            raw_messages.append(msg)
    
    system_prompt = "\n\n".join(system_prompt_parts).strip()

    # 2. Sanitize Messages (Claude Strict Rules)
    # - No empty content
    # - Alternating User/Assistant roles
    # - Must start with User
    # Content may be a plain string or a list of Anthropic content blocks
    # (text + image); everything is normalized to block lists.

    sanitized_messages = []

    for msg in raw_messages:
        role = msg.get("role")
        blocks = _content_blocks(msg.get("content"))

        if not blocks:
            continue

        if not sanitized_messages:
            # First message must be user; skip leading assistant messages.
            if role == "user":
                sanitized_messages.append({"role": "user", "content": blocks})
        else:
            prev = sanitized_messages[-1]
            if role == prev["role"]:
                prev["content"].extend(blocks)
            else:
                sanitized_messages.append({"role": role, "content": blocks})

    # Fallback if everything was filtered out
    if not sanitized_messages:
        sanitized_messages.append({"role": "user", "content": "Hello."})

    try:
        # 3. Call API
        kwargs = {
            "model": selected_model,
            "max_tokens": max_tokens,
            "messages": sanitized_messages,
        }
        if _supports_temperature(selected_model):
            kwargs["temperature"] = temperature
        
        if system_prompt:
            kwargs["system"] = system_prompt

        # TODO: Add tool support when needed. 
        
        response = await client.messages.create(**kwargs)

        try:
            from services import usage_costs
            u = getattr(response, "usage", None)
            if u is not None:
                usage = {
                    "prompt_tokens": getattr(u, "input_tokens", 0) or 0,
                    "completion_tokens": getattr(u, "output_tokens", 0) or 0,
                }
                usage_costs.record(
                    selected_model,
                    usage,
                    usage_costs.estimate_cost(selected_model, usage),
                    label="claude_chat",
                )
        except Exception:
            logger.warning("claude usage recording failed", exc_info=True)

        # Fable refusals are successful HTTP 200 responses with no text, not
        # API exceptions. Surface that distinction instead of crashing on an
        # empty content list.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            suffix = f" ({category})" if category else ""
            return f"❌ Claude Fable declined this request{suffix}."

        # Thinking-capable models can return multiple blocks. Only send their
        # user-visible text blocks to Discord.
        text_parts = [
            block.text
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        if text_parts:
            return "\n".join(text_parts)
        return "❌ Claude returned an empty response."

    except anthropic.APIStatusError as e:
        logger.error(f"Claude API Error: {e}")
        return f"❌ Claude API Error: {e.message}"
    except Exception as e:
        logger.exception("Unexpected error calling Claude")
        return f"❌ internal Error: {e}"
