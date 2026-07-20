# Agent Handoff

## Active objective

Make provider outages degrade cleanly: OpenAI image failures must try Gemini, OpenAI intent-classifier failures must try Claude Sonnet, and chat/Gemini tool fallbacks must not loop or emit raw tool calls.

## Completed work

- Added unconditional OpenAI-image to Gemini-image fallback, including billing/quota and empty-result failures.
- Added `ANTHROPIC_INTENT_MODEL` with `claude-sonnet-5` as the default secondary intent classifier.
- Bounded chat failover to one provider transition and reused the existing Discord status message.
- Preserved Gemini function-call `thought_signature` data during tool follow-ups.
- Stopped undeclared Gemini function calls from being rendered to Discord as raw pseudo-tool text.
- Added Sonnet 5 pricing to the usage ledger.
- Added regression coverage for all four paths.

## Current behavior

- Default image generation tries GPT Image once and then Gemini 3 Pro Image once when GPT Image returns no result or raises a moderation error.
- Intent classification tries OpenAI first, then Sonnet, then the deterministic keyword router only if both APIs fail.
- OpenAI chat outage fallback tries Gemini once. An empty Gemini response terminates with one user-facing error instead of returning to OpenAI.
- Gemini Elasticsearch tool follow-ups retain the provider-supplied signed call part.

## Validation performed

- `python -m py_compile config.py bot/chat_handler.py providers/openai_intents.py providers/stability_generation.py providers/gemini_text.py services/usage_costs.py`
- `python -m pytest -q`
- Result: 123 passed, 7 skipped, 72 subtests passed.
- `git diff --check` passed.

## Uncommitted implementation details

Modified provider routing, configuration, usage pricing, and tests. New tests are `tests/test_chat_fallback.py` and `tests/test_gemini_tool_followup.py`.

## Unresolved risks

- No paid live-provider calls were made. Production must have an Anthropic key with access to `claude-sonnet-5`; `ANTHROPIC_INTENT_MODEL` can override the model if needed.
- Existing dependency-version and Python `audioop` warnings remain unrelated to this change.

## Next concrete action

Review the diff, commit it, deploy the Multivac container, then trigger a controlled OpenAI-outage image request and inspect logs/status-message behavior.

## Deployment/status impact

Not committed, pushed, or deployed. Production behavior is unchanged.
