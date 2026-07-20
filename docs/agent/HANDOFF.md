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
- Production checkout verified at implementation commit `753ec8fcdb0f5448863a91ca852403997248911e`.
- Rebuilt and restarted only `multivac-multivac-1`; Elasticsearch remained running.
- Post-deploy verification: container started at `2026-07-20T01:01:45Z`, restart count 0, Discord gateway connected, and the bot logged `Bot is online and ready!`.
- Verified `.env` was restored to `root:root` mode `0600` after Compose completed.

## Uncommitted implementation details

None. Provider routing, configuration, usage pricing, tests, and this deployment handoff are committed and pushed.

## Unresolved risks

- No paid live-provider calls were made. Production must have an Anthropic key with access to `claude-sonnet-5`; `ANTHROPIC_INTENT_MODEL` can override the model if needed.
- Existing dependency-version and Python `audioop` warnings remain unrelated to this change.

## Next concrete action

Trigger a controlled OpenAI-outage image request and inspect the single status message plus Gemini result. No paid live-provider request was sent during deployment verification.

## Deployment/status impact

Implementation commit `753ec8f` was pushed to `origin/main` and deployed to the production Multivac container on 2026-07-20. The bot is connected and ready; no rollback was required.
