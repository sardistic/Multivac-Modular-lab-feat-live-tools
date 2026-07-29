# Agent Handoff

## 2026-07-29 stale research-evidence correction (not deployed)

- A live acceptance query reached `chat_research` and forced `web_search`, but
  the model used the vague wording `who won the last world cup`. Google CSE's
  leading results included pages published before the 2026 final, so the model
  incorrectly said the tournament was still ongoing.
- Direct comparison confirmed that the date-aware query
  `who won the last world cup 2026 final result winner` returns the completed
  Spain-Argentina final and Spain's win.
- The first forced OpenAI search now overrides model-generated arguments with a
  deterministic query derived from the user's wording and current UTC date.
  Recurring events add the current year plus `final result winner`; officeholder
  and other dynamic queries add an explicit as-of date. Explicit historical
  years stay historical.
- Subsequent tool turns remain automatic, preserving model-directed page reads
  and synthesis. Research instructions now require interpreting relative dates
  against the current UTC date, rejecting pre-event evidence after a scheduled
  completion date, checking newer authoritative evidence, and linking a strong
  source. Gemini receives equivalent guidance.
- Regression tests cover date-aware query construction, historical-year
  preservation, and forced-argument substitution in both OpenAI API paths.
- Validation: full combined desktop suite passed 197 tests with 7 expected skips
  and 94 subtests. Changed modules compile and `git diff --check` passes with
  only existing line-ending warnings.
- This correction is local only and has not yet been committed, pushed,
  deployed, or restarted. Production remains on `17c9fbae`.

## 2026-07-29 forced freshness research deployment

- Diagnosed the live miss from production logs: `who won the world cup` was
  classified as `chat_light`, routed to `gpt-5.4-mini`, and completed without
  calling the available search tool because automatic tool choice permits zero
  tool calls.
- Added a semantic `chat_research` classifier route with the current UTC date in
  its decision context. A narrow policy fallback promotes unmistakably dynamic
  questions about current officeholders, recurring winners, live results,
  releases, availability, prices, schedules, and explicit freshness or
  verification wording.
- Explicitly dated historical questions remain ordinary chat unless the user
  also asks for fresh verification; this avoids unnecessary searches for stable
  facts such as the 2018 World Cup winner.
- Research requests use the full chat model and force `web_search` on the first
  OpenAI tool turn. Later turns return to automatic selection so the model can
  read relevant pages and produce a seamless answer with strong source links,
  rather than narrating the search or returning a result dump.
- Gemini's native Google Search path now accepts the same forced-freshness signal
  for both direct Gemini requests and fallback handling.
- Regression coverage includes intent promotion and historical exclusions,
  forced tool-choice payloads for both OpenAI APIs, automatic follow-up tool
  selection, dispatcher propagation, and Gemini search activation.
- The combined desktop worktree passed 195 pytest cases with 7 expected skips
  and 94 subtests. The isolated release commit passed 192 pytest cases with 7
  expected skips and 94 subtests, then passed 199 unittest cases with 7 expected
  skips in the production image under no-network, capability-dropped,
  resource-limited constraints. Changed modules compile and `git diff --check`
  passes with only existing line-ending warnings.
- The research and progress changes were isolated from the unrelated
  code-generation confirmation gate, committed as `17c9fbae`, and pushed to
  `origin/main`. The confirmation code and its test remain uncommitted locally.
- Production runs immutable release
  `/srv/multivac-releases/manual-fresh-research-17c9fba`. Only the Multivac bot
  container was recreated; Elasticsearch and persistent state were preserved.
  Discord reached ready state, synchronized 16 commands, and reports zero
  restarts. The container is read-only as UID/GID 65532 and `/app` resolves to
  the exact pushed SHA.
- Live inspection confirmed the `chat_research` classifier and forced initial
  tool-call code are mounted in the running container. No synthetic Discord
  message was sent; the next natural matching question provides the end-to-end
  observation point. The prior
  `/srv/multivac-releases/manual-web-research-94d87a8` release remains the
  immediate rollback target. The deployment event was accepted with HTTP 201.

## 2026-07-28 classic progress-bar restoration deployment

- Restored the pre-`5dd15da` visual style: a 24-cell bar using solid, partial,
  and randomly shaded fade characters instead of the newer 18-cell scan line.
