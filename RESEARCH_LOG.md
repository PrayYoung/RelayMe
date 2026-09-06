# Research Log

## 2026-09-02 — Rootless Podman bind-mount mapping investigation

Question: why did the v0.4 local executor profile still fail to write its
disposable worktree after enabling its fixed `keep-id` namespace policy?

Result: the rootless Agent runs under a different numeric UID/GID than the
image's fixed non-root `USER`. `keep-id` correctly makes Agent-owned mounts
visible as the Agent identity, but it does not change the image process to
that identity. The fixed image user cannot write protected Agent-owned output
mounts. The current disposable worktree and its existing synthetic test file
are group-writable through the supplementary Agent group, so the prior
worktree-specific denial was not reproduced by the current profile/base. The
no-userns matrix fails as expected.

The investigation used only fresh disposable worktrees and output directories,
rootless Podman with `--network none` and a read-only root filesystem, and no
provider credentials or egress. All temporary remote containers, worktrees,
and output directories were removed.

Next: if separately authorized, evaluate one fixed Agent-local execution
identity aligned with `keep-id`; it must not be client-controlled and must not
broaden the Podman option surface.

## 2026-09-02 — Fixed Agent-local execution identity acceptance

The Agent now accepts only the literal profile mode `execution_identity:
"agent"`, and only with `user_namespace: "keep-id"`. At task launch it derives
the running Agent's non-root effective UID/GID and appends one fixed Podman
`--user` argument. Clients, briefs, and profiles cannot provide a numeric
identity or arbitrary Podman option.

The isolated non-provider acceptance passed: both protected bind mounts were
writable, the fixed patch-and-test task completed, all four bounded review
artifacts were retained, the source repository remained unchanged, the
disposable worktree and container were removed, and replay returned the
original result without another execution. No provider credential or egress
was used.

## 2026-09-06 — Single Gemini provider-profile acceptance

Question: can the existing fixed rootless executor use one opaque Gemini
credential through the already validated exact-host CONNECT attachment without
weakening its network or filesystem isolation?

Result: blocked before provider connectivity. The one permitted RelayMe task
started its rootless container but the fixed provider launcher exited in 962 ms
with a bounded executable-not-found error. The launcher had been built as a
dynamically linked host binary, while the minimal executor image lacks its
dynamic loader. This is a deployment-local launcher/image ABI mismatch, not a
RelayMe Core, credential, provider, or egress-policy failure. No provider
CONNECT occurred; the proxy audit contains only attachment prepare/release
events for the task.

The task result was retained, reviewable, and replayed with the original
execution ID and no source change. The temporary profile, scoped client,
runtime credential copy, exact-host proxy policy/attachment, launcher/image,
and disposable source/worktree were removed. No retry or policy expansion was
performed.

Next: only if separately authorized, build the same fixed launcher statically
or against the executor image runtime, then repeat one exact-host attempt with
the same sandbox and credential-isolation constraints.

Critic review: the direct mapping probe and the real task test different
layers. The probe establishes that the fixed derived UID/GID can write fresh
`0700` worktree and output mounts under the exact keep-id mapping. The actual
task independently establishes non-root execution, network-none and
forbidden-path checks, artifact retention, review retrieval, source
preservation, and idempotent replay. The accepted conclusion is limited to
this fixed local profile; it does not establish provider-enabled execution.
