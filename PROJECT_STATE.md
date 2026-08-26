# Project State

## Status

v0.1 external client interface implemented; private-network external validation complete.

## Completed

- Reviewed the supplied v0.1 implementation brief.
- Confirmed the workspace was empty; selected a minimal Python standard-library and SQLite design.
- Validated `list_hosts`, `host_status`, `process_list`, `read_logs`, `read_file`, and `git_diff` through RelayMe on a real host.
- Verified Agent-side `read_file` rejects an outside-root path.
- Corrected `host_status` to report elapsed uptime rather than boot time.
- Added bounded exponential-backoff recovery for temporary Controller/network failures without re-enrollment.
- Validated automatic Agent recovery after a Controller restart, followed by a successful `git_diff` task.
- Replaced tracked deployment identifiers with generic examples and rewrote Git history for public release.
- Created one isolated synthetic config/path-mismatch incident with a dedicated demo repository and service outside production namespaces.
- Registered only the demo allowed root, repository, and service with the Host Agent; validated all six R0 capabilities through RelayMe.
- Stored the incident ground truth and validation responses privately outside Git and outside the benchmark-visible allowed root.
- Added environment-configured external CLI commands for the existing six R0 capabilities, including optional HTTPS CA-certificate validation.
- Updated the Agent to support an optional Controller CA certificate while retaining its existing credential and outbound long-poll architecture.
- Bound the deployment Controller TLS listener only to its private Tailnet interface; it is not listening on a public interface.
- Validated all six R0 CLI capabilities from an external client directly through the private network, with no SSH used for those capability calls.
- Verified an external out-of-scope file request remains rejected by the Host Agent.

## Current work

- Private-network external access is validated. Runtime secrets, host configuration, Tailnet details, and fixture ground truth remain outside Git.

## Next action

- Run the existing blind synthetic-incident diagnosis using only the external RelayMe CLI over the private network.