- Removed the pulse glyph, bold label, numeric percentage, and elapsed-seconds
  telemetry from progress messages. Initial response status again uses the old
  compact `[emoji label ░░░░░░░░░░]` presentation.
- Preserved the newer shared progress lifecycle, honest sub-completion estimate,
  callable phase labels, failure indication, rate-limited Discord edits, and
  optional useful detail below the classic bar.
- Updated progress regressions to lock in the classic appearance and exact
  completed bar. Focused progress/chat/video tests passed 17 cases.
- The restoration shipped with forced freshness research in commit `17c9fbae`
  and the immutable `manual-fresh-research-17c9fba` production release.

## 2026-07-28 model-directed web research deployment

- Removed the explicit-search fast path from `discord_bot.py`. Requests containing
  `search`, `look up`, or `news` no longer bypass intent routing and return a raw
  Google CSE result list; they now reach the normal model tool loop.
- Added shared research guidance telling chat models to decide when current,
  changed, niche, uncertain, source-backed, or explicitly verified information
  needs a live search. Search results are treated as leads, and the model is
  instructed to open the most relevant result and synthesize an answer rather
  than returning snippets.
- URL-bearing prompts now tell the model to call `summarize_url` whenever the
  page content matters and never infer page contents from the URL. The URL tool
  description now explicitly covers reading, claim checking, and answering
  questions in addition to summarization.
- Increased the default extracted page context from 3,000 to 6,000 characters,
  bounded it to 1,000-12,000 characters, reports empty extractions explicitly,
  and moved both Google CSE calls and page fetch/extraction off the Discord event
  loop.
- Added regression coverage for a complete search -> source read -> synthesized
  answer sequence, URL-reading instructions, URL extraction success/failure,
  and the revised model-facing schemas.
- Validation: the combined desktop worktree passed 189 tests with 7 expected
  skips and 84 subtests. The isolated commit passed 186 pytest cases with 7
  expected skips and 84 subtests, then passed 193 unittest cases with 7 expected
  skips in the production image under no-network, capability-dropped,
  resource-limited constraints. Changed modules compile and `git diff --check`
  passes with only existing line-ending warnings.
- Search changes were isolated from the unrelated code-generation confirmation
  gate, committed as `94d87a8`, and pushed to `origin/main`. The confirmation
  code and its tests remain uncommitted locally.
- Production runs immutable release
  `/srv/multivac-releases/manual-web-research-94d87a8`. Only the Multivac bot
  container was recreated; Elasticsearch and persistent state were preserved.
  Discord reached ready state, synchronized 16 commands, and reports zero
  restarts. The container is read-only as UID/GID 65532 and `/app` resolves to
  the exact pushed SHA.
- Live tool smoke checks returned two valid Google CSE results and extracted
  readable content from an HTTP page. The prior
  `/srv/multivac-releases/manual-idle-reflection-5dd15da` release remains the
  immediate rollback target. The deployment event was accepted with HTTP 201.

## 2026-07-26 idle reflection and video pricing deployment

- Corrected the video model dropdown and confirmation copy against current
  provider pricing. Veo 3.1 Standard at 720p is now recorded and displayed at
  $0.40/second ($1.60/$2.40/$3.20 for 4/6/8 seconds); Veo 3.1 Fast remains
  $0.10/second.
- Removed the inaccurate claim that this Gemini API path disables audio. Veo
  3.1 pricing includes its native audio output.
- Clarified that the displayed Sora prices are OpenAI's 720p rates and that
  Sora 2 Pro image references can auto-select the supported 1024p tier at
  $0.50/second.
- Added regression assertions for dropdown prices and confirmation text.
- Validation: `python -m pytest tests/test_video_generation_flow.py
  tests/test_gemini_veo_config.py -q` passed 17 tests; changed modules compile;
  `git diff --check` passed with existing line-ending warnings.
- The combined idle-reflection, progress UI, and video-pricing revision was
  committed as `5dd15da`, pushed to `origin/main`, and deployed from immutable
  release `/srv/multivac-releases/manual-idle-reflection-5dd15da`.
- Desktop validation passed 181 pytest cases with 7 expected skips and 84
  subtests. The exact detached release passed 188 unittest cases with 7 expected
  skips in the production image under no-network, read-only,
  capability-dropped constraints.
- Only the Multivac container was recreated; Elasticsearch and persistent state
  remained in place. Discord connected, the bot reached ready state, synchronized
  16 application commands, and reports zero restarts. The container is read-only,
  runs as UID/GID 65532, and mounts the expected immutable release.
