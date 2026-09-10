# Code Style & Engineering Constraints

> Version: v1.0 — the framework is frozen during the demo sprint; rules may be appended, hard rules may never be bypassed.
> Applies to: `backend/` (Python), `frontend/` (TypeScript/React), `contracts/` (contracts)
> See also: `CONTRIBUTING.md` (repository & process), `docs/DESIGN.md` (architecture), `CLAUDE.md` (entry point for AI coding tools)

Every rule in this document is **checkable**. Anything stated as a rule must be decidable by a command, a test, or a code review.
Aspirations that cannot be verified ("code should be elegant") do not belong here.

---

## 0. First principle: three levels of priority

When rules conflict, resolve in this order:

1. **Security hard rules (§5)** — never bypassed, there is no "just for now".
2. **Runnable and deliverable** — during the demo sprint, prefer getting the pipeline working; mark unfinished parts with `# TODO(demo):`.
3. **Style and elegance** — optimise only after the first two are satisfied.

**Explicit counter-constraint:** "we're rushing the demo" is not a valid reason to break §5, and "it follows the style guide" is not a valid reason to delay the working pipeline.
When the two collide, use the simplification clauses in §6 — cut features, never cut safety.

---

## 1. Language and tooling baseline

| Item | Rule |
|---|---|
| Python | 3.11+ (currently 3.13); add `from __future__ import annotations` where forward references need it |
| Formatting | `ruff format`, line length **100** |
| Lint | `ruff check`, rule set `E,F,I,UP,B,SIM,C4,RUF` |
| Typing | Public functions/methods/boundaries must be annotated. `mypy` is declared as a dev dependency but **not configured or run yet** (`pyproject.toml` has no `[tool.mypy]`, and `dev.py` has no task for it); the annotations are nevertheless required, because they are what makes adding it a one-line change |
| Dependencies | `uv` (`uv.lock` committed); never mutate the environment with plain `pip install` |
| TypeScript | `strict: true`, `noUncheckedIndexedAccess`; `any` is banned (use `unknown` + narrowing) |
| Frontend formatting | Prettier + ESLint (flat config), `--max-warnings=0` before commit. **Not wired yet** — the toolchain is not installed, so `tsc --noEmit` is the only frontend gate today; registered as a gap in `docs/DEMO.md` §7 |
| Pre-commit | `python scripts/dev.py check` (see §7) |

**Banned:** `print()` (use `logging`), bare `except:`, `eval`/`exec`, `shell=True`, and reading `os.environ` directly in business code (go through `Settings`).

---

## 2. Layout and layering

### 2.1 Backend layering and dependency direction

```
backend/app/
├── main.py            Application assembly: middleware, routers, startup/shutdown hooks
├── api/               Route layer (thin). Does only: auth, validation, call service, return DTO
├── core/              Cross-cutting: config / logging / errors / auth
├── domain/            Domain models and value objects. Pure data + pure functions, no IO, no framework
├── services/          Business orchestration: git_service / run_service / artifact_service / kb_service
├── agent/             LLM orchestration: graph.py (graph construction), pipeline.py (flow), tools/
└── infra/             Infrastructure adapters: db / storage / llm / redaction
```

**Dependencies point one way only** (modules inside the same layer may reference each other):

```
api ──► services ──► infra ──► domain
          │  └────────────────► domain
          └──► agent ──► services (this direction only)
```

**Hard rules:**

| Rule | Rationale | How to check |
|---|---|---|
| `agent/` **must not** import `infra/` | The agent never talks to the database or raw filesystem directly — only via services | `grep -rn "from app.infra" backend/app/agent/` must be empty |
| `domain/` **must not** import any other layer | Domain models must be testable in isolation | `grep -rn "from app\." backend/app/domain/` may only match `app.domain.*` |
| `api/` holds no business logic | Thin routes make contract tests possible | Review: a route body over ~20 lines needs splitting |
| `services/` never returns ORM rows or raw dicts | Callers get domain models | Review + type annotations |

