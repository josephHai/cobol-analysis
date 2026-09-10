# Demo Runbook

How to run the console, what to show, and exactly what is *not* finished yet.
Read `CONTRIBUTING.md` §7 before changing anything during the sprint.

---

## 1. What the demo proves

One screen, no terminal, no manual steps:

```
New analysis (repo + pinned commit + entry point)
   → fetch → index → COBOL analysis → 3 deliverables in parallel → validate → archive → KB
   → live progress on screen
   → FDD readable in the console, two workbooks downloadable
   → knowledge base answers a question with citations traceable to a commit
```

Everything above is verified two ways:

* **`python scripts/dev.py e2e`** runs the whole pipeline against a generated sample repository
  and the configured gateway. It needs a model credential and **skips loudly** without one — a
  green run that never called a model would be worse than no test.
* **A live run against an OpenAI-compatible gateway** has been executed end to end: it produced
  a 291-line FDD, 17 test cases, 8 field mappings and 7 business rules in 62 s. Streaming, single
  tool calls and parallel tool calls were all confirmed against the live endpoint before the
  integration was written, because a gateway that cannot return multiple tool calls in one
  response cannot drive this pipeline.

---

## 2. Prerequisites

| Need | Version | Check |
|---|---|---|
| Python | 3.11+ (3.13 tested) | `python3 -V` |
| uv | any | `uv --version` |
| Node + pnpm | Node 20+, pnpm 9+ | `node -v && pnpm -v` |
| git | any | `git --version` |

The model gateway is reached over the network, so a credential and a reachable endpoint are
required for anything that runs the pipeline (`api`, `dev`, `e2e`). `check` runs entirely
offline: the sample repositories it uses are created on disk.

---

## 3. Run it

```bash
python scripts/dev.py install     # venv + backend deps + frontend deps (~2 min, first time)
```

`python scripts/dev.py` with no argument lists every task; `--print` shows the underlying command
without running it. No task requires `make`, a POSIX shell, or anything beyond Python.

**With the real model (default).** The service needs a gateway endpoint, a model name, and a
credential. No vendor is named in the project, so the endpoint and model are configuration:

```bash
export LLM_BASE_URL=https://gateway.example.com/v1
export LLM_MODEL=<model-name>
export LLM_AUTH_TOKEN=<your-key>
```

```bash
# terminal 1 — API + agent worker
DEMO_REPO_URL="$(pwd)/demo-repo" \
python scripts/dev.py api         # http://127.0.0.1:8000

# terminal 2 — console
python scripts/dev.py web         # http://127.0.0.1:5173
```

If the credential is already exported under a tool-specific name (`OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN`), it is picked up automatically.

Both services at once: `python scripts/dev.py dev`.

**No credential?** The pipeline needs a working gateway; there is no offline mode any more.
`python scripts/dev.py check` (lint plus the unit suite) runs without one, and `e2e` will tell you it is
skipping rather than pretending to pass.

Open <http://127.0.0.1:5173>. **There is no login step** — the dev proxy supplies the
identity header the backend reads (`AUTH_MODE=trusted_header`). Then:

1. **New analysis** → repository `demo`; the pinned commit resolves as soon as it is selected
   (press **Pull from remote** first if you want the newest refs)
2. Describe the analysis in the request box — e.g. *"Analyse PGM001, the customer balance
   inquiry: its call relationships, the business rules behind the balance calculation, and the
   fields read from the CUSTOMER table."* The entry point is **derived from that request** by
   the analysis skill; there is no field to type it into
3. Leave all three deliverables checked → **Start analysis**
4. Watch the six phases complete, then open the FDD and download both workbooks
5. **Knowledge base** → ask *"Which program reads ACCT_NO from the CUSTOMER table?"*

### One-command verification

```bash
python scripts/dev.py e2e    # creates its own sample repo + temp data root; prints the full report
```

Expected tail:

```
[ artifacts] {"fdd": "ready", "test_case": "ready", "data_mapping": "ready", ...}
[   mapping] 6 rows
[        kb] 5 citations via lexical
[    sealed] checkout mode=444
[      PASS] end-to-end pipeline verified
```

