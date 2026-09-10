"""Security hard-rule regression tests.

One test per rule in ``CODESTYLE.md`` §5 that can be checked without a live model. Each
rule gets a negative case — the thing that must fail — because a control that is never
observed rejecting anything is an assumption, not a control.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import pytest

from app.agent.graph import default_permissions


# --------------------------------------------------------------- S4: path guard
@pytest.mark.parametrize(
    "hostile_path",
    [
        "../../etc/passwd",
        "/../../etc/passwd",
        "/work/../../../etc/shadow",
        "~/.ssh/id_rsa",
    ],
)
def test_path_traversal_is_rejected(run_root: Path, build_agent_backend, hostile_path: str) -> None:
    """S4: the backend must refuse to resolve any path outside the run root.

    The guard raises rather than returning an error result, so both outcomes count as
    "rejected" — what matters is that no host file is ever opened.
    """
    backend = build_agent_backend(str(run_root))
    try:
        result = backend.read(hostile_path)
    except (ValueError, OSError):
        return  # rejected by raising, which is the documented behaviour
    assert getattr(result, "error", None) is not None, (
        f"traversal path {hostile_path!r} was not rejected"
    )


def test_symlink_escape_is_rejected(run_root: Path, build_agent_backend) -> None:
    """S4: a symlink pointing outside the root must not be followed.

    This is the case a naive string check misses: the path looks innocuous, and only
    resolution reveals the escape. ``virtual_mode`` resolves before the containment check,
    which is why it holds.
    """
    link = run_root / "work" / "escape"
    try:
        link.symlink_to("/etc")
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable on this platform")

    backend = build_agent_backend(str(run_root))
    try:
        result = backend.read("/work/escape/passwd")
    except (ValueError, OSError):
        return
    assert getattr(result, "error", None) is not None, "symlink escape was not rejected"


def test_paths_inside_the_root_still_work(run_root: Path, build_agent_backend) -> None:
    """S4 positive case: the guard must not be so strict that real work is impossible.

    Paths reaching the default backend are relative to the run root (the composite strips
    the route prefix), so this writes and reads through the backend rather than assuming a
    layout on disk. Without a positive case, an over-strict guard would look like a pass.
    """
    backend = build_agent_backend(str(run_root))
    backend.write("/work/notes.txt", "scratch")

    result = backend.read("/work/notes.txt")
    assert getattr(result, "error", None) is None, f"in-root read failed: {result}"
    assert "scratch" in str(getattr(result, "file_data", None) or result)


# ------------------------------------------------- S5/S6: permission rules
def test_checkout_is_denied_to_writers() -> None:
    """S5: a write rule must exist for /repo/** so the checkout stays evidence."""
    rules = default_permissions()
    write_denies = [
        rule
        for rule in rules
        if rule.mode == "deny" and "write" in rule.operations and "/repo/**" in rule.paths
    ]
    assert write_denies, "no deny rule protects /repo/** from writes"


@pytest.mark.parametrize(
    "secret_path",
    ["/**/.env*", "/**/secrets/**", "/**/*.pem", "/**/id_rsa*"],
)
def test_credential_paths_are_denied(secret_path: str) -> None:
    """S6: credential-shaped paths must be unreadable, even if a repo ships one."""
    rules = default_permissions()
    matching = [
        rule
        for rule in rules
        if rule.mode == "deny" and "read" in rule.operations and secret_path in rule.paths
    ]
    assert matching, f"no deny rule covers {secret_path}"


def test_env_file_is_denied_by_the_backend(run_root: Path) -> None:
    """S6 end-to-end: a real .env in the checkout must not be readable.

    Placed under /work rather than /repo so the failure can only come from the read rule,
    not from an unrelated path error.
    """

    secret = run_root / "work" / ".env"
    secret.write_text("DB_PASSWORD=hunter2", encoding="utf-8")

    # The rule set is what the agent's middleware enforces; assert on it directly rather
    # than driving a full agent turn, which would need a model.
    import deepagents.middleware.filesystem as fs

    rules = default_permissions()
    outcome = fs._check_fs_permission(rules, "read", "/work/.env")
    assert outcome == "deny", f"reading /work/.env resolved to {outcome!r}, expected 'deny'"


