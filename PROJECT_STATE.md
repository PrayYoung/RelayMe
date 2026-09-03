# Project State

## Status

v0.4 registered executor-task bridge is locally validated with a synthetic, non-provider patch-and-test profile. Existing v0.3 behavior remains validated; deployment credentials, network profiles, and real task policies remain local and untracked.

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
- Validated the generic MCP adapter with standalone Antigravity CLI using a local RelayMe-only policy. It discovered all seven R0 tools, completed the MCP smoke test (including an Agent-side outside-root rejection), and passed the blind synthetic-incident diagnosis against the private ground truth. The fixture was restored healthy. No RelayMe code changed.
- Implemented the single bounded R1 `run_registered_task` capability with locally registered fixed argv, cwd, clean environment, non-root execution, timeout/process-group cleanup, bounded output, cooldown, one-task concurrency, exact task scopes, Controller-generated execution IDs, short-lived Agent duplicate-result retention, and durable execution audit metadata.
- Extended resource discovery, CLI, and generic MCP with safe registered-task metadata and `run_registered_task`; no arbitrary arguments, shell, SSH, mutation, or model-specific behavior was added.
- Validated the v0.3 package in a clean temporary environment with the MCP SDK installed: all 38 regression tests passed and the MCP server constructed successfully.
- Completed real-host R1 acceptance using exactly one isolated `demo-health-check` task. Resource discovery, scoped CLI execution, generic MCP stdio execution, task-level authorization rejection, idempotency replay, audit metadata, non-root Agent identity, and all existing R0 capabilities passed. The disposable acceptance credential was revoked and removed after validation.
- Corrected R1 delivery safety without changing the capability model: SQLite-backed atomic idempotency records, retained bounded replay results with explicit expiry state, Agent pending-result redelivery after restart, harmless duplicate Controller acknowledgements, and a Controller-compatible per-stream output ceiling.
- Validated delivery safety with 47 automated tests, including concurrent same-key creation, replay after result read, duplicate result delivery, Agent restart before confirmation without task rerun, Controller restart ambiguity, result expiry, maximum output, and oversized-result audit handling. The isolated real host also confirmed retained replay result, a single execution/audit record, Agent restart continuity, and R0 continuity; the disposable credential was revoked.
- Added the v0.4 bounded `start_registered_executor_task` capability for the single fixed `patch-and-test` task specification. Clients can select only a registered executor profile, the fixed task specification, a bounded untrusted brief, and an optional idempotency key.
- Added Agent-local executor profiles with fixed repository/base revision, disposable worktree/output roots, rootless Podman invocation, `--network none`, read-only sandbox defaults, resource limits, bounded transcripts/artifacts, and opaque local credential/network profile references. No provider hostname, credential value, or provider-specific logic is present in RelayMe Core.
- Added owner-scoped executor task state/result/review queries to the Controller, CLI, and generic MCP adapter. Discovery exposes only safe profile metadata.
- Validated the synthetic `research-patch-test` acceptance profile: a fixed fake Podman launcher changed only a disposable Git worktree, produced bounded review artifacts, preserved the source repository, and confirmed replay does not rerun the execution. The full base suite passed (52 tests; its optional MCP-SDK test is skipped in the base interpreter); a clean MCP-enabled environment passed all MCP adapter tests, package install, wheel build, CLI entry points, and 12-tool MCP server construction.
- Added a narrow v0.4 Agent-local `user_namespace: keep-id` executor policy. It is the only supported non-default user-namespace value, emits one fixed Podman argument, is unavailable to clients/briefs, and defaults to prior behavior when omitted. Local regression passed with 54 tests.
- Repeated the non-provider real-host executor acceptance with the local `keep-id` profile. The task reached RUNNING but retained the same worktree write denial as before. RelayMe-only result/replay/review checks confirmed no second execution and no source-repository change. No workaround, provider access, or post-boundary SSH repair was performed. The deployment-local rootless UID/bind-mount mapping remains unresolved.
- Completed a deployment-only rootless Podman mapping investigation using fresh no-network disposable probes. `keep-id` is active, but the fixed image UID/GID differs from the non-root Agent identity: Agent-owned mounts become visible as the Agent identity while the image process retains its fixed identity. This explains the protected output-mount denial; all forensic containers, worktrees, and outputs were removed. No RelayMe policy or source changed.
- Added the sole Agent-local executor execution-identity mode, `execution_identity: "agent"`, valid only with `user_namespace: "keep-id"`. It derives the running non-root Agent UID/GID and emits one fixed Podman `--user` argument; client/brief numeric identities and arbitrary Podman options remain impossible.
- Repeated the isolated real-host non-provider executor acceptance: protected worktree and output writes passed, the fixed patch-and-test completed with all review artifacts, rootless no-network/forbidden-path checks passed, the source repository remained unchanged, cleanup completed, and the exact idempotency replay returned the retained reviewable result without a second execution. No provider credential or egress was used.

## Current work

- v0.4 executor-task bridge is locally and real-host accepted for the isolated non-provider profile, including fixed Agent-local identity alignment with rootless `keep-id`.

## Next action

- Conduct a separately authorized provider-enabled executor acceptance only after reviewing the deployment-local provider credential and egress policy.