> Example: `git_service.py` depends on `core.config`, returns `domain.models` types, and knows nothing about the LLM — that is correct.
> If git failures ever need to notify the agent, `services/run_service.py` orchestrates it; git does not reach into the agent layer.

### 2.2 Files and naming

| Object | Rule | Example |
|---|---|---|
| Module file | `snake_case.py`, named for the **responsibility**, not the technology | `git_service.py` ✅ / `utils.py`, `helpers.py`, `manager.py` ❌ |
| Class | `PascalCase`, noun phrase | `GitService`, `RunEventBus`, `RepoIndex` |
| Function | `snake_case`, verb first | `ensure_mirror`, `resolve_ref`, `mask_secrets` |
| Boolean | `is_` / `has_` / `can_` prefix | `is_cancelled`, `has_credential` |
| Constant | `UPPER_SNAKE`, defined at module top | `SKIP_DIRS`, `ANALYSIS_FILE` |
| Internal | `_` prefix | `_resolve_path`, `_pick_target` |
| Domain vocabulary | Use domain language consistently | `run`, `artifact`, `phase`, `slice`, `rule` |

**Banned generic names:** `data`, `info`, `obj`, `manager`, `processor`, `handle_data`.
If you cannot name a thing concretely, its responsibility is probably still undefined.

**Banned ad-hoc abbreviations.** Industry-standard short forms are fine (`repo`, `cfg`, `ctx`, `req`/`res`, `id`, `url`, `sha`); invented ones (`rcvd`, `mgmt`, `pymt`) are not.

### 2.3 Frontend layout (vertical feature slices)

```
frontend/src/
├── app/          Shell: router / providers / error boundaries
├── features/     One folder per domain, each with api.ts + hooks.ts + components/
│   ├── runs/  artifacts/  repos/  kb/  flow/  admin/
└── shared/       Cross-domain reuse: api (client + generated types), ui (design system), hooks, lib
```

**Hard rules:**

| Rule | How to check |
|---|---|
| Components never call `fetch` directly — always `shared/api/client.ts` | `grep -rn "fetch(" src/features src/app` must be empty |
| Server state lives in TanStack Query only; Zustand holds UI state only | Review the stores for server entities |
| Cross-feature imports go through `shared/`; `features/a` must not import `features/b` | `grep -rn "from '\.\." frontend/src/features/` must be empty (no `@/` alias is configured, so the check is written against the relative form actually used) |
| No inline magic values (status codes, event names, colours) | Codes live in `shared/api/constants`; colours come from design tokens |

---

## 3. Code conventions

### 3.1 Docstrings

Every module starts with a docstring explaining **why it exists and where its boundary is** — not a restatement of the function names.
Public classes and functions get a one-line contract. Follow the module docstrings in `git_service.py` and `events.py`.

```python
"""Git access layer: bare mirror repositories + per-run worktrees.

Design notes (see docs/DESIGN.md §2):
* The agent never runs git. Pulling is deterministic server-side code...
"""
```

### 3.2 Error handling

- Define exceptions per layer, inheriting from a service base: `GitError(GitErrorBase)`, `RepoAuthError(GitError)`.
- Exceptions carry **machine-decidable** information:
  ```python
  class GitError(RuntimeError):
      def __init__(self, message: str, *, retryable: bool = False) -> None:
          ...
  ```
  `retryable` decides whether the caller may retry — never infer retryability by string matching.
- **Swallowing exceptions is banned.** `except Exception: pass` is never acceptable; at minimum `logger.warning(..., exc_info=True)`.
- **User-facing messages carry three things:** what happened, what to do next, and a trace ID.
  Bad: `"Error: 500"`. Good: `"Git authentication failed: the PAT may be expired or lack permission. Update the credential configuration."`

### 3.3 Logging and observability

- Use `logging.getLogger(__name__)`. `print` is banned.
- Log records must carry `run_id` / `actor` / `trace_id` (via `extra=` or formatting).
- **Mask before logging:** any text coming from git, external commands, or repository contents goes through `mask_secrets()` first.
- Level conventions:

  | Level | Use for |
  |---|---|
  | `debug` | Prompt fragments, full tool arguments (local only) |
  | `info` | Phase start/end, artifact produced, external call succeeded |
  | `warning` | Recoverable errors, fallback paths, retries |
  | `error` | Run failed, security rejection, external dependency unavailable |