# ----------------------------------------------------- S3: credential masking
@pytest.mark.parametrize(
    "raw",
    [
        "https://oauth2:glpat-abcdefghijklmnopqrst@git.example.com/core/banking.git",
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "Authorization: Bearer github_pat_11ABCDEFG0abcdefghijklmnop",
        "sha 0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_secrets_are_masked(raw: str) -> None:
    """S3: anything credential-shaped is stripped before it reaches a log or event."""
    from app.services.git_service import mask_secrets

    masked = mask_secrets(raw)
    assert "glpat-" not in masked
    assert "ghp_" not in masked
    assert "github_pat_" not in masked
    assert "0123456789abcdef0123456789abcdef01234567" not in masked


def test_masking_keeps_ordinary_text_intact() -> None:
    """S3 positive case: masking must not garble normal identifiers."""
    from app.services.git_service import mask_secrets

    text = "Checked out PGM001.cbl at 2b5622d from core-banking"
    assert mask_secrets(text) == text


# ----------------------------------------------------------- S7: no shell
def test_no_shell_true_anywhere() -> None:
    """S7: subprocess must never be invoked through a shell."""
    backend_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        path.relative_to(backend_dir).as_posix()
        for path in backend_dir.rglob("*.py")
        if "shell=True" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"shell=True found in: {offenders}"


# ------------------------------------------------------------ S1: no execute
def test_execute_is_not_implemented_by_any_backend(tmp_path: Path, build_agent_backend) -> None:
    """S1: the guard is the absence of the protocol, not a configuration flag, on the real wiring.

    ``execute`` is offered when *any* backend in play implements
    ``SandboxBackendProtocol`` — ``CompositeBackend``'s default reaches most paths, and each
    route reaches the rest. So every one of them is checked, on the backend the pipeline actually
    builds (``build_backend_with_skills``), not on a test-only equivalent.
    """
    from deepagents.backends.protocol import SandboxBackendProtocol

    backend = build_agent_backend(str(tmp_path))
    assert not isinstance(backend, SandboxBackendProtocol)
    assert not isinstance(backend.default, SandboxBackendProtocol)
    for prefix, route_backend in backend.routes.items():
        assert not isinstance(route_backend, SandboxBackendProtocol), (
            f"the backend routed at {prefix} implements SandboxBackendProtocol, which makes the "
            "execute tool available to the model"
        )


def test_tool_budget_raises_when_exhausted() -> None:
    """The budget guard must fail loudly rather than silently truncating a run."""
    from app.agent.graph import ToolBudgetMiddleware

    guard = ToolBudgetMiddleware(max_tool_calls=2)

    class Request:
        tool_call: ClassVar[dict[str, object]] = {"name": "read_file", "args": {}}

    handler = lambda request: "ok"  # noqa: E731 - minimal stand-in for the real handler
    assert guard.wrap_tool_call(Request(), handler) == "ok"
    assert guard.wrap_tool_call(Request(), handler) == "ok"
    with pytest.raises(RuntimeError, match="budget exhausted"):
        guard.wrap_tool_call(Request(), handler)


# ------------------------------------------------- backend path layout
def test_virtual_paths_map_to_the_intended_physical_files(
    run_root: Path, build_agent_backend
) -> None:
    """The virtual and physical layouts must agree, for every route.

    This guards a bug that is invisible in review and fails *silently* in production: with
    the analysis route pointed at the wrong root, the model wrote
    ``<run>/analysis.json`` while the pipeline looked for ``<run>/analysis/analysis.json``.
    The run failed with "the analysis skill did not produce …" for a file that existed.

    ``CompositeBackend`` strips the whole route prefix, so the correctness rule is simply
    ``physical = route_root + remainder``. Asserting it here means a future route change
    cannot reintroduce the mismatch.
    """
    from app.agent.graph import ANALYSIS_FILE

    backend = build_agent_backend(str(run_root))
    expectations = [
        ("/work/notes.txt", "work/notes.txt"),
        (ANALYSIS_FILE, "work/analysis.json"),
        ("/artifacts/fdd.md", "artifacts/fdd.md"),
        ("/repo/PGM001.cbl", "repo/PGM001.cbl"),
    ]
    for virtual_path, expected in expectations:
        backend.write(virtual_path, "x")
        landed = {
            path.relative_to(run_root).as_posix() for path in run_root.rglob("*") if path.is_file()
        }
        assert expected in landed, (
            f"{virtual_path} landed at {sorted(landed)}, expected {expected}. "
            "A route prefix cannot preserve its own segment: pair each prefix with the "
            "directory it should resolve to."
        )


def test_analysis_file_sits_where_the_pipeline_reads_it(
    run_root: Path, build_agent_backend
) -> None:
    """The hand-off path is derived in two places; they must not drift."""
    from app.agent.graph import ANALYSIS_FILE

    backend = build_agent_backend(str(run_root))
    backend.write(ANALYSIS_FILE, '{"ok": true}')

    physical = run_root / ANALYSIS_FILE.lstrip("/")
    assert physical.is_file(), f"pipeline reads {physical}, which the agent's write did not create"


# ------------------------------------------------- label derivation from the request
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Review PGM001.", "Review PGM001."),
        (
            "Analyse the balance inquiry: what are the rules?",
            "Analyse the balance inquiry: what are the rules?",
        ),
        ("  Analyse   the   batch  ", "Analyse the batch"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_derived_label_is_the_operators_own_words(message: str, expected: str) -> None:
    """The run label comes from the request, so it cannot drift from what was asked.

    Trailing colons are trimmed because a request opening with a clause would otherwise produce
    a heading that reads like a truncation.
    """
    from app.agent.pipeline import _derive_label

    assert _derive_label(message) == expected


def test_derived_label_never_exceeds_the_width() -> None:
    """It ends up in a heading and in the FDD title, so it must be bounded."""
    from app.agent.pipeline import _derive_label

    for width in (20, 60, 80):
        assert len(_derive_label("word " * 100, width=width)) <= width


def test_no_derived_label_for_punctuation_only() -> None:
    """A degenerate request must not produce a label of punctuation.

    Leading punctuation that is not a strip character can legitimately survive — the point of
    this test is that a label is never *only* stripped characters, not that all punctuation is
    removed. `": ;,. "` therefore keeps its interior colon and comma, which is ugly but honest;
    `":"` and `"::"` become empty because nothing would remain.
    """
    from app.agent.pipeline import _derive_label

    assert _derive_label(":") == ""
    assert _derive_label("::") == ""
    assert _derive_label(" . ") == ""


# ------------------------------------------------------- git ref validation (S7)
@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "master",
        "origin/main",
        "refs/heads/main",
        "v1.2.3",
        "release/2024-01",
        "feature/ABC-123",
        "fix/nested/deep",
        "a" * 40,
        "HEAD",
    ],
)
def test_valid_refs_are_accepted(ref: str) -> None:
    """A validator that rejects real branch names is worse than none: it blocks normal use."""
    from app.services.git_service import validate_ref

    assert validate_ref(ref) == ref


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("-upload-pack=evil", "would be parsed as a git option"),
        ("--config=core.hooksPath=/tmp", "would be parsed as a git option"),
        ("-", "bare dash is an option"),
        ("", "empty"),
        (" main", "leading whitespace"),
        ("main ", "trailing whitespace"),
        ("a..b", "range operator"),
        ("x.lock", "git reserves .lock"),
        ("a//b", "empty path component"),
        ("a/", "trailing slash"),
        (".hidden", "component starting with a dot"),
        ("a b", "space"),
        ("a~1", "revision syntax"),
        ("a^", "revision syntax"),
        ("a:b", "colon is reserved"),
        ("a?b", "glob character"),
        ("a*b", "glob character"),
    ],
)
def test_unsafe_refs_are_rejected(ref: str, why: str) -> None:
    """The first two are the ones that matter: a ref is an argument to git."""
    from app.services.git_service import GitError, validate_ref

    with pytest.raises(GitError):
        validate_ref(ref)


