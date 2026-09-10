"""Shared test fixtures.

Fixtures are intentionally small and filesystem-local: no network, no live model, no
intranet resource (CODESTYLE.md §4).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SAMPLE_PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. PGM001.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CUST-REC.
           05  CUST-ACCT-NO  PIC X(12).
       PROCEDURE DIVISION.
       MAIN-PARA.
           CALL 'PGM002'.
           EXEC SQL SELECT ACCT_NO FROM CUSTOMER END-EXEC.
           STOP RUN.
"""

SAMPLE_COPYBOOK = """       01  CUSTCPY-REC.
           05  CPY-STATUS  PIC X(02).
"""


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Settings pointed at a throwaway data root so tests never touch the repo.

    The model endpoint is set to a placeholder. ``LLM_BASE_URL`` and ``LLM_MODEL`` are required
    because the project names no vendor: a baked-in default would be wrong for every other
    deployment. Unit tests never call the gateway, so the values only need to parse.
    """
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("API_KEYS", "test-key:admin")
    monkeypatch.setenv("LLM_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.delenv("REPOS_FILE", raising=False)

    from app.core.config import Settings

    config = Settings()
    config.ensure_dirs()
    return config


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A minimal but realistic COBOL repository on disk."""
    repo = tmp_path / "sample"
    (repo / "cpy").mkdir(parents=True)
    (repo / "PGM001.cbl").write_text(SAMPLE_PROGRAM, encoding="utf-8")
    (repo / "cpy" / "CUSTCPY.cpy").write_text(SAMPLE_COPYBOOK, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main", ".")
    git("add", "-A")
    git("-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "sample")
    return repo


@pytest.fixture
def git_service(settings, sample_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """A GitService whose default ``demo`` repository points at the sample repo.

    The settings are rebuilt after the environment is patched: ``demo_repo_url`` is read from
    ``Settings`` at construction time, so the ``settings`` fixture (built before this patch)
    would otherwise carry an empty demo URL.
    """
    monkeypatch.setenv("DEMO_REPO_URL", str(sample_repo))
    monkeypatch.setenv("DEMO_REPO_BRANCH", "main")

    from app.core.config import Settings
    from app.services.git_service import GitService

    return GitService(Settings())


@pytest.fixture
def run_root(settings, git_service, tmp_path: Path) -> Path:
    """A checked-out worktree laid out exactly as a real run directory."""
    run_id = "testrun"
    commit = git_service.resolve_ref("demo", None)
    git_service.create_worktree("demo", run_id, commit)
    root = git_service.run_dir(run_id)
    (root / "work").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def build_agent_backend(settings):
    """The backend the *pipeline* builds, for a given run root.

    Security tests assert on this and not on a test-only builder: a second route table that
    production never uses is how a permission test passes while the shipped wiring differs.
    The skills directory is the repository's own, so the fixture exercises the mounted
    ``/skills`` route as well.
    """
    from app.agent.graph import build_backend_with_skills

    skills_dir = Path(__file__).resolve().parent.parent / "skills"
    cfg = settings.model_copy(update={"skills_dir": skills_dir, "skills_enabled": True})

    def _build(run_root: str):
        backend, _sources = build_backend_with_skills(cfg, run_root)
        return backend

    return _build
