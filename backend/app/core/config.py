"""Application configuration.

All tunables come from environment variables or a ``.env`` file.
See ``docs/DEMO.md`` for the values that must be changed when moving to the intranet host.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Environment variables searched, in order, when ``llm_api_key_env`` is empty.
#:
#: :data:`LLM_AUTH_TOKEN_ENV` is the neutral, preferred name. The two after it are compatibility
#: fallbacks for hosts where a credential is already provisioned under a vendor- or tool-specific
#: name and cannot be renamed. Nothing here is required: naming the variable explicitly with
#: ``LLM_API_KEY_ENV`` takes precedence over all of them.
LLM_AUTH_TOKEN_ENV = "LLM_AUTH_TOKEN"

DEFAULT_KEY_ENVS: dict[str, tuple[str, ...]] = {
    "openai": (LLM_AUTH_TOKEN_ENV, "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "anthropic": (LLM_AUTH_TOKEN_ENV, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=os.environ.get("COBOL_ENV_PREFIX", ""),
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )
    """Environment variable names are **unprefixed**: ``DATA_ROOT``, ``LLM_MODEL``, …

    The prefix is read from ``COBOL_ENV_PREFIX`` at import time and is empty by default, so a
    host that wants namespacing (for example to coexist with unrelated services) can set
    ``COBOL_ENV_PREFIX=COBOL_`` and get the previous ``COBOL_*`` names back without touching
    code.

    The risk this accepts: an unprefixed name can collide with an unrelated variable on the
    same host. Verified that none of these names appear in a typical environment — but
    ``LLM_*`` and ``SKILLS_*`` are generic enough that a collision would be silent, so if a
    service on the same host exports any of them, set ``COBOL_ENV_PREFIX``.
    """

    # ---------------------------------------------------------------- storage
    data_root: Path = Field(default=Path("./data"))
    """Everything the service writes lives under here: mirrors, runs, sqlite, logs."""

    run_ttl_days: int = Field(default=7)
    """Run directories older than this are reclaimed by ``python scripts/dev.py gc``."""

    mirror_max_age_seconds: int = Field(default=300)
    """How stale a repository mirror may be before a run refetches it.

    A run never needs a network round trip if the mirror was refreshed in the last few minutes,
    and on a mainframe-sized repository that round trip is the slowest part of startup. Set to
    ``0`` to always fetch, which trades speed for the guarantee that every run sees the newest
    remote state. The explicit "pull from remote" action ignores this and always fetches.
    """

    # ------------------------------------------------------------------- auth
    auth_mode: Literal["trusted_header", "api_key", "disabled", "e2e_token"] = Field(
        default="trusted_header"
    )
    """How a caller is identified.

    * ``trusted_header`` — **the current default.** Identity arrives in a header set by
      whatever sits in front of this service (the Vite dev proxy today, an nginx/SSO gateway
      in production). No interactive step, which is what removes the need for a login screen.
      The header is trusted *without verification*, so this mode is only safe when the service
      is reachable exclusively through that proxy. See the warning below.
    * ``api_key`` — a shared key per caller. Kept because it is the only mode with an
      in-process credential, useful when no proxy is in front.
    * ``disabled`` — everyone is an admin. Local development only; never on a reachable host.
    * ``e2e_token`` — **reserved for the intranet SSO integration.** The client will send the
      gateway-issued token in ``E2E_TOKEN_HEADER``; the server will verify it and map the
      claims to roles. Unimplemented on purpose: it exists so the wiring and the client header
      do not have to change when the integration lands — only :func:`authenticate`.
    """

    trusted_actor_header: str = Field(default="E2E-token")
    """Header carrying the authenticated caller in ``trusted_header`` mode.

    Named for the SSO header the intranet gateway is expected to send, so the dev proxy and
    the production gateway agree on one name and no client change is needed later.
    """

    trusted_default_role: str = Field(default="admin")
    """Role granted to a caller identified by the trusted header.

    A single role because the header carries an identity, not a grant — a caller cannot
    promote itself by inventing a value. Per-user roles arrive with the SSO claim mapping;
    until then, narrow this to ``analyst`` on a host where not every user should be able to
    delete runs.
    """

    api_keys: str = Field(default="")
    """Comma separated ``key:role`` pairs, e.g. ``k1:analyst,k2:viewer``.
    A bare ``key`` means role ``analyst``. Only read in ``api_key`` mode."""

    cors_origins: str = Field(default="http://localhost:5173,http://127.0.0.1:5173")

    # --------------------------------------------------------------------- llm
    llm_provider: Literal["openai", "anthropic"] = Field(default="openai")
    """Which wire protocol the gateway speaks.

    * ``openai``    — ``POST /v1/chat/completions``. The default, and the dialect nearly every
      hosted and self-hosted gateway exposes. Verified against a live gateway: streaming, single
      tool calls, and parallel tool calls in one response all work.
    * ``anthropic`` — ``POST /v1/messages``. For a gateway that speaks only that dialect. Its
      responses may carry ``thinking`` blocks, which ``pipeline._text_delta`` filters out.

    Switching provider changes nothing else in the codebase: ``build_model`` is the only reader.

    **No vendor is named anywhere in this project.** The endpoint and model are configuration, so
    a deployment can move between providers — or run two side by side — without a code change.
    """

    llm_base_url: str = Field(default="")
    llm_api_key: str = Field(default="")
    """Explicit key. Prefer ``llm_api_key_env`` so the secret stays out of config files."""

    llm_api_key_env: str = Field(default="")
    """Name of the environment variable holding the key.

    Empty means the provider's conventional variables are searched, starting with
    ``LLM_AUTH_TOKEN`` — see :data:`DEFAULT_KEY_ENVS`. The indirection exists so
    a key can come from systemd credentials or a secret store without ever being written
    into a config file, a log line or a prompt.
    """

    llm_auth_token: str = Field(default="")
    """The credential under its neutral name, as written in a config file.

    This exists because ``.env`` is the surface an operator actually edits, and a
    ``LLM_AUTH_TOKEN=`` line there used to be silently discarded: the name is not a field of this
    class, and :meth:`resolve_api_key` reads the *process* environment, so the service started
    with no credential and failed every run at the first model call.

    A file is a weaker place for a secret than the environment, so the file must be ``0600`` and
    the environment still wins — see the resolution order in :meth:`resolve_api_key`.
    """

    llm_model: str = Field(default="")
    llm_temperature: float = Field(default=0.0)
    llm_max_tokens: int = Field(default=8192)
    llm_timeout_seconds: int = Field(default=600)

    analysis_model: str = Field(default="")
    """Optional model override for the analysis skill; empty means use ``llm_model``."""

    artifact_model: str = Field(default="")
    """Optional model override for deliverable generation (a cheaper/faster model is fine)."""

    # ------------------------------------------------------------------ skills
    skills_dir: Path = Field(default=Path(__file__).resolve().parent.parent.parent / "skills")
    """Directory holding one subdirectory per skill, each with a ``SKILL.md``.

    Defaults to ``<repo>/backend/skills``, which is correct for a source checkout. Set
    ``SKILLS_DIR`` on a deployed host where the code lives elsewhere.

    Skills are read through the agent's backend under the virtual path ``/skills``, so the
    directory only has to be readable — it is granted no write access and needs no entry in
    the run workspace.
    """

    skills_enabled: bool = Field(default=True)
    """Serve and advertise skills to the agent.

    Disabling it removes the ``SkillsMiddleware`` and the ``/skills`` route entirely, so the
    agent runs on its task prompt alone. Useful for isolating whether a behaviour change came
    from a skill edit, and as a kill switch if a skill file is found to be harmful.
    """

    # ----------------------------------------------------------------- budget
    max_parallel_artifacts: int = Field(default=3)
    max_tool_calls: int = Field(default=400)
    max_wall_seconds: int = Field(default=2700)
    max_slice_lines: int = Field(default=400)

    # ---------------------------------------------------------- repositories
    repos_file: Path | None = Field(default=None)
    """Registry file listing repositories (``backend/repos.example.yaml`` documents the shape).

    ``None`` means no registry: the built-in ``demo`` entry below is the only repository, which
    is what a fresh checkout and the integration test use.
    """

    demo_repo_url: str = Field(default="")
    demo_repo_branch: str = Field(default="main")
    """The built-in ``demo`` repository, for a single-repository deployment.

    A convenience rather than a second mechanism: these feed the same :class:`RepoSpec` the
    registry produces, so everything downstream — mirror, ref resolution, worktree — behaves
    identically. Empty ``demo_repo_url`` simply means the entry has no URL and is not listed.
    """

    # ------------------------------------------------------------------- misc
    git_bin: str = Field(default="git")
    log_level: str = Field(default="INFO")

    @field_validator("data_root", "skills_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @model_validator(mode="after")
    def _require_model_endpoint(self) -> Settings:
        """Refuse to start with no model endpoint rather than failing on the first run.

        The endpoint and model have no defaults on purpose: a baked-in vendor default would be
        wrong for every other deployment, and a silently empty one would surface as an opaque
        connection error minutes into an analysis. Failing at startup names the two variables to
        set.
        """
        missing = [
            name
            for name, value in (("LLM_BASE_URL", self.llm_base_url), ("LLM_MODEL", self.llm_model))
            if not value
        ]
        if missing:
            raise ValueError(
                f"{' and '.join(missing)} must be set. Point them at an OpenAI-compatible "
                f"(or Anthropic-compatible, with LLM_PROVIDER=anthropic) gateway; see "
                f".env.example for the shape of both."
            )
        return self

    # ------------------------------------------------------------- derived
    @property
    def mirrors_dir(self) -> Path:
        return self.data_root / "git" / "mirrors"

    @property
    def runs_dir(self) -> Path:
        return self.data_root / "runs"

    @property
    def db_path(self) -> Path:
        return self.data_root / "app.db"

    @property
    def key_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw in self.api_keys.split(","):
            raw = raw.strip()
            if not raw:
                continue
            key, _, role = raw.partition(":")
            out[key.strip()] = (role or "analyst").strip()
        return out

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def resolve_api_key(self) -> str:
        """Return the model credential, or an empty string when none is configured.

        Resolution order, most explicit first:

        1. ``llm_api_key`` — a value written straight into configuration;
        2. the variable named by ``llm_api_key_env``, when it is set in the environment;
        3. ``llm_auth_token`` — the neutral name, as written in ``.env``;
        4. the provider's conventional variables (:data:`DEFAULT_KEY_ENVS`), so a host whose key
           is already provisioned under a tool-specific name needs no change.

        The environment beats the file, which is what makes "export the real key for this shell"
        work while a placeholder sits in ``.env``. Errors never include the value, only the names
        consulted, so a failed startup cannot leak a secret into a log.
        """
        if self.llm_api_key:
            return self.llm_api_key
        candidates: list[str] = []
        if self.llm_api_key_env:
            candidates.append(self.llm_api_key_env)
        candidates.extend(DEFAULT_KEY_ENVS[self.llm_provider])
        for name in candidates:
            value = os.environ.get(name, "")
            if value:
                return value
        if self.llm_auth_token:
            return self.llm_auth_token
        logger.warning(
            "no model credential found; checked %s and LLM_AUTH_TOKEN in the config file. "
            "Set LLM_API_KEY_ENV, export one of those variables, or put LLM_AUTH_TOKEN in a "
            "0600 .env.",
            ", ".join(candidates) or "(none)",
        )
        return ""

    def ensure_dirs(self) -> None:
        for p in (self.data_root, self.mirrors_dir, self.runs_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