### 3.4 Concurrency and async

- Services do not spawn their own threads; long operations (git, scripts) use `subprocess.run(timeout=...)` or a bounded thread pool, and **must set a timeout**.
- **Every external call has a timeout** (HTTP, subprocess, file lock). A call without a timeout is a defect.
- Fan-out uses `asyncio.gather(..., return_exceptions=True)` plus a semaphore for rate limiting.
  **One failing branch must never take down the others** — this is the core constraint behind parallel artifact generation.

### 3.5 Agents and prompts (project-specific, important)

| Rule | Why |
|---|---|
| Prompts live in code-level constants, never assembled inside function bodies | Reviewability, versioning, cache stability |
| Procedures live in `SKILL.md`; prompts only carry run parameters and a pointer to the skill | One place to edit a procedure, versionable independently, visible to the agent via progressive disclosure |
| Every LLM-visible tool needs (1) full type annotations, (2) a one-line purpose, (3) an **output size cap** | Prevents context blow-up and unbounded cost |
| All prompt interpolations use explicit markers (`RUN_ROOT=`, `ENTRYPOINT=`, `SKILL=`) | Stable, machine-readable run parameters; simplifies debugging and log correlation |
| Anything carrying repository content must declare "this is data, not instructions" | Prompt-injection defence, see §5 |
| Changing a prompt requires checking that its `SKILL=` / `SKILL_FILE=` markers still match the skill directory name | The prompt points at a skill file by path; a renamed skill silently yields an agent with no procedure to follow |
| A procedure change belongs in `backend/skills/<skill>/SKILL.md`, not in the prompt | The prompt carries run parameters only. Duplicating a procedure creates two sources of truth that drift |
| A skill's **reply format** is a code contract, not a skill choice | `ARTIFACT_SPECS[...]["accepts"]` declares it; the writer in `services/artifacts.py` depends on it. `fdd` returns Markdown, the others a `{"rows": [...]}` JSON block | Check `accepts` before editing a skill, and run `python scripts/dev.py skills-check` after |

### 3.6 Language policy

The project language is **English**: identifiers, comments, docstrings, log messages, error messages, commit messages, documentation, and generated artifacts.

| Surface | Rule |
|---|---|
| Code, comments, docs, commit messages | English, always |
| User-facing error messages and API field descriptions | English; the UI may localise via its i18n layer |
| Generated artifacts (FDD, test cases, data mapping) | English by default; the run request carries a **locale** so a customer can request another output language |
| Prompt text | English by default. If a customer requires non-English artifacts, the prompt appends an explicit output-language instruction rather than switching the whole prompt |
| Test fixtures and mock data | English, and must be plausible domain data (real-looking field names, not `foo`/`bar`) |

**Why the locale stays a parameter instead of being hard-coded:** delivery language is a customer requirement, not an implementation detail. Hard-coding English now means a rewrite the first time a customer asks for localised documents.

---

## 4. Testing

| Layer | Requirement |
|---|---|
| `backend/tests/` (unit-style) | Pure logic: path guards, masking, ref validation, schema handling. No network, no LLM — this is where the suite lives today |
| `scripts/e2e_smoke.py` (integration) | The real pipeline against a local bare repository and a live gateway. Requires a credential; **must skip loudly, never pass silently, when none is available** |
| Frontend unit tests | Vitest, covering hooks and key component interactions |
| E2E | Playwright, covering the main flow (create → progress → artifacts) |

**Hard thresholds:**

