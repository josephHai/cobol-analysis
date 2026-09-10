#!/usr/bin/env python
"""End-to-end integration test: the whole pipeline against the real model gateway.

Creates a throwaway COBOL repository, registers it as the ``demo`` repo, starts the API
in-process, submits a run, follows the SSE stream, and asserts that all three deliverables
land and the knowledge base answers a question about them.

Every code path is real: git, the agent, the skills, the workbooks, the knowledge base. The
only thing this test does not control is the model, which is the point — it is the check that
must pass before a demo, and a mock gateway cannot validate a prompt or a skill.

**If no credential is configured the test exits 0 with a SKIP notice** rather than passing
silently. A silent pass would make this worthless as a gate. Run it in CI only where a key is
available; locally it needs one exported (see docs/DEMO.md §3).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

IDENTITY = "e2e-smoke"

SAMPLE_PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. PGM001.
       ENVIRONMENT DIVISION.
       DATA DIVISION.
       FILE SECTION.
       FD  CUSTFILE.
       WORKING-STORAGE SECTION.
       01  WS-CUST-REC.
           05  CUST-ACCT-NO   PIC X(12).
           05  CUST-NAME      PIC X(30).
       01  WS-BALANCE        PIC 9(9)V99.
       COPY CUSTCPY.
       PROCEDURE DIVISION.
       MAIN-PARA.
           CALL 'PGM002' USING WS-CUST-REC.
           EXEC CICS LINK PROGRAM('PGM003') END-EXEC.
           EXEC SQL SELECT ACCT_NO, BALANCE FROM CUSTOMER
                WHERE ACCT_NO = :CUST-ACCT-NO END-EXEC.
           PERFORM CALC-INTEREST.
           STOP RUN.
       CALC-INTEREST.
           COMPUTE WS-BALANCE = WS-BALANCE * 1.05.
"""

SAMPLE_COPYBOOK = """       01  CUSTCPY-REC.
           05  CPY-CUST-ID    PIC X(10).
           05  CPY-STATUS     PIC X(02).
"""

SAMPLE_JCL = """//RUN001  JOB (ACCT),'CUSTOMER INQUIRY'
//STEP1   EXEC PGM=PGM001
//STEP2   EXEC PGM=PGM002
"""


def log(step: str, message: str = "") -> None:
    print(f"[{step:>10}] {message}", flush=True)


