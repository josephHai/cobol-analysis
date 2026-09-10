"""Credential resolution: where the model key may come from, and in what order.

Written after a real misfire: an operator put ``LLM_AUTH_TOKEN`` in ``.env`` — the file the
project tells them to edit — and the service started without a credential, because that name was
not a field of :class:`Settings` and the resolver read only the process environment. Every run
then failed at its first model call, with a warning that scrolled past at startup.

The order matters as much as the sources: an exported variable must beat the file, so a
placeholder in ``.env`` cannot shadow the real key in a shell.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import Settings

#: Names the resolver may pick up from the ambient environment; cleared for every test here.
CREDENTIAL_ENV_NAMES = (
    "LLM_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _settings(settings: Settings, **overrides) -> Settings:
    return settings.model_copy(update=overrides)


def test_credential_in_the_config_file_is_used(clean_env, settings: Settings) -> None:
    """The neutral name works in ``.env``, which is the surface an operator edits."""
    cfg = _settings(settings, llm_auth_token="from-the-file")
    assert cfg.resolve_api_key() == "from-the-file"


def test_explicit_key_wins(clean_env, settings: Settings) -> None:
    cfg = _settings(settings, llm_api_key="explicit", llm_auth_token="from-the-file")
    assert cfg.resolve_api_key() == "explicit"


def test_environment_beats_the_file(
    clean_env, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real key exported for this shell must not be shadowed by a placeholder in ``.env``."""
    monkeypatch.setenv("LLM_AUTH_TOKEN", "from-the-environment")
    cfg = _settings(settings, llm_auth_token="placeholder-in-the-file")
    assert cfg.resolve_api_key() == "from-the-environment"


def test_a_named_variable_beats_the_file(
    clean_env, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SECRET_STORE_VAR", "from-the-store")
    cfg = _settings(settings, llm_api_key_env="MY_SECRET_STORE_VAR", llm_auth_token="from-the-file")
    assert cfg.resolve_api_key() == "from-the-store"


def test_a_named_variable_that_is_unset_falls_through(clean_env, settings: Settings) -> None:
    """Naming a variable that does not exist must not disable the file's credential."""
    cfg = _settings(
        settings, llm_api_key_env="NOT_EXPORTED_ANYWHERE", llm_auth_token="from-the-file"
    )
    assert cfg.resolve_api_key() == "from-the-file"


def test_no_credential_warns_and_returns_empty(
    clean_env, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The negative case: no key anywhere is reported, and the value never appears in the log."""
    cfg = _settings(settings, llm_api_key="", llm_auth_token="")
    with caplog.at_level(logging.WARNING):
        assert cfg.resolve_api_key() == ""
    assert any("no model credential found" in record.message for record in caplog.records)
