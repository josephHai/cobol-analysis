# Entry point for AI coding tools (Claude Code, Cursor, Copilot Workspace, …).
# Read this first, then follow the links. Keep this file short — it is a map, not a manual.

## What this project is

A platform that analyses COBOL legacy systems and produces deliverables: a Functional
Design Document, a test case workbook, and a data mapping workbook. A FastAPI service
runs a `deepagents`-based agent pipeline against a pinned git revision; a React console
drives it and exposes the results through a knowledge base.

## Read these, in this order

| Document | Why |
|---|---|
| `docs/CODESTYLE.md` | **Binding rules.** Layering, naming, security hard rules (§5), demo simplification clauses (§6) |
| `CONTRIBUTING.md` | Repository layout, contracts-first workflow, PR gates, demo sprint mode (§7) |
| `docs/DESIGN.md` | Architecture, verified `deepagents` capabilities, git model, security design |
| `docs/DEMO.md` | How to run the demo, and the current list of known gaps |

## Non-negotiables

1. **The agent never executes shell commands and never runs git.** Git is server-side
   code in `app/services/git_service.py`. Do not implement `SandboxBackendProtocol` on
   any backend, do not enable `LocalShellBackend`, do not add an `execute` tool.
2. **Credentials never appear** in a URL, `.git/config`, argv, a log line, an event, a
   prompt, or an artifact. PATs are injected via `GIT_ASKPASS`; log output goes
   through `mask_secrets()`. The host's own credential helper is cleared for our git
   invocations, so a cached credential cannot silently replace the injected one.
3. **Every file path is validated at the boundary.** `FilesystemBackend(virtual_mode=True)`
   plus `resolve()`/`relative_to(root)` on the server side.
4. **`/repo/**` is read-only** and credential-shaped files are never read.
5. **Git arguments are validated, not escaped.** Refs go through `git_service.validate_ref`
   before reaching git, and `_git()` is the single place any git command is built — it carries
   the hardening (hooks disabled, no system config, no prompts, masked output) for every caller.
6. Layering is one-directional: `api → services → infra → domain`, and `agent → services`.
   `app/agent/**` must not import `app/infra/**`.
7. **Contracts first.** Boundary changes start in `contracts/`, then regenerate types.

## Conventions that trip people up

- **Language is English** everywhere: identifiers, comments, docstrings, log and error
  messages, commits, docs, and generated artifacts.
- Error messages carry three things: what happened, what to do next, and a trace ID.
- Every external call (HTTP, subprocess) sets a timeout. A call without one is a defect.
- **Procedures live in `backend/skills/<skill>/SKILL.md`, not in prompts.** A prompt carries
  run parameters and a pointer to the skill file. Never duplicate a procedure into prompt
  text: that creates two sources of truth, and the drift is silent.
- Prompt interpolations use stable `KEY=value` markers (`RUN_ROOT=`, `ENTRYPOINT=`,
  `SKILL=`, `SKILL_FILE=`, `LOCALE=`); diagnostics depend on them.
- Skill loading needs **both** halves wired: `create_deep_agent(skills=[...])` *and* a
  backend route that can resolve the source path. Missing either yields an agent that
  silently runs with no skills. `build_backend_with_skills()` does both; keep its test.
- **The skills are replaceable content; the pipeline is the fixed part.** Four code-side
  contracts bind them: the four directory names, `SKILL.md` frontmatter `name` matching the
  directory, the analysis skill writing to the `ANALYSIS_FILE=` path and reporting
  `progress.entry_point`, and the deliverable reply shapes (`markdown` for `fdd`, a
  `{"rows": [...]}` JSON block for the other two — see `ARTIFACT_SPECS[...]["accepts"]`).
  `python scripts/dev.py skills-check` verifies all of it; run it after any skill edit.
- **Verified `CompositeBackend` semantics:** it strips the *entire* route prefix, so the
  backend root must be chosen so that `root + remainder` is the intended path. A route of
  `/analysis/` pointed at the run root lands files at `<run>/analysis.json`, not
  `<run>/analysis/analysis.json`.
- Fan-out uses `asyncio.gather(..., return_exceptions=True)`: one failing branch must
  never take down the others.
- **Never walk the whole checkout.** Repositories are customer source and can be gigabytes; an
  eager index costs minutes per run and was removed for exactly that reason (`docs/DESIGN.md`
  §2.7). Locate code lazily by searching for what the request names, and bound every read with
  `MAX_SLICE_LINES` / `MAX_TOOL_CALLS`. A future optimisation must be demand-driven and scoped,
  and needs an ADR.

## Configuration

Settings come from `app/core/config.py` only — never read `os.environ` elsewhere
(`main.py` was doing so for `LOG_LEVEL`, which silently made that setting a no-op).

