# githerd — Plain-English Multi-Repo Git CLI (Design)

Date: 2026-10-07
Status: Draft for review

## 1. Purpose

A Windows terminal tool. Opened in a directory, it discovers every git repo beneath it and lets the user work with them in plain English. It shows the real git commands being run, live progress, and interactive cards when something needs attention. It uses an LLM (NVIDIA build free tier by default; user-supplied key/provider optional) to interpret requests, draft commit messages and explain results.

## 2. Scope

### Platform and stack
- Windows only (Windows Terminal; `cmd.exe` best-effort).
- Python. Typer (CLI), Textual (TUI), asyncio, pydantic, `keyring`.
- LLM access through two official SDKs behind one neutral interface: `openai` (covers NVIDIA build, OpenRouter, Ollama, OpenAI and any OpenAI-compatible endpoint via `base_url`) and `anthropic` (Claude). No other code imports either SDK.
- Package `githerd`; entry points `githerd` and short alias `gherd` (not bare `herd`, which Laravel Herd uses on Windows). Tagline: "Herd all your git repos with plain English."
- Install via `uv tool install githerd` / `pipx`; PyInstaller `.exe` is a later option.

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
| `llm` | Provider/model-independent LLM layer: neutral types plus `LLMClient` protocol and two adapters (`OpenAIClient`, `AnthropicClient`). The only place SDKs are imported. See §3.1. | openai SDK, anthropic SDK |
| `agent` | LLM loop; sends messages + repo summary; executes tool calls; returns compact outcome summaries. Knows nothing about providers or models; depends only on the `llm` interface. | llm, tools |
| `undo` | Journal of operations; reverses an operation set. | gitops |
| `ui` | Textual app rendering chat, dashboard, cards, diff viewer from `Outcome` and progress events only. | textual |
| `config` | Provider settings; key in Windows Credential Manager. | keyring |

### 3.1 LLM abstraction (provider- and model-independent)

Goal: swapping provider or model is a config change; no agent, tool or UI code changes.

- **Neutral types** (pydantic, defined in `llm/types.py`): `Message(role, content, tool_calls, tool_call_id)`, `ToolSpec(name, description, parameters_json_schema)`, `ToolCall(id, name, arguments: dict)`, `LLMResponse(text, tool_calls, usage, stop_reason)`.
- **Interface** (`llm/base.py`): `class LLMClient(Protocol): async def complete(self, messages, tools, *, max_tokens) -> LLMResponse`. Streaming of text is optional via `stream(...)` yielding text deltas and a final `LLMResponse`.
- **Adapters:**
  - `OpenAIClient(base_url, api_key, model)` uses the `openai` SDK Chat Completions with function calling. Used for NVIDIA, OpenRouter, Ollama, OpenAI.
  - `AnthropicClient(api_key, model)` uses the `anthropic` SDK Messages API with tool use, translating neutral messages (system prompt, `tool_use`/`tool_result` blocks) to and from Anthropic's shape.
- **Provider presets** (`llm/presets.py`), each just a default `provider` + `base_url` + key requirement, resolved to an adapter by the factory:
  - `nvidia`: `openai-compatible`, `https://integrate.api.nvidia.com/v1`, key required (free tier). Default.
  - `ollama`: `openai-compatible`, `http://localhost:11434/v1` (host configurable for a remote Ollama), **no key**. The wizard detects a running Ollama (`GET /api/tags`), lists installed models to pick from, and reports "Ollama not running" with a hint if unreachable.
  - `openai`, `openrouter`: `openai-compatible` with their base URLs, key required.
  - `anthropic`: Claude via the `anthropic` SDK, key required.
  - `custom`: any OpenAI-compatible `base_url` + optional key.
- Local models vary in tool-calling quality. The adapter reports `supports_tools`; when a chosen Ollama model lacks it, githerd warns at setup and falls back to a constrained mode (plain-text intent parsing into the same tools) rather than failing. Running locally also means diffs never leave the machine, so the commit-draft privacy notice is skipped for `ollama`.
- **Factory** (`llm/factory.py`): `build_client(config.llm) -> LLMClient`, chosen by `provider` in config (`openai-compatible` or `anthropic`). Model name is a free string passed through; nothing branches on model name.
- **Capability flags** on the client (`supports_tools`, `supports_streaming`) so the agent can degrade (e.g. no tools: plain-text fallback with a clear message) instead of special-casing models.
- **Error mapping:** each adapter maps SDK exceptions to neutral `LLMAuthError`, `LLMRateLimitError`, `LLMTimeoutError`, `LLMError`, which is all the agent and UI handle.
- **Testing:** the agent is tested with a `FakeLLMClient`; each adapter is tested against recorded/mocked SDK responses to verify the neutral translation both ways.

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

- Before each mutation, `gitops` records `{repo, op, before_head, after_head, timestamp}` in a session journal under `.githerd/` at the working-directory root (repos themselves are untouched).
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