def make_sample_repo(base: Path) -> Path:
    """A tiny but realistic COBOL repository — copybooks, JCL, CICS and DB2."""
    repo = base / "src"
    (repo / "cpy").mkdir(parents=True)
    (repo / "jcl").mkdir(parents=True)
    (repo / "PGM001.cbl").write_text(SAMPLE_PROGRAM, encoding="utf-8")
    (repo / "cpy" / "CUSTCPY.cpy").write_text(SAMPLE_COPYBOOK, encoding="utf-8")
    (repo / "jcl" / "RUN001.jcl").write_text(SAMPLE_JCL, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-q", "-b", "main", ".")
    git("add", "-A")
    git(
        "-c",
        "user.email=demo@example.com",
        "-c",
        "user.name=Demo",
        "commit",
        "-qm",
        "sample",
    )
    return repo


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="cobol-e2e-"))
    data_root = workdir / "data"
    repo = make_sample_repo(workdir)

    os.environ["DATA_ROOT"] = str(data_root)
    os.environ["DEMO_REPO_URL"] = str(repo)
    os.environ["DEMO_REPO_BRANCH"] = "main"
    # Match the deployment default: the caller is identified by a header supplied by whatever
    # proxies to the service, not by a key the caller holds.
    os.environ["AUTH_MODE"] = "trusted_header"
    os.environ["TRUSTED_ACTOR_HEADER"] = "E2E-token"
    # Requires a real credential. Refuse to run without one instead of quietly
    # "passing": a green run that never called a model would be worse than no test.
    from app.core.config import Settings as _Settings

    if not _Settings().resolve_api_key():
        print(
            "[      SKIP] No model credential found. This test needs a live gateway; "
            "export ANTHROPIC_AUTH_TOKEN (or set LLM_API_KEY_ENV) and re-run.",
            flush=True,
        )
        shutil.rmtree(workdir, ignore_errors=True)
        return 0

    log("setup", f"workdir={workdir}")
    log("setup", f"sample repo={repo}")

    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        # Stands in for the proxy: the tests speak to the app directly, so they must supply
        # the header the proxy would have injected.
        headers = {"E2E-token": IDENTITY}

        # ---------------------------------------------------------- preflight
        health = client.get("/api/v1/health", headers=headers)
        assert health.status_code == 200, health.text
        h = health.json()
        log("health", f"model={h['model']} gateway={h['llm_base_url']}")

        repos = client.get("/api/v1/repos", headers=headers).json()
        log("repos", f"{[r['repo_key'] for r in repos]}")
        assert any(r["repo_key"] == "demo" for r in repos), (
            "demo repo must be registered"
        )

        resolved = client.post("/api/v1/repos/demo/resolve", headers=headers).json()
        log("resolve", f"commit={resolved['commit_sha'][:12]}")
        assert resolved["commit_sha"]

        # ------------------------------------------------------------- run
        created = client.post(
            "/api/v1/runs",
            headers=headers,
            json={
                "repo_key": "demo",
                # The request is free text; the entry point is derived from it. Naming the
                # program here keeps the smoke test deterministic.
                "message": (
                    "Analyse program PGM001: the customer balance inquiry. Cover its call "
                    "relationships, the business rules behind the balance calculation, and the "
                    "fields it reads from the CUSTOMER table."
                ),
                "skills": ["fdd", "test_case", "data_mapping"],
                "locale": "en",
            },
        )
        assert created.status_code == 201, created.text
        run = created.json()
        run_id = run["id"]
        log("run", f"created {run_id}")

        # Follow the SSE stream exactly as the browser does, to prove the contract.
        phases_seen: list[str] = []
        artifacts_seen: list[str] = []
        deadline = time.time() + 180
        with client.stream(
            "GET", f"/api/v1/runs/{run_id}/events", headers=headers
        ) as stream:
            event_name = ""
            for line in stream.iter_lines():
                if time.time() > deadline:
                    raise TimeoutError(
                        "run did not finish within the smoke-test budget"
                    )
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if event_name == "phase":
                        phases_seen.append(f"{data['phase']}:{data['status']}")
                    elif event_name == "artifact" and data.get("state") == "ready":
                        artifacts_seen.append(data["kind"])
                    elif event_name == "log":
                        log("log", data.get("message", ""))
                    elif event_name == "done":
                        log(
                            "done", f"status={data['status']} usage={data.get('usage')}"
                        )
                        break

        final = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        log("status", final["status"])
        assert final["status"] == "completed", f"run failed: {final.get('error')}"

        # -------------------------------------------------------- artifacts
        states = {a["kind"]: a["state"] for a in final["artifacts"]}
        log("artifacts", json.dumps(states))
        for kind in ("fdd", "test_case", "data_mapping"):
            assert states.get(kind) == "ready", f"{kind} is {states.get(kind)}"

        fdd = client.get(f"/api/v1/runs/{run_id}/artifacts/fdd", headers=headers).json()
        assert (
            fdd["type"] == "markdown" and "Functional Design Document" in fdd["content"]
        )
        log("fdd", f"{len(fdd['content'].splitlines())} lines of markdown")

        cases = client.get(
            f"/api/v1/runs/{run_id}/artifacts/test_case", headers=headers
        ).json()
        sheet = next(s for s in cases["sheets"] if s["name"] == "Test cases")
        log("test_case", f"{len(sheet['rows'])} rows, {len(sheet['columns'])} columns")
        assert len(sheet["rows"]) >= 3, "expected at least one case per discovered rule"

        mapping = client.get(
            f"/api/v1/runs/{run_id}/artifacts/data_mapping", headers=headers
        ).json()
        msheet = next(s for s in mapping["sheets"] if s["name"] == "Data mapping")
        log("mapping", f"{len(msheet['rows'])} rows")
        assert len(msheet["rows"]) >= 1

        for kind in ("fdd", "test_case", "data_mapping"):
            dl = client.get(
                f"/api/v1/runs/{run_id}/artifacts/{kind}/download", headers=headers
            )
            assert dl.status_code == 200 and len(dl.content) > 0, (
                f"download failed for {kind}"
            )
        log("download", "all three deliverables downloaded")

        # ------------------------------------------------------ knowledge base
        answer = client.post(
            "/api/v1/kb/query",
            headers=headers,
            json={"question": "Which program reads ACCT_NO from the CUSTOMER table?"},
        ).json()
        log("kb", f"{len(answer['citations'])} citations via {answer['provider']}")
        assert answer["citations"], "knowledge base returned no citations"

        # ------------------------------------------------ workspace inspection
        run_dir = data_root / "runs" / run_id
        produced = sorted(
            p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file()
        )
        log("files", f"{len(produced)} files under the run directory")
        for name in produced:
            print(f"             {name}")

        # Read-only sealing is a POSIX control: NFTS ignores POSIX mode bits, so on Windows the
        # layer is absent by design and `seal_worktree` logs a warning instead of claiming
        # success. Asserting 444 there would fail a correct build.
        if os.name == "nt":
            log(
                "sealed",
                "skipped: read-only sealing is not enforceable on this platform",
            )
        else:
            checkout = run_dir / "repo" / "PGM001.cbl"
            mode = oct(checkout.stat().st_mode)[-3:]
            log("sealed", f"checkout mode={mode} (expect 444)")
            assert mode == "444", "the checkout must be read-only at the OS level"

    log("PASS", "end-to-end pipeline verified")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
