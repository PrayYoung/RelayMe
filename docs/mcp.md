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

## 3. Composite Executor Round Actions (v0.7)

In Web MCP client workflows (such as Gemini Spark, Claude Web, or custom LLM interfaces), executing an executor task using primitive tools typically required **~3 separate user "Allow" confirmation dialogs** per round:
1. `start_registered_executor_task` (launching the task)
2. `get_task` / `task_result` (polling status)
3. `get_task_artifact` (retrieving the patch, test log, and report)

RelayMe v0.7 introduces two coarse-grained composite tools to condense this interaction loop into **1 single user confirmation** for bounded rounds (and 2 for long-running rounds):

### `run_executor_round`
- **Parameters**: `host` (str), `executor_profile_id` (str), `task_spec_id` (str), `brief` (str), `idempotency_key` (optional str), `wait_seconds` (optional int, default 35, max 120).
- **Behavior**: Starts the registered executor task and waits server-side for bounded completion.
  - If execution completes within `wait_seconds`, it packages and returns a **complete round review bundle**:
    - `task_id`: Durable execution identifier.
    - `state`: `SUCCEEDED` or `FAILED`.
    - `executor_report`: Executor-native markdown report (`executor_report.md` or `analysis.md`).
    - `patch`: Git patch diff applied to the worktree (`patch.diff`).
    - `test_log`: Raw test execution log (`test.log`).
    - `summary`, `changed_files`, `tests_run`, `duration_ms`: Structured execution metrics.
    - `artifacts`: Retained artifact manifest.
  - If execution exceeds `wait_seconds`, it returns cleanly with `state: "RUNNING"` without timing out or raising 504.

### `collect_executor_round`
- **Parameters**: `task_id` (str).
- **Behavior**: Inspects an existing executor task. If still running, returns `state: "RUNNING"`. If terminal, returns the exact same structured review bundle as `run_executor_round`.

### Invariants:
- **No client arbitrary execution**: Remote clients cannot select executables, arguments, working directories, or credentials.
- **Strict owner scoping**: Only the client token that initiated the task can collect its round review bundle.
- **Disposable worktrees**: Execution runs in an isolated worktree; the source repository remains 100% clean and untouched.

---

## 3. Remote Server Configuration Reference

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

## 4. Authentication Architecture

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

## 5. Security & Isolation Invariants

1. **Zero Credential Exposure in Tool Arguments**:
   MCP tools never accept authentication tokens or secret credentials as arguments. Tokens exist strictly in the HTTP transport layer.
2. **Non-Loopback TLS Guard**:
   Attempting to bind to a non-loopback interface (e.g. `0.0.0.0` or a public IP) without TLS certificates fails at startup unless `--allow-http-insecure` is explicitly enabled for environments behind a trusted reverse proxy.
3. **DNS Rebinding & Origin Protection**:
   `relayme-mcp-remote` enforces DNS rebinding protection via host validation and CORS origin controls (`TransportSecuritySettings`), preventing malicious browser scripts from interacting with local adapters.
4. **Health Check Isolation**:
   The `/health` and `/ping` endpoints are unauthenticated and return sanitized status (`{"status": "ok"}`) for load balancer and orchestrator health probes without exposing state or configuration.

---

## 6. Production Deployment Patterns

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

## 7. Web LLM Client Compatibility Investigation

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

## 6. OAuth 2.1 Edge — Web MCP Client Authentication (v0.6)

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
  └── /mcp        ← inline MCP (identical 13-tool schema)
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

