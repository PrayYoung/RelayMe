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

## 2026-09-06 — Launcher compatibility correction and one provider task

The original failure was reproduced safely: a dynamically linked ARM64 Go
binary built on the Ubuntu host requests the glibc loader, while the minimal
Alpine executor image exposes only its musl loader. A fixed launcher was then
built as a static ARM64 binary, verified to have no dynamic dependencies, and
placed in a temporary image derived from the same executor runtime. Its
offline self-test passed under the exact rootless `keep-id` and mapped Agent
identity, with a read-only root filesystem, network none, and isolated
worktree/output mounts.

The one newly authorized provider task did not reach the corrected launcher.
Its deployment-local profile inadvertently retained a base revision copied
from the unrelated synthetic repository, so Agent rejected the task with
`invalid_base_revision` before creating a worktree, container, proxy
attachment, credential mount, or provider request. Replay returned the same
retained execution without a second attempt; the isolated source Git diff was
empty and the task was not reviewable because no executor result existed.

The corrected launcher is therefore offline-compatible, but provider
acceptance remains untested. All temporary image, profile, policy, credential
copy, scoped client, source/worktree/output fixture, and proxy attachment were
removed. A future attempt needs separate authorization and only a
profile-local base-revision binding correction; it must not broaden RelayMe or
the exact-host policy.

Critic review: the offline result genuinely resolves the ABI question, but it
does not support any conclusion about Gemini availability, authentication,
free-tier use, CONNECT behavior, provider-only egress, or provider task
semantics. The profile base-revision error is an independent deployment
confounder. The conclusion is therefore limited to static-launcher runtime
compatibility and must not be interpreted as provider acceptance.

## 2026-09-06 — Fresh fixture-bound provider attempt

A new isolated repository was committed and its immutable HEAD was verified
in that exact repository before the temporary profile was atomically bound to
the same repository ID and commit. The registered binding was independently
checked before the single RelayMe task. The static launcher then started in
the rootless sandbox and the proxy audit recorded one CONNECT to the sole
exactly allowed provider destination, followed by attachment release.

The provider returned HTTP 404. The task retained a reviewable failed result;
replay returned the original execution without rerun and the source Git diff
was empty. No other endpoint was requested. All temporary deployment material
was removed. This rules out the earlier base-revision and dynamic-loader
issues for this attempt, but does not establish usable provider connectivity;
the remaining issue is the fixed provider request/model path.

Critic review: the experiment cleanly tests fixture binding, launcher startup,
and exact-host routing. It does not identify whether the 404 is a model name,
API version, endpoint path, or account/API availability issue; determining
that requires a separately authorized, non-broadening request-path review.

Critic review: the direct mapping probe and the real task test different
layers. The probe establishes that the fixed derived UID/GID can write fresh
`0700` worktree and output mounts under the exact keep-id mapping. The actual
task independently establishes non-root execution, network-none and
forbidden-path checks, artifact retention, review retrieval, source
preservation, and idempotent replay. The accepted conclusion is limited to
this fixed local profile; it does not establish provider-enabled execution.
