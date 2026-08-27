# RelayMe v0.1

RelayMe is a small, vendor-neutral, read-only remote operations bridge. A client creates a task with the Controller; a Host Agent obtains it through outbound long-polling, enforces its local policy, and returns a bounded structured result.

It implements only read-only R0 observation: `list_hosts`, `list_host_resources`, `host_status`, `process_list`, `read_logs`, `read_file`, and `git_diff`.

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
```

`RELAYME_CA_CERT` is optional when the Controller certificate chains to the client machine's normal trust store. The CLI never falls back to SSH or shell execution.

## MCP adapter

The optional MCP adapter translates standard MCP tool calls to the existing RelayMe Controller HTTP/JSON API. RelayMe Core stays vendor-neutral: the adapter has no host policy, host database, SSH access, or shell fallback. It exposes only read-only R0 tools. MCP servers publish standard unprefixed names; clients that prefix tools with the configured server name display them as `relayme_list_hosts`, `relayme_list_host_resources`, and so on.

- `list_hosts`
- `list_host_resources`
- `host_status`
- `process_list`
- `read_logs`
- `read_file`
- `git_diff`

Call `list_hosts` when the host identity is unknown, then `list_host_resources` before service-, repository-, or path-scoped tools. Host-scoped operations accept a canonical host ID or a unique hostname. Discovery returns only resources already registered by the Host Agent; it never lists arbitrary files or system services.

Install RelayMe normally, set the Controller URL and scoped R0 token in the MCP client's local environment (a safe template is [`examples/mcp.env.example`](examples/mcp.env.example)), then launch the standard stdio server:

```sh
python -m pip install .
export RELAYME_URL=https://controller.example.invalid:8765
export RELAYME_TOKEN=CHANGE_ME_SCOPED_R0_TOKEN
# Optional for a private Controller CA:
export RELAYME_CA_CERT=/secure/local/path/controller-ca.pem

relayme-mcp
```

An MCP-capable client launches `relayme-mcp` as a stdio server and supplies the environment locally. The Controller can be reached over localhost, a LAN, Tailscale, a secure MCP tunnel, or ordinary HTTPS; transport is deployment configuration, not a RelayMe dependency. The adapter makes no network listener of its own and does not make any capability writable.

Run the regression suite with `python -m unittest discover -s tests -v`.