- Live inspection confirmed the Veo 3.1 6-second dropdown entry reports `$2.40`.
  The prior `/srv/multivac-releases/manual-reflection-97e52be` release remains
  available for immediate rollback.

## Active objective

Observe the deployed fresh-research route on natural Discord queries while
preserving the production-verified hotload runtime. Keep the unrelated
code-generation confirmation gate local until it receives separate release
approval.

## Idle-driven live-session implementation (deployed)

- Replaces the fixed post-invocation tail with a five-minute sliding idle
  deadline. Every human message in the active channel and each
  completed Multivac reply extends the session; temporary progress messages and
  other bots do not.
- Queues one bounded in-memory nano/low/Flex pulse per message. Pulses return
  strict structured conclusions, persist only useful derived observations, and
  share the existing atomic `$1.50` daily automatic budget. Raw surrounding
  message text is not added to the reflection database.
- Keeps the final bounded Discord-history synthesis after idle close, selecting
  the most recent 100 messages for very long sessions. SQLite evidence IDs are
  bounded to 200 per session.
- `/reflection_status` and `/reflection_activity` now expose live-session and
  pulse-queue counts plus the five-minute idle schedule without exposing
  transcripts or private model reasoning.
- Reworked the shared response progress UI into a deterministic breathing scan
  with elapsed time, phase summaries, and pseudo-progress capped below
  completion until the underlying task finishes. The shared path covers chat,
  provider, image, video, weather, URL, and multimodal work; natural code
  proposals, `/code_generate`, and `/reflection_propose` now use it for their
  drafting and validation phases. Background reflection stays channel-silent.
- Targeted reflection validation passes 19 tests and progress rendering passes
  3 focused tests. The full desktop suite passes 180 tests with 7 expected skips
  and 84 subtests; changed modules compile and `git diff --check` passes.
- These changes touched core Discord wiring and were deployed through the normal
  immutable release/container recreation path.

## Deployed reflection implementation

- Added an invocation-consented background reflection worker with a 10-minute lookback and
  20-minute post-invocation Discord history tail. Surrounding channel text is
  fetched ephemerally, role-anonymized, redacted, capped, and not copied into the
  reflection database; a strict requester-and-reply Elasticsearch fallback is
  used when Discord history is unavailable.
- Added separate SQLite state for consent, invocation sessions, hashed-actor
  observations, recent-frequency events, evidence-backed ideas, model runs, and
  atomic daily budget reservations. Withdrawal during an in-flight extraction
  fails closed, removes user-derived state, and supersedes ideas that lose their
  final evidence.
- Added sanitized `ERROR`/`CRITICAL` template fingerprinting. Dynamic logging
  arguments are discarded, credential/identity-like values are redacted, and
  only the last seven days of occurrence events qualify an error as frequent.
- Split work across local host orchestration plus Flex model tiers: nano/low
  extraction, sol/high daily planning with read-only code context, luna/medium
  weekly idea cleanup, and mini/low owner-requested code drafting. Automatic
  model work has a shared `$1.50` daily reservation ceiling and retry throttling.
- Added `/reflection_off`, `/reflection_status`, `/reflection_activity`,
  `/reflection_ideas`, and `/reflection_propose`. Activity is owner-only and
  exposes sanitized derived observations, run outcomes, thresholds, and schedule
  without transcripts, identities, evidence IDs, actor hashes, or private
  reasoning. Ideas remain inert until the
  owner requests a normal proposal; validation, approval, signing, hotload or
  release deployment, and rollback remain in the existing gated pipeline.
- Added 30-day terminal-session retention, 90-day operational metadata
  retention, release Compose defaults, operator documentation, and an
  architectural decision record.
- Validation: changed modules compile; desktop suite is 172 passed, 7 skipped,
  84 subtests passed. The networkless, capability-dropped production-image gate
  passed 179 unittest cases with 7 expected skips. `git diff --check` passed.
- This feature changes core Discord wiring, creates a persistent SQLite schema,
  and adds global application commands. Its first activation requires a normal
  release/container recreation and Discord command-tree sync; that activation
  completed successfully. Later generated standalone ideas may be hotloadable
  under the existing contracts.

## Completed work

