<p align="center">
  <img src="assets/relayme-mark.svg" width="96" alt="RelayMe" />
</p>

<h1 align="center">RelayMe</h1>

<p align="center">
  <strong>Give AI agents hands on remote machines.</strong><br/>
  Safe, persistent, bounded execution for ChatGPT and other agent clients.
</p>

<p align="center">
  <code>AI Agent</code> &rarr; <code>RelayMe</code> &rarr; <code>Remote Machine</code> &rarr; <code>Retained Artifacts</code> &rarr; <code>Review</code>
</p>

---

### What is RelayMe?
RelayMe is a vendor-neutral execution bridge that gives ChatGPT, Claude, Codex, and other AI agents the ability to safely interact with remote systems. It acts as the secure "hands" for web-based and conversational agents.

### Why do I need it?
Browser and conversational agent sessions are ephemeral and isolated from your infrastructure. When an agent needs to diagnose an incident, inspect service health, or run a benchmark, it has no persistent, safe way to operate on remote servers.

### Why not just give the agent SSH or shell access?
Handing an LLM an open SSH key or an unconstrained `run_shell` tool invites accidental deletion, unauthorized changes, and security risks. RelayMe replaces arbitrary shell execution with **registered bounded tasks**, **rootless sandboxing**, **disposable Git worktrees**, and **retained review artifacts**.

---

## What Can I Do with RelayMe?

- **Inspect remote systems safely**: Query status, processes, service logs, and git diffs without opening inbound firewall ports or granting SSH access.
- **Run bounded research & test tasks**: An AI research lead can inspect an experiment, launch a pre-registered benchmark or test run, disconnect, and retrieve logs later to decide the next step.
- **Sandboxed code execution**: Run containerized `patch-and-test` tasks in disposable Git worktrees where source repositories stay immutable.
- **Durable multi-session workflows**: Return hours later or hand off work between models—tasks persist in SQLite with atomic idempotency and replayable results.
- **Vendor-neutral client access**: Works with ChatGPT, Claude, Antigravity, or custom agents via standard HTTP, CLI, and Model Context Protocol (MCP).

---

## Example: Bounded Task & Review

An external agent or engineer dispatches a bounded task, disconnects, and inspects the retained artifacts later:

```sh
# 1. Start a bounded task in an isolated disposable worktree
relayme start-executor-task host-1 research-patch-test patch-and-test \
  "Fix off-by-one error in pagination parser" --idempotency-key fix-101

# 2. Check execution status and retrieve the generated diff and test log
relayme task-result <TASK_ID>
relayme task-artifact <TASK_ID> patch.diff
relayme task-artifact <TASK_ID> test.log
```

The agent reviews the retrieved artifacts directly—without host filesystem access or repository mutations.

---

## Why Not Just MCP?

MCP tells an agent *how to call a tool*. RelayMe provides the **execution, isolation, state persistence, and artifact storage** *behind* those tools. RelayMe exposes a 13-tool FastMCP adapter on top of its durable execution engine.

---

## Safety by Default

- **Host Agent is final authority**: Local policies cannot be overridden by clients or Controller tokens.
- **Rootless container sandboxing**: Tasks run in rootless Podman with `--network none` and read-only roots.
- **Disposable Git worktrees**: Host source trees are never modified directly.
- **Outbound-only connections**: Target machines initiate outbound TLS polling; no inbound ports are opened.
- **Bounded review artifacts**: Artifact retrieval has strict byte limits and directory traversal guards.

---

## Current Scope (v0.4.1)

- **Supported**: Read-only observation (R0), bounded registered tasks (R1), containerized `patch-and-test` executor, durable SQLite state, idempotency replay, and 13-tool FastMCP adapter.
- **Non-goals**: No arbitrary remote shell, no automatic upstream Git commit/push (R2), no GPU scheduler, no autonomous research workflow engine.

---

## Documentation

- [Architecture & Detailed Setup](docs/architecture.md) — Controller, Agent, and CLI setup guide.
- [Security Model & Disclosure](SECURITY.md) — Defense-in-depth architecture and vulnerability reporting.
- [Changelog](CHANGELOG.md) — Version history and release notes.

## License

[Apache 2.0](LICENSE)