1. **Every new security rule ships with at least one positive and one negative test.** Example: out-of-root path, denied `.env` read.
2. **Every new tool or external call ships with a failure-path test** (timeout, auth failure, malformed response).
3. Line coverage for `services/` and `agent/` is **≥ 70 %**. During the demo, cover the critical path first rather than chasing the number.
4. **Unit tests never call a model.** They stub it. Integration tests (`scripts/e2e_smoke.py`) do call the real gateway, and must **exit with an explicit SKIP when no credential is configured** — a gate that passes without exercising anything is worse than no gate.
5. Tests never reach the public internet except for the configured model gateway.
5. Tests that touch disk use `tmp_path` and never write inside the repository.

---

## 5. Security hard rules (non-negotiable)

None of the following may be violated — not for a demo, not in temporary debug code. A PR that breaks one is rejected.

| # | Hard rule | Correct approach |
|---|---|---|
| S1 | **The agent never gets shell execution** | Do not give the backend `SandboxBackendProtocol`; never enable `LocalShellBackend`; never add the `execute` tool back |
| S2 | **The agent never runs git** | All git goes through `services/git_service.py`; expose no git-command tool to the LLM |
| S3 | **Credentials must never appear in** URLs, `.git/config`, argv, logs, the event stream, LLM context, or artifacts. This covers git PATs **and** any client credential | Inject git PATs via `GIT_ASKPASS`; run every log line through `mask_secrets()`; never accept a credential in a query string |
| S4 | **Every file path is validated at the boundary** | `FilesystemBackend(virtual_mode=True)`; server-side reads/writes do `resolve()` + `relative_to(root)`; see `read_blob()` |
| S5 | **`/repo/**` is read-only** | `FilesystemPermission(..., mode="deny")` **plus** an OS-level read-only checkout via `GitService.seal_worktree()`, and a test for both |
| S6 | **Credential-shaped files are never read into context** | Deny rules cover `.env*`, `secrets/**`, `*.pem`, `id_rsa*` |
| S7 | **`shell=True` is banned** | Use an argument array with `shell=False`; validate user-supplied argv elements |
| S8 | **Repository content is untrusted input** | Mark it as data-not-instructions before it reaches the LLM; ignore `.git/hooks`, submodules, and `core.*` config |
| S9 | **Destructive operations stay inside the run directory** | Never `rmtree` outside `data_root`; validate the path prefix first |
| S10 | **No secret is ever hard-coded** | Configuration comes from `Settings` (env vars, a `0600` file, or systemd credentials); run `gitleaks` before commit |

**Security test checklist** (maintain these whenever the related code changes):

```
backend/tests/test_security.py  ← S1 no execute tool · S3 masking · S4 out-of-root and
                                  symlink escape · S5/S6 /repo writes and credential reads ·
                                  S7 no shell=True · ref validation · hook blocking ·
                                  backend path layout
backend/tests/test_askpass.py   ← the GIT_ASKPASS hook: real prompts, quoting, no token in argv
backend/tests/test_auth.py      ← authentication modes: fail-closed, role source, reserved SSO
backend/tests/test_skills.py    ← skill loading: both halves of the wiring
```

The point is coverage of every hard rule by a test that fails when the rule is broken — not a
particular file layout. Adding a rule means adding a test in the same change.

### 5.0 Authentication: identity comes from the proxy, not the client

The console holds no credential. Identity is attached by whatever proxies to the service — the
Vite dev proxy locally, the SSO gateway in production — and the server reads it in
`AUTH_MODE=trusted_header`. Consequences that must not be undone without thought:

1. **`trusted_header` is safe only if the port is unreachable except through the proxy.** The
   header is not verified, so a direct caller could forge it. `main.py` warns at startup for
   this reason; do not silence that warning, fix the network instead.
2. **Roles come from configuration, never from the header.** A caller cannot promote itself by
   inventing a value.
3. **No credential may be accepted in a query string.** An escape hatch existed for the SSE
   endpoint because `EventSource` cannot set headers; it was removed once the client stopped
   sending credentials, because its only remaining effect was secrets in access logs.
4. **`AUTH_MODE=e2e_token` is reserved for the SSO integration and must fail loudly** (501)
   until it is implemented. A permissive stub would be indistinguishable from a finished
   integration and would ship.

Adding a new client-facing credential means changing this section first, not just the code.

