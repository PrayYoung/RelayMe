# RelayMe v0.3

RelayMe is a small, vendor-neutral remote operations bridge. A client creates a task with the Controller; a Host Agent obtains it through outbound long-polling, enforces its local policy, and returns a bounded structured result.

It implements seven read-only R0 capabilities: `list_hosts`, `list_host_resources`, `host_status`, `process_list`, `read_logs`, `read_file`, and `git_diff`. v0.3 adds one bounded R1 capability: `run_registered_task`.

R1 is not shell access. The client selects only a host, a locally registered task ID, and an optional opaque idempotency key. The Agent fixes the executable, argv, working directory, minimal environment, timeout, output limits, and execution identity; tasks never use a shell and must not run as root.

For an idempotency key, RelayMe atomically reuses the original execution. Its bounded result remains replayable for a short retention period; after expiry RelayMe returns stable execution metadata with a `result_expired` state and never runs the task again.

## Local demo

Create an agent config from [`examples/agent.example.json`](examples/agent.example.json), then start both processes. For remote deployments, supply `--tls-cert` and `--tls-key` to the Controller and use its `https://` URL in the agent configuration:

```sh
python -m relayme.controller --db relayme.db --bootstrap-token CHANGE_ME_ADMIN_TOKEN
python -m relayme.agent --config agent.json
```

Install the project (`python -m pip install .`) to use the `relayme` CLI. The bootstrap token authenticates administrative enrollment. Create or revoke scoped client tokens only on the Controller host by opening its protected local database; this does not call the Controller HTTP API:

```sh
relayme admin --db /protected/path/relayme.db create-client \
  --name local-cli --capability list_hosts --capability host_status \
  --output-file /protected/path/new-client-token
relayme admin --db /protected/path/relayme.db revoke-client --name local-cli
```

Grant an R1 task only with its exact scope; there is no all-task scope:

```sh
relayme admin --db /protected/path/relayme.db create-client \
  --name bounded-runner --capability list_host_resources \
  --capability run_registered_task:example-health-check \
  --output-file /protected/path/bounded-runner-token
```

The create command emits the new token once unless `--output-file` is used. Only its hash and audit metadata are retained in the Controller database. Use the resulting scoped token to create tasks or list hosts:

```sh
relayme --url http://127.0.0.1:8765 --token CLIENT_TOKEN list-hosts
relayme --url http://127.0.0.1:8765 --token CLIENT_TOKEN task HOST_ID host_status '{}'
```

## External R0 client

The same CLI is the external client. Keep connection details local to the client machine:

```sh
export RELAYME_URL=https://controller.example.invalid:8765
export RELAYME_TOKEN=CHANGE_ME_SCOPED_R0_TOKEN
export RELAYME_CA_CERT=/secure/local/path/controller-ca.pem

relayme hosts
relayme list-host-resources HOST_ID
relayme status HOST_ID
relayme processes HOST_ID
relayme logs HOST_ID example.service --last-n 100 --max-bytes 65536
relayme read-file HOST_ID /srv/example-app/config.json
relayme git-diff HOST_ID example-app
relayme run-task HOST_ID example-health-check --idempotency-key one-safe-run
```

`RELAYME_CA_CERT` is optional when the Controller certificate chains to the client machine's normal trust store. The CLI never falls back to SSH or shell execution.

## MCP adapter

The optional MCP adapter translates standard MCP tool calls to the existing RelayMe Controller HTTP/JSON API. RelayMe Core stays vendor-neutral: the adapter has no host policy, host database, SSH access, or shell fallback. MCP servers publish standard unprefixed names; clients that prefix tools with the configured server name display them as `relayme_list_hosts`, `relayme_list_host_resources`, and so on.

- `list_hosts`
- `list_host_resources`
- `host_status`
- `process_list`
- `read_logs`
- `read_file`
- `git_diff`
- `run_registered_task`

Call `list_hosts` when the host identity is unknown, then `list_host_resources` before service-, repository-, path-, or task-scoped tools. Host-scoped operations accept a canonical host ID or a unique hostname. Discovery returns only resources already registered by the Host Agent; it never lists arbitrary files or system services. Registered-task discovery exposes only safe IDs, descriptions, and timeout bounds—not executable paths, argv, environments, or working directories.

Install RelayMe normally, set the Controller URL and scoped R0 token in the MCP client's local environment (a safe template is [`examples/mcp.env.example`](examples/mcp.env.example)), then launch the standard stdio server:

```sh
python -m pip install .
export RELAYME_URL=https://controller.example.invalid:8765
export RELAYME_TOKEN=CHANGE_ME_SCOPED_R0_TOKEN
# Optional for a private Controller CA:
export RELAYME_CA_CERT=/secure/local/path/controller-ca.pem

relayme-mcp
```

An MCP-capable client launches `relayme-mcp` as a stdio server and supplies the environment locally. The Controller can be reached over localhost, a LAN, Tailscale, a secure MCP tunnel, or ordinary HTTPS; transport is deployment configuration, not a RelayMe dependency. The adapter makes no network listener of its own. Its sole execution tool is the bounded `run_registered_task` R1 operation; it does not expose arbitrary commands, mutations, SSH, or shell access.

Run the regression suite with `python -m unittest discover -s tests -v`.
