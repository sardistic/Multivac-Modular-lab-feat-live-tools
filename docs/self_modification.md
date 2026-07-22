# Local Self-Modification Architecture

Multivac's self-modification system starts with versioned, personal behavior
overlays. The checked-in code remains the immutable baseline.

## Implemented: behavior overlays

Each proposal is stored in SQLite with its owner, parent version, source,
timestamps, and lifecycle state. A proposal moves through these states:

```text
draft -> active -> superseded
                 -> rolled_back
```

Only one version can be active for a user. Activating a draft supersedes the
current version. Rolling back marks the current version as rolled back and
restores its recorded parent. The existing `user_instructions` table is retained
as the current-state projection read by the chat hot path; `behavior_changes` is
the version history.

Existing saved instructions are automatically imported as active legacy
versions when the database initializes.

### Discord commands

- `/behavior_propose <instruction>` creates an inert draft.
- `/behavior_propose --clear` drafts a return to baseline.
- `/behavior_show <id>` shows a personal change.
- `/behavior_history [limit]` lists recent versions and states.
- `/behavior_activate <id>` activates a personal version.
- `/behavior_rollback` restores the active version's parent.

Change IDs cannot be accessed or activated by a different user.

## Implemented: reviewed code proposals

Executable change requests use a separate, owner-only review subsystem. It does
not grant the Discord conversation process write access to its own live source.

The current local workflow is:

```text
request -> attach unified diff -> protected-path policy
        -> temporary baseline snapshot -> patch/syntax checks
        -> owner approval or rejection
```

Every request records its exact committed baseline SHA. Validation archives that
baseline to a temporary directory, applies the patch there, and parses changed
Python files without importing or executing them. The temporary directory is
discarded afterward. Approval is an audit decision only: it does not apply the
patch, restart the bot, or deploy anything.

### Natural-language owner workflow

The primary interface is ordinary mention/reply conversation. Requests to
change the bot's own implementation are classified as `code_change`; questions
about how the code works remain normal chat. Any Discord user may submit and
generate one open proposal at a time. Only the Discord application owner may
approve, reject, deploy, or roll back proposals. A proposer may inspect their
own proposal status. For example:

```text
@Multivac change your readme to add a deployment note
@Multivac approve this code change
@Multivac what is the status of that change?
@Multivac roll back that deployment
```

Multivac infers the latest applicable proposal when no proposal number is
given. Code-management commands are excluded from Discord's slash-command
registry; their handlers remain only as an emergency text-admin fallback.

### Administrative fallback

- `/code_propose <request>` records a request against the current commit.
- `/code_generate <id>` selects relevant allowed source at that commit, asks the
  configured OpenAI coding model for a unified diff, applies policy checks, and
  runs static validation. The generated diff is attached for human review.
- `/code_patch <id> <attachment>` attaches a UTF-8 `.diff` or `.patch`.
- `/code_validate <id>` runs policy, clean-apply, and Python syntax checks.
- `/code_show <id>` shows request and review state.
- `/code_diff <id>` downloads the stored patch for review.
- `/code_history [limit]` lists recent proposals.
- `/code_approve <id>` records approval after successful validation.
- `/code_reject <id>` records rejection.
- `/code_deployment <id>` shows activation and health-check results.
- `/code_rollback <id>` queues rollback through the separate host supervisor.

These commands use Discord's application-owner check. Patches are capped at
256 KB and 20 files. Binary patches, symlinks, submodules, traversal paths,
credentials, dependency manifests, startup files, Git metadata, and the
self-modification/audit implementation are blocked.

## Isolated execution and activation

Approved proposals are activated by `ops/proposal_supervisor.py`, which runs on
the Debian host as a separate systemd oneshot service and timer. The Discord
process cannot call it directly.

For each newly approved proposal, the supervisor:

1. Revalidates the stored patch against its exact baseline SHA.
2. Creates a detached worktree under `/srv/multivac-releases`.
3. Runs the full suite in the existing bot image with networking disabled,
   capabilities dropped, and CPU/memory/process limits.
4. Resets the tested worktree and reapplies the stored patch, preventing test
   execution from altering the artifact that will run.
5. Switches only the bot container to that release while bind-mounting the
   persistent SQLite files from `/srv/multivac`.
6. Waits for both Discord-ready and command-sync log markers.
7. Automatically recreates the previous release if activation or health checks
   fail, recording the result in `code_deployments`.
8. Signs the release manifest with a host-only HMAC key, retains the five most
   recent releases, and sends the proposal owner a best-effort Discord DM with
   activation, failure, or rollback results.

Approved proposals containing only standalone `live_tools/*.py` modules take a
no-restart branch after the same revalidation and networkless suite. The
supervisor additionally imports the modules in a restricted container, signs a
content-addressed tool artifact, publishes it through a read-only mount, and
requests an atomic registry activation from the running bot. The bot verifies
the file digests, persists active source records for restart restoration, and
reports the resulting registry generation. Mixed proposals and all other code
continue to use full release activation.

Standalone `live_commands/*.py` proposals follow the same signed artifact path.
The bot loads them through the discord.py Cog lifecycle, synchronizes the global
application-command tree once per batch, persists active sources for restart
restoration, and restores the prior Cogs and tree if setup or synchronization
fails. Tool modules and command modules require separate proposals.

Standalone `live_components/*.py` proposals form a third no-restart channel.
The permanent Discord shell captures an immutable generation and routes message,
reaction, command-error, exact intent, provider, and runtime-setting lookups
through it. Component setup and health checks run before activation; old calls
drain before tasks, Views, clients, and other owned resources are torn down.
Active behavior state persists, and rollback restores the previous artifact.
Each hotload kind requires its own proposal.

The active bot runs as UID/GID 65532 with a read-only root filesystem and
read-only release-code mount. Only `/tmp` and the dedicated `/state` mount are
writable. Git metadata and retained worktrees are mounted
read-only so self-inspection remains available.

Host operations:

```bash
sudo systemctl status multivac-proposal-supervisor.timer
sudo systemctl start multivac-proposal-supervisor.service
sudo /usr/bin/python3 ops/proposal_supervisor.py status
sudo /usr/bin/python3 ops/proposal_supervisor.py rollback
```

Remaining hardening opportunities:

1. Sign release manifests with a host-only key rather than relying solely on the
   recorded patch SHA-256.
2. Move the protected regression tests outside the application repository for
   an additional filesystem-level boundary.
3. Run activated variants as an unprivileged container UID with a read-only code
   mount and a dedicated writable log mount.

The conversation model may describe or request a code change, but it must never
generate, approve, deploy, and restart that change using one shared authority.
