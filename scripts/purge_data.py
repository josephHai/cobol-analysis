#!/usr/bin/env python
"""Delete all runtime data: mirrors, run checkouts, deliverables and the SQLite database.

A script rather than a documented shell incantation because the unseal step is not optional and
is easy to forget: run checkouts are made read-only at the OS level (CODESTYLE.md hard rule S5),
so a plain ``rm -rf data`` fails with *Permission denied* on every source file and leaves a
half-deleted tree behind. This does it in the right order.

Refuses to run without ``--yes``: it is the only destructive command in the project, and it
deletes deliverables that may not be reproducible without another model run.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def human_mb(path: Path) -> float:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return round(total / 1024 / 1024, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete all runtime data")
    parser.add_argument("--yes", action="store_true", help="confirm the deletion")
    parser.add_argument("--root", type=Path, default=None, help="defaults to DATA_ROOT")
    args = parser.parse_args()

    if args.root is not None:
        target = args.root.expanduser().resolve()
        runs_dir = target / "runs"
    else:
        from app.core.config import get_settings

        settings = get_settings()
        target = settings.data_root
        runs_dir = settings.runs_dir

    if not target.exists():
        print(f"{target} does not exist; nothing to do")
        return 0

    size = human_mb(target)
    if not args.yes:
        print(f"would delete {target} ({size} MB)")
        print(
            "re-run with --yes to confirm. This removes mirrors, runs and deliverables."
        )
        return 1

    # Unseal every checkout first, or rmtree cannot remove the read-only files.
    if runs_dir.is_dir():
        from app.services.git_service import GitService

        for run_dir in runs_dir.iterdir():
            if run_dir.is_dir():
                GitService.unseal_worktree(run_dir / "repo")

    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        print(
            f"could not fully remove {target}. On a sealed checkout, try "
            "`chmod -R u+w <data_root>` and re-run.",
            file=sys.stderr,
        )
        return 1

    print(f"removed {target} ({size} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
