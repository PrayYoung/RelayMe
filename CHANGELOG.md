# Changelog

All notable changes to RelayMe are documented in this file.

## [0.7.0] - 2026-09-06

### Added
- Real non-interactive coding executor integration with vendor-neutral runner (`adapters/executors/launcher.py`) supporting Codex CLI within disposable worktrees.
- Coarse-grained composite round actions: `run_executor_round` (start + server-side bounded wait + bundle assembly) and `collect_executor_round` (terminal bundle retrieval).
- Whitelisted `executor_report.md` as a core retained artifact across Controller and Host Agent policies.
- 15-tool FastMCP surface exposed consistently across stdio, remote HTTP/SSE, and OAuth 2.1 transports.
- Verified real Web-client acceptance with Gemini Spark Web (2 composite MCP calls, 2 human-observed Allow clicks, zero primitive MCP calls needed, 100% clean repository preservation).

## [0.6.0] - 2026-09-06

### Added
- Standards-compliant OAuth 2.1 authorization server edge (`relayme-mcp-oauth`) for remote MCP interoperability with web-based LLMs (Gemini Spark, Claude Web).
- RFC 9728 Protected Resource Metadata and RFC 8414 Authorization Server Metadata discovery.
- Dynamic Client Registration (RFC 7591), PKCE S256 code challenge verification, and refresh token rotation.
- OAuth subject to scoped RelayMe bearer identity mapping with full capability enforcement.

## [0.5.0] - 2026-09-06

### Added
- Generic remote MCP server transport (`relayme-mcp-remote`) over Streamable HTTP (`/mcp`) and SSE (`/sse` + `/messages/`).
- Dynamic multi-tenant Bearer token pass-through middleware (`RemoteAuthMiddleware`) preserving Controller-side scoping and owner isolation.
- Unauthenticated `/health` and `/ping` endpoints.
- Tool schema and description parity with stdio MCP.

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