---

## 4. Security posture during the demo

These hold in the current build and are worth stating out loud if asked:

| Property | How it is achieved | How to verify |
|---|---|---|
| The agent cannot run shell commands | No backend implements `SandboxBackendProtocol`, so `execute` is never offered | `test_security.py` asserts this for the composite, its default, and every route — on the backend the pipeline builds |
| The agent cannot run git | Git is server-side code in `git_service.py`; no git tool is registered with the agent | The agent's tool list comes from `create_deep_agent` (file tools + skills), and the pipeline reaches git only through `GitService` |
| The checkout cannot be modified | `FilesystemPermission` denies writes **and** the tree is `chmod 444` | `e2e` prints `checkout mode=444` (POSIX only; Windows has no equivalent) |
| Credentials never leak | PATs injected via `GIT_ASKPASS`; all git output masked | `mask_secrets()` is applied in `_git()` before any log line |
| Paths cannot escape the run root | `virtual_mode=True` + `resolve()`/`relative_to()` | Backend call with `../..` raises before touching disk |
| The model cannot exceed the budget | Tool-call cap + wall-clock cap | A looping prompt fails fast with a clear message |

---

## 4.1 Replacing the skills

The real analysis and deliverable skills live on the intranet. They are **content** — this build
treats them as replaceable — but the slot has a shape, and `python scripts/dev.py skills-check` verifies it in
seconds instead of at the end of a 90-second run.

```bash
python scripts/dev.py skills-check                          # checks <repo>/backend/skills
SKILLS_DIR=/opt/cobol-agent/skills python scripts/dev.py skills-check
```

On Windows use `set SKILLS_DIR=...` (cmd) or `$env:SKILLS_DIR=...` (PowerShell); `make` is not
required and is not part of Windows.

**What a replacement skill must satisfy.** These are code-side contracts, not conventions:

| Requirement | Why | If unmet |
|---|---|---|
| Directory names: `cobol-analysis`, `fdd`, `test-case`, `data-mapping` | The pipeline invokes each by that exact name. Note the API artifact key and the directory name differ on purpose (`test_case` vs `test-case`) | The run fails with "did not produce …" or the deliverable never generates |
| `SKILL.md` with YAML frontmatter whose `name` equals the directory name | `SkillsMiddleware` keys skills by the **frontmatter** name | The skill loads under a name nothing references, so it is silently never used |
| A non-empty `description` | That is the only part the model sees until it chooses to read the skill | The skill is never selected |
| **`cobol-analysis` writes its hand-off to the `ANALYSIS_FILE=` path given in the prompt**, and reports the resolved target in `progress.entry_point` | The pipeline reads that file, and surfaces that field as the run's target | The run fails at Analyze — deliberately, because continuing without the hand-off would produce fabricated deliverables |
| **`fdd` replies with a Markdown document** | It is written to `fdd.md` and rendered as Markdown in the console | The deliverable is marked failed; the other two still complete |
| **`test-case` and `data-mapping` reply with a ```` ```json ```` block containing `{"rows": [...]}`** | Parsed into rows and rendered into a workbook | Same — that deliverable fails, the rest complete |

The last three are the ones most likely to bite, because they constrain the **reply format**, not
the procedure. If an intranet skill returns something else — a `.docx` for the FDD, or a workbook
built by its own script — either adapt the skill to reply in the expected shape, or change the
matching entry in `ARTIFACT_SPECS` (`accepts` plus `filename`) and the writer in
`services/artifacts.py`. The check states the expected shape per skill so this is visible before
a run, not after.

**Skills the build does not invoke** are harmless: they will be advertised to the agent but never
executed by the pipeline, and the check warns about them so nobody assumes they run. If your
skills need the pipeline to fan out differently — different deliverable set, extra stages — that
is `ARTIFACT_SPECS` and `pipeline._generate`, not the skill file.

**Verify the swap on one small repository before pointing it at customer code.** The failure
modes above are loud and isolated per deliverable, but the cheapest possible confirmation is a
single run against `demo-repo`.

## 5. Switching to the intranet

Moving onto a customer host means pointing at their gateway and their repositories. Both are
configuration only.

```bash
# 1. The customer's gateway.
#    It must support tool calling AND streaming — verify with the probe in §5.1 first.
LLM_PROVIDER=openai            # or anthropic, if the gateway only speaks /v1/messages
LLM_BASE_URL=https://llm-gateway.intranet.example/v1
LLM_MODEL=<model-name>
LLM_API_KEY_ENV=<env-var-holding-the-key>

