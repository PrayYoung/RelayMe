# Security Policy

## Security Model

RelayMe is designed as a capability-scoped execution plane for AI agents and automation systems operating on remote machines. Its security model is based on defense-in-depth:

- **Agent-Side Authority**: The Host Agent is the final policy enforcement boundary. It never relies solely on Controller or client claims.
- **No Arbitrary Shell**: RelayMe does not expose raw shell execution or interactive interpreters. Tasks execute only pre-registered binaries and task specifications.
- **Container Sandbox**: Executor tasks execute inside rootless Podman containers with enforced `--network none`, read-only root filesystems, and strict resource bounds.
- **Identity Isolation**: Supports Agent-derived non-root execution identities (`keep-id` + `agent`) to prevent root privilege escalation.
- **Repository Immutability**: All modifications occur in disposable Git worktrees that are discarded after execution. Upstream repositories are never modified directly.
- **Bounded Artifact Retrieval**: Retained artifacts (`patch.diff`, `result.json`, `test.log`, `analysis.md`) are subject to strict canonical path validation, directory traversal prevention, symlink protection, and a conservative byte ceiling.

## Reporting a Vulnerability

If you discover a security vulnerability in RelayMe, please report it responsibly:

- Please use **GitHub Private Vulnerability Reporting** via the repository's [Security Advisories](https://github.com/PrayYoung/RelayMe/security/advisories/new) tab.
- Please do **not** open a public issue or discussion for undisclosed security vulnerabilities.
- Provide a detailed description of the issue, reproduction steps, and any proof-of-concept code.
