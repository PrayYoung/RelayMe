# RelayMe Model Context Protocol (MCP) Guide

RelayMe provides native support for the Model Context Protocol (MCP), enabling AI agents—both local coding assistants and web-based LLMs—to safely observe remote hosts and trigger bounded, pre-registered execution.

---

## 1. Adapters: Stdio vs. Remote

RelayMe provides two MCP adapter entrypoints, both acting as thin protocol translators over the RelayMe Controller HTTP API:

| Feature | `relayme-mcp` (Stdio) | `relayme-mcp-remote` (Remote) |
|---|---|---|
| **Primary Use Case** | Local developer tooling (Claude Desktop, Cursor, Antigravity, CLI pipelines) | Web-based LLM clients (Claude Web, Gemini, ChatGPT custom connectors) |
| **Transport** | Standard input / standard output (`stdio`) | Streamable HTTP (`/mcp`) and Server-Sent Events (`/sse`, `/messages/`) |
| **Hosting Model** | Spawned as a child process by the local AI client | Long-running ASGI service (Uvicorn / Starlette) |
| **Authentication** | Process-level environment variable (`RELAYME_TOKEN`) | HTTP `Authorization: Bearer <token>` header (per-request or dedicated) |
| **Multi-Tenancy** | Single client identity per process | Dynamic multi-tenant token pass-through preserving caller scopes |
| **Network Exposure** | None (local pipe) | Configurable network interface (TLS enforced for non-loopback) |

Neither adapter interacts directly with the Host Agent or owns execution state. All authorization, policy enforcement, worktree sandboxing, and auditing are handled exclusively by RelayMe Core.

---

## 2. Shared Tool Surface (15 Tools)

Both the stdio and remote adapters expose the exact same 15 tools with identical schemas, parameter validations, and descriptions:

### Observation & Discovery (R0)
1. `list_hosts`: List enrolled RelayMe hosts visible to the calling token.
2. `list_host_resources`: List registered services, repositories, and allowed file roots for a host.
3. `host_status`: Read bounded system load, memory, disk, and uptime.
4. `process_list`: List running host processes (read-only snapshot).
5. `read_logs`: Read bounded log lines for a registered service.
6. `read_file`: Read file contents strictly within agent-registered allowed roots.
7. `git_diff`: Read working tree `git diff` for a registered repository.

### Bounded Execution (R1)
8. `run_registered_task`: Run a locally registered, idempotent fixed task without arguments.
9. `start_registered_executor_task`: Start a containerized `patch-and-test` task in a disposable worktree (`--network none`, rootless Podman).

### Composite Executor Round Actions (v0.7 Web-Friendly Flow)
10. `run_executor_round`: Start a registered executor task, wait server-side for bounded completion, and return a structured round review bundle (status, native report, patch diff, test log, result summary). If execution exceeds wait duration, returns state `RUNNING` without error.
11. `collect_executor_round`: Inspect a running executor task and retrieve the round review bundle upon completion, or return `RUNNING` if still executing.

### Task State & Evidence Retrieval
12. `get_task`: Inspect owner-scoped task status, execution state, and bounded metadata.
13. `task_result`: Fetch the retained result summary for a completed task.
14. `list_reviewable_tasks`: List terminal executor tasks retaining reviewable artifacts.
15. `get_task_artifact`: Retrieve bounded text artifacts (`patch.diff`, `result.json`, `test.log`, `analysis.md`, `executor_report.md`).

---

---

## 3. Preferred Web-Agent Interface: Coarse-Grained Rounds (v0.7)

In Web MCP client environments (such as Gemini Spark, Claude Web, or custom LLM interfaces), executing an executor task using primitive tools typically required **~3 separate user "Allow" confirmation dialogs** per round:
1. `start_registered_executor_task` (launching the task)
2. `get_task` / `task_result` (polling status)
3. `get_task_artifact` (retrieving the patch, test log, and report)

RelayMe v0.7 introduces two coarse-grained composite tools as the **canonical interface for Web LLMs**, minimizing approval friction while preserving strict sandbox isolation and durable state tracking.

### `run_executor_round`
- **Parameters**:
  - `host` (str): Enrolled host identifier or hostname.
  - `executor_profile_id` (str): Registered executor profile name.
  - `task_spec_id` (str): Registered task specification (e.g. `patch-and-test`).
  - `brief` (str): Free-form, high-level task instructions.
  - `idempotency_key` (optional str): Caller-supplied deduplication key.
  - `wait_seconds` (optional int): Synchronous wait duration (default 35s, maximum 120s).