- Added immutable, request-scoped tool snapshots and digest-verified standalone
  tool loading with atomic activation, persistence, unload, and rollback.
- Added managed Discord Cog activation with one tree sync per batch, sync-failure
  rollback, restart restoration, and original-command restoration.
- Added exact `TOOL_OVERRIDES` and `COMMAND_OVERRIDES` declarations. Reviewed
  modules can replace checked-in symbols only when the declared scope matches;
  originals return on failure, unload, or rollback.
- Added a behavior component runtime for event, intent, provider, and runtime
  setting namespaces. Each Discord event captures one immutable generation, and
  nested routing/provider/setting lookups reuse it.
- Routed the message, raw-reaction, and command-error event shell, exact intent
  dispatch, principal OpenAI/Gemini/Claude chat paths, image/vision paths, and
  Sora/Veo entry points through the behavior registry.
- Added async component setup and health checks, old-generation request draining,
  stop signals, tracked task cancellation, tracked client/View cleanup, teardown,
  bounded history, persistent active state, and rollback.
- Added a third persistent supervisor stream for `live_components/*.py`, plus
  isolated validation, signed content-addressed artifacts, no-restart activation,
  rollback, audit state, and retention alongside tool and command artifacts.
- Protected all tool, command, and behavior activation-authority/control/validator
  files from generated proposals. Each hotload kind must be a standalone proposal;
  mixed hotload kinds and core files are rejected.
- Added owner-only local controls for tools, commands, and behaviors; direct
  mutation is disabled whenever supervisor control is configured.
- Documented all three module contracts, lifecycle rules, override declarations,
  security boundaries, restart-only boundaries, and architectural decisions.
- Fixed proposal baseline resolution so a detached running release reads the
  shared canonical branch, and successful promotion advances that local ref.
- Corrected the control channel from runtime-only `0700` ownership to a private
  `2770` setgid directory shared by the deploy supervisor and runtime group.
- Activated the first owner-approved production tool proposal, `falcon-juniper`,
  through the signed no-restart channel as registry generation 2.
- Invoked its `hotload_deployment_smoke` handler through Discord, received the
  exact expected JSON result, then rolled proposal 11 back as generation 3.
- Activated owner-approved command proposal `mango-wren` as command generation
  1, adding the managed `DeploymentSmokeCommands` Cog without a restart.
- Invoked `/hotload_command_smoke` as a native Discord application command,
  received the expected JSON result, then rolled proposal 12 back as generation 2.
- Activated owner-approved behavior proposal `river-mango` as behavior generation
  1, publishing the `deployment.smoke` runtime setting after setup and healthcheck.
- Rolled proposal 13 back as behavior generation 2, running teardown and removing
  the setting without warnings, a process restart, or a gateway reconnect.

## Current behavior

- `live_tools/*.py` changes model-visible schemas and handlers atomically.
- `live_commands/*.py` changes managed Cogs and synchronizes the Discord command
  tree. Explicit declarations can temporarily displace checked-in direct commands.
- `live_components/*.py` changes exact events, intents, provider entry points,
  and settings. New events see the new generation while prior events finish on
  the old generation before its resources are torn down.
- Activation requests contain exact artifact-relative paths and SHA-256 digests.
  Active sources survive later full releases and process restarts.
- Setup, validation, health, batch activation, or Discord sync failures preserve
  or restore the prior persistent projection.
- Dependencies/native modules, database migrations, Discord token/intents,
  logging/event-loop bootstrap, mounts, the gateway shell, and hotload authority
  remain normal release-and-restart changes by design.

## Validation performed

- Compiled every changed runtime, control worker, validator, routed bot module,
  supervisor, and `discord_bot.py` with `python -m py_compile`.
- Behavior tests cover exact override authorization, immutable settings, reload,
  rollback, state restoration, failed-batch rollback, owned-task shutdown, and an
  in-flight old request draining after new traffic switches generations.
- Command tests cover built-in displacement/restoration and Discord tree-sync
  rollback. Tool tests cover declared built-in replacement and restoration.
- Supervisor/policy tests cover behavior-only detection, isolated branch routing,
  protected authority files, and mixed-kind rejection.
- Full suite: 153 passed, 7 skipped, 84 subtests passed.
- Production-image gate: 160 unittest cases passed with 7 expected skips in a
  networkless, read-only, capability-dropped container.
