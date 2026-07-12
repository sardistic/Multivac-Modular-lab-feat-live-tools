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

### Owner commands

- `/code_propose <request>` records a request against the current commit.
- `/code_patch <id> <attachment>` attaches a UTF-8 `.diff` or `.patch`.
- `/code_validate <id>` runs policy, clean-apply, and Python syntax checks.
- `/code_show <id>` shows request and review state.
- `/code_diff <id>` downloads the stored patch for review.
- `/code_history [limit]` lists recent proposals.
- `/code_approve <id>` records approval after successful validation.
- `/code_reject <id>` records rejection.

These commands use Discord's application-owner check. Patches are capped at
256 KB and 20 files. Binary patches, symlinks, submodules, traversal paths,
credentials, dependency manifests, startup files, Git metadata, and the
self-modification/audit implementation are blocked.

## Next phase: isolated execution and activation

Before approved code can run, add:

1. A separately privileged local supervisor outside the Discord bot process.
2. Container or OS-level isolation with no production secrets and restricted
   filesystem, network, CPU, memory, and time permissions.
3. A system-owned regression suite that proposed patches cannot modify.
4. Signed or hashed build artifacts tied to the approved proposal and SHA.
5. Plugin/subprocess activation, health checks, automatic fallback, and explicit
   rollback records.

The conversation model may describe or request a code change, but it must never
generate, approve, deploy, and restart that change using one shared authority.