- **Behavior**: Starts the registered executor task and waits server-side for completion up to `wait_seconds`:
  - **Terminal Completion**: If the task finishes within the wait window, it compiles and returns a complete **round review bundle**:
    - `task_id`: Durable execution identifier.
    - `state`: `SUCCEEDED` or `FAILED`.
    - `executor_report`: Full text of `executor_report.md` (or fallback `analysis.md`).
    - `patch`: Unified git diff applied to the worktree (`patch.diff`).
    - `test_log`: Test execution log from the runner (`test.log`).
    - `summary`, `changed_files`, `tests_run`, `duration_ms`, `exit_code`: Structured execution telemetry.
    - `artifacts`: Retained artifact manifest.
  - **In-Flight Return**: If the task exceeds `wait_seconds`, it cleanly returns `{"task_id": "...", "state": "RUNNING", "message": "..."}` without raising an HTTP 504 Gateway Timeout error.

### `collect_executor_round`
- **Parameters**: `task_id` (str).
- **Behavior**: Inspects an active task:
  - If still running, returns `{"task_id": task_id, "state": "RUNNING"}`.
  - If terminal, compiles and returns the exact same structured review bundle as `run_executor_round`.

### Execution Patterns

1. **Short Tasks (Completing within wait window)**:
   ```text
   Web Lead → run_executor_round → Final Review Bundle
   ```
   Requires only **1 single MCP call**.

2. **Longer Tasks (Exceeding wait window)**:
   ```text
   Web Lead → run_executor_round → state: "RUNNING"
   Web Lead → collect_executor_round → Final Review Bundle
   ```
   Requires only **2 MCP calls total**.

### Web Client Approval Behavior

Approval dialogs belong strictly to the Web client application (e.g. Google AI Studio, Claude Web) and may change independently of RelayMe. RelayMe does not weaken permissions or bypass confirmations to reduce prompts. In real end-to-end acceptance with Gemini Spark:
- **v0.6 primitive flow**: approximately 3 separate human "Allow" clicks per round.
- **v0.7 composite flow**: **2 human "Allow" clicks** because the real task exceeded the initial 35-second synchronous window and was cleanly retrieved via `collect_executor_round`.
- A task completing within the initial wait window requires only **1 single Allow click**.

---

## 4. Executor-Native Report (`executor_report.md`)

RelayMe whitelists `executor_report.md` as a core retained artifact alongside `patch.diff`, `test.log`, and `result.json`.

### Why It Exists
RelayMe adheres to a strict architectural boundary: **RelayMe contains no LLM reasoning loop**. It does not invoke a secondary model to interpret or summarize the coding agent's actions.

Instead, the Web lead receives:
1. The **coding agent's own native final report** (`executor_report.md`), detailing its root-cause analysis and modifications.
2. The **actual unified git diff** (`patch.diff`), demonstrating exactly what source lines were altered.
3. The **actual test execution log** (`test.log`), proving whether test assertions passed or failed.
4. Structured execution metadata (`result.json`), capturing exit code, duration, and files modified.

This design enables the Web research lead to independently evaluate the coding agent's claims against real, un-hallucinated evidence.

---

## 5. Privacy & Disclosure Boundary

When using RelayMe with cloud-based Web LLMs (Gemini Spark, Claude Web, ChatGPT), the MCP transport acts as a deliberate disclosure boundary:

### What Is Disclosed
Any artifact returned through MCP is transmitted to the connected model provider. For composite rounds, this includes:
- Task brief and metadata (`task_id`, `state`, `duration_ms`)
- Executor markdown report (`executor_report.md`)
- Bounded patch diff (`patch.diff`, capped at configured byte limit)
- Bounded test log (`test.log`, capped at configured byte limit)

### What Is Strictly Guarded
RelayMe guarantees that the following are **never returned or leaked** to MCP clients:
- Entire source repositories or un-modified files
- Files outside registered allowed roots
- Environment variables or system configuration
- RelayMe Controller bearer tokens
- OAuth server secrets and client credentials
- Host SSH keys or cloud provider credentials
- Coding executor model API keys

> [!WARNING]
> **Deployment Ingress Notice**: Temporary Cloudflare quick tunnels (e.g. `*.trycloudflare.com`) used during acceptance testing provide ephemeral ingress without identity verification. For production deployments, use an authenticated Cloudflare Named Tunnel, Tailscale Funnel with access controls, or a trusted reverse proxy terminating your own TLS domain.

---

## 6. Sanitized Real Acceptance Record (v0.7.0)

