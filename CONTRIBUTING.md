# Contributing

How we manage this repository: layout, branches, commits, review, versioning and releases.
For *how to write the code*, see `CODESTYLE.md`. For *what we are building*, see `docs/DESIGN.md`.

---

## 1. Repository shape: monorepo

One repository holds the API, the agent runtime, the web client, the shared contracts and the skills.

**Why one repository rather than four:**
the contracts (`contracts/openapi.json`, artifact JSON Schemas, event schemas) are consumed by *both* the backend and the frontend. Split repositories would force a version-negotiation dance for every field added during active development, which is precisely the cost we cannot afford on this timeline. Skills and prompts also version together with the code that executes them.

**When to split** — revisit if any of these becomes true:

| Trigger | Split out |
|---|---|
| Delivery deadlines for web and agent diverge by more than a sprint | `cobol-web` |
| The agent runtime is consumed by a second product | `cobol-agent-core` (library) |
| The skills gain their own release cadence and external contributors | `cobol-skills` |
| Compliance requires the skills/prompt IP to be access-controlled separately | `cobol-skills` |

```
cobol-analysis/
├── README.md  CODESTYLE.md  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md
├── scripts/dev.py               # the single definition of every dev task (no Makefile)
├── .editorconfig  .gitattributes  .gitignore  .env.example
├── CLAUDE.md                    # entry point for AI coding tools
├── docs/
│   ├── DESIGN.md                # architecture (single source of truth)
│   ├── CODESTYLE.md             # coding rules
│   ├── DEMO.md                  # demo runbook + known gaps
├── contracts/                   # cross-cutting contracts (single source of truth)
│   └── openapi.json
├── backend/
│   ├── pyproject.toml  uv.lock
│   ├── app/                     # see CODESTYLE.md §2.1
│   ├── skills/                  # versioned skill packages
│   └── tests/{unit,integration,fixtures}
├── frontend/
├── deploy/{systemd,otel,grafana}/
└── scripts/
```

---

## 2. Contracts first

`contracts/` is the single source of truth for anything crossing a boundary.

| Artifact | Consumer | Generation |
|---|---|---|
| `openapi.json` | FastAPI routers, web client types, E2E tests | `python scripts/dev.py openapi` |
| `artifact-schemas/*.json` | skill output validation, KB chunking, web tables | `python scripts/dev.py openapi` |
| `events.schema.json` | SSE emitter and the web event reducer | `python scripts/dev.py openapi` |

**The rule:** a change to a boundary starts in `contracts/`, then propagates through generated types.
Changing the same field by hand on both sides is a defect even when it works.

CI enforces this: regenerating from the contracts must produce no diff. A PR that changes a route or an artifact field without touching `contracts/` fails the build.

---

## 3. Branching and commits

**Trunk-based with short-lived branches.** `main` is always green and always deployable.

```
main ──●──●──●──●──●──●──●   (protected; green)
        \      \       \
         feat/…  fix/…   chore/…   (each lives ≤ 3 days)
```

| Branch | Use for |
|---|---|
| `feat/<slug>` | New capability |
| `fix/<slug>` | Defect repair |
| `chore/<slug>` | Tooling, dependencies, refactors with no behaviour change |
| `docs/<slug>` | Documentation only |
| `hotfix/<slug>` | Production incident; may be merged with a reduced checklist |

**Commit messages** follow Conventional Commits, in English:

```
feat(agent): add read_slice tool with line-range clamping
fix(git): mask PAT in subprocess error output
docs(design): record the decision to keep approvals backend-only
chore(deps): bump deepagents to 0.7.13
```

Rules:
- Scope names come from the layer or feature: `agent`, `git`, `api`, `web`, `kb`, `contracts`, `skills`, `deploy`.
- One logical change per commit. A commit that both refactors and fixes is two commits.
- Describe the *why* in the body when the reason is not obvious from the diff.
- Never commit generated files (build output, `.venv`, `data/`).

---

## 4. Pull requests

Every PR description answers four questions:

1. **What changed** — one paragraph, no diff narration.
2. **Why** — the problem, with a link to the issue.
3. **How it was verified** — the exact commands run, plus observed output for anything non-obvious.
4. **What is not done** — any `# TODO(demo):`, follow-up issue, or accepted limitation.

**Review gates (all must pass):**

| Gate | Command |
|---|---|
| Formatting and lint | `python scripts/dev.py lint` |
| Types | `python scripts/dev.py typecheck` |
| Unit + integration tests | `python scripts/dev.py test` |
| Contract drift | `python scripts/dev.py contracts-check` |
| Frontend build | `python scripts/dev.py web-build` |
| Secret scan | `gitleaks detect` |
| Dependency audit | `pip-audit`, `pnpm audit` |

**Review requirements:**

