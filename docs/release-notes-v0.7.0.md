# RelayMe v0.7.0 Release Notes

**RelayMe now lets a Web LLM hand a bounded coding task to a real coding agent, wait for completion, and review the agent's native report, patch, and test evidence.**

RelayMe v0.7.0 transitions RelayMe from synthetic task fixtures to a production-quality, Web-led coding workflow. In this release, a remote Web research lead (such as Gemini Spark or Claude Web) can dispatch a high-level task brief over standards-compliant OAuth 2.1 remote MCP, have a real non-interactive coding agent run its own internal edit/test/debug loop in an isolated disposable worktree, and review the resulting native report, patch diff, and test evidence without host filesystem access or manual copy/pasting.

---

## What's New in v0.7.0

### 1. Real Coding Executor Integration
- Integrates real non-interactive coding agents (such as Codex CLI) capable of autonomous code inspection, editing, and test execution.
- Includes a standalone, vendor-neutral execution launcher (`adapters/executors/launcher.py`) that executes inside disposable worktrees and translates task briefs into verified coding rounds.

### 2. Coarse-Grained Web Composite Actions (`run_executor_round` & `collect_executor_round`)
Web-based LLM clients frequently impose approval dialog friction per tool invocation. RelayMe v0.7.0 provides two composite actions:
- `run_executor_round`: Dispatches a registered executor task, waits server-side for bounded completion (default 35s, configurable up to 120s), and returns a complete round review bundle. If the task exceeds the wait window, it returns cleanly with `state: "RUNNING"` without raising an HTTP 504 error.
- `collect_executor_round`: Inspects an active task and retrieves the complete review bundle once terminal.
- **Workflow Efficiency**: Condenses the workflow from ~3 approval dialogs per round down to **1 single approval** for short rounds (and **2 approvals** if the task exceeds the initial sync window).

### 3. First-Class Native Report Retention (`executor_report.md`)
- Whitelisted `executor_report.md` as a core retained artifact across Controller and Host Agent policies.
- RelayMe avoids using another LLM to summarize executor work. Instead, the Web lead receives the coding agent's own native markdown report alongside the raw unified patch and test runner logs, allowing the Web lead to verify claims directly against the evidence.

### 4. Sandbox Isolation & Repository Immutability
- All code modifications take place inside ephemeral Git worktrees detached from the base revision.
- Source repositories remain 100% untouched and clean.
- Disposable worktrees are automatically cleaned up and deleted upon task completion.

### 5. Durable Task State & Atomic Idempotency
- Tasks, execution metadata, and audit records persist in SQLite.
- Replaying `run_executor_round` with an existing `idempotency_key` returns the retained review bundle without re-triggering agent execution.

### 6. Standards-Compliant OAuth 2.1 Web Ingress
- Operates seamlessly over RelayMe's OAuth 2.1 edge (`relayme-mcp-oauth`) supporting RFC 9728 Protected Resource Metadata, RFC 8414 Authorization Server Metadata, Dynamic Client Registration (RFC 7591), and PKCE S256.
- Full parity across 15 MCP tools on stdio, remote HTTP/SSE, and OAuth 2.1 transports.

### 7. Real Web Acceptance Verified (Gemini Spark)
- Validated end-to-end against a real coding fixture: Gemini Spark submitted a high-level debugging brief, Codex CLI diagnosed an off-by-one formula bug in `math_utils.py`, fixed the code, verified all unit tests with pytest, and returned the evidence.
- Gemini reviewed the findings, quoted the patch, confirmed test results, and verified repository safety—requiring exactly 2 tool calls and 2 human Allow clicks total.

---

## What v0.7.0 Does NOT Provide (Scope Boundaries)

To preserve architectural integrity and security invariants, RelayMe v0.7.0 explicitly does not provide:
- **No autonomous multi-round research loops**: RelayMe is an execution and evidence bridge, not an autonomous research orchestrator. The Web lead remains in control of multi-round iteration.
- **No arbitrary shell execution**: Remote clients cannot execute unconstrained shell commands or specify arbitrary binaries.
- **No upstream commit/push/deploy (R2)**: Changes remain local worktree patches; no commits are created and no remote pushes are performed.
- **No GPU scheduling**: Workloads are bounded local CPU/container tasks.
- **No unrestricted host or network access**: Network access inside sandboxes remains disabled (`--network none`), and machine authority stays with the local Host Agent.

---

## Upgrade & Compatibility

- **Backward Compatible**: All 13 primitive MCP tools (`list_hosts`, `list_host_resources`, `host_status`, `process_list`, `read_logs`, `read_file`, `git_diff`, `run_registered_task`, `start_registered_executor_task`, `get_task`, `task_result`, `list_reviewable_tasks`, `get_task_artifact`) behave identically to prior releases. Total tool count is now 15.
- **Full Test Coverage**: 132/132 unit and composite tests pass cleanly (`pytest tests/`).
