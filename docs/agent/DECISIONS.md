# Architectural Decisions

## 2026-07-29: Force evidence collection for high-confidence fresh-fact queries

Model-directed research remains the default, but `tool_choice: auto` is not a
freshness guarantee because a model may answer from memory without calling a
tool. Intent classification now has a semantic `chat_research` route for
questions whose answers are likely to have changed, supplemented by a narrow
deterministic safety net for unmistakable freshness, current-role, live-result,
and recurring-winner query shapes.

Research requests force `web_search` only on the first model turn. Follow-up
turns return to automatic tool selection so the model can open relevant pages,
gather enough evidence, and synthesize a normal conversational answer with
source links instead of exposing a raw search-results list. Explicitly dated
historical questions are excluded from the safety net unless the user also asks
for current verification. Gemini's native search fallback follows the same
policy.

## 2026-07-28: Route web lookup through the model's research tool loop

Explicit search wording is no longer a deterministic Discord fast path that
returns provider snippets. Search and URL access are evidence-gathering tools
inside the normal chat generation loop. The model decides when fresh information
is necessary, may search without special trigger words, opens a relevant page
when snippets are insufficient, and synthesizes the final response.

Dedicated URL-summary intent routing remains for direct summary requests, while
URL-bearing analytical questions use the same model-directed page reader as
search follow-ups. Blocking search, fetch, and extraction work runs outside the
Discord event loop.

## 2026-07-21: Hotload model-callable tools through immutable registry snapshots

Tool hotloading is limited to explicit, trusted module activation. Each model
request captures one immutable registry generation containing both schemas and
handlers, preventing a reload from exposing one schema version and executing a
different handler version. Registry mutations use copy-on-write replacement,
bounded per-source rollback history, and explicit override permission.

There is no filesystem watcher. The runtime accepts only relative `.py` paths
beneath `MULTIVAC_TOOL_HOTLOAD_DIR`; the external review/test/signing supervisor
populates that directory and Compose mounts it read-only. This preserves the
rule that bot-writable or user-supplied code is never implicitly executed in
the credential-bearing Discord process.

General core-code hotloading, tool lifecycle hooks, and isolated generated-tool
workers remain separate future decisions.

## 2026-07-21: Publish reviewed tools through a signed host artifact channel

Pure `live_tools/*.py` proposals use the same baseline revalidation, complete
networkless test suite, audited commit, owner approval, and host signing key as
normal releases. The supervisor then publishes only those modules into an
immutable artifact directory mounted read-only at `/tool-artifacts` and sends a
bounded activation batch through `/state/tool-control`.

The bot verifies every requested SHA-256, applies the batch with registry
rollback on failure, and persists active logical source IDs for restoration
after restarts. The supervisor records both previous and activated source state
so proposal-specific rollback remains possible. Live-tool changes must be
standalone proposals; checked-in replacement requires an exact reviewed
`TOOL_OVERRIDES` declaration.

Mixed or non-tool changes retain the existing container recreation path. This
keeps host publication authority outside the Discord process without requiring
a bot disconnect for subsequent reviewed tool updates.

## 2026-07-21: Hotload Discord commands only through managed Cogs

Pure `live_commands/*.py` proposals share the signed host artifact channel but
use a separate persistent request/result stream and active-source projection.
Modules must implement the standard asynchronous discord.py `setup(bot)` entry
point and add commands only through newly registered Cogs. This gives the
runtime an enumerable lifecycle boundary for listeners, prefix commands,
hybrid commands, and application commands.

Command batches perform one global tree sync after all local mutations. Any
setup or sync failure rolls each source back in reverse order and attempts to
resynchronize the restored tree. Active Cogs are restored and synchronized
after process restarts, and proposal audit rows retain exact previous/current
artifact records for rollback.

Direct command-table mutation is not accepted through this channel. A reviewed
module may replace exact checked-in, non-Cog primary command names declared in
`COMMAND_OVERRIDES`; the original command objects are restored on unload or
rollback. Event, intent, and provider changes use managed behavior components.

## 2026-07-22: Hotload behavior through request-scoped component generations

