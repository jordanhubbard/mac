# Fleet recovery: evidence and remaining acceptance

Status snapshot: **2026-09-14, 10:52 UTC**. This is a dated investigation,
not a live dashboard. Use `mac task show <id>` for current task evidence and
`mac agent list --json` for current worker state. The hub task ledger is the
execution record; this page explains how to interpret it.

## What is known now

Rocky, Natasha, and Bullwinkle reported **healthy, idle, and not dispatch-held**
at the snapshot. The user also confirmed all three conversing in Slack.
Those observations establish availability and conversation, not successful
coding, independent review, publication, or acceptance of the final runtime.
No holds were changed during this inspection.

The required recovery cohort is the three named workers. HGX work was withdrawn
in the shared ledger and is outside this rollout. A stale enabled HGX registry
entry must not silently expand the cohort: resolve targets from the current
`~/.mac/fleets.yaml` and select the three workers explicitly for deployment.

The intended steady state is Hermes with preserved personas, active memories,
credentials, and compatible tools, including tools absent from the recorded
inventory. MAC and Hermes use **Python 3.14.7 and uv 0.12.12**, with locked core
dependencies and matching CI coverage. Repository execution and verification
run in Linux OpenShell. The macOS host runs native control and service clients;
installing an OpenShell runtime there is not the recovery path.

The last direct interpreter inventory, around 08:00 UTC, still showed drift:

| Runtime | Rocky | Natasha | Bullwinkle |
| --- | --- | --- | --- |
| MAC Python | 3.14.7 | 3.12.3 | 3.14.4 |
| Hermes Python | 3.11.14 | 3.11.14 | 3.11.16 |

This inventory is a baseline, not deployment acceptance. The active Hermes
home was `~/.hermes`; some configuration still named the legacy OpenClaw path.
Do not delete a legacy directory merely because of its name: verify active
references, memory stores, schedules, tools, and restart persistence first.

## Source progress is separate from fleet readiness

