# COBOL Analysis Platform

An agent pipeline that reads a COBOL legacy codebase and produces the documentation a
modernisation project actually needs — a **Functional Design Document**, a **test case workbook**
and a **data mapping workbook** — then serves the results through a queryable knowledge base.

Point it at a repository revision, describe the interface you care about in your own words, and it
resolves the code, walks the call graph, and writes deliverables in which every statement is
traceable to a file and a line.

```
  repository ──► fetch ──► analyse ──┬──► FDD             (Markdown)
  (pinned                            ├──► test cases      (xlsx)
   commit)                           └──► data mapping    (xlsx)
                                            │
                                            ▼
                                   knowledge base ──► console
```

**Status: working end to end.** A sample run against a live gateway produced a 291-line FDD,
17 test cases, 8 field mappings and 7 business rules in 62 s, each rule carrying its source file
and line. See [Current limitations](#current-limitations) for what is deliberately not done yet.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using the console](#using-the-console)
- [Configuration](#configuration)
- [Repository registry](#repository-registry)
- [Skills](#skills)
- [HTTP API](#http-api)
- [Development](#development)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Security model](#security-model)
- [Current limitations](#current-limitations)
- [Documentation](#documentation)

---

## How it works

1. **Fetch** — the service clones a bare mirror of the repository once, then creates a per-run
   worktree pinned to an immutable commit. The checkout is sealed read-only.
2. **Analyse** — an agent reads the request, locates the code it names, walks the call graph
   outward, and writes a JSON hand-off: progress, call flow, code slices, and business rules with
   code evidence.
3. **Generate** — three deliverables are produced **in parallel** by independent agent
   invocations. One failing deliverable never fails the run.
4. **Validate → archive → index** — each artifact is checked before it is announced, a manifest
   records the commit and model used, and the results are chunked into the knowledge base.

Every run executes in an isolated directory:

```
data/runs/<run_id>/
├── repo/         read-only checkout, pinned to the commit
├── work/         scratch space for the analysis
├── artifacts/    fdd.md · test_cases.xlsx · data_mapping.xlsx · manifest.json
└── events.ndjson append-only event journal behind the live progress stream
```

## Requirements

| | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.13 tested |
| [uv](https://docs.astral.sh/uv/) | any | environment and dependency management |
| Node.js | 20+ | console only |
| pnpm | 9+ | console only |
| git | any | must be on `PATH` |
| Model gateway | OpenAI- or Anthropic-compatible | must support **tool calling** (including parallel calls) and streaming |

Linux, macOS and Windows are all supported. No task assumes a POSIX shell.

## Quick start

```bash
git clone <this-repo> && cd cobol-analysis
python scripts/dev.py install          # venv + backend and frontend dependencies
```

Set the model endpoint and credential. **Both are required**: no vendor is named anywhere in
this project, so there is no default endpoint that would be right for every deployment.

```bash
export LLM_BASE_URL=https://gateway.example.com/v1     # Windows: set / $env:
export LLM_MODEL=<model-name>
export LLM_AUTH_TOKEN=<your-key>
```

`LLM_PROVIDER` selects the dialect (`openai` for `/v1/chat/completions`, `anthropic` for
`/v1/messages`). The credential may also be named explicitly with `LLM_API_KEY_ENV`, which
overrides everything; `OPENAI_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are accepted as fallbacks so a
host whose key is already provisioned under a tool-specific name needs no change.

Register a repository (see [Repository registry](#repository-registry)), then start the two
services in separate terminals:

```bash
python scripts/dev.py api              # http://127.0.0.1:8000
python scripts/dev.py web              # http://127.0.0.1:5173
```

Open <http://127.0.0.1:5173>. There is no login step: the dev proxy attaches the identity header
the API reads.

### Verify the pipeline without a browser

```bash
python scripts/dev.py e2e              # creates a sample repo, runs a real analysis, asserts
                                       # every artifact, and prints the full run report
```

`e2e` needs a model credential. Without one it prints a `SKIP` notice and exits 0 rather than
passing silently.

## Using the console

1. **Repository** — pick a repository; the default branch is resolved immediately and the pinned
   commit SHA is shown before you commit to a run. **Pull from remote** refreshes the mirror.
2. **Request** — describe what you want analysed in your own words:

   > *Analyse the customer balance inquiry: which programs and CICS transactions does it reach,
   > and what business rules govern the balance calculation?*

   Naming a program works too, but it is not required: the analysis resolves the wording against
   the code and reports the entry point it settled on.
3. **Deliverables** — select which to produce, and the output language.
4. **Follow** — the run page shows six phases live over SSE, a timeline, logs, and each artifact.
   FDD renders as Markdown; workbooks are previewed as tables and downloadable as `.xlsx`.

## Configuration

All settings are environment variables read through `app/core/config.py`. Names are unprefixed;
set `COBOL_ENV_PREFIX=COBOL_` to namespace them. Copy [`.env.example`](.env.example) to `.env` to
get started.

**Model**

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` (chat completions) or `anthropic` (messages) |
| `LLM_BASE_URL` | *required* | gateway endpoint |
| `LLM_MODEL` | *required* | model identifier |
| `LLM_AUTH_TOKEN` | — | the credential, or name its variable with `LLM_API_KEY_ENV` |
| `ANALYSIS_MODEL` / `ARTIFACT_MODEL` | — | optional per-skill overrides |

**Budgets** — a runaway run is bounded in code, not by hope:

| Variable | Default | Purpose |
|---|---|---|
| `MAX_TOOL_CALLS` | `400` | hard cap on agent tool calls |
| `MAX_WALL_SECONDS` | `2700` | wall-clock cap per run |
| `MAX_SLICE_LINES` | `400` | cap on a single source read |
| `MAX_PARALLEL_ARTIFACTS` | `3` | deliverable fan-out width |
| `MIRROR_MAX_AGE_SECONDS` | `300` | skip the network if the mirror is fresher |

**Storage and auth**

| Variable | Default | Purpose |
|---|---|---|
| `DATA_ROOT` | `./data` | mirrors, runs, deliverables, SQLite |
| `RUN_TTL_DAYS` | `7` | age at which `gc` reclaims a run |
| `AUTH_MODE` | `trusted_header` | `trusted_header`, `api_key`, `disabled`, `e2e_token` |
| `TRUSTED_ACTOR_HEADER` | `E2E-token` | identity header, set by the proxy |
| `TRUSTED_DEFAULT_ROLE` | `admin` | role granted to a proxy-identified caller |
| `API_KEYS` | — | `key:role` pairs, read only in `api_key` mode |

> `AUTH_MODE=trusted_header` trusts an unverified header. It is safe **only** when the service is
> reachable exclusively through the proxy that sets it. The service warns at startup; fix the
> network rather than silencing the warning.

## Repository registry

`REPOS_FILE` points at a YAML file listing repositories:

```yaml
repos:
  - repo_key: core-banking
    name: Core Banking COBOL
    url: https://git.example.com/core/banking.git
    default_branch: main
    username: oauth2
    credential_env: CORE_BANKING_PAT    # the NAME of the env var holding the PAT
    shallow: false
```

Credentials are **never** stored in this file. `credential_env` names an environment variable that
is injected for the lifetime of a single git command; the token never reaches `.git/config`, a
command line, a log line or a prompt. Credentials embedded in a `url` are rejected at load time.
See [`backend/repos.example.yaml`](backend/repos.example.yaml).

For a single repository during development, `DEMO_REPO_URL` and `DEMO_REPO_BRANCH` are enough.

## Skills

The four agent skills live in [`backend/skills/`](backend/skills) as `SKILL.md` files and are
advertised to the agent through progressive disclosure:

| Skill | Produces |
|---|---|
| `cobol-analysis` | the JSON hand-off: progress, call flow, code slices, business rules |
| `fdd` | Functional Design Document (Markdown) |
| `test-case` | test case rows, rendered into a workbook |
| `data-mapping` | field mapping rows, rendered into a workbook |

They are **replaceable content** — the pipeline is the fixed part. Verify a replacement set before
running anything:

```bash
python scripts/dev.py skills-check
SKILLS_DIR=/opt/cobol-agent/skills python scripts/dev.py skills-check
```

The check enforces the code-side contracts: directory names, frontmatter `name` matching the
directory, the analysis hand-off path and `progress.entry_point`, and each deliverable's reply
shape. See [docs/DEMO.md §4.1](docs/DEMO.md).

## HTTP API

Interactive documentation at `/docs`. Every endpoint is under `/api/v1`; the first column is the
permission required — **R** read, **C** create_run, **A** admin, **Q** query_kb.

| | Endpoint | Purpose |
|---|---|---|
| R | `GET /health` · `GET /whoami` | service facts · resolved identity |
| A | `GET /audit` | recent audited actions |
| R | `GET /repos` | registered repositories |
| R | `POST /repos/{key}/resolve` | resolve a ref to a commit SHA, list branches |
| C | `POST /repos/{key}/fetch` | pull the latest refs from the remote |
| C | `POST /runs` | start a run |
| R | `GET /runs` · `GET /runs/{id}` | list · full state (phases, artifacts, usage) |
| C | `POST /runs/{id}/cancel` | request cancellation |
| A | `DELETE /runs/{id}` | delete a run, optionally purging files |
| R | `GET /runs/{id}/events` | Server-Sent Events, replay-aware |
| R | `GET /runs/{id}/logs` | flattened log view, filterable |
| R | `GET /runs/{id}/artifacts[/{kind}]` | artifact states · preview (workbooks as rows) |
| R | `GET /runs/{id}/artifacts/{kind}/download` | download the original file |
| Q | `POST /kb/query` | knowledge base query with citations |
| R | `GET /kb/stats` · `POST /kb/reindex/{id}` | index statistics · rebuild a run's index |
| A | `POST /admin/gc` · `POST /admin/reload-repos` | reclaim disk · reload the registry |

## Development

Every task is defined once, in [`scripts/dev.py`](scripts/dev.py), and works on any platform:

```bash
python scripts/dev.py                  # list all tasks
python scripts/dev.py check            # the pre-commit gate: lint + tests + contract + skills
python scripts/dev.py fmt              # auto-format
python scripts/dev.py test             # backend tests
python scripts/dev.py typecheck        # frontend types
python scripts/dev.py web-build        # production frontend bundle
python scripts/dev.py openapi          # regenerate contracts/openapi.json
python scripts/dev.py contracts-check  # fail if the contract drifted
python scripts/dev.py gc               # reclaim disk from expired runs
python scripts/dev.py clean            # build and cache artefacts only
python scripts/dev.py purge-data       # delete ALL runtime data (the only destructive task; asks first)
```

`--print` shows the underlying command without running it.

There is no Makefile by design: GNU Make is absent from Windows, and bootstrapping the toolchain
already requires Python, so a Makefile could only forward to this file.

## Testing

```bash
python scripts/dev.py check     # 55 test functions → 100 cases, plus contract and skills checks
```

| Suite | Covers |
|---|---|
| `tests/test_security.py` | every hard rule in `docs/CODESTYLE.md` §5 — path traversal and symlink escape, `/repo` write denial, credential-path denial, secret masking, no `shell=True`, no `execute` tool (asserted on the backend the pipeline builds), backend path layout, git ref validation, repository hook blocking |
| `tests/test_askpass.py` | the `GIT_ASKPASS` hook: real git prompts, shell quoting, token absent from `argv` |
| `tests/test_auth.py` | authentication modes: fail-closed, role source, the reserved SSO mode |
| `tests/test_skills.py` | skill loading: both halves of the wiring |
| `tests/test_run_lifecycle.py` | how a run ends: wall-clock timeout, cancellation, failure, success — and exactly one terminal event each |
| `tests/test_services.py` | service invariants: event sequence across a restart, replay cursor, stable knowledge-base chunk ids, recorded mirror fetch time |

Controls are tested against their own counter-example: the hook-blocking test proves the hook
*does* run without the override, and the ref validator is cross-checked against
`git check-ref-format`.

## Project layout

```
backend/
├── app/
│   ├── api/          HTTP routes — validate, authorise, delegate
│   ├── agent/        deepagents graph, pipeline, prompts
│   ├── services/     git · runs · artifacts · events · knowledge base
│   ├── domain/       models and pure constructors
│   ├── infra/        SQLite
│   └── core/         configuration · authentication
├── skills/           one directory per skill, each with a SKILL.md
└── tests/
frontend/src/
├── features/         runs (incl. the artifact preview panel) · kb
└── shared/           API client · hooks · UI primitives
scripts/              dev.py (every task) · e2e_smoke.py · skills_check.py · purge_data.py
contracts/            openapi.json — the frontend contract's single source of truth
docs/                 design · code style · demo runbook
```

Dependencies point one way: `api → services → infra → domain`, and `agent → services`.

## Security model

The agent has real filesystem access — it reads and writes files much as a developer would — but it
is boxed inside each run's directory by four independent controls:

| Control | Enforced by |
|---|---|
| Path containment | `FilesystemBackend(virtual_mode=True)` — rejects `..`, `~`, and symlink escapes |
| Path rules | Permission rules deny writes to `/repo/**` and reads of `.env*`, `secrets/**`, `*.pem`, `id_rsa*` |
| Read-only checkout | POSIX mode bits, so even a server-side write through the backend fails |
| No shell | No backend implements the sandbox protocol, so the `execute` tool is never offered |

Git is server-side code, not an agent capability. Refs are validated before reaching git
(cross-checked against `git check-ref-format`), repository-supplied hooks cannot execute, the host
credential helper is cleared so only the injected PAT applies, and every git output line is masked
before it reaches a log.

[`docs/CODESTYLE.md` §5](docs/CODESTYLE.md) lists the full set of rules and where each control is
actually enforced.

> Read-only sealing is a POSIX control. On Windows `chmod` does not implement POSIX permissions,
> so that layer is unavailable and the service says so at run time rather than claiming otherwise.

## Current limitations

Stated plainly, because a demo that overstates itself is worse than one that does not.

| Area | Limitation |
|---|---|
| **Deliverable content** | Structurally correct and traceable, but bounded by what the request names. A vague request yields a vague document. |
| **Scale** | Verified against a small sample and synthetic large checkouts. A real multi-gigabyte mainframe repository has not been run end to end, and the model's search cost on one is unmeasured. |
| **Cost** | Tool calls and wall clock are capped; there is **no currency cap**. A large repository will cost proportionally more. |
| **Authentication** | No SSO yet. Every caller behind the proxy shares one role. `AUTH_MODE=e2e_token` is reserved and returns 501 until the gateway integration is written. |
| **Approvals** | The backend supports interrupts; there is no UI for them and the pipeline does not use them. |
| **Knowledge base** | Lexical (BM25-style) retrieval rather than embeddings, and the answer is assembled from retrieved evidence rather than generated. No vector store is wired in. |
| **Templates** | Customer-owned workbook templates are not honoured; the column layout is ours. |
| **Tests** | Coverage is concentrated on the security surface. `services/` and the frontend have little beyond what `e2e` exercises. |

[docs/DEMO.md §7](docs/DEMO.md) carries the same list with impact and the milestone that closes
each item.

## Documentation

| Document | Contents |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | architecture, verified library capabilities, git model, security design, delivery plan |
| [docs/DEMO.md](docs/DEMO.md) | runbook, skill replacement, intranet switch-over, known gaps, troubleshooting |
| [docs/CODESTYLE.md](docs/CODESTYLE.md) | binding rules: layering, naming, testing, security hard rules |
| [CONTRIBUTING.md](CONTRIBUTING.md) | repository conventions, contracts-first workflow, review gates |
| [CLAUDE.md](CLAUDE.md) | orientation for AI coding tools |
