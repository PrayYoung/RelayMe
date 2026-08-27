# Project State

## Status

v0.2 release candidate metadata is prepared; the synthetic demo is reset healthy. The generic MCP adapter and the agent-facing discovery/identifier improvements are validated.

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
- Completed a blind diagnosis of the isolated synthetic incident using six external R0 capability calls only; the evidence identifies a configuration regression that references a missing input file.
- Confirmed the blind diagnosis used no SSH or other non-RelayMe remote access path, and did not inspect private ground truth.
- Evaluated the blind diagnosis against private ground truth: PASS. It correctly identified the configuration path regression, missing input, and expected log/config/diff evidence chain.
- Reset only the isolated synthetic demo using its documented procedure; its one-shot unit completed successfully with the healthy configuration restored.
- Pruned stale unreachable Git objects left by earlier history rewriting and confirmed the remaining object database is clean of private deployment patterns.
- Added public-safe release hygiene: Apache-2.0 licensing, packaging metadata, a generic Agent configuration example, corrected CLI documentation, and broader local-artifact exclusions.
- Added a separate generic MCP stdio adapter that translates exactly the six read-only R0 tools to the existing Controller HTTP/JSON API.
- Validated the MCP adapter's mappings, error preservation, configuration checks, package entry point, and a local MCP-client-to-Controller `list_hosts` round trip without SSH or a managed host.
- Added a local-only Controller administration path for scoped client-token creation and revocation, with hashed-token audit metadata and no remote client-token creation endpoint.
- Created and externally authenticated a dedicated `opencode` client scoped to exactly the six read-only R0 capabilities; its local configuration and public Controller CA remain outside Git.
- Restarted the deployment Controller through its protected local workflow; verified the scoped client still authenticates, the legacy remote client-token route returns `404`, and the listener remains private-interface-only.
- Configured OpenCode to launch the generic MCP adapter from protected local client configuration; validated all six R0 tools against the enrolled demo host and confirmed an outside-root `read_file` rejection through MCP, with no non-RelayMe tool call recorded.
- Added Controller support for a protected local bootstrap-token file, avoiding bootstrap-secret command-line exposure in service managers.
- Installed local-only systemd units for the Controller and Host Agent with network/Tailscale ordering and bounded restart-on-failure behavior; no unit or deployment configuration is tracked in Git.
- Validated a controlled host reboot: Tailscale returned, the Controller resumed its private listener, the Agent reconnected using its existing credential, and an external scoped `list_hosts` call reported the host online without manual RelayMe startup.
- Validated the isolated synthetic demo fixture after host recovery: its static one-shot unit runs successfully with healthy configuration, reliably fails with the documented broken configuration, and is observable through RelayMe logs, file reads, and Git diffs. Its private reset mechanics no longer rely on an inapplicable `reset-failed` step.
- Completed one restricted OpenCode blind-diagnosis run using only the RelayMe MCP namespace. It did not identify the incident root cause because the six R0 tools do not provide registered-resource discovery and the client used non-registered labels; evaluation was FAIL. No bypass tool was attempted, and the fixture was restored healthy.
- Added the read-only `list_host_resources` capability, exposing only already-registered services, repositories, and allowed file roots for a host.
- Added prompt hostname resolution for host-scoped operations while retaining canonical host IDs; unknown and ambiguous identifiers now return structured errors instead of waiting for task expiry.
- Standardized generic MCP server tool names so client namespace prefixes yield clean `relayme_*` names.
- Revalidated the isolated blind diagnosis with the same restricted OpenCode model: it discovered registered resources through RelayMe, identified the configuration reference to the absent input file, and used no bypass access. Evaluation against the private ground truth: PASS. The fixture was restored healthy.
- Rewrote repository commit identity metadata to the public-safe project identity, recreated the historical v0.1.0 tag at its rewritten commit, and pruned pre-rewrite objects.

## Current work

- v0.2 release-candidate implementation is validated. Runtime secrets, host configuration, private-network details, and fixture ground truth remain outside Git.

## Next action

- Perform the final v0.2.0 privacy verification and create the release tag.
