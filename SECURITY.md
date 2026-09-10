# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it to the security owner directly, with:

- the affected component (API, agent runtime, git layer, console, knowledge base)
- reproduction steps, or the smallest input that demonstrates the problem
- the impact you believe it has
- any mitigation you have already identified

Credential exposure is always treated as a **P1 incident**. If a PAT, API key or session token has
reached a log, a commit, a prompt, an event stream or an artifact, **rotate it first and
investigate second** — revoking a leaked credential costs minutes, and an exfiltration is not
reversible.

## Supported versions

This is a pre-release project. Only the current `main` receives security fixes.

## What this project assumes about its environment

The threat model is an **intranet, single-host deployment behind a proxy**. Two assumptions carry
most of the weight, and each is a precondition rather than a suggestion:

1. **`AUTH_MODE=trusted_header` trusts an unverified header.** Identity is attached by whatever
   proxies to the service — the Vite dev proxy, or the SSO gateway in production. If the service
   port is reachable without going through that proxy, a caller can forge the header and take the
   role named by `TRUSTED_DEFAULT_ROLE`. The service logs a warning at startup in this mode. Fix
   the network; do not silence the warning.
2. **The repository host is trusted infrastructure.** Source code is treated as untrusted *input*
   — it cannot instruct the agent, and repository-supplied hooks cannot execute — but the git
   remote itself is assumed to be a legitimate internal system.

## Controls worth knowing about

The full list, with where each control is actually enforced, is in
[`docs/CODESTYLE.md` §5](docs/CODESTYLE.md). The short version:

| Property | How |
|---|---|
| The agent cannot run shell commands or git | No backend implements the sandbox protocol, so the `execute` tool is never offered; git is server-side service code |
| The agent cannot leave its run directory | `FilesystemBackend(virtual_mode=True)` plus permission rules; symlink escapes are resolved and rejected |
| The checkout cannot be modified | Permission rules **and** POSIX read-only mode bits (POSIX only — see below) |
| Credentials stay in one process | PATs are injected through `GIT_ASKPASS` for a single command; the host credential helper is cleared; all git output is masked before logging |
| Git arguments cannot inject options | Every ref is validated before reaching git, cross-checked against `git check-ref-format` |
| A runaway run is bounded | Tool-call and wall-clock caps, plus a per-read line cap |

### Known platform deviation

Read-only checkout sealing uses POSIX mode bits. On Windows, `chmod` does not implement POSIX
permissions, so that layer is **not in effect**; the service logs a warning at run time rather
than reporting success. The permission rules remain the only layer protecting the checkout there.

### Deliberate non-goal

Content-level redaction of repository source before it reaches the model is **out of scope**,
by decision of the project owner. Credentials are masked; business data in source is not filtered.
This is recorded as an accepted risk in [`docs/DEMO.md` §7](docs/DEMO.md). Revisit it before the
platform is pointed at repositories containing production data extracts.