| Change touches | Approval needed |
|---|---|
| `backend/skills/**`, prompts, `contracts/**` | One reviewer **plus** the skill/contract owner |
| `deploy/**`, `.github/**`, anything handling credentials | One reviewer **plus** the security owner |
| `docs/**`, comments only | One reviewer |
| Everything else | One reviewer |

**CODEOWNERS** must list at least: `backend/skills/`, `backend/app/agent/`, `contracts/`, `deploy/`.

---

## 5. The three-question review (what reviewers actually check)

Beyond reading the diff, every reviewer answers:

1. **Does this break a hard rule in `CODESTYLE.md` §5?** If it touches security-adjacent code, was the matching test added?
2. **Is the blast radius understood?** For a prompt or schema change: what happens to runs already in flight, and to previously generated artifacts?
3. **Will this be comprehensible in three months?** Names, comments and the PR description should let a new engineer reconstruct the reasoning.

Reviewers do not re-litigate style that `ruff`, `prettier` and `eslint` already enforce. Machines handle formatting; humans handle design.

---

## 6. Versioning and releases

| Unit | Versioned by | Recorded in |
|---|---|---|
| Service | SemVer tag `v0.2.0` | CHANGELOG.md, image/artifact label |
| Skills | Independent SemVer in `skills/<name>/SKILL.md` frontmatter | every run's `manifest.json` |
| Prompts | Content hash | every run's `manifest.json` |
| Artifact schemas | `schema_version` field in each schema | every produced artifact |

**Why skills and prompts carry their own version:** a generated FDD is only reproducible if we can state which skill text and which prompt hash produced it. When a customer disputes a document, `manifest.json` is the answer.

Release steps:

```bash
python scripts/dev.py check && python scripts/dev.py e2e
# update CHANGELOG.md under a new "## [x.y.z] - YYYY-MM-DD" heading
git tag -a v0.2.0 -m "v0.2.0"
python scripts/dev.py web-build   # production frontend, then tag the artefacts
```

Immutable labels: every release is tagged with its git SHA. Never re-tag or overwrite an existing version.

---

## 7. Demo sprint mode (temporary, time-boxed)

The demo has a hard deadline. To move fast without accruing silent debt, this mode is explicitly time-boxed and its exits are defined.

**Permitted during demo mode:**

- Merging to `main` with a self-review, provided the PR description is complete and CI is green.
- `# TODO(demo):` markers instead of complete implementations — **but only for items listed in `CODESTYLE.md` §6**.
- Skipping the E2E gate for changes outside the demo path (must be stated in the PR).
- Deferring performance work.

**Never permitted in demo mode:** any `CODESTYLE.md` §5 hard rule, the layering direction, or hand-editing generated types.

**Every `# TODO(demo):` must be registered in `docs/DEMO.md` under "Known gaps"** with an owner and the milestone that closes it. An unregistered TODO is treated as an unanswered review comment.

**Exit criteria for demo mode** — whichever comes first:

1. The demo is delivered, **or**
2. Three working days elapse without a demo-path change.

On exit: sweep every `# TODO(demo):`, convert each into an issue or fix it, and delete the markers. The feature-freeze list below then applies again.

---

## 8. Issues and planning

- Every non-trivial change starts as an issue. Trivial means: typo, comment, formatting, dependency patch bump.
- Issue titles are imperative and English: `Add SSE replay from Last-Event-ID`.
- Labels: `area:backend|web|agent|contracts|deploy`, `type:feat|fix|chore|docs`, `priority:P1..P3`, `security`.
- Anything touching a security hard rule carries the `security` label and gets the security owner as reviewer.
- Milestones follow `docs/DESIGN.md` §5.4 (M0–M6). Work not attached to a milestone is either a bug or an unplanned feature — say which in the issue.

---

## 9. Documentation duties

| Change | Documentation that must be updated in the same PR |
|---|---|
| Architecture, layering, data flow | `docs/DESIGN.md` |
| A rule future contributors must follow | `docs/CODESTYLE.md` (append; do not rewrite history without an ADR) |
| A consequential design decision | A **decision record** section in `docs/DESIGN.md` — context, decision, consequences, revisit criteria |
| Demo-affecting behaviour or a new known gap | `docs/DEMO.md` |
| A contract change | `contracts/` **and** the generated types |

Decision records live in `docs/DESIGN.md` and are appended, not rewritten: to reverse one, add a new section that supersedes it and mark the old one accordingly. A separate `docs/adr/` directory is worth introducing once the project outlives its demo.

---

## 10. Security reporting

Do not open a public issue for a vulnerability. Report it to the security owner directly, with:
affected component, reproduction steps, impact, and any suggested mitigation.
See `SECURITY.md`. Credential exposure is always treated as a P1 incident — if a PAT
reaches a log, a commit, or a model prompt, rotate it first and investigate second.