# 2. The customer's repositories (see backend/repos.example.yaml)
REPOS_FILE=/etc/cobol-agent/repos.yaml
```

### 5.1 Verify a new gateway before trusting it

A gateway that cannot do parallel tool calls will produce a pipeline that looks like it
works and yields poor artifacts. Probe it directly — this is the same check that was run
against a live gateway before the integration was written:

```bash
python - <<'EOF'
import json, os, urllib.request
base = os.environ["LLM_BASE_URL"].rstrip("/")
body = {"model": os.environ["LLM_MODEL"], "max_tokens": 512, "tools": [
    {"name": "read_file", "description": "read a file",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                      "required": ["file_path"]}},
    {"name": "glob", "description": "list files",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}}],
    "messages": [{"role": "user",
                  "content": "Use two tool calls in one turn: glob **/*.cbl, and read /repo/A.cbl"}]}
req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
    headers={"content-type": "application/json",
             "authorization": "Bearer " + os.environ.get("ANTHROPIC_AUTH_TOKEN", "")})
d = json.loads(urllib.request.urlopen(req, timeout=60).read())
calls = d["choices"][0]["message"].get("tool_calls") or []
print("tool calls in one response:", len(calls), [c["function"]["name"] for c in calls])
print("PASS" if len(calls) >= 2 else "FAIL - this gateway cannot fan out in parallel")
EOF
```

A **FAIL** here is a blocker, not a warning: the deliverable fan-out and most of the
analysis loop depend on multi-tool responses. Report it rather than working around it.

`repos.yaml` — one entry per repository. The PAT is **never** written here; only the
name of the environment variable that holds it:

```yaml
repos:
  - repo_key: core-banking
    name: Core Banking COBOL
    url: https://git.intranet.example/core/banking.git
    default_branch: main
    username: oauth2
    credential_env: PAT_CORE_BANKING   # exported from the secret store
    shallow: false
