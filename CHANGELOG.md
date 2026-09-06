# Changelog

All notable changes to RelayMe are documented in this file.

## [0.4.1] - 2026-09-06

### Added
- Bounded executor-task artifact retrieval capability (`get_task_artifact`) across HTTP (`GET /v1/tasks/{task_id}/artifacts/{artifact_name}`), CLI (`relayme task-artifact`), and generic FastMCP adapter (13 tools total).
- Manifest-only artifact resolution limited to `patch.diff`, `result.json`, `test.log`, and `analysis.md`.
- Strict canonical filesystem validation: rejection of directory traversal (`..`), path separators, symlink escapes, directories, and non-regular files.
- Conservative per-response byte ceiling (100,000 bytes) with explicit truncation metadata while preserving full artifact size and SHA-256 digest.
- Full external-agent review loop verified end-to-end with standalone Antigravity CLI as generic MCP client on real-host executor tasks without SSH or filesystem access.

## [0.4.0] - 2026-09-06

### Added
- Bounded registered executor task bridge (`start_registered_executor_task`) for the fixed `patch-and-test` task specification.
- Rootless Podman container sandboxing with `--network none`, read-only root filesystems, and strict CPU/memory/PID limits.
- Agent-local `user_namespace: keep-id` and `execution_identity: agent` modes deriving non-root execution identities from the running Host Agent.
- Isolated disposable Git worktree lifecycle: modifications run inside ephemeral worktrees while host source repositories remain immutable.
- SQLite-backed atomic idempotency and short-term result retention.
- Owner-scoped terminal task state, result, and review queries (`get_task`, `task_result`, `list_reviewable_tasks`).

## [0.3.1] - 2026-08-30

### Changed
- Corrected package metadata to declare Python >= 3.10 requirement.

## [0.3.0] - 2026-08-29

### Added
- Bounded registered task execution (`run_registered_task`) with locally registered fixed binary argv, working directory, and environment.
- Idempotency replay with atomic retention and expiry handling.
- Local Controller admin CLI for scoped client-token generation and revocation.

## [0.2.0] - 2026-08-26

### Added
- Standard FastMCP stdio adapter translating MCP tool calls to the RelayMe Controller HTTP API.
- Safe resource discovery capability (`list_host_resources`) for registered services, repositories, and allowed file roots.

## [0.1.0] - 2026-08-25

### Added
- Core RelayMe Controller and Host Agent architecture with outbound TLS long-polling.
- Read-only R0 capabilities: `list_hosts`, `host_status`, `process_list`, `read_logs`, `read_file`, and `git_diff`.
- SQLite-backed persistence and capability-scoped client authentication.
