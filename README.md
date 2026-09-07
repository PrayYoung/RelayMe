<p align="center">
  <img src="assets/relayme-mark.svg" width="96" alt="RelayMe" />
</p>

<h1 align="center">RelayMe</h1>

<p align="center">
  <strong>Give AI agents hands on remote machines.</strong><br/>
  Safe, persistent, bounded execution for ChatGPT and other agent clients.
</p>

<p align="center">
  <code>Web LLM</code> &rarr; <code>RelayMe</code> &rarr; <code>Bounded Coding Executor</code> &rarr; <code>Retained Evidence</code> &rarr; <code>Web LLM Review</code>
</p>

---

### What is RelayMe?
RelayMe is a vendor-neutral execution bridge that gives ChatGPT, Claude, Gemini, Codex, and other AI agents the ability to safely interact with remote systems. It acts as the secure "hands" for web-based and conversational agents.

### Why do I need it?
Browser and conversational agent sessions are ephemeral and isolated from your infrastructure. When an agent needs to diagnose an incident, fix a bug, or run a benchmark, it has no persistent, safe way to operate on remote servers.

### Why not just give the agent SSH or shell access?
Handing an LLM an open SSH key or an unconstrained `run_shell` tool invites accidental deletion, unauthorized changes, and security risks. RelayMe replaces arbitrary shell execution with **registered bounded tasks**, **rootless sandboxing**, **disposable Git worktrees**, and **retained review artifacts**.

---

## What Can I Do with RelayMe?

- **Web-led coding workflows**: A Web LLM (e.g. Gemini Spark or Claude Web) hands a bounded coding objective to a real non-interactive coding agent, which performs its own local edit/test/debug loop in a disposable worktree and returns its native report and evidence.
- **Inspect remote systems safely**: Query status, processes, service logs, and git diffs without opening inbound firewall ports or granting SSH access.
- **Run bounded research & test tasks**: An AI research lead can inspect an experiment, launch a pre-registered benchmark or test run, disconnect, and retrieve logs later to decide the next step.
- **Durable multi-session workflows**: Return hours later or hand off work between models—tasks persist in SQLite with atomic idempotency and replayable results.
- **Standards-compliant remote access**: Works with Claude Web, Gemini Spark, and custom agents via OAuth 2.1 / PKCE and Model Context Protocol (MCP).

---

## Example: Web-Led Coding Round

A Web LLM calls RelayMe's coarse-grained composite actions to execute and review an executor task:

```python
# 1. Start bounded executor round (server waits up to wait_seconds for completion)
run_executor_round(
    host="host-1",
    executor_profile_id="research-coding",
    task_spec_id="patch-and-test",
    brief="Diagnose why test_boundary fails in math_utils.py, fix it, and verify with tests.",
)
# Returns immediately if finished, or cleanly returns RUNNING if task exceeds initial window:
# {"task_id": "task-101", "state": "RUNNING", "message": "..."}

# 2. Retrieve completed review bundle once finished
collect_executor_round(task_id="task-101")
# Returns:
# {
#   "task_id": "task-101",
#   "state": "SUCCEEDED",
#   "executor_report": "Fixed test_boundary: corrected index calculation in math_utils.py...",
#   "patch": "diff --git a/math_utils.py ...",
#   "test_log": "=== 3 passed in 0.01s ===",
#   "changed_files": ["math_utils.py"],
#   "artifacts": [...]
# }
```

The Web lead reviews the executor's native report, patch diff, and test evidence directly—without host filesystem access, manual copy/pasting, or source repository mutations.

---

## Why Not Just MCP?

MCP tells an agent *how to call a tool*. RelayMe provides the **execution, isolation, state persistence, and artifact storage** *behind* those tools. RelayMe exposes a 15-tool FastMCP adapter on top of its durable execution engine across stdio, remote HTTP/SSE, and OAuth 2.1 transports.

---

## Safety by Default

- **Host Agent is final authority**: Local policies cannot be overridden by clients or Controller tokens.
- **Rootless container sandboxing**: Tasks run in rootless containers / launchers with `--network none` and read-only roots.
- **Disposable Git worktrees**: Host source trees are never modified directly.
- **Outbound-only connections**: Target machines initiate outbound TLS polling; no inbound ports are opened.
- **Bounded review artifacts**: Artifact retrieval has strict byte limits and directory traversal guards.

---

## Current Scope (v0.7.0)

- **Supported**: Real non-interactive coding executors, coarse-grained composite round actions (`run_executor_round`, `collect_executor_round`), 15-tool FastMCP adapter (stdio, remote HTTP/SSE, OAuth 2.1), disposable Git worktree sandboxing, native report retention (`executor_report.md`), SQLite durable state, and idempotency replay.
- **Non-goals**: No arbitrary remote shell, no automatic upstream Git commit/push (R2), no GPU scheduler, no autonomous research workflow engine.

---

## Documentation

- [Architecture & Detailed Setup](docs/architecture.md) — Controller, Agent, and CLI setup guide.
- [Security Model & Disclosure](SECURITY.md) — Defense-in-depth architecture and vulnerability reporting.
- [Changelog](CHANGELOG.md) — Version history and release notes.

## License

[Apache 2.0](LICENSE)
