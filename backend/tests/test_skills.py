"""Skill-loading regression tests.

Skill loading failed silently once: the SKILL.md files existed on disk, but no
``skills=`` argument was passed to the agent and no backend route could resolve the source
path, so the agent ran with no procedures at all and nothing reported a problem.

These tests assert the three things that must all hold, because any one of them missing
produces an agent that looks fine and behaves worse.
"""

from __future__ import annotations

import pathlib

import pytest

from app.agent.graph import ARTIFACT_SPECS, build_backend_with_skills
from app.core.config import Settings


@pytest.fixture
def cfg(settings: Settings) -> Settings:
    """Settings pointing at the repository's real skill library."""
    skills_dir = pathlib.Path(__file__).resolve().parent.parent / "skills"
    return settings.model_copy(update={"skills_dir": skills_dir, "skills_enabled": True})


def test_every_expected_skill_directory_exists(cfg: Settings) -> None:
    """Each skill referenced by the pipeline must have a SKILL.md on disk.

    The API artifact key and the skill directory name differ by design (`test_case` vs
    `test-case`), so the mapping is data, not convention — and this is what catches a rename
    that only updates one side.
    """
    expected = {"cobol-analysis"} | {spec["skill"] for spec in ARTIFACT_SPECS.values()}
    missing = [
        name for name in sorted(expected) if not (cfg.skills_dir / name / "SKILL.md").is_file()
    ]
    assert not missing, f"missing SKILL.md for: {missing}"


def test_skill_frontmatter_has_the_required_fields(cfg: Settings) -> None:
    """`name` and `description` are what progressive disclosure shows the model.

    A skill without a usable description is never selected, so it is dead weight rather than
    a harmless mistake.
    """
    import yaml

    problems: list[str] = []
    for skill_dir in sorted(p for p in cfg.skills_dir.iterdir() if p.is_dir()):
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        if not text.startswith("---"):
            problems.append(f"{skill_dir.name}: no YAML frontmatter")
            continue
        front = yaml.safe_load(text.split("---", 2)[1])
        if front.get("name") != skill_dir.name:
            problems.append(
                f"{skill_dir.name}: frontmatter name {front.get('name')!r} != directory name"
            )
        if not str(front.get("description", "")).strip():
            problems.append(f"{skill_dir.name}: empty description")
    assert not problems, problems


def test_backend_resolves_the_skills_route(cfg: Settings, tmp_path: pathlib.Path) -> None:
    """Half one: the source path must be resolvable through the agent's own backend.

    `SkillsMiddleware` loads sources through the backend, not the host filesystem, so a
    correct directory that is not mounted as a route does not exist as far as loading is
    concerned.
    """
    for sub in ("work", "artifacts", "repo"):
        (tmp_path / sub).mkdir()

    backend, sources = build_backend_with_skills(cfg, str(tmp_path))
    assert sources == ["/skills/"], "the pipeline must advertise exactly one skill source"
    assert "/skills/" in backend.routes

    listing = backend.ls("/skills/")
    # Entries carry `path` (not `name`), e.g. {'path': '/skills/fdd/', 'is_dir': True, ...}.
    names = {
        entry["path"].rstrip("/").rsplit("/", 1)[-1]
        for entry in (getattr(listing, "entries", None) or [])
        if entry.get("is_dir")
    }
    expected = {"cobol-analysis"} | {spec["skill"] for spec in ARTIFACT_SPECS.values()}
    assert expected <= names, f"skills not visible through the backend: {expected - names}"


def test_skills_are_actually_loaded_by_the_middleware(
    cfg: Settings, tmp_path: pathlib.Path
) -> None:
    """Half two, end to end: the middleware must parse the skills it can see.

    Asserts on the parsed metadata rather than on configuration, because configuration
    looking right is exactly the state that previously hid the failure.
    """
    from deepagents.middleware.skills import _list_skills_with_errors

    for sub in ("work", "artifacts", "repo"):
        (tmp_path / sub).mkdir()

    backend, sources = build_backend_with_skills(cfg, str(tmp_path))
    assert sources, "no skill sources were produced"

    loaded: dict[str, str] = {}
    for source in sources:
        skills, error = _list_skills_with_errors(backend, source)
        assert error is None, f"skill source {source} failed to load: {error}"
        loaded.update({s["name"]: s["path"] for s in skills})

    expected = {"cobol-analysis"} | {spec["skill"] for spec in ARTIFACT_SPECS.values()}
    assert expected <= set(loaded), f"middleware did not load: {expected - set(loaded)}"
    for name, path in loaded.items():
        assert path.endswith(f"/{name}/SKILL.md"), f"{name} resolved to {path}"


def test_disabling_skills_yields_no_sources(cfg: Settings, tmp_path: pathlib.Path) -> None:
    """The kill switch must fully remove the route, not merely stop advertising it."""
    for sub in ("work", "artifacts", "repo"):
        (tmp_path / sub).mkdir()

    off = cfg.model_copy(update={"skills_enabled": False})
    backend, sources = build_backend_with_skills(off, str(tmp_path))
    assert sources == []
    assert "/skills/" not in backend.routes


def test_missing_skill_directory_degrades_without_crashing(
    tmp_path: pathlib.Path, settings
) -> None:
    """A deployment without skills still runs — but produces no sources.

    Degrading rather than raising is deliberate: a missing skills directory should not stop
    the service. It is logged at error level for exactly this reason.
    """
    for sub in ("work", "artifacts", "repo"):
        (tmp_path / sub).mkdir()

    broken = settings.model_copy(
        update={"skills_dir": tmp_path / "does-not-exist", "skills_enabled": True}
    )
    backend, sources = build_backend_with_skills(broken, str(tmp_path))
    assert sources == []
    assert "/skills/" not in backend.routes