- First-run wizard: choose a preset (NVIDIA free tier, local Ollama, OpenAI, OpenRouter, Anthropic, custom), paste key if the preset needs one, pick model (Ollama: from detected installed models), verify with a test call.
- Config at `%APPDATA%\githerd\config.toml`; key in Credential Manager, never in the file.
- Settings: concurrency, diff size cap, repo-discovery ignore globs, privacy-notice acknowledgement.
- `githerd config` to change provider later.
- Config `[llm]` holds `provider` (`openai-compatible` | `anthropic`), `model`, and `base_url` (openai-compatible only). Default: `provider = "openai-compatible"`, NVIDIA endpoint below. Claude users set `provider = "anthropic"` and a Claude model name.
- NVIDIA endpoint: `https://integrate.api.nvidia.com/v1` (OpenAI-compatible). The model must support reliable tool calling; candidate models are evaluated early (an explicit first implementation task).

## 9. Testing

- `gitops`, `safety`, `repos`, `undo`: tested against throwaway real git repos with local bare remotes (dirty, diverged, conflict, fast-forward), no network.
- `safety`: exhaustive table tests; every destructive pattern must classify as destructive; unknown defaults to destructive.
- `agent`: fake LLM returning scripted tool calls.
- `ui`: Textual pilot harness driven by synthetic progress events.
- One optional, manually run integration test against the real NVIDIA endpoint.

## 10. UI/UX

The UI is the product. It must look clean and intentional in a terminal.

### Rendering model: inline flow
- Behaves like a normal CLI session: header, prompt, responses scroll in the terminal's own scrollback.
- Live regions (the parallel dashboard, progress, spinners) update in place while work runs, then collapse into a static summary left in scrollback (selectable, copyable).
- Cards expand inline beneath the relevant row.
- Only the diff viewer goes full-screen (`d` opens, `q` returns to the flow).
- Implementation: spike Textual inline mode on Windows Terminal first. Fallback: `rich` (Live) for rendering plus `prompt_toolkit` for input, with the diff viewer as a `rich`/`prompt_toolkit` full-screen pager. The spike is the first UI task; the choice is made on real behaviour (flicker, resize, scrollback, input handling), not preference.
- The UI layer renders from `Outcome` and progress events only (see §5), so swapping the renderer does not touch other units.

### Principles
1. Calm by default: mostly neutral text, one accent color for interactive elements. Green/amber/red reserved for status only.
2. Glyphs, not emoji: `✓` ok, `!` needs attention, `✕` failed, `◌` running, `·` queued/idle, `↓` incoming, `↑` outgoing, `●` dirty, `›` prompt. Consistent set; no emoji.
3. Alignment: fixed columns (name, branch, status, detail); truncate with `…`, never wrap rows.
4. Hierarchy by weight: bold for the subject, dim for metadata and commands, normal for content.
5. The real git command is shown quietly (dim) beneath each action's result.
6. Progressive disclosure: summary, then files, then diff. Nothing shown before it is relevant.
7. A one-line key-hint bar is always shown when input is expected (e.g. `d diff · s stash · c commit · k skip`).
8. Motion only while working; finished work is static.
9. Respect the terminal: truecolor with 16-color fallback, honour `NO_COLOR`, adapt layout from 80 to 200+ columns, degrade to plain text when stdout is not a TTY.

### Palette (semantic tokens, not hard-coded colors)
`accent`, `ok`, `warn`, `error`, `dim`, `fg`. Defined once in a theme module with dark and light variants chosen from the terminal background where detectable.

### Screens and components
- **Header:** one line: app name, working directory, repo count, active model.
- **Prompt:** `›` with history and tab-completion of repo names and branches.
- **Live dashboard:** header line with overall `n/total` bar; one row per repo (glyph, name, branch, status/progress, elapsed); the command line in dim at the end.
- **Static summary:** the collapsed dashboard plus a plain-English sentence from the agent.
- **Cards:** attached beneath the row they concern; show the facts (file list with M/A/D/?? status), then the key-hint bar.
- **Diff viewer (full-screen):** file list on the left when width allows, syntax-highlighted diff on the right, unified view on narrow terminals; `j/k` scroll, `n/p` next/previous file, `q` quit.
- **Confirmation prompt (destructive only):** names the exact command and repos affected and requires typing `y` explicitly; the default is No.
- **Commit-draft box:** the proposed message in an editable box with `↵ accept · e edit · r regenerate · esc cancel`.
- **Undo notice:** after any mutation, a dim line `undo available: "undo that"`.

### Behavioural rules
- Never block the prompt on cosmetic work; rendering must keep up with 7+ concurrent repos without flicker (rate-limit redraws).
- Resize mid-run re-flows the live block without corrupting scrollback.
- Ctrl+C cancels in-flight operations cleanly (git subprocesses terminated, partial results reported) and returns to the prompt; a second Ctrl+C exits.

## 11. Open items
- Which NVIDIA-hosted model gives the most reliable tool calling (resolved by a spike at the start of implementation).
- Textual inline mode vs `rich` + `prompt_toolkit` (resolved by the UI spike, §10).
- Exact default for dashboard concurrency (5 assumed; tune with real use).
