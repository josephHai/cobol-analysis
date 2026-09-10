"""Authentication and the actor dependency.

The console has no login step. Identity is established *before* the request reaches this
service — by the development proxy today, by the intranet SSO gateway in production — and
this module's only job is to turn that into an :class:`Actor` with roles.

One function decides identity (:func:`authenticate`); one dependency applies it
(:func:`current_actor`); routes only ever name a permission level. That shape is the point:
the console was simplified by removing its API-key handling, and the SSO integration that
follows will not touch a single route or any client code.

.. warning::

   ``trusted_header`` mode trusts an unverified header. It is safe **only** when the service
   is reachable exclusively through the proxy that sets it. If the port is directly reachable,
   anyone can forge the header and become an admin. ``main.py`` logs a warning at startup in
   this mode for that reason.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field

from fastapi import Header, HTTPException, Request, status

from app.core.config import Settings

logger = logging.getLogger(__name__)

#: Role → permitted actions. Kept coarse on purpose: the labels matter more than
#: fine-grained policy, and every role is a single word an operator can reason about.
ROLE_ACTIONS: dict[str, set[str]] = {
    "viewer": {"read"},
    "analyst": {"read", "create_run", "cancel_run", "query_kb"},
    "reviewer": {"read", "create_run", "cancel_run", "query_kb", "approve"},
    "admin": {"read", "create_run", "cancel_run", "query_kb", "approve", "admin"},
}


@dataclass(frozen=True)
class Actor:
    id: str
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"viewer"}))
    mode: str = "unknown"
    """Which authentication mode produced this actor. Recorded in the audit log so a
    reviewer can tell a proxied SSO call from a local bypass."""

    def can(self, action: str) -> bool:
        return any(action in ROLE_ACTIONS.get(role, set()) for role in self.roles)


def authenticate(
    settings: Settings,
    *,
    presented: str | None = None,
    actor_hint: str | None = None,
    trusted_identity: str | None = None,
) -> Actor:
    """Resolve a caller to an :class:`Actor` according to ``AUTH_MODE``.

    ``presented`` is an API key; ``trusted_identity`` is the value of the proxy header. The
    two are separate parameters rather than one "credential" because they carry different
    trust: a key is verified here, a proxy header is not.
    """
    mode = settings.auth_mode

    if mode == "disabled":
        return Actor(id="local", roles=frozenset({"admin"}), mode=mode)

    if mode == "trusted_header":
        identity = (trusted_identity or "").strip()
        if not identity:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    f"Missing {settings.trusted_actor_header!r} header. It is set by the "
                    "proxy in front of this service; if you are calling the API directly, "
                    "either point the console at the service through that proxy or switch "
                    "the server to AUTH_MODE=api_key."
                ),
            )
        # The gateway's value is an opaque identity, not a credential: it is a name, and the
        # role comes from configuration rather than from the header, so a caller cannot
        # promote itself by inventing a value.
        # TODO(demo): every caller behind the gateway shares one role until the SSO claim
        # mapping lands (docs/DEMO.md §7 gap 8) — narrow TRUSTED_DEFAULT_ROLE meanwhile.
        return Actor(id=identity[:128], roles=frozenset({settings.trusted_default_role}), mode=mode)

    if mode == "e2e_token":
        # Reserved for the intranet SSO integration. Deliberately not faked: a permissive
        # stub here would be indistinguishable from a working integration and would ship.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "AUTH_MODE=e2e_token is reserved for the SSO integration and is not "
                "implemented yet. Verify the gateway token here and map its claims to roles. "
                "Use 'trusted_header' (proxy sets the header) or 'api_key' in the meantime."
            ),
        )

    # ---------------------------------------------------------------- api_key
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing API key. Send it as 'Authorization: Bearer <key>' or "
                "'X-API-Key'. API keys are configured in API_KEYS on the server."
            ),
        )
    for key, role in settings.key_map.items():
        # compare_digest avoids leaking key length or prefix through timing.
        if hmac.compare_digest(key, presented):
            label = (actor_hint or "api-key").strip()[:64] or "api-key"
            return Actor(id=f"{label}:{role}", roles=frozenset({role}), mode=mode)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key. Verify API_KEYS on the server.",
    )


def extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Pull the API key out of either accepted header form (``api_key`` mode only)."""
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _resolve(
    request: Request,
    *,
    authorization: str | None,
    x_api_key: str | None,
    x_actor: str | None,
    e2e_token: str | None,
) -> Actor:
    """Resolve an actor from explicitly-passed header values.

    Both dependencies below declare their own ``Header(...)`` parameters and funnel into here.
    They cannot call one another as plain coroutines: a dependency's header defaults are only
    resolved by FastAPI's injection, so the other would receive ``None`` for every header.
    """
    settings: Settings = request.app.state.settings
    return authenticate(
        settings,
        presented=extract_key(authorization, x_api_key),
        actor_hint=x_actor,
        trusted_identity=e2e_token,
    )


async def current_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_actor: str | None = Header(default=None),
    e2e_token: str | None = Header(default=None, alias="E2E-token"),
) -> Actor:
    """The single extension point for authentication."""
    return _resolve(
        request,
        authorization=authorization,
        x_api_key=x_api_key,
        x_actor=x_actor,
        e2e_token=e2e_token,
    )


def require(action: str):
    """Dependency factory: reject callers whose role cannot perform ``action``.

    There is deliberately **no** way to pass a credential in the query string. Such an escape
    hatch existed for the SSE endpoint, because `EventSource` cannot set headers — but the
    console no longer sends credentials at all (the proxy supplies the identity header), so the
    only remaining effect of keeping it would be credentials in access logs.
    """

    async def guard(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
        x_actor: str | None = Header(default=None),
        e2e_token: str | None = Header(default=None, alias="E2E-token"),
    ) -> Actor:
        actor = _resolve(
            request,
            authorization=authorization,
            x_api_key=x_api_key,
            x_actor=x_actor,
            e2e_token=e2e_token,
        )
        if not actor.can(action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role {sorted(actor.roles)} is not permitted to '{action}'. "
                    "An administrator can grant a higher role."
                ),
            )
        return actor

    return guard