### 5.0.1 Why git stays server-side (decided, with the reasoning recorded)

Giving the agent a git tool was considered and rejected. The decisive argument is concrete
rather than general: the PAT is injected into **the environment of whichever process runs
git**, so letting the agent run git would put the credential inside the agent's own process —
one prompt injection away from a tool result or an artifact, which is exactly what S3 exists to
prevent. Two further reasons of the same weight:

* **Argument injection.** Branch and tag names are validated server-side
  (``git_service.validate_ref``) precisely because git accepts flags wherever it accepts a ref.
  Handing argument control to a model reintroduces ``--upload-pack=...`` and friends. The
  validator is cross-checked against ``git check-ref-format`` in the tests, so it cannot drift
  into a private dialect that rejects real branch names.
* **Session consistency.** The checkout the agent reads is sealed read-only and pinned to one
  commit. A mid-run fetch would replace the files under it, destroying the guarantee that every
  line the agent read maps to that commit.

Also note: **a skill is not a capability.** ``SKILL.md`` is procedure text injected into the
system prompt; it only tells the model how to use tools that already exist. Wrapping git in a
skill would therefore change the packaging, not the risk.

If the requirement is "the agent discovered it needs code outside the checkout", the correct
shape is a **constrained read tool** (``request_repository_scope``): the agent expresses intent,
the server validates scope and performs the operation. See docs/DESIGN.md for the open item.

### 5.0.2 Platform rules

The project builds and runs on Linux, macOS and Windows. Nothing may assume a POSIX shell.

| Rule | Why |
|---|---|
| **No shell invocation from application code.** No ``shell=True``, no writing ``.sh`` files, no reliance on ``grep``/``find``/``sed`` | Windows has no ``/bin/sh``; a shell script silently fails there and works everywhere else, so the breakage lands on one developer |
| **Resolve platform paths through a helper, never by hand** | A virtualenv's interpreter is ``bin/python`` on POSIX and ``Scripts/python.exe`` on Windows |
| **Tasks are defined once, in `scripts/dev.py`.** There is no Makefile | GNU Make is absent from Windows and its recipes assume a POSIX shell. Since bootstrapping already requires Python, a Makefile could only forward to this file — adding a second surface that can break (tab indentation) and drift (task names documented twice) for no gain |
| **A control that a platform cannot enforce must degrade loudly** | ``chmod`` does not implement POSIX permissions on NTFS, so read-only checkout sealing is unavailable on Windows. `seal_worktree` logs a warning and returns rather than reporting success for a layer that is not in effect |

The one intentional exception is `GIT_ASKPASS`: it is a command line git invokes through a shell,
so it must be quoted for the shell git uses — single quotes on POSIX, double quotes on Windows
(``cmd.exe`` has no single-quote quoting). That quoting lives in one function,
``git_service._quote_command``, and is covered by tests.

### 5.0.3 Do not scan the whole checkout

The repository is customer source and can be gigabytes. An eager whole-repository pass is
therefore treated as a defect, not an optimisation to add later:

* **No component may walk the entire checkout to build an index.** An eager `repo_index.json`
  was measured at 46 s for 319 MB, extrapolating to ~25 min at 10 GB — paid on every run, before
  the model is called. It was removed; see `docs/DESIGN.md` §2.7 for the full reasoning.
* **Locate lazily.** The analysis resolves the request by searching for what it names and
  widening along the call graph. `glob`/`grep` cost what they match, not what the repository
  contains.
* **Bound every read.** `MAX_SLICE_LINES` caps a single file read and `MAX_TOOL_CALLS` caps the
  loop, so a wrong assumption degrades into a failed run rather than an hour of work.
* If a future profile shows search dominating the tool budget, the answer is a **narrow,
  demand-driven** index scoped to the entry point's call graph — never a full scan. Write an ADR
  first.

### 5.0.4 No vendor name in the repository

The gateway is configuration, not architecture. Naming a provider in source, prompts, defaults or
documentation couples the project to one vendor and makes the next integration look like a
rewrite.

