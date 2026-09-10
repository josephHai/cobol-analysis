"""Authentication-mode tests.

The console lost its login step when identity moved to a proxy-supplied header, so these
tests pin the properties that make that safe: a missing header fails closed, a forged role
cannot be self-assigned, and the reserved SSO mode refuses rather than silently allowing.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.auth import Actor, authenticate
from app.core.config import Settings


def cfg(settings: Settings, **overrides) -> Settings:
    return settings.model_copy(update=overrides)


# --------------------------------------------------------- trusted_header (default)
def test_trusted_header_identifies_the_caller(settings: Settings) -> None:
    actor = authenticate(
        cfg(settings, auth_mode="trusted_header", trusted_default_role="analyst"),
        trusted_identity="alice@corp",
    )
    assert actor.id == "alice@corp"
    assert actor.roles == frozenset({"analyst"})
    assert actor.mode == "trusted_header"


def test_trusted_header_denies_a_missing_header(settings: Settings) -> None:
    """Fails closed. A proxy misconfiguration must not become an open service."""
    with pytest.raises(HTTPException) as exc:
        authenticate(cfg(settings, auth_mode="trusted_header"), trusted_identity=None)
    assert exc.value.status_code == 401
    assert "E2E-token" in exc.value.detail


def test_blank_header_is_treated_as_missing(settings: Settings) -> None:
    with pytest.raises(HTTPException) as exc:
        authenticate(cfg(settings, auth_mode="trusted_header"), trusted_identity="   ")
    assert exc.value.status_code == 401


def test_role_comes_from_config_not_from_the_header(settings: Settings) -> None:
    """A caller cannot promote itself.

    The header carries an opaque identity; the role is decided server-side. So an identity
    that *looks* privileged grants nothing extra — which is what makes the unverified header
    tolerable at all.
    """
    impersonating = authenticate(
        cfg(settings, auth_mode="trusted_header", trusted_default_role="viewer"),
        trusted_identity="admin",
    )
    assert impersonating.roles == frozenset({"viewer"})
    assert not impersonating.can("admin")


def test_long_identity_is_truncated(settings: Settings) -> None:
    """The header is attacker-controlled in the worst case, so it is bounded before logging."""
    actor = authenticate(cfg(settings, auth_mode="trusted_header"), trusted_identity="x" * 5000)
    assert len(actor.id) == 128


# ------------------------------------------------------------------------ api_key
def test_api_key_mode_verifies_the_key(settings: Settings) -> None:
    mode = cfg(settings, auth_mode="api_key", api_keys="k1:analyst,k2:viewer")
    assert authenticate(mode, presented="k1").roles == frozenset({"analyst"})
    assert authenticate(mode, presented="k2").roles == frozenset({"viewer"})


def test_api_key_mode_rejects_a_bad_key(settings: Settings) -> None:
    mode = cfg(settings, auth_mode="api_key", api_keys="k1:analyst")
    for bad in (None, "", "wrong"):
        with pytest.raises(HTTPException) as exc:
            authenticate(mode, presented=bad)
        assert exc.value.status_code == 401


def test_api_key_mode_ignores_the_trusted_header(settings: Settings) -> None:
    """Modes must not bleed into each other: an identity header alone is not a credential."""
    mode = cfg(settings, auth_mode="api_key", api_keys="k1:analyst")
    with pytest.raises(HTTPException):
        authenticate(mode, presented=None, trusted_identity="alice")


# ----------------------------------------------------------------------- disabled
def test_disabled_grants_admin_for_local_development(settings: Settings) -> None:
    actor = authenticate(cfg(settings, auth_mode="disabled"))
    assert actor.can("admin")
    assert actor.mode == "disabled"


# ---------------------------------------------------------------------- e2e_token
def test_e2e_token_refuses_instead_of_allowing(settings: Settings) -> None:
    """The reserved SSO mode must not be a permissive stub.

    A stub that allowed every request would be indistinguishable from a finished integration
    and would ship. Returning 501 makes the omission visible the moment it is selected.
    """
    with pytest.raises(HTTPException) as exc:
        authenticate(cfg(settings, auth_mode="e2e_token"), trusted_identity="token-value")
    assert exc.value.status_code == 501
    assert "not implemented" in exc.value.detail.lower()


# -------------------------------------------------------------------------- roles
@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    [
        ("viewer", "read", True),
        ("viewer", "create_run", False),
        ("analyst", "create_run", True),
        ("analyst", "admin", False),
        ("reviewer", "approve", True),
        ("admin", "admin", True),
    ],
)
def test_role_matrix(role: str, action: str, allowed: bool) -> None:
    actor = Actor(id="x", roles=frozenset({role}))
    assert actor.can(action) is allowed


def test_unknown_role_grants_nothing() -> None:
    """A typo in configuration must deny, not fall through to a default grant."""
    assert not Actor(id="x", roles=frozenset({"superuser"})).can("read")