[PR #802](https://github.com/jordanhubbard/mac/pull/802) and
[PR #833](https://github.com/jordanhubbard/mac/pull/833) are independently
reviewed and published source changes. Their publication receipts do not prove
that a worker runs those sources or a matching immutable image.

At the snapshot, [PR #834](https://github.com/jordanhubbard/mac/pull/834)
passed its complete Linux OpenShell contract gate and exact-commit independent
review, while GitHub's sanity job remained in progress. Its change requires a
real exact-commit Linux verification result before code push and keeps the
later independent review. Live deployment remains a separate task:
`task_5974916018ba8e91f37d9de7f446facd`.

The persistence repair belongs to
`task_e6710b8c65924f86b0d6851cd64eeb8d` and the existing
[PR #724](https://github.com/jordanhubbard/mac/pull/724). That PR originally
looked like documentation work but also changed worker security behavior. The
reconciled scope must describe both. On unchanged canonical source, six
redaction regressions failed while two durable-evidence controls passed in
Linux OpenShell; evidence `ev_e3e7a240246a48ab832020c03247f346` records the baseline.
A baseline failure or a prepared repair is not a passing acceptance receipt.

## Persistence boundary

A previous canary copied environment credentials into durable worker evidence.
Prompt instructions and `HERMES_REDACT_SECRETS=true` did not prevent it. The
worker must sanitize execution diagnostics and structured evidence **before
signing and durable upload**, including secondary JSON artifacts.

The repair covers known credential fields and recognizable text forms:
credential assignments, authorization headers, authenticated URL user-info,
provider token formats, and private-key blocks. It preserves return codes,
nonsecret provenance, usage counts, repository identity, and the worker's
signing authority. It does not promise that a pattern matcher can recognize
an arbitrary unlabeled secret or audit repository bundles and media contents.

Executor-authored manifests require bounded reads, regular-file checks,
no-follow opens, and identity checks. Unsafe or malformed input must produce
invalid evidence rather than a successful signed verdict. Atomic replacement
avoids truncating hard-linked files. Uploaded artifact digests describe the
actual uploaded bytes; source digests, when available, describe the original
file. Sanitized copies of secondary inputs must not alter task instructions.

Previously exposed credentials still require their separately tracked rotation
and rejection checks. A source-level redaction fix does not revoke a credential
or remove old durable records by itself.

## Historical incident and decisions to preserve

The [September 3 incident record](https://github.com/jordanhubbard/mac/blob/8bddc13aba05dd154310c830b399288593c50627/docs/problems.md)
contains the original observations and attempted recoveries. Its OpenClaw
recreation steps, degraded-worker table, runtime revision, and HGX expansion
recipe are historical, not current instructions.

The material lessons remain:

- An interrupted attestation cutover left a node signing with an uncommitted
  candidate while the hub retained the predecessor. Repeated restarts could
  not reconcile that authority split.
- A local journal described an epoch as open after the hub had aborted it.
  Reconcile against the authoritative receipt before beginning another epoch.
- Flat worker credentials and two fleet-name suffixes selected different
  credentials. Temporary compatibility credentials restored liveness but did
  not establish the intended per-worker bound identity.
- A graceful checkpoint restored a corrupt gateway SQLite database during a
  recreation attempt. Preserve evidence and understand ownership before
  replacing state; retain active memory during the Hermes migration.
- Historical gateway advertisements reported success after runtime state had
  disappeared. Fresh process and route probes are required.
- Rocky executed a canary whose signature failed review admission. Execution
  success must not be relabeled end-to-end completion.

Preserve normalized PostgreSQL authority, lease fences, transactional event
publication, signed evidence, independent review, the canonical merge queue,
read-only observability, and suppression of unwanted work generation. These
are useful boundaries. Repair their implementation and deployment rather than
bypassing them or replacing the architecture with a new framework.

## Remaining work and the proof required

| Work | Ledger task | Required evidence |
| --- | --- | --- |
| Runtime and image rollout | `task_5974916018ba8e91f37d9de7f446facd` | Published source, matching immutable image provenance and anonymous readback, actual running interpreters and locked dependencies on all three workers. |
| Supported verifier resources | `task_b2488df9e0a87cd741ecd3feacf2825e` | A fresh independent review using the supported bounded-tmpfs profile, original policy, and durable PostgreSQL evidence; retire temporary host overrides safely. |
| Attestation recovery | `task_99d9fccdb580449a81095b55a17442de` | Reconciled epoch journal, fenced identity probes, old-credential rejection, and the required interruption tests across cutover steps. |
| Worker readiness | `task_f2758902a67c4a11af432c169a1b3923` | Fresh supervisor, transport, provider route, signing identity, restart, and end-to-end canary evidence for each named worker. |
| Hermes state preservation | `task_039a06ac2683472193a2755a531d2d31` | Active personas, memories, schedules, compatible recorded and unrecorded tools, and persistence after restart; obsolete services retired. |
| Read-only reports | `task_bef7068af16860069f61e61801c8d4bd` | Native Rocky and Linux report canaries with trusted Linux verification and normal review/publication where the task contract requires it. |
| Backlog grooming | `task_348276615bc54552880b7b57d521663d` | Required report acceptance, then the specified Aviation and nanolang grooming settings, preserving active projects. |
| Hermes retry investigation | `task_5edcc3b58c0d8e53f5e5f828b0cc4144` | Controlled comparison of first-attempt failures across Python versions, separating existing upstream failures without hiding them with more retries. |
| Migration performance | `task_8e2fbc40ccae4b20ba34d570b7d88b2d` | Bounded lock-contention measurements; elapsed times from different hardware alone are insufficient. |
| Mac disk consumption | `task_b60b3df0f07d4b0ba71de748654193c8` | Attributed growth and ownership-aware cleanup, with measured free-space recovery. |

The disk investigation identified a live `literate-ai` wheel-smoke temporary
directory growing by about 39 MB in 57 seconds, backed by a Python process whose
working directory matched it. This is a confirmed contributor, not an
explanation of all historical loss. Evidence
`ev_faed25e2b5f44bde936aa7385f98a7aa` records the observation. Its process and
files were not stopped or deleted as part of MAC cleanup.

## Acceptance order

1. Publish the source repairs through their existing tasks and independent
   review, then obtain matching immutable worker and verifier images.
2. Reconcile attestation and deploy the supported runtime/configuration to
   Rocky, Natasha, and Bullwinkle explicitly, preserving their active state.
3. Prove actual versions, routes, identity, and restart persistence. A package
   installation receipt alone does not prove the running interpreter changed.
4. Run a targeted coding canary on each worker through normal execution,
   independent verification, and canonical publication. Record task, agent,
   source/image identity, evidence, review, publication, and completion time.
5. After those canaries pass, release any remaining real agent holds and the
   dependent backlog, as authorized by the user. Observe ordinary work through
   completion. An already-clear hold does not supply missing canary evidence.
6. Complete the report, state-preservation, credential, and operational
   follow-ups above. Report elapsed time, known versus unknown cost, and operator
   interventions without treating unknown measurements as zero.

The original product assessment also requires the supported request-to-result
workflow and recovery from worker loss, failed tests, needs-input, and
publication conflicts. Keep those obligations alongside the fleet work; a
successful deployment does not replace product acceptance.
