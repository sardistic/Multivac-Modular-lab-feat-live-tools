import logging
import anthropic
from typing import List, Dict, Any, Optional

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger("discord_bot")

CLAUDE_MODEL = ANTHROPIC_MODEL


def _supports_temperature(model: str) -> bool:
    """Fable/Mythos 5 use always-on adaptive thinking and reject temperature."""
    return not model.startswith(("claude-fable-5", "claude-mythos-5"))

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
    
    sanitized_messages = []
    
    for msg in raw_messages:
        content = (msg.get("content") or "").strip()
        role = msg.get("role")
        
        if not content:
            continue
            
        if not sanitized_messages:
            # First message must be user
            if role == "user":
                sanitized_messages.append({"role": "user", "content": content})
            else:
                # If first is assistant, we skip it OR convert it? 
                # Better to skip to avoid confusion, or prepend a dummy user message?
                # Let's skip leading assistant messages for now.
                pass
        else:
            prev_role = sanitized_messages[-1]["role"]
            if role == prev_role:
                # Merge with previous
                sanitized_messages[-1]["content"] += f"\n\n{content}"
            else:
                sanitized_messages.append({"role": role, "content": content})

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
