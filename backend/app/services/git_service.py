"""Git access layer: bare mirror repositories + per-run worktrees.

Design notes (see docs/DESIGN.md §2):

* The agent never runs git. Pulling is deterministic server-side code, so it can
  be audited, rate limited, retried and cleaned up.
* One long-lived **bare mirror** per repository keeps repeated analyses cheap
  (incremental ``fetch --prune``); each run gets a lightweight **worktree**
  pinned to an immutable commit.
* Credentials (HTTPS + PAT) are injected through ``GIT_ASKPASS`` for the duration
  of a single command: they never reach ``.git/config``, the process argv, the
  repository URL, or any log line.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.core.config import Settings
from app.domain.models import now_iso

logger = logging.getLogger(__name__)

#: Environment variable holding the PAT for the lifetime of one git invocation.
ASKPASS_TOKEN_ENV = "COBOL_ASKPASS_TOKEN"

#: Directory used as ``core.hooksPath`` so no repository-supplied hook can run. It lives beside
#: the mirrors rather than in a temp directory so the path is stable across invocations.
EMPTY_HOOKS_DIR = Path(tempfile.gettempdir()) / "cobol-analysis-no-hooks"


def _quote_command(*parts: str) -> str:
    """Quote an argv into a single command string for ``GIT_ASKPASS``.

    Git runs ``GIT_ASKPASS`` through a shell, so the pieces have to be quoted for the shell
    git uses, which differs by platform:

    * POSIX — single quotes, escaping embedded single quotes.
    * Windows — double quotes, because ``cmd.exe`` does not understand single quotes as
      quoting characters at all.

    Git for Windows ships ``sh.exe`` and runs the hook through it, in which case double quotes
    also work. Using double quotes on Windows is therefore correct under both possibilities.
    """
    if os.name == "nt":
        quoted = []
        for part in parts:
            # A literal double quote cannot be escaped inside a cmd.exe double-quoted string,
            # and no path or Python source we build here contains one.
            quoted.append(f'"{part}"' if (" " in part or not part) else part)
        return " ".join(quoted)
    quoted = ["'" + part.replace("'", "'\\''") + "'" for part in parts]
    return " ".join(quoted)


# Files worth indexing for a mainframe codebase.
_SECRET_PATTERNS = [
    re.compile(r"https://[^/@\s]+:[^/@\s]+@"),  # https://user:token@host
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b[A-Fa-f0-9]{40}\b"),  # generic 40-hex token
]


def mask_secrets(text: str) -> str:
    """Strip anything credential-shaped before it reaches a log or an event.

    TODO(demo): this covers credentials in logs and event text only. Repository *content* is
    still sent to the model unfiltered — an accepted risk recorded in docs/DEMO.md §7 gap 9, and
    the place to build the audit trail for it is DESIGN.md §6.3 (`llm_egress_log`).
    """
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("***", out)
    return out


class GitError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RepoNotFound(GitError):
    pass


#: Refs we refuse outright, before git is invoked. Two distinct hazards:
#:   * anything starting with ``-`` would be parsed by git as an option. `shell=False` means it
#:     cannot become shell injection, but it can still become *git* argument injection —
#:     ``--upload-pack=...`` is the classic escape from a ref-shaped argument.
#:   * names git itself would reject would otherwise surface as a confusing "cannot resolve ref"
#:     instead of a clear message.
_INVALID_REF_CHARS = set(" \t\n~^:?*[\\")


def validate_ref(ref: str) -> str:
    """Reject a ref that could be read as an option or is not a legal git ref name.

    Applied to operator input and to registry config alike. The check mirrors
    ``git check-ref-format`` for the cases that matter here without shelling out to git for every
    resolution — a subprocess per validation would cost more than it prevents. Cross-checked
    against git on a spread of real branch names; the only deliberate divergence is that this
    accepts ``HEAD``, which ``git check-ref-format --branch`` rejects because that command asks
    whether a name could be *created*. ``HEAD`` is a value the resolver passes itself as a
    last-resort candidate, so rejecting it would break default-branch resolution.
    """
    if not ref or ref != ref.strip():
        raise GitError("Ref must not be empty or padded with whitespace")
    if ref.startswith("-"):
        raise GitError(
            f"Ref {ref!r} starts with '-'; it would be parsed as a git option rather than a ref"
        )
    if _INVALID_REF_CHARS & set(ref):
        raise GitError(f"Ref {ref!r} contains characters git forbids in a ref name")
    if ".." in ref or ref.endswith(".lock") or ref.endswith("/") or "//" in ref:
        raise GitError(f"Ref {ref!r} is not a valid git ref name")
    if any(part.startswith(".") for part in ref.split("/")):
        raise GitError(f"Ref {ref!r} has a path component starting with '.'")
    return ref


class RepoAuthError(GitError):
    pass


@dataclass
class RepoSpec:
    repo_key: str
    url: str
    name: str = ""
    default_branch: str = "main"
    description: str = ""
    username: str = "oauth2"
    credential_env: str = ""  # name of the env var / systemd credential holding the PAT
    shallow: bool = False

    @property
    def display_name(self) -> str:
        return self.name or self.repo_key


class GitService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._specs = self._load_specs(settings)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.RLock()
        self._prepare_hooks_dir()

    @staticmethod
    def _prepare_hooks_dir() -> None:
        """Create the empty hooks directory used as ``core.hooksPath``.

        An empty *directory* is used rather than ``/dev/null``: git requires a directory here, and
        a non-existent path makes some git versions warn or error. The directory is shared across
        repositories and never written to.
        """
        try:
            EMPTY_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - unusual temp permissions
            logger.warning(
                "could not create the empty hooks directory %s: %s. Repository-supplied hooks "
                "may be able to run.",
                EMPTY_HOOKS_DIR,
                exc,
            )

    # ------------------------------------------------------------------ specs
    @staticmethod
    def _load_specs(settings: Settings) -> dict[str, RepoSpec]:
        """Load the repository registry.

        Priority: ``REPOS_FILE`` (yaml) > built-in demo entry.
        The yaml is the intranet integration point — see docs/DEMO.md. Both are read through
        :class:`Settings`, never from ``os.environ`` here (CODESTYLE.md §1).

        Two rules are enforced at load time rather than at fetch time, because configuration is
        input and a bad entry should fail while an operator is watching:

        * credentials must not be embedded in the URL (they leak into logs and ``.git/config``
          on the first fetch) — use ``credential_env`` with ``GIT_ASKPASS`` injection;
        * the scheme must be HTTPS, which is what the PAT design assumes. SSH and ``file://``
          remotes would each add their own credential surface, and neither is supported.

        Both rules are stated at the bottom of ``backend/repos.example.yaml``; keep the two in
        step, or the example documents a control that does not exist.
        """
        path = settings.repos_file
        specs: dict[str, RepoSpec] = {}
        if path and Path(path).exists():
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            for item in raw.get("repos", []):
                spec = RepoSpec(
                    repo_key=item["repo_key"],
                    url=item["url"],
                    name=item.get("name", ""),
                    default_branch=item.get("default_branch", "main"),
                    description=item.get("description", ""),
                    username=item.get("username", "oauth2"),
                    credential_env=item.get("credential_env", ""),
                    shallow=bool(item.get("shallow", False)),
                )
                # Refuse a credentials-in-URL config outright: it leaks into logs
                # and into .git/config on the first fetch.
                if re.match(r"^https?://[^/@]+:[^/@]+@", spec.url):
                    raise GitError(
                        f"repo {spec.repo_key}: credentials must not be embedded in the URL; "
                        "use credential_env with GIT_ASKPASS injection"
                    )
                scheme = spec.url.split("://", 1)[0].lower()
                if not spec.url.startswith("https://"):
                    raise GitError(
                        f"repo {spec.repo_key}: the registry accepts https:// URLs only, this "
                        f"entry uses {scheme!r}. Use an https remote with credential_env; ssh "
                        "and file remotes are not supported."
                    )
                specs[spec.repo_key] = spec
        # Demo fallback so the service is usable before the registry is filled in.
        specs.setdefault(
            "demo",
            RepoSpec(
                repo_key="demo",
                url=settings.demo_repo_url,
                name="Demo repository",
                default_branch=settings.demo_repo_branch,
                description="Configured via DEMO_REPO_URL / DEMO_REPO_BRANCH",
            ),
        )
        return specs

    def reload_specs(self) -> int:
        self._specs = self._load_specs(self.settings)
        return len(self._specs)

    def list_repos(self) -> list[RepoSpec]:
        return [s for s in self._specs.values() if s.url]

    def spec(self, repo_key: str) -> RepoSpec:
        spec = self._specs.get(repo_key)
        if not spec or not spec.url:
            raise RepoNotFound(f"Repository {repo_key!r} is not present in the repository registry")
        return spec

    # ----------------------------------------------------------------- locks
    def _repo_lock(self, repo_key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(repo_key, threading.RLock())

    # ---------------------------------------------------------------- askpass
    def askpass_command(self, username: str, token_env: str) -> str:
        """Build the ``GIT_ASKPASS`` command line for supplying a credential to git.

        Git invokes this command with a single prompt argument and reads the credential from
        stdout, so any executable works. **Python is used rather than a shell script**, which
        removes three problems at once:

        * it works on Windows, where git cannot execute a ``#!/bin/sh`` file without a Git-Bash
          installation on ``PATH``;
        * no temporary file is written and no permission bit has to be set, so a credential
          never touches the filesystem;
        * the username is passed as an argument rather than interpolated into a shell script,
          so it cannot break out of quoting.

        The token itself is **not** in the command line — it travels in ``token_env`` inside the
        child process environment, because process arguments are readable by other users on the
        host via ``ps``.
        """
        # git sends the prompt as argv[1] and reads the answer from stdout. The username test is
        # case-insensitive because git capitalises it ("Username for ..."), and matching the
        # lower-case form would send the token where the username belongs — it would still
        # authenticate in most setups, which is exactly why the mistake is worth avoiding.
        code = (
            "import os,sys;"
            f"p=(sys.argv[1] if len(sys.argv)>1 else '').lower();"
            f"print({username!r} if 'username' in p else os.environ.get({token_env!r},''))"
        )
        return _quote_command(sys.executable, "-c", code)

    @contextmanager
    def _env(self, spec: RepoSpec) -> Iterator[dict[str, str]]:
        env = {
            **os.environ,
            # No interactive prompts, ever: a prompt would hang a worker forever.
            "GIT_TERMINAL_PROMPT": "0",
            # Ignore /etc/gitconfig, which an operator could have configured in ways this service
            # does not expect.
            "GIT_CONFIG_NOSYSTEM": "1",
            # A checkout's `.git/hooks` is repository-controlled content, and this service treats
            # repository content as untrusted. Pointing hooksPath at an empty directory means a
            # hook shipped in the repository cannot execute ourselves.
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(EMPTY_HOOKS_DIR),
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
        }
        token = ""
        if spec.credential_env:
            token = os.environ.get(spec.credential_env, "")
        if not token:
            yield env
            return
        # Deliberately a specific name: a bare `GIT_PAT` in the child process environment could
        # collide with an unrelated variable on the host and silently supply the wrong
        # credential. It is set and consumed within this one invocation.
        env[ASKPASS_TOKEN_ENV] = token
        env["GIT_ASKPASS"] = self.askpass_command(spec.username, ASKPASS_TOKEN_ENV)
        try:
            yield env
        finally:
            env.pop(ASKPASS_TOKEN_ENV, None)

    def _git(
        self,
        spec: RepoSpec | None,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 600,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env_ctx = self._env(spec) if spec else _null_ctx()
        with env_ctx as env:
            argv = [self.settings.git_bin, *args]
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(cwd) if cwd else None,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise GitError(
                    f"git command timed out: {' '.join(args[:2])}", retryable=True
                ) from exc
        if check and proc.returncode != 0:
            stderr = mask_secrets(proc.stderr.strip())[:800]
            lowered = stderr.lower()
            if (
                "authentication failed" in lowered
                or "403" in lowered
                or "invalid username" in lowered
            ):
                raise RepoAuthError(
                    "Git authentication failed: the PAT may be expired or lack permission. "
                    "Update the credential configuration.",
                    retryable=False,
                )
            if "repository not found" in lowered or "404" in lowered:
                raise RepoNotFound(f"Repository not found or access denied: {stderr}")
            raise GitError(f"git command failed ({proc.returncode}): {stderr}", retryable=True)
        return proc

    # ---------------------------------------------------------------- mirror
    def mirror_path(self, repo_key: str) -> Path:
        digest = hashlib.sha1(repo_key.encode()).hexdigest()[:8]
        return self.settings.mirrors_dir / f"{repo_key}-{digest}.git"

    def ensure_mirror(self, repo_key: str, *, max_age_seconds: int | None = None) -> Path:
        """Make sure a usable mirror exists, fetching when it is absent or stale.

        ``max_age_seconds`` bounds how old the local copy may be:

        * ``0`` — always fetch. This is what an explicit "pull from remote" action passes; the
          operator asked for the latest, so a cached answer would be wrong.
        * ``None`` — use the configured default (``MIRROR_MAX_AGE_SECONDS``). A run does not
          need a network round trip if the mirror was refreshed moments ago, and on a
          mainframe-sized repository that round trip is the slowest part of startup.

        Fetching is serialised per repository, so concurrent runs on the same repo wait rather
        than corrupt the mirror.
        """
        spec = self.spec(repo_key)
        mirror = self.mirror_path(repo_key)
        age_limit = (
            self.settings.mirror_max_age_seconds if max_age_seconds is None else max_age_seconds
        )

        with self._repo_lock(repo_key):
            if not (mirror / "HEAD").exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                logger.info("cloning mirror %s", repo_key)
                args = ["clone", "--mirror", "--no-recurse-submodules"]
                if spec.shallow:
                    args += ["--depth", "1"]
                args += [spec.url, str(mirror)]
                self._git(spec, args, timeout=1800)
                self._touch_fetched(mirror)
            elif age_limit <= 0 or self.mirror_age_seconds(repo_key) >= age_limit:
                # The mirror is keyed by repo_key, so a registry change that points the same key
                # at a different URL would otherwise keep fetching the OLD origin and silently
                # analyse the wrong repository. Re-point it first.
                self._sync_origin(spec, mirror)
                logger.info("fetching mirror %s", repo_key)
                self._git(
                    spec,
                    [
                        "-c",
                        "protocol.file.allow=always",
                        "fetch",
                        "--prune",
                        "--prune-tags",
                        "--tags",
                        "--no-recurse-submodules",
                        "origin",
                        "+refs/heads/*:refs/heads/*",
                    ],
                    cwd=mirror,
                    timeout=1800,
                )
                self._touch_fetched(mirror)
            else:
                logger.debug(
                    "mirror %s is %ds old (limit %ds); using the local copy",
                    repo_key,
                    self.mirror_age_seconds(repo_key),
                    age_limit,
                )
        return mirror

    def _fetch_stamp(self, repo_key: str) -> Path:
        return self.mirror_path(repo_key) / ".last_fetch"

    def _touch_fetched(self, mirror: Path) -> None:
        """Record when the remote was last contacted.

        A file rather than the directory mtime, because git rewrites directory entries during a
        fetch in ways that make mtime an unreliable clock.
        """
        try:
            (mirror / ".last_fetch").write_text(str(int(time.time())), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - best effort
            logger.warning("could not record fetch time for %s: %s", mirror, exc)

    def mirror_age_seconds(self, repo_key: str) -> int:
        """Seconds since the remote was last contacted, or a large number if unknown.

        Unknown age is reported as "very old" on purpose: treating a missing stamp as fresh
        would let a stale mirror be used forever.
        """
        recorded = self._recorded_stamp(repo_key)
        if recorded is None:
            return 10**9
        return max(0, int(time.time()) - recorded)

    def last_fetched_at(self, repo_key: str) -> str | None:
        """When the remote was last contacted, as an ISO timestamp, or ``None`` if unknown.

        The console shows this next to a repository, so it has to be the recorded fetch time
        rather than "now": reporting the moment of the request would make a mirror that has
        not been refreshed in weeks look current.
        """
        recorded = self._recorded_stamp(repo_key)
        if recorded is None:
            return None
        return datetime.fromtimestamp(max(0, recorded), UTC).isoformat(timespec="seconds")

    def _recorded_stamp(self, repo_key: str) -> int | None:
        """Read the fetch stamp, or ``None`` when it is missing or unreadable."""
        try:
            return int(self._fetch_stamp(repo_key).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def fetch_remote(self, repo_key: str) -> dict[str, object]:
        """Pull the latest refs from the remote, on demand.

        The operator-facing counterpart to ``ensure_mirror``: it always contacts the remote and
        reports what it found, so the console can show a concrete result instead of a spinner
        that may or may not have done anything.
        """
        spec = self.spec(repo_key)
        started = time.monotonic()
        # max_age_seconds=0 forces the fetch; the age check would otherwise short-circuit it.
        self.ensure_mirror(repo_key, max_age_seconds=0)
        branches = self.list_branches(repo_key, limit=200)
        default_ref = self.resolve_ref(repo_key, spec.default_branch)
        return {
            "repo_key": repo_key,
            "url": spec.url,
            "default_branch": spec.default_branch,
            "default_branch_sha": default_ref,
            "branches": branches,
            "branch_count": len(branches),
            "fetched_at": now_iso(),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def _sync_origin(self, spec: RepoSpec, mirror: Path) -> None:
        """Ensure the mirror's ``origin`` matches the registry, re-pointing if needed."""
        proc = self._git(None, ["remote", "get-url", "origin"], cwd=mirror, check=False)
        current = proc.stdout.strip()
        if current == spec.url:
            return
        logger.warning(
            "repo %s origin changed (%s -> %s); re-pointing the mirror",
            spec.repo_key,
            mask_secrets(current),
            mask_secrets(spec.url),
        )
        self._git(None, ["remote", "set-url", "origin", spec.url], cwd=mirror)

    # ------------------------------------------------------------------- refs
    def resolve_ref(self, repo_key: str, ref: str | None) -> str:
        spec = self.spec(repo_key)
        mirror = self.ensure_mirror(repo_key)
        # The operator's ref *and* the configured default branch are both validated: a registry
        # is configuration, and configuration is input. `origin/HEAD` and `HEAD` are literals.
        candidates = [validate_ref(ref)] if ref else []
        default = validate_ref(spec.default_branch)
        candidates += [f"origin/{default}", default, "origin/HEAD", "HEAD"]
        for candidate in candidates:
            if not candidate:
                continue
            proc = self._git(
                spec,
                ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                cwd=mirror,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        raise GitError(f"Cannot resolve ref: {ref or spec.default_branch}")

    def list_branches(self, repo_key: str, limit: int = 50) -> list[str]:
        spec = self.spec(repo_key)
        mirror = self.ensure_mirror(repo_key)
        proc = self._git(
            spec,
            [
                "for-each-ref",
                "--format=%(refname:short)",
                "--sort=-committerdate",
                f"--count={limit}",
                "refs/heads",
            ],
            cwd=mirror,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    # --------------------------------------------------------------- worktree
    def worktree_dir(self, run_id: str) -> Path:
        return self.settings.runs_dir / run_id / "repo"

    def create_worktree(self, repo_key: str, run_id: str, commit: str) -> Path:
        """Check out ``commit`` into an isolated directory for this run.

        Nothing here trims ``.git/hooks``: in a worktree ``.git`` is a pointer *file*, and the
        clone fallback below only ever receives git's inert ``*.sample`` hooks. Repository-supplied
        hooks are instead disabled for every git invocation, by ``core.hooksPath`` in :meth:`_env`
        — one control that covers fetch, checkout and the fallback alike.
        """
        spec = self.spec(repo_key)
        mirror = self.ensure_mirror(repo_key)
        target = self.worktree_dir(run_id)
        if target.exists():
            # A sealed tree is read-only, so drop the bits before removing it.
            self.unseal_worktree(target)
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._repo_lock(repo_key):
            # A bare mirror cannot have a worktree attached, so checkout into a
            # plain clone of the local mirror when worktree support is absent.
            proc = self._git(
                spec,
                [
                    "-c",
                    "protocol.file.allow=always",
                    "worktree",
                    "add",
                    "--detach",
                    "--force",
                    str(target),
                    commit,
                ],
                cwd=mirror,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning(
                    "worktree add failed, falling back to local clone: %s",
                    mask_secrets(proc.stderr.strip())[:200],
                )
                self._git(
                    spec,
                    [
                        "-c",
                        "protocol.file.allow=always",
                        "clone",
                        "--no-checkout",
                        "--no-recurse-submodules",
                        str(mirror),
                        str(target),
                    ],
                    timeout=1800,
                )
                # `core.fileMode=false`: the checkout is sealed read-only immediately after this,
                # and on a filesystem that reflects that in the mode bits (POSIX, or the read-only
                # attribute on NTFS) git would otherwise report every file as modified. Harmless on
                # POSIX, necessary on Windows.
                self._git(
                    spec,
                    ["-c", "core.fileMode=false", "checkout", "--detach", commit],
                    cwd=target,
                    timeout=600,
                )

        self.seal_worktree(target)
        return target

    @staticmethod
    def seal_worktree(worktree: Path) -> None:
        """Make the checkout read-only at the OS level, where the platform supports it.

        ``FilesystemPermission`` rules are enforced by the agent's filesystem *middleware*, which
        protects the model's tool calls but not a direct backend call from server-side code.
        Making the tree read-only adds an independent, kernel-enforced layer: even if a future
        code path writes through the backend, the checkout cannot change, so every analysis stays
        reproducible against its pinned commit.

        ``.git`` stays writable because its metadata needs it; the working tree itself does not.

        **On Windows this layer is not available.** NTFS does not implement POSIX permission bits
        through ``chmod``: setting ``0o444`` only toggles the read-only attribute and does not stop
        the owner from writing. Rather than report success for a control that is not in effect, the
        degradation is logged once per run, so a Windows deployment is not mistaken for a fully
        layered one. ``FilesystemPermission`` still blocks the agent's own writes there.
        """
        if os.name == "nt":
            logger.warning(
                "read-only checkout sealing is not enforced on Windows (chmod has no POSIX "
                "effect on NTFS); the agent's filesystem permission rules remain the only layer "
                "protecting %s",
                worktree,
            )
            return
        dot_git = worktree / ".git"
        for path in sorted(worktree.rglob("*"), reverse=True):
            if path == dot_git or dot_git in path.parents:
                continue
            try:
                if path.is_symlink() or path.is_file():
                    path.chmod(0o444)
                elif path.is_dir():
                    path.chmod(0o555)
            except OSError as exc:  # pragma: no cover - platform dependent
                logger.warning("could not seal %s: %s", path, exc)
        try:
            worktree.chmod(0o555)
        except OSError as exc:  # pragma: no cover
            logger.warning("could not seal worktree root %s: %s", worktree, exc)

    @staticmethod
    def unseal_worktree(worktree: Path) -> None:
        """Restore write bits so the checkout can be removed."""
        if not worktree.exists():
            return
        for path in worktree.rglob("*"):
            try:
                if path.is_symlink():
                    continue
                path.chmod(0o755 if path.is_dir() else 0o644)
            except OSError:  # pragma: no cover
                continue
        with contextlib.suppress(OSError):  # pragma: no cover - best effort
            worktree.chmod(0o755)

    def head_commit(self, worktree: Path) -> str:
        proc = self._git(None, ["rev-parse", "HEAD"], cwd=worktree, check=False)
        return proc.stdout.strip()

    def read_blob(self, worktree: Path, rel_path: str, start: int, end: int) -> str:
        """Read a line range from the checked out source (server-side slicing)."""
        target = (worktree / rel_path).resolve()
        root = worktree.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise GitError(f"Path escapes the repository root: {rel_path}") from exc
        if not target.is_file():
            raise GitError(f"File not found: {rel_path}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start)
        end = min(len(lines), end, start + self.settings.max_slice_lines - 1)
        width = len(str(end))
        return "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))

    # ------------------------------------------------------------- maintenance
    def run_dir(self, run_id: str) -> Path:
        return self.settings.runs_dir / run_id

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def work_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "work"

    def cleanup_worktree(self, repo_key: str, run_id: str) -> None:
        """Drop the checkout, keep artifacts. Frees the bulk of the disk."""
        target = self.worktree_dir(run_id)
        mirror = self.mirror_path(repo_key)
        if target.exists():
            self._git(None, ["worktree", "prune"], cwd=mirror, check=False)
            self.unseal_worktree(target)
            shutil.rmtree(target, ignore_errors=True)

    def disk_usage_mb(self) -> float:
        total = 0
        for root in (self.settings.mirrors_dir, self.settings.runs_dir):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except OSError:
                    continue
        return round(total / 1024 / 1024, 1)

    def gc(self, older_than_days: int | None = None) -> dict[str, object]:
        """Delete run directories past their TTL. Mirrors are kept."""
        days = self.settings.run_ttl_days if older_than_days is None else older_than_days
        cutoff = time.time() - days * 86400
        removed: list[str] = []
        freed = 0
        if self.settings.runs_dir.exists():
            for run_dir in self.settings.runs_dir.iterdir():
                if not run_dir.is_dir() or run_dir.stat().st_mtime > cutoff:
                    continue
                for path in run_dir.rglob("*"):
                    try:
                        if path.is_file() and not path.is_symlink():
                            freed += path.stat().st_size
                    except OSError:
                        continue
                self.unseal_worktree(self.worktree_dir(run_dir.name))
                shutil.rmtree(run_dir, ignore_errors=True)
                removed.append(run_dir.name)
        return {
            "removed_runs": removed,
            "freed_mb": round(freed / 1024 / 1024, 1),
            "disk_usage_mb": self.disk_usage_mb(),
        }


@contextmanager
def _null_ctx() -> Iterator[dict[str, str]]:
    yield {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