- Live verification: the container runs commit `3f71745` with zero restarts;
  Discord reached ready state and synchronized 11 application commands.
- `/app` and `/tool-artifacts` are read-only mounts, artifact content is readable
  by UID 65532, and `/state/tool-control` is writable by UID/GID 65532.
- The proposal-supervisor timer is enabled and active; its latest service result
  and the dashboard service result are successful. Both run as `sardistic` with
  supplementary state group 65532.
- Production has zero approved proposals awaiting deployment. Existing approved
  proposals all have recorded release deployments, so no live channel can be
  exercised without a new owner-reviewed proposal.
- Regression coverage creates a detached release whose `HEAD` differs from
  `main` and proves proposal creation still records the canonical commit. It also
  verifies supervisor validation and promotion use and advance that same ref.
- Expanded full suite: 156 passed, 7 skipped, 84 subtests passed.
- Corrected production-image gate: 163 unittest cases passed with 7 expected
  skips in the networkless, read-only, capability-dropped container.
- Live verification at `290599d`: Discord reached ready state, synchronized 11
  commands, and retained zero restarts. From inside the detached container,
  `get_baseline_sha()`, `HEAD`, and `refs/heads/main` all resolved to `290599d`.
- After the second tool smoke proposal failed closed on control-directory access,
  production permissions were repaired without restarting the bot and verified
  writable by both the `sardistic` supervisor identity and runtime UID 65532.
- Expanded suite after the shared-control regression test: 157 passed, 7
  skipped, 84 subtests passed.
- Shared-control production-image gate: 164 unittest cases passed with 7
  expected skips. An explicit systemd reconcile then succeeded as `sardistic`,
  preserved mode `2770`, and both supervisor and runtime access checks passed.
- Promoting the host-only fix did not recreate the Discord container: its start
  timestamp and zero restart count remained unchanged while its canonical
  baseline advanced to `b9839c0`.
- `falcon-juniper` passed revalidation, the networkless suite, artifact signing,
  digest verification, registry activation, health checking, and canonical Git
  promotion. The active SHA-256 matches the immutable artifact and audit row.
- Tool activation did not recreate or restart the container. Its original start
  timestamp remains unchanged, and no new Discord gateway connection appeared;
  the only activation log records generation 2.
- Proposal-specific rollback emptied `active-tools.json`, recorded an explicit
  generation-3 unload, marked the deployment `rolled_back`, retained the signed
  immutable artifact, and caused no restart or gateway reconnect.
- `mango-wren` passed the signed command channel and global tree sync. Its
  artifact digest and audit row match, `active-commands.json` records proposal
  12, and Discord reports 12 synchronized application commands.
- Command activation left the container start timestamp and zero restart count
  unchanged; no new Discord gateway connection was logged.
- Command rollback emptied `active-commands.json`, synchronized the global tree
  from 12 back to 11 entries, removed the smoke command from Discord's global
  API, marked the audit row `rolled_back`, and retained the signed artifact.
- Native invocation, propagation, and rollback caused no container restart or
  gateway reconnect.
- `river-mango` passed revalidation, networkless testing, artifact signing,
  digest verification, setup, healthcheck, atomic setting publication, and
  canonical Git promotion. Generation 1 reported no warnings.
- Behavior rollback emptied `active-behaviors.json`, recorded a warning-free
  generation-2 unload, marked the audit row `rolled_back`, retained the signed
  immutable artifact, and caused no restart or gateway reconnect.
- `git diff --check` passed; Git emitted only LF/CRLF normalization warnings.
- Existing dependency-version and Python `audioop` warnings remain.

## Implementation details

The complete tool, command, and behavior hotload stack, deployment-gate fixes,
canonical-baseline fix, and shared-control fix are pushed through `b9839c0`. The
supervisor keeps SQLite/control state under `/tmp`
inside networkless, capability-dropped validation containers while retaining
fail-closed ownership enforcement for production state. The production-image
gate then exposed shared outer state in the dynamically imported supervisor test
fixture; its state root is now explicitly isolated per test.
The tool, command, and behavior guides now accurately distinguish the deployed
infrastructure from the still-pending end-to-end proposal smoke tests.
The canonical-baseline fix and its regression tests are deployed.
The supervisor's persistent initialization contract now preserves the shared
setgid control-directory permissions; this host-side fix does not require a bot
restart.
That persistence fix and its regression test are deployed in the host control
checkout; the live directory is repaired and verified.
Canonical commit `177311a` adds `live_tools/deployment_smoke.py`; its handler
returned the requested static smoke result while active. The source is now
unloaded, while its signed artifact remains retained for audit and restoration.
Canonical commit `39bd3e3` adds `live_commands/deployment_smoke.py`; its managed
Cog and hybrid command were invoked successfully and are now unloaded. The
signed command artifact remains retained for audit and restoration.
Canonical commit `0ee9530` adds `live_components/deployment_smoke.py`; its setup,
healthcheck, generation-scoped setting, teardown, and rollback all completed in
production. The source is unloaded and its signed behavior artifact is retained.

