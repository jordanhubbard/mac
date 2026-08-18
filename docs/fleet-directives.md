# Fleet directives

Fleet directives are MAC's durable channel for standing rules that apply to
every worker. Use them when a rule must survive restarts, be checked against
other global rules, and become part of the policy snapshot attached to new
work. Do not use `mac agent tell`: that is a one-time message to one agent, not
a fleet policy.

The feature is gated by `MAC_DIRECTIVES_ENABLED=1`. Disabled hubs expose an
empty, explicitly disabled snapshot and perform no dispatch gating. This makes
it possible to ship the implementation before enabling a production cohort.

## The contract

Each proposal is a `mac.directive.v1` YAML or JSON document. The hub creates an
immutable version and SHA-256 digest. The only scope in version 1 is `fleet`;
projects and repositories may supply variable bindings or receive audited
waivers, but they cannot override a global policy value.

```yaml
schema: mac.directive.v1
name: build.bazel-first
description: Convert Make repositories to Bazel through a reviewed workflow.
scope: fleet
when:
  eq:
    - fact: repository.metadata.build_system
    - literal: make
variables:
  primary_target:
    type: string
    binding: build.primary_target
    required: true
set:
  build.bazel.required: true
macro:
  workflow: build-system.make-to-bazel
  version: 1
  inputs:
    repository_id:
      fact: repository.id
    primary_target:
      template: "${primary_target}"
  effects:
    exclusive:
      - template: "repository:${repository.id}:build-system"
```

The policy above does not execute a conversion itself. When it matches, MAC
loads the enabled, exact-version `build-system.make-to-bazel` workflow and
files the resulting mutation and verification tasks. Those tasks remain held.
An operator reviews the generated graph and releases it through the normal
task lifecycle.

## Conditions and facts

The condition language has only these operators:

- `all`, `any`, and `not` combine conditions.
- `eq`, `ne`, `in`, `contains`, `starts_with`, and `ends_with` compare two
  explicitly marked operands.
- `exists` checks one fact operand.

Operands are `{fact: path}` or `{literal: value}`. Supported fact roots are
`fleet`, `project`, `repository`, and `agent`. Repository facts come from the
hub's registered `project_repositories` row and its metadata; a repository
cannot provide an executable policy file. There is no `eval`, shell, Python,
CEL, regex engine, network lookup, or dynamically named condition operator.

Evaluation is bounded to 16 levels and 256 nodes. A missing fact does not
silently invent a value. It makes the relevant comparison false; a required
variable that remains unresolved after a condition matches blocks the check.

## Bindings and substitution

Variable bindings resolve in this order:

1. repository;
2. project;
3. fleet;
4. the variable's declared default.

Use JSON values so type validation is unambiguous:

```console
mac admin directive binding set repository repo_c26 build.primary_target \
  --value '"//kernel:all"' --actor operator
mac admin directive binding set fleet fleet build.primary_target \
  --value '"//:all"' --actor operator
mac admin directive binding list --target-type repository --target-id repo_c26
```

Only `{fact: ...}`, `{var: ...}`, and `{template: ...}` values inside macro
inputs or effects are substituted. Ordinary strings, policy keys, fact paths,
workflow names, versions, and scope are never interpolated. Every `${name}` in
a marked template must resolve. Credential-like keys and values, authenticated
URLs, bearer tokens, and private keys are rejected before persistence.

## Lifecycle

The safe path is propose, check, approve, then activate:

```console
mac admin directive propose --document-file bazel-first.yaml --actor operator
mac admin directive check build.bazel-first --version 1 --actor operator
mac admin directive approve build.bazel-first --version 1 \
  --digest SHA256 --check-id directive_check_ID --actor operator
mac admin directive activate build.bazel-first --version 1 \
  --digest SHA256 --actor operator
mac admin directive impact build.bazel-first
mac admin directive effective --repository-id repo_c26
```

`check` evaluates every enabled registered repository, resolves bindings,
verifies the named workflow version, and compares policy keys and macro effect
sets with active or distributing directives. Different values for the same
policy key are safe only when the hub can prove the conditions disjoint.
Unknown overlap blocks instead of guessing. Identical values deduplicate.
Writes, exclusivity, and external effects use the same conflict rules as work
packages.

Approval binds the directive version, directive digest, context digest, policy
digest, and passing check ID. Activation reruns the check. Any repository,
binding, waiver, active policy, or macro-impact change invalidates approval and
requires a new check and approval.

Activation creates a monotonic epoch in `distributing`. The hub sends each live
worker a small AgentBus notice, but workers fetch the durable effective state
from the HTTP API and acknowledge the exact digest. Until the full cohort has
acknowledged it, no member of that cohort is eligible for a new claim. Workers
registered after activation must acknowledge the active epoch before their
first claim. Existing running tasks retain their original snapshot and are not
interrupted. Deactivation stops the directive from entering newly created
work; it does not rewrite tasks already running.

## Waivers

Waivers are exceptions, not overrides. They bind one immutable directive
version to one registered project or repository, require a reason, may expire,
and can be revoked. A waiver for version 1 never applies to version 2.

```console
mac admin directive waiver create build.bazel-first --version 1 \
  --target-type repository --target-id repo_archived \
  --reason "Frozen pending archival" --expires-at 2026-08-01T00:00:00Z
mac admin directive waiver list --directive build.bazel-first
mac admin directive waiver revoke waiver_ID --reason "Repository is active again"
```

The reserved `system.executor-safety` directive cannot be edited, waived, or
deactivated. It represents the executor's existing hard constraints: no host
package installation, tests required, CodeGraph required for code changes,
independent review, no secret exposure, and hub-owned canonical publication.

## HTTP API

Operators use the global-fleet-authorized endpoints under `/directives`,
`/directive-bindings`, and `/directive-waivers`. Workers use only their
self-bound paths:

- `GET /agents/{agent_id}/directives/effective`
- `POST /agents/{agent_id}/directive-activations/{activation_id}/ack`

The effective response is `mac.directive.snapshot.v1` and includes the merged
policy set, applied immutable versions, snapshot digest, epoch, and pending
activations. Task creation pins that snapshot in `metadata.directive_snapshot`
when the feature is enabled. Secrets and credential values are never part of a
directive, snapshot, acknowledgement, notification, or operational memory.