End-to-end acceptance was validated using a real non-interactive coding executor driven from Gemini Spark Web over OAuth 2.1:

| Metric | Result | Notes |
|---|---|---|
| **Acceptance Client** | Gemini Spark Web | Connected over OAuth 2.1 / PKCE |
| **Tool Surface** | 15 MCP Tools | Full parity across stdio, remote, and OAuth |
| **Executor Engine** | Codex CLI | Real autonomous edit/test/debug loop |
| **Acceptance Fixture** | `real-coding-repo` | Python fixture with intentional formula bug in `math_utils.py` |
| **Initial State** | `pytest` FAILS | `test_triangular_small` fails (`assert 0 == 1`) |
| **Diagnosis** | Root cause identified | Codex diagnosed `n * (n - 1) // 2` formula error |
| **Code Modification** | Minimal patch | Corrected formula to `n * (n + 1) // 2` |
| **Verification** | `pytest` PASSED | 3/3 tests passed in 0.01s; clean exit code 0 |
| **Repository Safety** | Source 100% clean | Source repository unmodified (`git status` clean) |
| **Workspace Cleanup** | 100% removed | Ephemeral worktree deleted immediately upon completion |
| **Native Report** | `executor_report.md` | Retained and returned in review bundle |
| **MCP Calls** | 2 calls total | `run_executor_round` (returned RUNNING at 35s) + `collect_executor_round` |
| **Human Approvals** | 2 Allow clicks | Exactly 2 human UI confirmations in Gemini Spark Web |
| **Primitive Calls** | 0 calls needed | Zero calls to `get_task`, `task_result`, or `get_task_artifact` |
| **Credential Hygiene** | 0 secrets leaked | No bearer tokens, keys, or private paths exposed |

---

## 7. Remote Server Configuration Reference

### Command-Line Arguments (`relayme-mcp-remote`)

| Argument | Environment Variable | Default | Description |
|---|---|---|---|
| `--controller-url` | `RELAYME_URL` | *(required)* | Base URL of the upstream RelayMe Controller |
| `--host` | `RELAYME_MCP_HOST` | `127.0.0.1` | Network interface to bind |
| `--port` | `RELAYME_MCP_PORT` | `8000` | Port to listen on |
| `--token` | `RELAYME_TOKEN` | `None` | Default client token for unauthenticated clients |
| `--transport` | `RELAYME_MCP_TRANSPORT` | `streamable-http` | `streamable-http`, `sse`, or `both` |
| `--ca-cert` | `RELAYME_CA_CERT` | `None` | CA certificate for Controller HTTPS verification |
| `--wait-seconds` | `RELAYME_WAIT_SECONDS` | `35` | Task polling timeout in seconds |
| `--tls-cert` | `RELAYME_MCP_TLS_CERT` | `None` | Path to TLS certificate for HTTPS |
| `--tls-key` | `RELAYME_MCP_TLS_KEY` | `None` | Path to TLS private key for HTTPS |
| `--allow-http-insecure` | `RELAYME_MCP_ALLOW_HTTP_INSECURE` | `false` | Allow non-loopback HTTP without TLS (for reverse proxies) |
| `--allowed-origin` | `RELAYME_MCP_ALLOWED_ORIGINS` | `*` | Allowed CORS / DNS-rebinding origin (can repeat) |
| `--allowed-host` | `RELAYME_MCP_ALLOWED_HOSTS` | loopbacks | Allowed Host header for DNS-rebinding protection |

---

## 8. Authentication Architecture

The remote MCP adapter supports two authentication operational modes:

### Mode A: Dynamic Bearer Token Pass-Through (Recommended)
In multi-tenant or shared deployments, each incoming HTTP request must include the standard header:
```http
Authorization: Bearer <relayme_client_token>
```
1. The remote adapter's ASGI authentication middleware extracts this Bearer token.
2. The token is dynamically bound to the async request context via `current_token`.
3. Calls to the RelayMe Controller use this token, ensuring Controller-side client scoping, capability permissions, and owner isolation are fully preserved.
4. If no header is provided and no default token is configured, the server immediately returns HTTP `401 Unauthorized` with a `WWW-Authenticate: Bearer` challenge.

### Mode B: Dedicated Deployment Token
When launching a dedicated adapter for an individual agent or environment:
```sh
relayme-mcp-remote --controller-url https://controller.example:8765 --token $(cat agent-token.txt)
```
- Requests omitting the `Authorization` header fall back to the configured `--token`.
- Requests explicitly providing an `Authorization: Bearer <token>` header take precedence and override the server default for that request.

