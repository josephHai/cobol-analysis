#!/usr/bin/env python
"""Verify a skills directory is compatible with this build, before running anything.

Written for the case that matters: the real skills live on an intranet host and are dropped in
as a replacement for the ones in this repository. The skills are content — this code treats them
as replaceable — but the slot has a shape, and a mismatch is much cheaper to find here than at
the end of a 90-second run.

Checks, in the order they bite:

1. Every skill the pipeline invokes exists, under the exact directory name the code references.
   The API artifact key and the skill directory name differ on purpose (``test_case`` vs
   ``test-case``), so a rename that updates only one side is a real failure mode.
2. Frontmatter parses and ``name`` matches the directory. ``SkillsMiddleware`` keys skills by the
   frontmatter name, so a mismatch means the skill loads under a name nothing references.
3. The ``SKILL.md`` is a real procedure rather than a stub.
4. The deliverable format each skill is expected to return is stated, and the prompt actually
   points the model at the skill file — a skill that is loaded but never mentioned is dead weight.

Exit code is non-zero when something would break a run, and zero when the directory is usable
(with warnings printed for things worth knowing but not fatal).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# The default skills directory comes from Settings, which refuses to start without a model
# endpoint. This check never calls a model, so placeholders are supplied when the environment has
# none — otherwise a static check of the skills directory would demand gateway credentials, which
# is exactly how it fails on a fresh checkout. Same pattern as `dev.py openapi`/`contracts-check`.
os.environ.setdefault("LLM_BASE_URL", "http://gateway.invalid/v1")
os.environ.setdefault("LLM_MODEL", "skills-check")

import yaml
from app.agent.graph import ANALYSIS_FILE, ARTIFACT_PROMPT, ARTIFACT_SPECS
from app.core.config import Settings

#: The analysis skill is not in ARTIFACT_SPECS: it produces the hand-off the others read.
ANALYSIS_SKILL = "cobol-analysis"
ANALYSIS_SKILL_FILE = "/skills/cobol-analysis/SKILL.md"

OK = "  ok  "
WARN = " warn "
FAIL = " FAIL "


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"[{FAIL}] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[{WARN}] {msg}")

    def ok(self, msg: str) -> None:
        print(f"[{OK}] {msg}")


def check_skill_dir(
    report: Report, skills_dir: pathlib.Path, name: str, must_contain: list[str]
) -> bool:
    """Validate one skill directory. Returns True when it is usable."""
    skill_dir = skills_dir / name
    skill_md = skill_dir / "SKILL.md"

    if not skill_dir.is_dir():
        report.fail(
            f"missing skill directory {skill_dir}. The pipeline invokes it by this exact name, "
            "so a rename must update ARTIFACT_SPECS too."
        )
        return False
    if not skill_md.is_file():
        report.fail(
            f"{skill_dir} has no SKILL.md; SkillsMiddleware discovers skills by that file"
        )
        return False

    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        report.fail(f"{name}: SKILL.md has no YAML frontmatter, so it will not load")
        return False

    try:
        front = yaml.safe_load(text.split("---", 2)[1]) or {}
    except yaml.YAMLError as exc:
        report.fail(f"{name}: frontmatter is not valid YAML ({exc})")
        return False

    declared = str(front.get("name", "")).strip()
    if declared != name:
        report.fail(
            f"{name}: frontmatter name is {declared!r}. SkillsMiddleware keys skills by the "
            "frontmatter name, so this skill would load under a name nothing references."
        )
        return False
    if not str(front.get("description", "")).strip():
        report.fail(
            f"{name}: empty description — the model sees only the description until it "
            "decides to read the skill, so an empty one means it is never chosen"
        )

    body = text.split("---", 2)[2] if text.count("---") >= 2 else ""
    if len(body.strip()) < 400:
        report.warn(
            f"{name}: SKILL.md body is only {len(body.strip())} characters. If this is a stub, "
            "the agent will follow a procedure that is not there."
        )

    # Only meaningful for contracts the *skill* has to honour. Requiring e.g. "SKILL.md" or
    # "LOCALE" here would be noise: the prompt supplies those, and the skill never needs to name
    # them. A warning that fires on a correct skill trains people to ignore warnings.
    # A skill may satisfy a requirement either by naming the marker (``ANALYSIS_FILE``) or by
    # quoting its literal value. Both are correct: the prompt supplies the value, and a skill that
    # says "write to whatever ANALYSIS_FILE says" is doing the right thing. Comparing only the
    # literal path produced a warning on a correct skill.
    def _satisfied(requirement: str) -> bool:
        if requirement in text:
            return True
        return requirement == ANALYSIS_FILE and "ANALYSIS_FILE" in text

    missing = [m for m in must_contain if not _satisfied(m)]
    if missing:
        report.warn(
            f"{name}: does not mention {missing}. The pipeline passes these in the prompt, but the "
            "skill may not be reading the inputs it needs — check it deliberately."
        )
    report.ok(f"{name} — loads, {len(body.splitlines())} lines of procedure")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check a skills directory against this build"
    )
    parser.add_argument(
        "--skills-dir",
        type=pathlib.Path,
        default=None,
        help="defaults to SKILLS_DIR, i.e. <repo>/backend/skills",
    )
    args = parser.parse_args()

    skills_dir = (args.skills_dir or Settings().skills_dir).expanduser().resolve()
    report = Report()

    print(f"skills directory : {skills_dir}")
    print(f"analysis hand-off: {ANALYSIS_FILE}")
    print()

    if not skills_dir.is_dir():
        report.fail(f"{skills_dir} is not a directory")
        return 1

    # ---------------------------------------------------------------- analysis skill
    print("required by the pipeline")
    check_skill_dir(
        report,
        skills_dir,
        ANALYSIS_SKILL,
        # These two are hard contracts: the pipeline reads the hand-off file from that path and
        # reads the chosen target from that field. A skill that does neither cannot be used as-is.
        must_contain=["ANALYSIS REQUEST", ANALYSIS_FILE, "entry_point"],
    )

    # -------------------------------------------------------------- artifact skills
    for key, spec in ARTIFACT_SPECS.items():
        skill = spec["skill"]
        # No must_contain here: what an artifact skill must say is its own business. What the
        # *code* requires of it is the output shape, asserted below.
        check_skill_dir(report, skills_dir, skill, must_contain=[])

        # What this skill's output must look like for the code to persist it.
        accepts = spec.get("accepts", "?")
        shape = {
            "markdown": "a Markdown document as the reply body",
            "json_rows": 'a ```json block containing {"rows": [...]}',
        }.get(accepts, f"unknown format {accepts!r}")
        print(f"         ↳ {key} must return {shape}; written as {spec['filename']}")

        # A prompt that never names the skill means the model never reads it.
        rendered = ARTIFACT_PROMPT.replace("$label", spec["label"]).replace(
            "$skill", skill
        )
        if f"/skills/{skill}/SKILL.md" not in rendered:
            report.fail(
                f"{key}: the prompt does not point at /skills/{skill}/SKILL.md, so the model has "
                "no reason to read the skill's procedure"
            )

    # ------------------------------------------------------------- extras and stubs
    print()
    print("skill library contents")
    known = {ANALYSIS_SKILL} | {s["skill"] for s in ARTIFACT_SPECS.values()}
    for entry in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        if entry.name in known:
            extras = sorted(
                f.relative_to(entry).as_posix()
                for f in entry.rglob("*")
                if f.is_file() and f.name != "SKILL.md"
            )
            listing = ", ".join(extras) if extras else "SKILL.md only"
            print(f"[{OK}] {entry.name:16} {listing}")
        else:
            # Not an error: unwired skills are harmless and may be staged ahead of the code that
            # will use them. Worth saying so nobody assumes they run.
            report.warn(
                f"{entry.name}: present but not invoked by this build, so it will be advertised to "
                "the agent yet never executed by the pipeline"
            )

    # --------------------------------------------------------------------- summary
    print()
    if report.failures:
        print(f"{len(report.failures)} problem(s) would break a run:")
        for msg in report.failures:
            print(f"  - {msg}")
        print("\nFix these before pointing the service at this skills directory.")
        return 1

    if report.warnings:
        print(f"usable, with {len(report.warnings)} warning(s) worth reading above.")
    else:
        print(
            "usable: every skill the pipeline invokes is present and correctly shaped."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