| Rule | Why |
|---|---|
| **No provider or model name in code, defaults, or docs.** `LLM_BASE_URL` and `LLM_MODEL` have no defaults and a validator refuses to start without them | A baked-in default is wrong for every other deployment; a silently empty one surfaces as an opaque connection error mid-run |
| Concrete endpoints and model names belong in `.env.example` and `docs/DEMO.md` | That is operator configuration, and an example is how an operator knows what to fill in |
| **Verification records are the exception.** "Verified against `<gateway>`: parallel tool calls work" stays, because it is a test result, not a dependency | Deleting the record would lose the evidence that the integration was ever exercised |
| The credential variable order is `LLM_API_KEY_ENV` → `LLM_AUTH_TOKEN` → tool-specific fallbacks | A host can provision any name; the neutral name is what new deployments should use |

Adding a second provider must be a configuration change. If it requires a code edit, the abstraction
is in the wrong place.

### 5.1 Where each control is actually enforced (verified, not assumed)

This distinction was established by probing the installed `deepagents` 0.7.13 build, and it
changes where you must add defences:

| Control | Enforced by | Protects against | Does **not** protect against |
|---|---|---|---|
| `FilesystemPermission` (`deny`) | The agent's filesystem **middleware** | The model's tool calls | Server-side code calling the backend directly |
| `virtual_mode=True` path checks | The **backend**, on every call | `..`, `~`, and symlink escapes, from any caller | Paths legitimately inside the run root |
| OS read-only bits (`seal_worktree`) | The **kernel** — POSIX only. **Not enforced on Windows** (NTFS ignores POSIX mode bits), where it logs a warning and stands down | Any write to the checkout, from any caller | Writes to `/work` and `/artifacts`, which must stay writable; on Windows, any write at all |
| `execute` tool absence | **Backend capability gating** at request build time | Shell execution | Nothing — but it depends on the backend never implementing `SandboxBackendProtocol` |

**Consequences for how we write code:**

1. Never claim a permission rule protects a server-side code path. If server-side code writes
   to the checkout, that is a defect regardless of the permission list.
2. `execute` is filtered out because `CompositeBackend`'s *default* backend is a
   `FilesystemBackend`. Swapping that default for a sandbox backend would silently make
   `execute` available — so S1 is a property of the **wiring**, and the assertion in
   `backend/tests/test_security.py` must
   assert on the tool list actually sent to the model, not on the middleware's internal state.

---

## 6. Demo-phase simplification clauses (legal downgrades, not violations)

Explicitly permitted simplifications. Each one **must** carry a `# TODO(demo):` marker and be logged under "Known gaps" in `docs/DEMO.md`.

| Permitted simplification | Cost | Do it properly when |
|---|---|---|
| Mock gateway instead of the intranet LLM | Artifact content is script-generated | During intranet integration — swap `LLM_BASE_URL` only |
| In-process worker instead of a queue | A restart loses in-flight runs | Concurrency exceeds 5 — move to a Redis queue |
| API-key auth only | Cannot attribute actions to a person | When OIDC lands — only the `current_actor` dependency changes |
| Approvals backend-only, no UI | The demo does not exercise approvals | Milestone M4 completes the frontend |
| Customer Excel templates not yet wired in | Column names may not match the delivery spec | The moment the templates arrive, they win |

**Never permitted to be simplified** (these cost far more to retrofit than to do now):
any §5 hard rule, the layering direction, contract-first, and the three-part error message.

---

## 7. Pre-commit self-check (30-second version)

```bash
python scripts/dev.py check            # ruff format --check, ruff check, pytest -q,
                                       # contract drift, skills directory
cd frontend && pnpm typecheck          # tsc --noEmit (ESLint/Prettier are not wired yet)
```

Then three human questions:

1. Did this change touch a **§5 hard rule**? If so, did I add the matching test?
2. If I added a field or endpoint, did I **update the contract** in `contracts/`?
3. If I left a gap, did I mark it `# TODO(demo):` and register it in `docs/DEMO.md`?