---

## 9. Security & Isolation Invariants

1. **Zero Credential Exposure in Tool Arguments**:
   MCP tools never accept authentication tokens or secret credentials as arguments. Tokens exist strictly in the HTTP transport layer.
2. **Non-Loopback TLS Guard**:
   Attempting to bind to a non-loopback interface (e.g. `0.0.0.0` or a public IP) without TLS certificates fails at startup unless `--allow-http-insecure` is explicitly enabled for environments behind a trusted reverse proxy.
3. **DNS Rebinding & Origin Protection**:
   `relayme-mcp-remote` enforces DNS rebinding protection via host validation and CORS origin controls (`TransportSecuritySettings`), preventing malicious browser scripts from interacting with local adapters.
4. **Health Check Isolation**:
   The `/health` and `/ping` endpoints are unauthenticated and return sanitized status (`{"status": "ok"}`) for load balancer and orchestrator health probes without exposing state or configuration.

---

## 10. Production Deployment Patterns

### Pattern A: Reverse Proxy with Caddy (Automatic TLS)
Run `relayme-mcp-remote` on localhost, letting Caddy terminate HTTPS and manage certificates:
```caddy
mcp.example.com {
    reverse_proxy 127.0.0.1:8000
}
```
Run the adapter:
```sh
relayme-mcp-remote \
  --controller-url https://controller.internal:8765 \
  --host 127.0.0.1 \
  --port 8000 \
  --transport both
```

### Pattern B: Cloudflare Tunnel (Zero Public Ports)
Expose the remote MCP server securely without public IP or inbound firewall rules:
```sh
cloudflared tunnel run relayme-mcp
```

### Pattern C: Standalone TLS
Direct HTTPS termination without an external reverse proxy:
```sh
relayme-mcp-remote \
  --controller-url https://controller.internal:8765 \
  --host 0.0.0.0 \
  --port 8443 \
  --tls-cert /etc/ssl/certs/relayme-mcp.crt \
  --tls-key /etc/ssl/private/relayme-mcp.key
```

---

## 11. Web LLM Client Compatibility Investigation

### Claude Web Custom MCP (Anthropic)
- **Transport Compatibility**:
  - Supports both **Streamable HTTP** (`/mcp`) and **SSE** (`/sse` + `/messages/`).
  - RelayMe's `--transport both` option provides maximum compatibility across both web and desktop versions.
- **Authentication**:
  - Connects using standard HTTP headers (`Authorization: Bearer <token>`).
  - RelayMe's dynamic Bearer pass-through middleware seamlessly integrates with Claude's connection settings.
- **Confirmation & Safety UX**:
  - Claude classifies MCP tools into read-only observation vs mutating operations.
  - RelayMe's 7 R0 tools (`list_hosts`, `list_host_resources`, `host_status`, `process_list`, `read_logs`, `read_file`, `git_diff`) are marked as read-only operations that can be approved smoothly.
  - RelayMe's R1 tools (`start_registered_executor_task`, `run_registered_task`) prompt for user confirmation before executing, aligning with bounded execution safety.

### Gemini Web / Google Spark / Antigravity
- **Transport Compatibility**:
  - Antigravity and the Google agent ecosystem natively support stdio and remote MCP over HTTP/SSE.
  - Gemini function calling schemas match RelayMe's strict JSON Schema tool definitions.
- **Header Propagation**:
  - Remote connector configurations support custom headers for Bearer authentication.

### ChatGPT Custom MCP / Actions (OpenAI)
- **Current Architecture**:
  - Custom GPTs on ChatGPT Web currently connect to external APIs via **OpenAPI 3.0 Actions**.
  - OpenAI Desktop supports MCP via local stdio.
  - The OpenAI developer platform is actively standardizing remote connectors to align with MCP.
- **Integration Options**:
  - For current ChatGPT Web: RelayMe Controller's existing HTTP REST endpoints (`/v1/hosts`, `/v1/tasks`, `/v1/reviewable-tasks`) can be directly imported via an OpenAPI schema.
  - For upcoming ChatGPT Web remote MCP support: `relayme-mcp-remote`'s `/mcp` Streamable HTTP endpoint complies with the 2025 MCP specification.

---

## 12. OAuth 2.1 Edge — Web MCP Client Authentication (v0.6)

Web-based MCP clients such as **Gemini Spark** and **Claude Web** require standards-compliant OAuth 2.1 authentication. They cannot use raw Bearer tokens in connector configuration. `relayme-mcp-oauth` provides a self-contained OAuth 2.1 authorization server edge that sits in front of the MCP endpoint, mapping OAuth-authenticated identities to the existing RelayMe bearer token model.

