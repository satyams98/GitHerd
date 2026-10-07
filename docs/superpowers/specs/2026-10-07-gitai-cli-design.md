# gitai — Plain-English Multi-Repo Git CLI (Design)

Date: 2026-10-07
Status: Draft for review

## 1. Purpose

A Windows terminal tool. Opened in a directory, it discovers every git repo beneath it and lets the user work with them in plain English. It shows the real git commands being run, live progress, and interactive cards when something needs attention. It uses an LLM (NVIDIA build free tier by default; user-supplied key/provider optional) to interpret requests, draft commit messages and explain results.

## 2. Scope

### Platform and stack
- Windows only (Windows Terminal; `cmd.exe` best-effort).
- Python. Typer (CLI), Textual (TUI), asyncio, pydantic, `openai` SDK, `keyring`.
- Install via `uv tool install` / `pipx`; PyInstaller `.exe` is a later option.

### v1 features
**Sync and status**
- Status table across repos: branch, dirty/clean, ahead/behind, last commit.
- Fetch, pull, push on all repos or a named subset.
- Filters: behind, ahead/unpushed, dirty, not on a given branch (computed from repo snapshots, not by the LLM).

**Branching**
- Switch, create, delete branches in one repo or many; list stale/merged branches.

**Committing**
- Stage, commit, amend, unstage/restore; stash, pop, list.
- Commit message: if the user supplies one it is used verbatim (no LLM call, no diff sent). Otherwise the LLM drafts it from the staged diff and the user accepts, edits or regenerates; nothing is committed until accepted.
- Drafting privacy: diff is size-capped; lockfiles, binaries and `.env`-style files are excluded; a one-time notice explains the diff is sent to the configured provider.

**Inspecting**
- Diff (per file), log graph, blame, "what changed since X", history search (`log -S/-G`).

**Conflicts**
- Conflict card: files, then hunks, then ours/theirs/manual or an LLM-proposed resolution the user reviews before it is staged.

**Undo**
- "Undo that" reverses the last operation set (e.g. a 7-repo pull) using the journal (see §6).

**Cross-cutting**
- Follow-up memory: the agent keeps the last result set ("show the diff for that one", "push those").
- Dry-run/preview: "what would pulling do?" via fetch + `log HEAD..@{u}`, no changes made.
- Session log: every command per repo with exit status, exportable.
- No unrequested mutation: no surprise `git add -A`, no auto-stash.

### Out of scope for v1
- v1.5: explain mode, merge/rebase/cherry-pick flows, tags, worktrees/submodules, clone/remote management.
- Later: PRs, CI status, issues via `gh`/GitLab APIs.
- macOS/Linux.

## 3. Architecture

```
TUI (Textual): chat | dashboard rows | cards / diff viewer
        ▲ messages     ▲ progress events      ▲ outcomes
Agent loop (LLM) ─▶ Tool registry ─▶ Git ops layer (async)
                         │                  │
                    Safety gate        Repo discovery
```

| Unit | Responsibility | Depends on |
|---|---|---|
| `repos` | Walk cwd, find `.git` dirs, cache per-repo snapshot (branch, dirty files, ahead/behind, remote). No AI. | git |
| `gitops` | Async git runner; one function per operation; emits progress events; returns typed `Outcome`. | git, pydantic |
| `safety` | Classifies an operation as `read`, `mutate` or `destructive`. Enforced in code. | gitops |
| `tools` | LLM-facing tool schemas: single-repo tools plus bulk tools (`pull_repos`, `fetch_repos`, `status_all`) that drive the parallel dashboard as one call. | gitops, safety, repos |
| `agent` | LLM loop; sends messages + repo summary; executes tool calls; returns compact outcome summaries. Provider-agnostic (`base_url`, key, model). | openai SDK, tools |
| `undo` | Journal of operations; reverses an operation set. | gitops |
| `ui` | Textual app rendering chat, dashboard, cards, diff viewer from `Outcome` and progress events only. | textual |
| `config` | Provider settings; key in Windows Credential Manager. | keyring |

### Hybrid agent design
The LLM decides and converses through tool calls. Common bulk operations are single tool calls that run the whole parallel dashboard and return one summary to the model, so the model reasons only about exceptions (e.g. the dirty repos) rather than every repo.