```

Then export the secret and restart the API:

```bash
export PAT_CORE_BANKING=<pat>
```

**First checks after switching:**

1. `curl -H "Authorization: Bearer $KEY" localhost:8000/api/v1/repos` lists the repository.
2. `POST /api/v1/repos/core-banking/resolve` returns a commit — if it fails with
   *"Git authentication failed"*, the PAT is expired or lacks `read_repository`.
3. Run one analysis on a small program and confirm the FDD content matches expectations.
   Expect the first run on a real codebase to be considerably slower and more expensive than
   the sample: budget scales with how much source the model chooses to read.

---

## 6. Architecture in one paragraph

The API process owns both HTTP and the run workers. Each run gets an isolated directory
under `data/runs/<run_id>/` containing a read-only checkout pinned to a commit, a
writable work area, and the deliverables. The agent is a `deepagents` graph whose
filesystem access is routed to that directory through a `CompositeBackend`, with `/repo`
read-only and credential-shaped paths denied. It writes the intermediate analysis, then
three independent agent invocations generate the deliverables in parallel with a
semaphore; one failing deliverable never fails the run. Progress is written to an
append-only event journal that also backs the SSE stream, so a browser refresh replays
from the last sequence number instead of losing history.

---

## 7. Known gaps

Every gap has an owner action and the milestone from `docs/DESIGN.md` §5.4 that closes it.
This list is the honest boundary of what the demo claims.

| # | Gap | Impact | Closes in |
|---|---|---|---|
| 1 | ~~Model gateway is mocked~~ | **Closed, and the mock is now deleted.** The pipeline runs against the configured gateway; a live end-to-end run is recorded in §1. There is no offline mode, so `e2e` needs a credential and skips loudly without one. | — |
| 2 | **Test coverage is concentrated.** `test_security.py` covers every §5 hard rule plus the backend path layout, and `test_askpass.py` / `test_auth.py` / `test_skills.py` / `test_run_lifecycle.py` / `test_services.py` cover the askpass hook, the auth modes, skill loading, run termination and a few service invariants (55 test functions, 100 cases via parametrisation). The pipeline stages themselves and the whole frontend remain untested. | A refactor of the pipeline phases, the workbook writers or the console could break behaviour that `e2e` happens not to exercise. | M2 — extend to the pipeline phases; M4 — Vitest for hooks |
| 3 | **Runs are in-process.** A restart marks in-flight runs failed. | Cannot survive a deploy mid-run; no queue, no retry. | M6 — move to a Redis queue |
| 4 | **Knowledge base retrieval is lexical** (BM25-style), not vector. | Good on identifiers and field names, weaker on paraphrase. No embedding model is called. | M5 — implement `Retriever` against the intranet vector store |
| 5 | **The knowledge-base answer is assembled, not generated.** It presents retrieved evidence and says so. | Reads as a results list, not as prose. Deliberate: with no model in the loop, inventing fluent text would be worse. | M5 — the gateway becomes the answer generator |
| 6 | **No approvals UI.** `interrupt_on` is not wired. | A run cannot pause for human confirmation. Nothing in the demo path needs it. | M4 |
| 7 | **No customer Excel templates.** Column names are ours. | Generated workbooks may not match the delivery spec. | The moment templates arrive, they win |
| 8 | **No SSO yet; one role for everyone behind the proxy.** `AUTH_MODE=trusted_header` takes the identity from an unverified header, and every such caller gets `TRUSTED_DEFAULT_ROLE`. Roles exist and are enforced, but not per user. | No per-user attribution in the audit log beyond the header's identity value, and every browser user can delete runs unless `TRUSTED_DEFAULT_ROLE=analyst`. `AUTH_MODE=e2e_token` is reserved and returns 501 until the gateway verification is written. | When the SSO integration lands — only `core/auth.py:authenticate` changes |
| 9 | **No content-level redaction pipeline.** `mask_secrets()` covers credentials in logs only; repository *content* is sent to the model unfiltered. | **Accepted risk, explicitly waived by the project owner**: source code is out of scope for sensitive-data filtering. The risk that remains is not the code itself but any *real customer data* embedded in test fixtures, JCL parameters or copybook literals. | Revisit if the scope changes to include production data extracts; `llm_egress_log` (DESIGN.md §6.3) is the place to build the audit trail |
| 10 | **No offline-diff/golden regression set.** | Prompt or skill changes cannot be regression-tested against a known-good output. | M6 |
| 11 | **Static analysis is declared but not wired.** ESLint, Prettier, Vitest and Playwright are named as gates in `CODESTYLE.md` / `CONTRIBUTING.md` but are not installed in `frontend/`, and `mypy` is a declared dev dependency with no configuration and no task. `pnpm typecheck` (`tsc --noEmit`) is the only frontend gate today; `ruff` covers formatting and lint for the backend. | Style and dead-code findings are caught by review rather than by a machine, and the documents name gates a new contributor cannot run. | M4 — add the frontend toolchain; whenever the Python type gate is wanted, add `[tool.mypy]` plus a `dev.py` task |

> Two gaps deserve emphasis now that a real model is wired in.
>
> **Gap 1 is closed and gap 9 is waived**, so the pipeline genuinely calls the gateway: source
> code leaves the host. That was a deliberate decision, and it moves the risk from "content
> filtering" to **cost control** and **gateway limits**:
>
> * Budgets are enforced in code (`MAX_TOOL_CALLS`, `MAX_WALL_SECONDS`) but
>   there is no currency-denominated cap at all — no such setting exists, so spend is bounded
>   by tool calls and wall clock, never by money.
>   A runaway loop is bounded by tool calls, not by spend.
> * Every run records tokens and tool calls in `manifest.json`, so cost is *observable*
>   after the fact even though it is not yet *capped*.
> * A real analysis of a large COBOL codebase will cost far more than this 48 k-token
>   sample. Run the first real repository with a small entry point and watch the usage
>   figures before pointing it at a mainframe-sized one.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/api/v1/health` returns 401 | It is authenticated on purpose — the payload names the data root and the model gateway, which is an internal-layout disclosure | Use `GET /` for an unauthenticated liveness probe, or set `AUTH_MODE=disabled` locally |
| `Cannot reach the API service` | API not running | `python scripts/dev.py api` (check the port matches `VITE_API_TARGET`) |
| `Missing 'E2E-token' header` | Calling the API directly instead of through the proxy that sets it | Use the console on :5173, or set `AUTH_MODE=disabled` for local curl work |
| Browser shows "Cannot reach the API service" but the API is up | The dev proxy is not running, so no identity header is attached | Start `pnpm dev`; the proxy is what authenticates the console |
| Run fails at **Analyze** with *"did not produce /work/analysis.json"* | The gateway is not returning tool calls, so the analysis skill never wrote its hand-off | Check `LLM_BASE_URL`; a gateway without function calling cannot drive this agent |
| Run fails with *"Tool call budget exhausted"* | The model is looping — usually because the skill file it was told to read does not exist, so it improvises rather than following a procedure | Check the run's log for skills load errors; verify `SKILLS_DIR` and that the skill directory name matches `ARTIFACT_SPECS[...]["skill"]` |
| *"Git authentication failed"* | Expired or under-scoped PAT | Update the secret and `POST /api/v1/admin/reload-repos` |
| A deliverable is `failed` but the run is `completed` | Working as designed — failures are isolated | Read the error on the artifact card; other deliverables are unaffected |
| `rm -rf data` fails with `Permission denied` on source files | Run checkouts are sealed read-only at the OS level (hard rule S5), which is exactly what `rm` is hitting | Use `python scripts/dev.py purge-data`, which unseals first and asks for confirmation itself |
| A task fails with `command not found: make` | `make` is not installed — it is not part of Windows | Use `python scripts/dev.py <task>`; it is the same definition |
| Artifact preview 404s | Not generated, or the kind filter is wrong | Check the artifact state on the run page |
| Run fails at **Analyze** in ~1 s with `did not produce /work/analysis.json` | A path-layout mismatch between a backend route and the directory it resolves to, or the gateway is not returning tool calls at all | Check the gateway probe in §5.1; `test_virtual_paths_map_to_the_intended_physical_files` guards the layout |
| A workbook deliverable fails with `Cannot convert [...] to Excel` | The model returned a field as a JSON array where the schema describes a string — now coerced automatically, so seeing this means a genuinely unsupported value (e.g. a nested object) | Inspect the artifact's `error`; `_cell_value` in `services/artifacts.py` is the coercion point |
| A deliverable contains the model's own commentary before the content | The prompt is leaking implementation detail, so the model narrates its process | Keep implementation notes out of prompt text — see the contract comment above `ARTIFACT_PROMPT` |
| Every artifact is ready but the content is thin and generic | The gateway accepted `tools` but silently ignores them (or the model is too weak) | Run the §5.1 probe; try `ANALYSIS_MODEL` with a stronger model |

---

## 9. Demo script (5 minutes)

1. **Frame it** — "We point at a repository revision and get three deliverables, traceable
   back to the exact commit." *(10 s)*
2. **New analysis** — resolve the commit; point out that the pinned SHA is shown *before*
   submitting. *(30 s)*
3. **Start** — switch to the run page immediately so the pipeline is watched live rather
   than reported after the fact. *(30 s)*
4. **Name what is happening** — "Fetching a pinned checkout, indexing, then the analysis
   skill; the three deliverables generate in parallel and one failing does not stop the
   others." *(60 s)*
5. **Open the FDD** — scroll to a business rule and follow its code evidence. *(60 s)*
6. **Download the test case workbook** — show the row count and that the Run info sheet
   records the commit and run id. *(45 s)*
7. **Knowledge base** — ask the ACCT_NO question; follow a citation back into the run.
   *(60 s)*
8. **Close on the boundary** — state one known gap unprompted (gap 9 or 2). Volunteering a
   limitation is what makes the rest of the demo credible. *(30 s)*