### Architecture

```
Web Client (Claude Web / Gemini Spark)
  ↓ OAuth 2.1 / PKCE Authorization Code Flow
relayme-mcp-oauth   (http://localhost:8001 or public HTTPS via tunnel)
  ├── /.well-known/oauth-protected-resource  ← RFC 9728 PRM (required by both clients)
  ├── /.well-known/oauth-authorization-server ← RFC 8414 ASM
  ├── /register   ← RFC 7591 DCR (legacy fallback; both clients still use it in practice)
  ├── /authorize  ← PKCE S256 authorization code endpoint
  ├── /token      ← opaque access token issuance + refresh rotation
  ├── /revoke     ← RFC 7009
  └── /mcp        ← inline MCP (identical 15-tool schema)
        ↓ RelayMe bearer (resolved per-request from allowlist; never exposed to web client)
   existing RelayMe Controller
```

**Security properties**:
- RelayMe bearer tokens are **never returned** to web clients — only short-lived opaque access tokens
- All tokens stored as SHA-256 hashes; cleartext never persisted to SQLite
- PKCE S256 mandatory; `plain` method rejected
- Explicit subject allowlist: `sub → relayme_token` (no wildcards, no auto-grant)

### Quick Start

**Step 1: Register a subject in the allowlist**

```bash
relayme-mcp-oauth add-client \
  --sub user@example.com \
  --token eUhC3WGp8azsCPufRf3loJfH9s9ApaVSfypUHjTBkY4 \
  --label "Owner"
```

**Step 2: Start the OAuth edge**

```bash
# With Cloudflare Tunnel (recommended — establishes public HTTPS)
cloudflared tunnel --url http://127.0.0.1:8001 &
# Set the tunnel URL as the public base URL
export RELAYME_OAUTH_BASE_URL=https://your-tunnel.trycloudflare.com
export RELAYME_URL=https://100.x.y.z:18765
export RELAYME_CA_CERT=~/.config/relayme/controller-ca.pem

relayme-mcp-oauth serve \
  --base-url "$RELAYME_OAUTH_BASE_URL" \
  --controller-url "$RELAYME_URL" \
  --ca-cert "$RELAYME_CA_CERT" \
  --allow-http-insecure      # only for loopback; tunnel handles TLS termination
```

**Step 3: Add connector in Gemini Spark or Claude Web**

Enter `https://your-tunnel.trycloudflare.com/mcp` as the MCP server URL.

The client will:
1. Hit `/mcp` → receive `401` with `WWW-Authenticate: Bearer resource_metadata=".../oauth-protected-resource"`
2. Discover authorization server via PRM → ASM documents
3. Register via `/register` (DCR)
4. Run PKCE Authorization Code flow through `/authorize`
5. Exchange code for opaque access token at `/token`
6. Subsequent `/mcp` calls authenticated with the opaque token

### Client Redirect URIs

| Client | Redirect URI |
|---|---|
| Claude Web | `https://claude.ai/api/mcp/auth_callback` |
| Gemini Spark | Shown in Gemini connector setup UI (varies) |

These redirect URIs are sent by the client during DCR and automatically registered.

### Management CLI Reference

| Command | Description |
|---|---|
| `relayme-mcp-oauth serve` | Start the OAuth edge server |
| `relayme-mcp-oauth add-client --sub <sub> --token <relay_bearer>` | Add subject to allowlist |
| `relayme-mcp-oauth list-clients` | Show all allowlist entries |
| `relayme-mcp-oauth revoke-client --sub <sub>` | Remove subject from allowlist |
| `relayme-mcp-oauth list-tokens` | Show active (non-expired) access tokens |

### Scope Support

| Scope | Description |
|---|---|
| `relayme.read` | R0 observation and discovery tools |
| `relayme.execute` | R1 bounded registered task execution |
| `ACCESS_VIEW_MANAGE_MCP_CONTENT` | Required by Gemini Spark; automatically accepted |
| `offline_access` | Required by Gemini Spark for refresh token issuance |

### Development Mode

For local testing without a browser, use `--dev-auto-approve-sub`:

```bash
relayme-mcp-oauth serve \
  --base-url http://localhost:8001 \
  --controller-url ... \
  --dev-auto-approve-sub test-user@example.com \
  --allow-http-insecure
```

> **Never use `--dev-auto-approve-sub` in production.** It bypasses the approval form and auto-grants any `/authorize` request with the specified subject.