## 4. Execution and safety policy

| Tier | Examples | Behavior |
|---|---|---|
| read | status, fetch, log, diff, blame | Always immediate |
| mutate | pull, commit, checkout, stash, non-force push, merge | Immediate when the user requested it. Model-proposed follow-ups the user did not ask for (e.g. "stash and pull") require the user to pick them from a card. |
| destructive | push --force, reset --hard, clean, branch -D, rebase, discarding changes | Always confirmed; enforced by `safety`, not the model |

`safety` parses the actual git arguments; an unclassified operation defaults to `destructive`.

## 5. Outcomes and UX

### Outcome union (pydantic)
`Ok`, `UpToDate`, `BlockedDirty(files)`, `Diverged(ahead, behind)`, `Conflict(files, hunks)`, `AuthRequired(remote)`, `NetworkError`, `Failed(stderr_tail)`.

The same objects drive the UI and the LLM summary. Every mutation records `before_head`.

### Dashboard
- One row per repo: spinner, branch, progress bar parsed from `git --progress` stderr, result line. Header shows overall `n/total`.
- Bounded concurrency (default 5). Finished rows remain; failures auto-expand into cards.

### Cards
- Dirty: changed files (M/A/D/??), then diff on request, then stash / commit / skip.
- Diverged: merge, rebase (confirmed), skip.
- Conflict: files, hunks, resolution options.
- Auth: row flips to "needs credentials", TUI suspends (`App.suspend()`) and hands the terminal to git; credentials never touch the LLM or app code. Git runs non-interactively by default (`GIT_TERMINAL_PROMPT=0`); Git Credential Manager handles most auth.
- Keyboard-driven (`d`, `s`, `c`, `k`); every action is also reachable in plain English.
- Diff viewer: scrollable, per file, syntax-highlighted, opens in place.

### LLM context
The model sees compact summaries (e.g. `5 Ok, 1 UpToDate, 1 BlockedDirty(infra: 4 files)`) plus repo snapshots, never raw progress logs. File lists and diffs are sent only when needed to answer a question or draft a commit message.

## 6. Undo

- Before each mutation, `gitops` records `{repo, op, before_head, after_head, timestamp}` in a session journal under `.gitai/` at the working-directory root (repos themselves are untouched).
- "Undo that" reverses the last operation set, restoring each affected repo to its `before_head`.
- Fast-forward pulls, commits and amends: reset to `before_head`. Stashes: popped. Branch switches: switched back.
- If undoing would discard work done after the operation, the normal destructive confirmation applies first.
- Pushes cannot be undone locally; the tool says so and offers `revert`.
- Undo itself is journaled and can be undone.

## 7. Error handling

- All failures become typed outcomes, never stack traces; one repo failing never stops others.
- LLM errors: rate limit/timeout retry with backoff and a visible waiting state; bad key returns to key setup.
- Invalid tool call: one retry with the validation error, then a plain-English fallback.
- Missing `git` or no repos found: clear message and exit.

## 8. Config and first run

- First-run wizard: choose NVIDIA free tier or own provider, paste key, pick model, verify with a test call.
- Config at `%APPDATA%\gitai\config.toml`; key in Credential Manager, never in the file.
- Settings: concurrency, diff size cap, repo-discovery ignore globs, privacy-notice acknowledgement.
- `gitai config` to change provider later.
- NVIDIA endpoint: `https://integrate.api.nvidia.com/v1` (OpenAI-compatible). The model must support reliable tool calling; candidate models are evaluated early (an explicit first implementation task).

## 9. Testing

- `gitops`, `safety`, `repos`, `undo`: tested against throwaway real git repos with local bare remotes (dirty, diverged, conflict, fast-forward), no network.
- `safety`: exhaustive table tests; every destructive pattern must classify as destructive; unknown defaults to destructive.
- `agent`: fake LLM returning scripted tool calls.
- `ui`: Textual pilot harness driven by synthetic progress events.
- One optional, manually run integration test against the real NVIDIA endpoint.

## 10. Open items
- Which NVIDIA-hosted model gives the most reliable tool calling (resolved by a spike at the start of implementation).
- Exact default for dashboard concurrency (5 assumed; tune with real use).
