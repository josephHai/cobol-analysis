"""Tests for the GIT_ASKPASS hook.

The hook is the only place a git credential is handled, and it is invoked by git through a
shell — so both the quoting and the prompt matching are worth asserting rather than eyeballing.
The previous shell-script implementation could not run on Windows at all; the Python one can,
which is why the shape changed.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from app.services.git_service import ASKPASS_TOKEN_ENV, GitService, _quote_command

TOKEN = "s3cr3t-token-value"
USERNAME = "oauth2"


@pytest.fixture
def hook() -> str:
    return GitService.__new__(GitService).askpass_command(USERNAME, ASKPASS_TOKEN_ENV)


def _run_as_git_does(hook: str, prompt: str) -> str:
    """Invoke the hook the way git does: prompt appended, executed through a shell."""
    result = subprocess.run(
        f"{hook} {_quote_command(prompt)}",
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, ASKPASS_TOKEN_ENV: TOKEN},
    )
    assert result.returncode == 0, f"hook failed: {result.stderr[:200]}"
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        # git capitalises "Username" — matching the lower-case form would send the token where the
        # username belongs, which authenticates anyway in most setups and so hides the bug.
        ("Username for 'https://git.example.com': ", USERNAME),
        ("Password for 'https://oauth2@git.example.com': ", TOKEN),
        ("Password: ", TOKEN),
    ],
)
def test_hook_answers_the_prompts_git_sends(hook: str, prompt: str, expected: str) -> None:
    assert _run_as_git_does(hook, prompt) == expected


def test_token_is_absent_from_the_command_line(hook: str) -> None:
    """Args are world-readable through `ps`, so the token must travel in the environment only."""
    assert TOKEN not in hook
    assert ASKPASS_TOKEN_ENV in hook


def test_hook_writes_no_file_and_needs_no_shell(hook: str) -> None:
    """No temp script and no `sh`: the previous implementation had both, and neither works on
    Windows without a Git-Bash installation on PATH."""
    assert ".sh" not in hook
    assert "askpass-" not in hook
    assert "/bin/sh" not in hook


def test_missing_token_yields_an_empty_password(hook: str) -> None:
    """A missing credential must not print something misleading — git then fails cleanly."""
    result = subprocess.run(
        f"{hook} {_quote_command('Password: ')}",
        shell=True,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != ASKPASS_TOKEN_ENV},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_no_prompt_argument_does_not_crash(hook: str) -> None:
    """Defensive: if a git version invokes the hook with no argument, it must still exit 0."""
    result = subprocess.run(
        hook,
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, ASKPASS_TOKEN_ENV: TOKEN},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == TOKEN


def test_username_with_quotes_cannot_break_out() -> None:
    """The username is interpolated into the hook, so a quote in it must not escape.

    Credentials come from an operator's config, but a registry value is still input, and a
    shell-injection path here would run arbitrary code as the service user.
    """
    hostile = "o'; rm -rf /tmp/should-not-happen; echo '"
    hook = GitService.__new__(GitService).askpass_command(hostile, ASKPASS_TOKEN_ENV)
    assert _run_as_git_does(hook, "Username: ") == hostile


def test_quoting_is_platform_appropriate() -> None:
    """`cmd.exe` has no single-quote quoting, so Windows must use double quotes."""
    quoted = _quote_command("py", "-c", "print(1)")
    if os.name == "nt":
        assert quoted.startswith('"')
    else:
        assert quoted.startswith("'")