def test_ref_validation_matches_git_on_real_branch_names() -> None:
    """Cross-check against git itself, so this does not become a private dialect.

    Skipped when git is unavailable. Divergence on ``HEAD`` is expected and intentional — see
    ``validate_ref``.
    """
    import shutil
    import subprocess

    from app.services.git_service import GitError, validate_ref

    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    for ref in (
        "main",
        "origin/main",
        "refs/heads/main",
        "v1.2.3",
        "feature/ABC-123",
        "fix/nested/deep",
        "1.0",
        "with.dots",
        "release/2024-01",
    ):
        try:
            validate_ref(ref)
            ours = True
        except GitError:
            ours = False
        git_ok = (
            subprocess.run(
                ["git", "check-ref-format", "--branch", ref], capture_output=True
            ).returncode
            == 0
        )
        assert ours == git_ok, f"{ref!r}: ours={ours} git={git_ok}"


def test_hooks_and_credential_helper_are_disabled(run_root: Path, git_service) -> None:
    """S8: repository-supplied hooks and host-stored credentials must not take effect.

    ``core.hooksPath`` points at an empty directory so a hook shipped inside a repository cannot
    execute as this service. ``credential.helper`` is cleared because the host's global gitconfig
    commonly has ``credential.helper=store``, which would silently substitute a cached credential
    for the PAT this service injects.
    """
    from app.services.git_service import EMPTY_HOOKS_DIR

    spec = git_service.spec("demo")
    with git_service._env(spec) as env:
        assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert env["GIT_CONFIG_VALUE_0"] == str(EMPTY_HOOKS_DIR)
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_1"] == ""
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    assert EMPTY_HOOKS_DIR.is_dir(), "git errors on a hooksPath that is not a directory"
    assert not any(EMPTY_HOOKS_DIR.iterdir()), "the hooks directory must stay empty"


def test_repository_supplied_hook_cannot_run(tmp_path: Path) -> None:
    """S8, verified against a real hook rather than by inspecting environment variables.

    A repository is untrusted input, and `.git/hooks` is repository-controlled. With
    ``core.hooksPath`` pointed at an empty directory, a hook installed in the repository must not
    execute. The control case at the end proves the hook *would* run otherwise, so this is not a
    test that passes because nothing was ever triggered.
    """
    import shutil
    import subprocess

    from app.services.git_service import EMPTY_HOOKS_DIR

    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    repo = tmp_path / "src"
    repo.mkdir()
    marker = tmp_path / "PWNED"
    (repo / "file.txt").write_text("sample", encoding="utf-8")

    def git(*args: str, env: dict[str, str] | None = None) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={**os.environ, **(env or {})},
        )

    git("init", "-q", "-b", "main", ".")
    git("add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")

    hooks = repo / ".git" / "hooks"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)

    hardened = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(EMPTY_HOOKS_DIR),
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    checkout = ("-c", "core.fileMode=false", "checkout", "--detach", "HEAD")

    git(*checkout, env=hardened)
    assert not marker.exists(), "a repository-supplied hook executed under the hardened env"

    git(*checkout)
    assert marker.exists(), (
        "the hook did not run even without the override, so this test proves nothing"
    )