## Unresolved risks

- All hotloaded modules are trusted code executing inside the credential-bearing
  bot process. Review and isolated validation reduce risk but do not sandbox them.
- Lifecycle cleanup is enforceable only for resources registered through the
  component context; a trusted module can still create unmanaged global state.
- Provider overrides must preserve the documented fallback call signature.
- A component replacing the full `message` event owns command pass-through and
  must call `bot.process_commands` when appropriate.
- Discord command activation depends on external global tree sync availability
  and rate limits; failed sync is locally rolled back and followed by restorative
  sync, but the flow has not been exercised against live Discord.
- The complete tool, command, and behavior lifecycles are proven in production,
  including invocation or publication, teardown, rollback, artifact retention,
  and continuity of the gateway process.
- Database schema/data changes remain restart-only and require forward-compatible
  migrations; code rollback alone cannot reverse persisted mutations safely.
- Reflection's first real extraction, planning, and cleanup model calls have not
  yet occurred in production. Budget, model configuration, persistent schema,
  worker startup, and command synchronization are verified, but owner command
  invocation and a complete 20-minute observation window remain live smoke tests.

## Next concrete action

After explicit release approval, commit and deploy the pending idle-driven
reflection changes as a normal immutable release, then invoke Multivac and watch
`/reflection_activity` show message-level `pulse` runs, a sliding live-session
count, and final extraction after five quiet minutes. Keep
`/srv/multivac-releases/manual-reflection-97e52be` as the immediate rollback
release.

## Deployment/status impact

Deployed on 2026-07-22. Production runs commit `290599d` from
`/srv/multivac-releases/manual-baseline-fix-290599d`. The immediately prior
`3f71745` release remains at `/srv/multivac-releases/manual-hotload-0c5e723`,
with `27f18e7` also preserved at the earlier rollback worktree. The host
supervisor includes the `b9839c0` permission fix, and the active tool proposal
promoted canonical commit `177311a`. Proposal 11 is now rolled back at runtime;
all promotions and its unload left the Discord container on immutable core
release `290599d` without a restart. Active command proposal 12 promoted
canonical commit `39bd3e3` and likewise caused no restart. Proposal 12 is now
rolled back at runtime and Discord's global tree is restored to 11 commands.
Behavior proposal 13 promoted canonical commit `0ee9530`, published its smoke
setting at generation 1, and rolled back at generation 2. All three hotload
channels are production-verified end to end.

On 2026-07-22, bounded reflection commit `97e52be` was pushed to `origin/main`
and deployed from `/srv/multivac-releases/manual-reflection-97e52be`. The
networkless production-image gate passed before activation. The recreated bot
reports Discord ready, zero restarts, and 16 synchronized application commands,
including all five reflection commands. `/app` is the expected read-only release
mount; reflection is enabled with the `$1.50` cap, the persistent SQLite database
exists, configured model tiers are nano/sol/luna, and initial queues and spend are
zero. `/srv/multivac-releases/manual-baseline-fix-290599d` remains intact as the
immediate rollback release. The canonical deployment event was reported with
HTTP 201. After publishing the deployment handoff, the shared canonical `main`
ref was atomically fast-forwarded and the running bot resolved that newest
baseline without a container restart.

On 2026-07-26, the combined idle-driven reflection, shared progress UI, and
video-pricing revision was pushed as `5dd15da` and deployed from
`/srv/multivac-releases/manual-idle-reflection-5dd15da`. The exact release passed
the production-image gate before activation. The recreated bot reached Discord
ready state, synchronized 16 application commands, retained its persistent
state, and reports zero restarts. The prior
`/srv/multivac-releases/manual-reflection-97e52be` release remains intact as the
immediate rollback target.