Event, intent, provider, and runtime-setting changes use standalone
`live_components/*.py` modules rather than Python module reload. The permanent
Discord shell captures one immutable registry generation per event; nested
intent, provider, and setting lookups reuse that generation. New events switch
atomically while in-flight work continues with its original callbacks.

Components have an explicit async setup, optional health check and teardown,
tracked task/resource scope, stop signal, bounded drain period, persistent
active-state projection, and per-source rollback history. The supervisor uses a
separate signed behavior request/result stream and accepts behavior-only
proposals. Dependency state, schema migrations, Discord gateway bootstrap,
credentials, and the activation authority remain restart-only.

Reviewed tool and command modules may now replace checked-in symbols only when
they declare exact `TOOL_OVERRIDES` or `COMMAND_OVERRIDES`. This preserves a
recoverable original object for unload and rollback while preventing a module
from expanding its replacement scope implicitly.

## 2026-07-22: Resolve proposal baselines through the canonical branch

The running bot may execute from an immutable detached release while the host
control checkout and canonical branch continue advancing. Code proposals must
therefore record `refs/heads/main` (or the explicitly configured canonical
branch), never the running release's `HEAD`.

The supervisor validates against the same canonical ref and advances its local
copy after every successful promotion. Shared read-only Git metadata makes that
ref immediately visible to the running release without weakening code-mount
immutability. This prevents harmless documentation promotions and earlier
hotloads from making every later proposal stale.

## 2026-07-22: Share the control channel through a private setgid group

The bot runtime and host supervisor use different Unix users. Their control
directory is therefore group-owned by the dedicated runtime state group with
mode `2770`, not a single-owner `0700` directory. Both principals can traverse
and atomically replace channel files, setgid preserves the shared group on new
files, and unrelated host users retain no access.

Supervisor initialization now enforces usable read/write/traverse access and
fails closed for production paths. Restricted validation containers may retain
their disposable `/tmp` ownership exception.

## 2026-07-22: Bound reflection to invocation-consented windows and owner-reviewed proposals

Ongoing product reflection is a local scheduled subsystem, not an always-on chat
participant or autonomous code agent. A user's explicit mention or reply is
consent for a channel-scoped lookback and post-invocation tail. Discord history
is processed ephemerally; persistent reflection state contains only derived
observations, keyed actor hashes, bounded evidence IDs, recent-frequency events,
ideas, and cost/audit records. Operational errors are fingerprinted from
sanitized logging templates so recurrence can be measured without retaining
formatted request arguments.

The host handles scoping, redaction, storage, deduplication, frequency tracking,
budgeting, and read-only code selection. Structured model calls use Flex with a
cheap extractor, strong daily planner, and medium weekly cleanup pass. Automatic
work shares an atomic `$1.50` default daily ceiling and never falls back to a
more expensive tier. Provider failures are retry-throttled.

Ideas are visible only when the owner asks. A separate cheap coding pass may
turn a selected idea into an ordinary reviewable proposal, but reflection has no
approval, signing, activation, deployment, or rollback authority. Eligible
standalone changes retain the existing signed hotload path; all other changes
retain the normal release boundary.

Owner observability exposes only structured, sanitized activity: observation
summaries and counts, anonymized actor totals, run outcomes, thresholds,
schedules, and budget. Raw chat, message/evidence IDs, actor hashes, identities,
source transcripts, and private model reasoning are not part of that interface.

## 2026-07-22: Make reflection sessions idle-driven and incrementally active

An invocation now starts a channel session that closes after five quiet minutes
instead of using a fixed post-invocation tail. Every new human message and each
completed Multivac reply atomically moves that idle deadline and queues one
small, low-reasoning nano-model pulse. This makes the loop responsive throughout
the conversation while preserving the explicit invocation boundary.

Pulse text exists only in a bounded in-memory queue. The model returns a strict,
small structured conclusion; ordinary conversation is discarded, and only a
useful derived observation may be persisted. A final bounded history synthesis
still runs after idle close. Pulses share the existing atomic `$1.50` daily
automatic budget, use Flex only, and stop when the budget is unavailable.
