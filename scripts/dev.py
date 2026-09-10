#!/usr/bin/env python
"""Development tasks, defined once for every platform.

A Python task runner rather than a Makefile, for three reasons: GNU Make is not part of Windows,
Makefile recipes assume a POSIX shell (``grep``, ``find``, ``rm -rf``, ``trap``), and a virtualenv
puts its interpreter at ``.venv/bin/python`` on POSIX but ``.venv\\Scripts\\python.exe`` on Windows.
Any of those makes the developer instructions wrong for somebody.

There is deliberately no Makefile forwarding to this: bootstrapping the toolchain already requires
Python, so ``make install`` would only add a second surface that can break (tab indentation) and
drift (task names documented in two places).

    python scripts/dev.py                # list tasks
    python scripts/dev.py check          # lint + tests
    python scripts/dev.py api            # run the API
    python scripts/dev.py check --print  # show the commands without running them

Every task is also just a command you can type yourself.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"

#: A task is a list of steps. A step is either an argv to execute, or a single-element list naming
#: another task to compose. Composition rather than duplication is what keeps `check` honest about
#: what it covers.
Step = list[str]
Task = Callable[[], list[Step]]


def venv_python() -> Path:
    """Path to the virtualenv interpreter, which differs by platform.

    ``uv venv`` follows the platform convention: ``bin/python`` on POSIX and
    ``Scripts/python.exe`` on Windows. Hard-coding the POSIX path is the most common way a
    developer instruction quietly breaks on Windows.
    """
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_exe(name: str) -> str:
    """Path to a console script installed in the virtualenv (``ruff``, ``pytest``, …)."""
    path = (
        VENV
        / ("Scripts" if os.name == "nt" else "bin")
        / (f"{name}.exe" if os.name == "nt" else name)
    )
    return str(path)


def py(*args: str) -> Step:
    """Run something with the virtualenv interpreter, falling back to the current one.

    The fallback matters before ``install`` has run: an ambient interpreter gives a clear
    "module not found" instead of a confusing "file not found".
    """
    python = venv_python()
    return [str(python if python.exists() else sys.executable), *args]


def pnpm() -> str:
    """pnpm is a ``.cmd`` shim on Windows, so it needs the extension to be resolvable."""
    return "pnpm.cmd" if os.name == "nt" else "pnpm"


def in_frontend(step: Step) -> bool:
    return step[0] == pnpm()


# --------------------------------------------------------------------------------- tasks


def task_install() -> list[Step]:
    return [
        ["uv", "venv", "--python", "3.13", str(VENV)],
        [
            "uv",
            "pip",
            "install",
            "-p",
            str(VENV),
            "-r",
            str(BACKEND / "pyproject.toml"),
        ],
        [pnpm(), "install"],
    ]


def task_api() -> list[Step]:
    port = os.environ.get("API_PORT", "8000")
    return [
        py(
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(BACKEND),
            "--port",
            port,
            "--reload",
        )
    ]


def task_web() -> list[Step]:
    return [[pnpm(), "dev"]]


def task_fmt() -> list[Step]:
    return [
        [venv_exe("ruff"), "format", str(BACKEND)],
        [venv_exe("ruff"), "check", "--fix", str(BACKEND)],
    ]


def task_lint() -> list[Step]:
    return [
        [venv_exe("ruff"), "format", "--check", str(BACKEND)],
        [venv_exe("ruff"), "check", str(BACKEND)],
    ]


def task_typecheck() -> list[Step]:
    return [[pnpm(), "typecheck"]]


def task_test() -> list[Step]:
    return [py("-m", "pytest", "-q", "-c", str(BACKEND / "pyproject.toml"))]


def task_check() -> list[Step]:
    """The pre-commit gate from CODESTYLE.md §7: format, lint, tests, contracts, skills.

    Composed, so it cannot miss a step: the contract and skill checks are in here rather than
    left to a reviewer to remember, because both catch drift that a green test run would not.
    """
    return [["lint"], ["test"], ["contracts-check"], ["skills-check"]]


def task_skills_check() -> list[Step]:
    args = ["scripts/skills_check.py"]
    if os.environ.get("SKILLS_DIR"):
        args += ["--skills-dir", os.environ["SKILLS_DIR"]]
    return [py(*args)]


def task_e2e() -> list[Step]:
    return [py("scripts/e2e_smoke.py")]


def export_openapi() -> int:
    """Write the API contract to ``contracts/openapi.json``.

    In-process rather than a separate script: the work is three lines and the logic it needs
    lives in ``app/``. A script would only add another place that repeats the ``sys.path``
    bootstrap.
    """
    import json

    # The app refuses to start without a model endpoint, which is deliberate. Contract generation
    # does not need a real one, so supply placeholders when the environment has none — otherwise
    # `openapi` and `contracts-check` would demand gateway credentials in CI for no reason.
    os.environ.setdefault("LLM_BASE_URL", "http://gateway.invalid/v1")
    os.environ.setdefault("LLM_MODEL", "contract-generation")

    sys.path.insert(0, str(BACKEND))
    from app.main import app

    target = ROOT / "contracts" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = app.openapi()
    target.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {target.relative_to(ROOT)} ({len(schema['paths'])} paths)")
    return 0


def check_contracts() -> int:
    """Fail when the committed contract differs from what the app serves.

    Without this, a route change can land while the contract and the client types silently drift.
    """
    import json

    # The app refuses to start without a model endpoint, which is deliberate. Contract generation
    # does not need a real one, so supply placeholders when the environment has none — otherwise
    # `openapi` and `contracts-check` would demand gateway credentials in CI for no reason.
    os.environ.setdefault("LLM_BASE_URL", "http://gateway.invalid/v1")
    os.environ.setdefault("LLM_MODEL", "contract-generation")

    sys.path.insert(0, str(BACKEND))
    from app.main import app

    target = ROOT / "contracts" / "openapi.json"
    current = app.openapi()
    if not target.exists():
        print(
            f"{target.relative_to(ROOT)} is missing. Run `python scripts/dev.py openapi`."
        )
        return 1

    committed = json.loads(target.read_text(encoding="utf-8"))
    if committed == current:
        print(f"contract is up to date ({len(current['paths'])} paths)")
        return 0

    for added in sorted(set(current["paths"]) - set(committed.get("paths", {}))):
        print(f"  + {added}  (in code, missing from the contract)")
    for removed in sorted(set(committed.get("paths", {})) - set(current["paths"])):
        print(f"  - {removed}  (in the contract, missing from code)")
    print(
        "Contract drift detected. Run `python scripts/dev.py openapi` and commit the result."
    )
    return 1


def gc() -> int:
    """Reclaim disk from runs past their TTL, keeping artifacts and manifests."""
    import json

    sys.path.insert(0, str(BACKEND))
    from app.core.config import get_settings
    from app.services.git_service import GitService

    service = GitService(get_settings())
    before = service.disk_usage_mb()
    report = service.gc()
    print(json.dumps(report, indent=2))
    print(f"disk usage: {before} MB -> {report['disk_usage_mb']} MB")
    return 0


def task_openapi() -> list[Step]:
    return [py("scripts/dev.py", "_openapi")]


def task_contracts_check() -> list[Step]:
    return [py("scripts/dev.py", "_contracts")]


def task_gc() -> list[Step]:
    return [py("scripts/dev.py", "_gc")]


def task_purge_data() -> list[Step]:
    """Delete all runtime data, after unsealing the read-only checkouts.

    Requires ``--yes``: it is the only destructive task, and it deletes deliverables that cost a
    model run to produce.
    """
    return [py("scripts/purge_data.py", "--yes")]


def task_web_build() -> list[Step]:
    return [[pnpm(), "build"]]


def task_clean() -> list[Step]:
    """Remove build and cache artefacts. Never touches ``data/``."""
    return [py("scripts/dev.py", "_clean")]


def task_dev() -> list[Step]:
    """Both long-lived services, which ``run_pair`` starts concurrently.

    Declared as a normal task list so ``--print dev`` shows what would actually run, rather
    than a placeholder pointing at one half of the pair.
    """
    return [*task_api(), *task_web()]


TASKS: dict[str, tuple[str, Task]] = {
    "install": (
        "Create the venv and install backend + frontend dependencies",
        task_install,
    ),
    "api": ("Run the API service (live model gateway unless overridden)", task_api),
    "web": ("Run the Vite dev server", task_web),
    "dev": ("Run API + web together in one terminal", task_dev),
    "fmt": ("Auto-format backend code", task_fmt),
    "lint": ("Lint backend code (no writes)", task_lint),
    "typecheck": ("Type-check the frontend", task_typecheck),
    "test": ("Run backend tests", task_test),
    "check": (
        "The pre-commit gate: lint + tests + contract drift + skills",
        task_check,
    ),
    "skills-check": (
        "Verify the skills directory is compatible with this build",
        task_skills_check,
    ),
    "e2e": ("End-to-end integration test; needs a model credential", task_e2e),
    "openapi": ("Export the OpenAPI schema to contracts/", task_openapi),
    "contracts-check": (
        "Fail if the committed contract drifted from the code",
        task_contracts_check,
    ),
    "web-build": ("Build the frontend for production", task_web_build),
    "gc": ("Reclaim disk: drop run worktrees past their TTL", task_gc),
    "purge-data": (
        "Delete ALL runtime data (mirrors, runs, deliverables)",
        task_purge_data,
    ),
    "clean": ("Remove build and cache artefacts (never touches data/)", task_clean),
}


# ----------------------------------------------------------------------------- execution


def _resolve(step: Step) -> Step | tuple[str, Task]:
    if len(step) == 1 and step[0] in TASKS:
        name = step[0]
        return name, TASKS[name][1]
    return step


def run_steps(name: str, steps: list[Step], *, dry_run: bool) -> int:
    """Run steps in order, stopping at the first failure."""
    for step in steps:
        resolved = _resolve(step)
        if isinstance(resolved, tuple):
            inner_name, factory = resolved
            code = run_steps(inner_name, factory(), dry_run=dry_run)
            if code:
                return code
            continue

        print(f"\n$ {' '.join(resolved)}", flush=True)
        if dry_run:
            continue
        cwd = FRONTEND if in_frontend(resolved) else ROOT
        try:
            completed = subprocess.run(resolved, cwd=str(cwd), check=False)
        except FileNotFoundError:
            print(
                f"\ncommand not found: {resolved[0]}\n"
                "Run `python scripts/dev.py install` to set the toolchain up.",
                file=sys.stderr,
            )
            return 127
        if completed.returncode:
            return completed.returncode
    return 0


def run_pair(entries: list[str], *, dry_run: bool) -> int:
    """Run two long-lived tasks concurrently, stopping both on Ctrl-C or when either exits.

    A shell would use ``trap 'kill 0'``, a bash builtin with no Windows equivalent. Spawning the
    children here and forwarding the signal works on both.
    """
    steps = [step for entry in entries for step in TASKS[entry][1]()]
    if dry_run:
        for step in steps:
            print(f"\n$ {' '.join(step)}", flush=True)
        return 0

    children: list[subprocess.Popen[bytes]] = []
    for step in steps:
        cwd = FRONTEND if in_frontend(step) else ROOT
        print(f"$ {' '.join(step)}", flush=True)
        children.append(subprocess.Popen(step, cwd=str(cwd)))

    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    # SIGTERM does not exist on Windows; installing only what the platform provides keeps this
    # portable without a branch on os.name.
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, stop)
        except (
            ValueError,
            OSError,
        ):  # pragma: no cover - not the main thread, or unsupported
            pass

    try:
        while True:
            for child in children:
                if child.poll() is not None:
                    stop()
                    return child.returncode or 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                child.kill()


def clean() -> int:
    """Remove build and cache artefacts. ``data/`` is deliberately untouched."""
    removed: list[str] = []
    for target in (
        FRONTEND / "dist",
        FRONTEND / "node_modules" / ".vite",
        FRONTEND / "tsconfig.tsbuildinfo",
        BACKEND / ".pytest_cache",
        ROOT / ".ruff_cache",
        BACKEND / ".ruff_cache",
    ):
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        removed.append(target.relative_to(ROOT).as_posix())

    for cache in list(BACKEND.rglob("__pycache__")) + list(
        (ROOT / "scripts").rglob("__pycache__")
    ):
        shutil.rmtree(cache, ignore_errors=True)
        removed.append(cache.relative_to(ROOT).as_posix())

    print(
        "removed: "
        + (", ".join(sorted(set(removed))) if removed else "nothing to remove")
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Development tasks for this project, on any platform."
    )
    parser.add_argument("task", nargs="?", help="task name; omit to list them")
    parser.add_argument(
        "--print",
        dest="dry_run",
        action="store_true",
        help="print the commands without running them",
    )
    args = parser.parse_args()

    # Internal entry points used by the task table itself. Each runs in a child process so a
    # failure cannot leave this one with a half-imported app module.
    if args.task == "_clean":
        return clean()
    if args.task == "_openapi":
        return export_openapi()
    if args.task == "_contracts":
        return check_contracts()
    if args.task == "_gc":
        return gc()

    if not args.task:
        width = max(len(name) for name in TASKS)
        print("Development tasks:\n")
        for name, (help_text, _) in TASKS.items():
            print(f"  {name:>{width}}  {help_text}")
        print("\nAdd --print to see the underlying commands without running them.")
        return 0

    entry = TASKS.get(args.task)
    if entry is None:
        print(
            f"unknown task {args.task!r}. Run `python scripts/dev.py` to list them.",
            file=sys.stderr,
        )
        return 2

    help_text, factory = entry
    print(f"{args.task}: {help_text}")
    if args.task == "dev":
        return run_pair(["api", "web"], dry_run=args.dry_run)
    return run_steps(args.task, factory(), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