**Environment variable names are unprefixed**: `DATA_ROOT`, `LLM_MODEL`, `AUTH_MODE`, …
`COBOL_ENV_PREFIX` restores namespacing for hosts that need it. Four names are still
deliberately specific because a collision would be silent and harmful:

| Variable | Why the name is not generic |
|---|---|
| `LLM_AUTH_TOKEN` | The neutral name this project expects; the credential resolution order is documented under Model wiring |
| `OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | Compatibility fallbacks only, for a host whose key is already provisioned under a tool-specific name |
| `COBOL_ASKPASS_TOKEN` | Set and consumed inside one askpass script; a bare `GIT_PAT` could pick up an unrelated host variable and supply the wrong credential |
| `*_PAT` in `repos.yaml` | Names come from the operator's secret store, not from us |
| `COBOL_ENV_PREFIX` | Must be readable *before* the prefix is applied |

Adding a setting means adding it to `Settings` **and** to `.env.example`; a name that
appears in one and not the other is a defect.

## Authentication

The console has no login step and holds no credential. Identity is attached by the proxy in
front of the service — the Vite dev proxy (`vite.config.ts` sets the header) or the SSO gateway
— and the server reads it in `AUTH_MODE=trusted_header`. `core/auth.py:authenticate` is the
single decision point; routes only name a permission level, so the SSO integration
(`AUTH_MODE=e2e_token`, reserved and returning 501) will not touch a route or the client.

Two rules that are easy to break by accident:

- `trusted_header` trusts an unverified header, so it is safe **only** when the port is
  unreachable except through that proxy. `main.py` warns at startup; fix the network, do not
  silence the warning.
- **Never accept a credential in a query string.** The SSE escape hatch was removed for exactly
  that reason once the client stopped sending credentials.

## Model wiring

`LLM_PROVIDER` selects the wire protocol and `build_model()` in `app/agent/graph.py` is the only
reader. **No vendor is named anywhere in this project** — `LLM_BASE_URL` and `LLM_MODEL` have no
defaults and a `model_validator` refuses to start without them, because a baked-in default would
be wrong for every other deployment and a silently empty one would surface as an opaque
connection error mid-run.

| Provider | Endpoint | Notes |
|---|---|---|
| `openai` (default) | `POST /v1/chat/completions` | the common dialect; endpoint and model come from configuration |
| `anthropic` | `POST /v1/messages` | for a gateway that speaks only that dialect; responses may carry `thinking` blocks, which `pipeline._text_delta` filters out |

The credential is resolved through `Settings.resolve_api_key()`, in this order: `LLM_API_KEY_ENV`
(explicit, wins), `LLM_AUTH_TOKEN` (the neutral name), then `OPENAI_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` as compatibility fallbacks for a host whose key is already provisioned
under a tool-specific name. Never log the key; never commit it.

## Platform

The project runs on Linux, macOS and Windows. Rules that matter when adding code:

- **No `shell=True` and no `.sh` files from application code.** Windows has no `/bin/sh`.
- **Never hand-build a virtualenv path.** Use `scripts/dev.py:venv_python()`; it is
  `bin/python` on POSIX and `Scripts/python.exe` on Windows.
- **Tasks are defined once in `scripts/dev.py`.** There is no Makefile: GNU Make is absent from
  Windows, and bootstrapping already requires Python, so it could only forward. Run
  `python scripts/dev.py` to list tasks, and `--print` to see the command without running it.
- **A control the platform cannot enforce must degrade loudly.** `chmod` does not implement POSIX
  permissions on NTFS, so read-only checkout sealing is unavailable on Windows; `seal_worktree`
  warns and returns instead of claiming success.
- The single deliberate exception is `GIT_ASKPASS`, which git runs through a shell and therefore
  needs platform-specific quoting — `git_service._quote_command`, covered by tests.

## Commands

```bash
python scripts/dev.py               # list every task
python scripts/dev.py install       # venv + dependencies
python scripts/dev.py api           # API + agent worker on :8000 (live gateway by default)
python scripts/dev.py web           # console on :5173
python scripts/dev.py dev           # API + web together in one terminal
python scripts/dev.py check         # ruff format --check, ruff check, pytest,
                                    # contract drift, skills  ← pre-commit gate
python scripts/dev.py skills-check  # verify the skills directory against this build
python scripts/dev.py e2e           # integration test; needs a credential, skips loudly without
python scripts/dev.py gc            # reclaim disk from expired runs
```

## When you are unsure

- Prefer following an existing file's pattern over inventing a new one — `git_service.py`
  and `events.py` are the reference implementations for services.
- If a rule in `CODESTYLE.md` genuinely blocks the work, **append** a rule with the
  rationale and tell the user; do not silently deviate.
- If you must leave something incomplete, mark it `# TODO(demo):` and register it in
  `docs/DEMO.md`. An unregistered TODO is treated as an unanswered review comment.
