!!! warning "Historical field note"
    This design/spec note is retained for provenance only. It is not a current operating contract; the premises or implementation path it describes have been superseded or never shipped.

# Job-per-task Role Specialisation — Design Spec

> Status: draft **v2** 2026-05-28 — addresses codex review (see
> [`job-per-task-roles-spec-review.md`](job-per-task-roles-spec-review.md)).
> Supersedes the "Deployment-scaling" approach previously sketched in
> [`docs/archive/field-notes/k8s-native-rewrite-plan.md`](k8s-native-rewrite-plan.md) Phase 5.
> See also: `src/mac/k8s/runner.py` (the existing
> `build_job_spec` / `claim_and_launch_one` we're modifying).
>
> **v2 highlights** (from review):
> - §6.1/§6.3: explicit authorization matrix for Option B; lease renewal
>   stays under dispatcher identity so the Job never needs to renew as
>   the role agent.
> - §6.1: removed first-capability role fallback; added
>   `MAC_RUNNER_CAPABILITY_ROLE_ALIASES` env for the transitional
>   capability→role mapping Hermes-created tasks need.
> - §7: dispatcher identity (`mac-runner`) ownership rules clarified.
>   Seeder owns role agents only; the runner Deployment's existing
>   init container owns the dispatcher row + its union capabilities.
> - §8: retitled "Author/reviewer separation"; v2 makes explicit that
>   one reviewer agent satisfies the rule iff the rule needs N=1
>   distinct reviewer.
> - §12: PR phasing split — PR1 (inert) → PR2a (seeder live) → PR2b
>   (runner env live, first specialised task).
> - §11: ArgoCD claim toned down — no `replicas` fight, but Git
>   remains source-of-truth for the Deployment + ConfigMap.

## 1. Goal

Make the K8s runner produce **role-aware Jobs** so different kinds of
work (e.g. `python-coder` vs `python-reviewer`) run with the right
image, executor command, and agent identity — without introducing
long-lived per-role worker Deployments and without conflicting with
ArgoCD's reconciliation.

Concretely:

- A task in mac with `metadata.required_role = "python-coder"` causes
  the runner to create a `batch/v1 Job` whose pod uses the
  codex-runner image, sets `MAC_AGENT_ID = mac-worker-python-coder`,
  and executes the role's configured task command.
- Two-reviewer rule (a task's reviewer cannot be the agent that
  authored the work) is preserved by having distinct stable agent
  identities per role.
- Idle resource cost stays at zero: there are no per-role worker
  Deployments. Specialisation is a property of the spawned Job, not
  of a long-lived pool.

## 2. Non-goals

- Pool-of-workers scaling (`mac-worker-<role>` Deployments scaled by
  a controller from 0→N on demand). The previous Phase 5 plan moved
  in that direction; this spec replaces it.
- Demand-driven autoscaling that reacts to
  `agent_provisioning_requests`. With per-Job pods, throughput is
  bounded by the runner's poll cadence and K8s scheduler latency, not
  by a Deployment replica count. Provisioning requests retain a
  smaller role (see §11).
- Per-Job ephemeral agent identities. Mac-api returns an attestation
  key only on FIRST registration of an `agent_id` (api.py:2480); a
  new identity per Job would re-mint keys on every claim, breaking
  signed-evidence continuity. Spec uses stable per-role agents.
- Per-Job mac-api bearer tokens (least-privilege per claim). Future
  hardening — see open question §13. v1 keeps the existing admin
  `MAC_WORKER_TOKEN` shared across all roles.

## 3. The pain this addresses

The previous design (separate `mac-worker-<role>` Deployments scaled
0→N by `mac-k8s-controller`) had four real problems:

1. **ArgoCD vs controller fight over `spec.replicas`.** ArgoCD's
   `selfHeal: true` would revert the controller's scale-up on every
   sync, SIGTERMing running pods. Workable via
   `ignoreDifferences[].jsonPointers: [/spec/replicas]` per
   Application, but that's a per-app annotation gotcha for every new
   role.
2. **Cold-start latency.** A new burst of work requires controller
   reconcile (≤30s) + Deployment scale-up + pod start before any
   claim can fire. Per-Job pods skip this — runner sees the task on
   its 5s poll, creates the Job immediately.
3. **Scale-down logic doesn't exist.** The reference `K8sDeploymentScaler`
   in `controller.py` only bumps replicas UP. Going back to 0 needs
   a separate idle-detection reconciler that wasn't designed.
4. **Two pod-creation patterns is one too many.** Today mac already
   creates one `batch/v1 Job` per claim via `mac-k8s-runner`. Adding
   a "scale a worker Deployment" pattern alongside means two ways to
   spawn agent work, with overlapping semantics. Job-only is simpler.

## 4. Design overview

```
                ┌──────────────────────────────┐
                │  mac-api (Deployment, n)     │
                └───────────────┬──────────────┘
                                │ POST /agents/{id}/claim-next
                                ▼
                ┌──────────────────────────────┐
                │  mac-k8s-runner (Deployment) │   one per cluster.
                │  reads task.required_role +   │   2 replicas (already).
                │  task.required_capabilities, │
                │  picks image + agent_id,     │
                │  creates Job per claim       │
                └───────────────┬──────────────┘
                                │ BatchV1Api.create_namespaced_job
                                ▼
                ┌──────────────────────────────┐
                │  batch/v1 Job                │   one per task/lease.
                │  per-Job env:                │   restartPolicy: Never
                │    MAC_AGENT_ID              │   backoffLimit: 0
                │    MAC_AGENT_ROLE            │   ttlSecondsAfterFinished
                │    MAC_TASK_EXECUTOR_COMMAND │
                │  per-role image              │
                │  mac-task-runner exec        │
                └───────────────┬──────────────┘
                                │ POST /agents/{id}/claim-next, /tasks/{id}/start,
                                │      /tasks/{id}/evidence, /tasks/{id}/transition
                                ▼
                          (back to mac-api)
```

Specialisation is populated by the runner through these per-Job
fields when it calls `build_job_spec`:

| Where | What it controls |
|---|---|
| `MAC_AGENT_ID` in Job pod env | Which pre-registered agent identity authors evidence + lifecycle records on this Job |
| `MAC_AGENT_ROLE` in Job pod env | Operator-visible label; future executor hooks may branch on it |
| `image` in Job container spec | The image (codex-runner vs plain mac vs others) |
| `MAC_TASK_EXECUTOR_COMMAND` env | The shell command mac-task-runner spawns as the executor |

The lease itself is **claimed by the runner Deployment's dispatcher
identity** (see §6.1 Option B and §6.3 for the authorisation
matrix). Everything else stays as it is today.

## 5. Why Job-per-task beats Deployment-pool

| Concern | Deployment-pool (deferred) | Per-Job (this spec) |
|---|---|---|
| Idle cost | Pool sits at replicas≥0 by config; controller scales down only via new code | Zero by construction; Job exists only while task runs |
| Cold-start | Controller reconcile (≤30s) + pod start | Runner tick (≤5s) + pod start |
| Scale-down | Needs new reconciler | `ttlSecondsAfterFinished` |
| ArgoCD interaction | Conflicts on `spec.replicas`, needs `ignoreDifferences` | No conflict — Jobs aren't in git |
| Per-task isolation | Pool worker handles many tasks, contamination risk | One Job per task; fresh fs + process |
| Two-reviewer rule | Distinct Deployments → distinct agent_ids | Distinct Job env → distinct agent_ids via per-role mapping |
| Specialisation | Per-role Deployment | Per-role Job env populated at claim time |
| Code surface added | Controller scaling logic + idle-down reconciler + ArgoCD ignores | A few lines in `build_job_spec` |

## 6. What changes in mac code

Localised to two files. No new modules.

### 6.1 `src/mac/k8s/runner.py`

**`RunnerConfig`** (~10 lines added):

```python
@dataclass
class RunnerConfig:
    mac_url: str
    agent_id: str                    # default ("dispatcher") agent id;
                                     # unchanged behaviour for tasks
                                     # with no role hit
    namespace: str = "mac"
    service_account: str = "mac-task-runner"
    default_image: str = DEFAULT_TASK_IMAGE
    role_images: Dict[str, str] = field(default_factory=dict)            # NEW
    role_agent_ids: Dict[str, str] = field(default_factory=dict)         # NEW
    role_executors: Dict[str, str] = field(default_factory=dict)         # NEW
    capability_role_aliases: Dict[str, str] = field(default_factory=dict) # NEW
    # ... existing fields (poll_interval_seconds, backoff_limit, etc.)

    @classmethod
    def from_env(cls) -> "RunnerConfig":
        # ... existing env reads ...
        return cls(
            ...,
            role_images=_json_env("MAC_RUNNER_ROLE_IMAGES", {}),
            role_agent_ids=_json_env("MAC_RUNNER_ROLE_AGENT_IDS", {}),
            role_executors=_json_env("MAC_RUNNER_ROLE_EXECUTORS", {}),
            capability_role_aliases=_json_env(
                "MAC_RUNNER_CAPABILITY_ROLE_ALIASES", {}
            ),
        )
```

The new maps are JSON-encoded env vars to keep operator config
declarative:

```yaml
# components/ai/mac/values.yaml — mac-runner Deployment env
MAC_RUNNER_ROLE_IMAGES: |
  {
    "python-coder":    "gitea.omv.${CLUSTER_DOMAIN}/vpogu/mac-codex-runner:<digest>",
    "python-reviewer": "gitea.omv.${CLUSTER_DOMAIN}/vpogu/mac-codex-runner:<digest>"
  }
MAC_RUNNER_ROLE_AGENT_IDS: |
  {
    "python-coder":    "mac-worker-python-coder",
    "python-reviewer": "mac-worker-python-reviewer"
  }
MAC_RUNNER_ROLE_EXECUTORS: |
  {
    "python-coder":    "/usr/local/bin/mac-task-executor-codex",
    "python-reviewer": "/usr/local/bin/mac-task-executor-opencode-review"
  }
# Transitional: capability → role alias map. Lets Hermes tasks
# created with required_capabilities=[python] route to the
# python-coder role without setting metadata.required_role
# explicitly. Removed once Hermes plugin sets required_role
# directly. Conflict resolution: first matching capability wins,
# in task.required_capabilities order.
MAC_RUNNER_CAPABILITY_ROLE_ALIASES: |
  {
    "python": "python-coder",
    "review": "python-reviewer"
  }
```

**`_resolve_task_image`** gains role lookup; precedence:

```python
def _resolve_task_image(task: JsonDict, cfg: RunnerConfig) -> str:
    # 1. Per-task override always wins (operator escape hatch)
    runtime_meta = (task.get("metadata") or {}).get("runtime") or {}
    if runtime_meta.get("image"):
        return str(runtime_meta["image"])
    k8s_meta = (task.get("metadata") or {}).get("k8s") or {}
    if k8s_meta.get("image"):
        return str(k8s_meta["image"])
    # 2. Role-based mapping
    role = _resolve_task_role(task)
    if role and role in cfg.role_images:
        return cfg.role_images[role]
    # 3. Default
    return cfg.default_image
```

**New `_resolve_task_role`** — explicit-first, alias-second, no
naked first-capability fallback (review M1):

```python
def _resolve_task_role(task: JsonDict, cfg: RunnerConfig) -> Optional[str]:
    meta = task.get("metadata") or {}
    # 1. Explicit per-task override always wins
    if meta.get("required_role"):
        return str(meta["required_role"])
    # 2. Capability → role alias, scanning in declared order
    aliases = cfg.capability_role_aliases
    for cap in task.get("required_capabilities") or []:
        role = aliases.get(str(cap))
        if role:
            return role
    # 3. No alias hit → no role → default agent + default image
    return None
```

Why this matters: in v1 the role namespace (`python-coder`) and the
capability namespace (`python`) are distinct. A task with
`required_capabilities = [python]` ONLY routes to `python-coder` if
the operator opts in via `MAC_RUNNER_CAPABILITY_ROLE_ALIASES`. Without
the alias, the task falls through to the default agent / default
image, which is the same behaviour as today. This kills the codex
review's M1 "first capability silently becomes the role" surprise.

**New `_resolve_agent_id_for_role`**:

```python
def _resolve_agent_id_for_role(role: Optional[str], cfg: RunnerConfig) -> str:
    if role and role in cfg.role_agent_ids:
        return cfg.role_agent_ids[role]
    return cfg.agent_id  # default for unspecialised tasks
```

**`build_job_spec`** populates new env vars and uses the role-derived
agent id. Existing `MAC_AGENT_ID` value is now derived per-Job, not
read from `cfg.agent_id` blindly. Existing label additions:

```python
"labels": {
    ...
    "mac.task.id": _sanitize_dns_label(task_id),
    "mac.lease.id": _sanitize_dns_label(lease_id),
    "mac.role": _sanitize_dns_label(role or "default"),    # NEW
    "mac.agent.id": _sanitize_dns_label(job_agent_id),     # NEW
},
```

New env entries on the Job container:

```python
{"name": "MAC_AGENT_ID", "value": job_agent_id},               # changed
{"name": "MAC_AGENT_ROLE", "value": role or ""},                # new
{"name": "MAC_TASK_EXECUTOR_COMMAND",
 "value": _resolve_executor_for_role(role, cfg)},               # new
```

**`claim_and_launch_one`** needs one wrinkle: the claim call currently
goes to `/agents/{cfg.agent_id}/claim-next`. With role-aware claims,
the runner could either:

- **Option A** — claim as the role's agent_id from the start (multiple
  parallel claim loops, one per role).
- **Option B** — keep claiming as `cfg.agent_id` (a "dispatcher
  identity"), then re-attribute to the role's agent in the Job spec.
  The DB row already supports this: `tasks.owner_agent_id` is whoever
  CLAIMED; downstream evidence + lifecycle records can be authored by
  a different agent.

**Spec picks Option B** for v1. Rationale:

- Single claim loop is simpler than spawning N polling threads
- mac-api's claim-next dispatches by capability, not by caller's
  identity, so calling as `cfg.agent_id` is fine as long as that
  agent has the union of needed capabilities
- The Job-side `mac-task-runner` overrides `MAC_AGENT_ID` to the
  role's id; subsequent calls (`/tasks/{id}/start`, `/evidence`,
  `/transition`) authored by the role agent — which is what we want
  for the author/reviewer separation rule (§8)
- Lease renewal stays on the runner side (see §6.3), so the role
  agent never needs to authenticate against the lease record

This means `cfg.agent_id` (e.g. `mac-runner`) is the "dispatcher"
identity, and `cfg.role_agent_ids[<role>]` is the "executor" identity
for each role. Both must be pre-registered (see §7).

### 6.2 `src/mac/k8s/job_executor.py` — env override on MAC_AGENT_ID only

The executor already reads `MAC_AGENT_ID` and `MAC_TASK_EXECUTOR_COMMAND`
from env (see `job_executor.py:_default_subprocess_executor` and
`run_one_lease`). Per-Job specialisation flows through env transparently
**except for lease renewal** (see §6.3 below).

### 6.3 Authorization matrix (Option B)

This section is the codex review's blocker fix (B1). It pins down
which agent identity is on the wire for each endpoint, so the design
can't get stuck on "depends on what the API enforces."

| Endpoint | Caller agent_id | Why |
|---|---|---|
| `POST /agents/{id}/claim-next` | **dispatcher** (`cfg.agent_id`) | Single claim loop; dispatcher must hold the union of role capabilities |
| `POST /leases/{id}/renew` | **dispatcher** (`cfg.agent_id`) | Renewal happens server-side authored by whoever claimed; the Job pod does NOT renew under the role identity |
| `POST /tasks/{id}/start` | **role agent** (`MAC_AGENT_ID` in Job env) | Marks the role agent as the work author; survives review-service "never owned this task" check by making the role agent the owner of record |
| `POST /tasks/{id}/evidence` | **role agent** | Evidence `created_by` is the role agent; review-service can pick reviewers by excluding evidence authors |
| `POST /tasks/{id}/submit-for-review` | **role agent** | Same author/reviewer separation guarantee |
| `POST /tasks/{id}/transition` | **role agent** | Lifecycle records authored by the role agent |

Concrete consequence for §6.2: the Job pod does **NOT** renew the
lease. Renewal stays in the runner Deployment. Two ways to wire this:

1. **Runner-side renewal loop (preferred for v1).** The runner keeps
   a renewal goroutine per active claim. The Job's role agent just
   does work + evidence + transitions; if the Job pod crashes, the
   runner's renewal stops on its own (and the lease eventually
   expires). This requires the runner to track in-flight Jobs but
   that's already implicit in `claim_and_launch_one`.
2. **Job renews under dispatcher identity.** Job pod env carries
   `MAC_DISPATCHER_AGENT_ID = cfg.agent_id` in addition to
   `MAC_AGENT_ID = <role agent>`; `job_executor.py` uses the
   dispatcher id only for the renew call, role id for everything
   else. Less code in the runner Deployment; more env on every Job.

Spec picks (1) — runner-side renewal. Rationale: the Job never holds
authority over the lease, which matches the intent (the runner is
"in charge" of the work; the role agent is just "who did it"). It
also means a malicious or buggy role-agent token can't extend its
own deadline. See §13 Q1 for the still-open detail of how the
runner discovers when the Job is done so it can stop renewing.

This authorisation model assumes `MAC_WORKER_TOKEN` is **bearer-admin
scope** — the token itself authorises all of the above; the agent_id
on the request is a label/audit dimension, not an authentication
secret. That's already the case in the existing deployment (one
shared admin token across runner + Jobs). Per-Job least-privilege
tokens are a v2 follow-up (see §13 Q2).

## 7. Agent identity model

**Pre-registered stable agents per role**, declared in `mac-seed`'s
ConfigMap (see [`docs/archive/field-notes/linear-bridge-spec.md`](linear-bridge-spec.md)
§9 for the seed pattern). Example seed YAML:

```yaml
agent_roles:
  - slug: python-coder
    name: Python Coder
    default_capabilities: [python, ops]
    required_capabilities: [python]
    system_prompt: |
      You are a careful Python coder. Run tests + ruff before
      declaring done. Commit to a branch named mac/<task_id>.
      Push the branch and report the PR URL.

  - slug: python-reviewer
    name: Python Reviewer
    default_capabilities: [review, python]
    required_capabilities: [review]
    system_prompt: |
      You are an independent reviewer. Never approve unverified
      evidence; always cite the evidence id in your verdict.

machines:
  - id: mac-worker-machine
    hostname: mac-worker.ai.svc.cluster.local
    labels: { kind: virtual, owner: mac-seed }

agents:
  - id: mac-worker-python-coder
    name: mac-worker-python-coder
    machine_id: mac-worker-machine
    capabilities: [python, ops]
    role_slug: python-coder

  - id: mac-worker-python-reviewer
    name: mac-worker-python-reviewer
    machine_id: mac-worker-machine
    capabilities: [review, python]
    role_slug: python-reviewer
```

Properties:

- **`agent_id` is stable across Job runs.** No churn of attestation
  keys on every claim.
- **One machine row serves all role agents.** They're all logically
  running in K8s; the `hostname` is informational. Operators who want
  distinct machine rows for accounting can split them.
- **Each role agent is registered ONCE** by `mac-seed` on its first
  invocation; subsequent invocations no-op via the existing
  `register_agent` `(machine_id, name)` lookup → reuse path
  (identity_service:220-243).
- **`mac-seed` does NOT register `mac-runner` itself.** That agent is
  registered by the runner Deployment's own init container today
  (`register-mac-agent` in `home-ops/components/ai/mac/values.yaml`).
  Spec keeps that as-is; the seeder is for role-specialised agents.
- **Dispatcher capabilities are owned by the runner Deployment's init
  container, not the seeder.** Specifically: the `AGENT_CAPABILITIES`
  env on `register-mac-agent` must list the UNION of all role
  capabilities (today: `["ops","python","review","hermes"]`). When you
  add a new role to `mac-seed`, you must also update
  `AGENT_CAPABILITIES` in `values.yaml` so the dispatcher can claim
  that role's work. This is the operator constraint flagged in §13 Q5.
- **Minimum viable seeder (PR2a precondition).** The seeder must be
  idempotent over (machine, role, agent) rows. Implementation can be
  as small as a `mac-seed-config` ConfigMap + an init-container that
  POSTs to `/machines` and `/agents` (same shape as
  `register-mac-agent`), using the existing `ON CONFLICT(id) DO UPDATE`
  semantics in mac's identity layer (identity_service:220-243). It does
  NOT need a new console-script or new mac-api endpoints to land.

## 8. Author/reviewer separation under this model

> v2 retitled per codex review M3. The original heading
> ("two-reviewer rule") read like the rule required two distinct
> reviewers. The actual invariant in mac's review-service is "the
> agent who authored the work cannot be a reviewer of it" — i.e.
> author/reviewer separation, requiring **N≥1** distinct reviewer
> agent. If a deployment wants stricter "two independent reviewers"
> semantics, that requires `mac-seed` to declare N≥2 reviewer agents
> AND the review-service to enforce 2-of-N approval (not in v1).

With this design:

- Tasks with `metadata.required_role = "python-coder"` (or whose
  `required_capabilities` route via the capability-alias map to that
  role) are claimed by the dispatcher and executed under
  `mac-worker-python-coder` (via the role mapping).
- When the task is submitted for review, the review-service picks any
  healthy agent with `review` capability that has NOT owned this
  task. With `mac-worker-python-reviewer` registered, it's the
  natural pick.
- Operators who want multiple distinct reviewers can register
  `mac-worker-python-reviewer-1` / `-2` / etc. in `mac-seed` and the
  review-service round-robins (configuration, not code).

Two pre-existing assumptions worth checking against the actual review
code before implementation:

- Does `submit_for_review` filter reviewer candidates strictly on
  "never owned this task," or also "never owned any task in this
  workflow"? Spec assumes the former.
- Does the review-service require the reviewer agent to be in
  `status="idle"`? If yes, the reviewer can't be in another Job
  simultaneously, which would serialise reviews. Worth confirming
  (open question §13 Q4).

## 9. Provisioning requests under this model

`agent_provisioning_requests` previously meant "scale up a Deployment."
With per-Job specialisation it means something narrower:

> A task needs role X, but no agent with role X exists in the
> `agents` table.

That's an **operator misconfiguration**, not a scale event. Right
response: file a finding, alert the operator to add the role to
`mac-seed`'s ConfigMap. No auto-scaling.

Concretely, `mac-k8s-controller`'s `reconcile_provisioning_requests`
becomes:

- For each open `agent_provisioning_request`:
  - If the `role_slug` matches an existing agent → close as
    "spurious; an agent already exists" (no-op surfaces a finding)
  - If no matching agent → emit
    `integration_findings(finding_type="agent.missing_for_role",
    severity="warning", title="Task needs role X but no agent exists")`
- No scaling, no Deployment patches.

`K8sDeploymentScaler` stays in `src/mac/k8s/controller.py` for now,
unused — operators who want pool-scaling for other reasons can still
wire it. Within mac it's dormant.

## 10. mac-k8s-controller responsibilities (revised)

| Old (deferred design) | New (this spec) |
|---|---|
| Reconcile stuck Jobs | ✓ kept |
| Scale `mac-worker-<role>` Deployments based on provisioning | ✗ removed |
| Idle-pool scale-down | ✗ removed |
| Provisioning-request triage as findings | ✓ added (replaces scaling) |

Net: controller shrinks. The only thing it actively MUTATES is
deleting stuck Jobs. Everything else is observe-and-record.

## 11. ArgoCD interaction

> v2: claim was "zero conflict." That's too strong. The accurate
> statement is "no `spec.replicas` fight," because nothing in this
> design tries to mutate a Deployment's replicas at runtime.

With this design, ArgoCD owns (as application config in Git):

- The `mac-k8s-runner` Deployment (image, env, RBAC)
- The `mac-k8s-controller` Deployment
- The `mac-seed` ConfigMap + init container that POSTs roles/agents
- The `mac-api` Deployment + `mac-task-runner` ServiceAccount

ArgoCD does NOT see:

- Jobs created by the runner (they're spawned at runtime via the K8s
  API; not in Git)
- Pods spawned by those Jobs
- The agent rows in mac's DB (application data, not K8s resources)

Net effects:

- **No `spec.replicas` fight.** Nothing scales Deployments at runtime,
  so the `ignoreDifferences` workaround the deferred plan needed is
  not required.
- **Config still flows through Git.** Changes to role images, role
  agent ids, or the seeder ConfigMap require a Git commit and an
  ArgoCD sync. That's a feature (auditability), not a regression.
- **DB drift is not continuously reconciled.** If someone manually
  edits agent rows, the seeder won't notice until the next time it
  runs. v1 keeps the seeder idempotent so a re-roll repairs drift;
  there's no continuous DB-reconciler. (Open question §13 Q5.)

## 12. Migration from current state

Today: `mac-runner` exists, claims as `mac-runner`, generates Jobs
that set `MAC_AGENT_ID=mac-runner`. One image, no role awareness.

Path to this spec, in **three** PRs (v2 split per codex review M4):

### PR1 — Role-aware `build_job_spec` (no behaviour change)

- Add `role_images`, `role_agent_ids`, `role_executors`,
  `capability_role_aliases` to `RunnerConfig` + `from_env`
- Add `_resolve_task_role`, `_resolve_agent_id_for_role`,
  `_resolve_executor_for_role`
- Add the runner-side renewal loop refactor described in §6.3 (the
  Job pod no longer renews; the runner owns the renewal goroutine).
  Keep behaviour identical when no roles are configured — the
  dispatcher is also the executor, so renewal-by-dispatcher matches
  today's behaviour.
- Tests: ensure default behaviour (task with no role) is unchanged;
  add fixtures for tasks with explicit roles
- The runner env in home-ops stays unset for the new vars → existing
  behaviour preserved bit-for-bit

After PR1: code supports role specialisation but production isn't
using it. Production behaviour is reversible by leaving the env
unset; the code changes themselves remain.

### PR2a — `mac-seed` live, role agents registered

- `mac-seed` ConfigMap declares `python-coder` + `python-reviewer`
  roles + matching agent rows (per §7)
- New init container OR a one-shot Job runs the seeder against
  mac-api; idempotent via `ON CONFLICT(id) DO UPDATE`
- Bump `register-mac-agent`'s `AGENT_CAPABILITIES` env to include
  every role's required capability (the union; see §7)
- No runner env changes; no role-image map. The role agents exist in
  the DB but no Job spawns under them yet (no task carries
  `metadata.required_role` and the alias map is unset).

After PR2a: the DB has all the role identities. Verifies the seeder
works in isolation, before any execution path depends on it. If the
seeder is broken, no work is affected — it just sits as
unreferenced rows.

### PR2b — Runner env live, first specialised task

- Build `mac-codex-runner` image (separate Dockerfile, codex + git +
  uv on the mac base); push to registry
- Runner env adds `MAC_RUNNER_ROLE_IMAGES`,
  `MAC_RUNNER_ROLE_AGENT_IDS`, `MAC_RUNNER_ROLE_EXECUTORS`,
  `MAC_RUNNER_CAPABILITY_ROLE_ALIASES`
- Test: create a task with `metadata.required_role = "python-coder"`,
  watch the Job spawn with the codex image, run codex against the
  ivan-plugin repo, push a branch
- Then: enable the capability alias and confirm Hermes-created tasks
  with `required_capabilities=[python]` route correctly without
  setting `metadata.required_role` explicitly

After PR2b: role specialisation is live in production. Reversible by
unsetting the runner env vars (the seeder rows survive but stop
being referenced).

PR1 production behaviour is reversible by leaving env unset.
PR2a is reversible by deleting the seeder ConfigMap (the DB rows
become dormant). PR2b is reversible by unsetting the runner env
vars.

## 13. Open questions

1. **Runner-side renewal completion signal.** §6.3 picks runner-side
   renewal: the runner keeps a per-claim renewal goroutine. Open:
   how does the runner discover that the Job is done so it can STOP
   renewing? Options:
   - Watch the Job's status via the K8s API (kube-client `watch`),
     stop renewal when `status.succeeded ≥ 1` or `status.failed ≥ 1`.
   - Poll mac-api: task transitions to a terminal state
     (`submitted-for-review`, `failed`, `cancelled`) → stop renewing.
   - Both, as a defence-in-depth.
   PR1 needs to pick one. Leaning K8s watch for fast feedback +
   mac-api poll as a safety net.

2. **Per-Job least-privilege tokens.** Today every Job uses the same
   admin `MAC_WORKER_TOKEN`. Future hardening: mint a short-lived
   token per Job scoped to `task:execute` for that task id. Not v1;
   would need a `/tokens/mint` endpoint (deferred in
   `k8s-native-rewrite-plan.md`).

3. **Capability alias vs explicit-role contract.** v2 removed the
   naked first-capability fallback; tasks now route via an explicit
   `metadata.required_role` OR an operator-configured
   `MAC_RUNNER_CAPABILITY_ROLE_ALIASES` map. Open: should the alias
   map live in mac's DB (alongside agent_roles) instead of runner
   env, so the rule is data-driven and visible to mac-api callers?
   Not blocking PR1 but a candidate refactor before adding many
   roles.

4. **Reviewer-status check.** If `submit_for_review` requires the
   reviewer to be `status=idle`, but `mac-worker-python-reviewer` is
   currently running another review Job, it'll fail. Need to verify
   how review-service picks reviewers under concurrent load.

5. **Continuous DB-reconciler vs seeder-on-roll.** §11 v2 admits the
   seeder is the only DB-reconciliation point. Open: do we need a
   periodic reconcile (the seeder running every N hours) or is
   re-rolling the runner Deployment on config change enough? For
   now the latter; revisit if drift starts costing us.

6. **Capability gating on the claim side.** When the runner calls
   `/agents/{cfg.agent_id}/claim-next`, mac-api filters by the
   AGENT's capabilities, not the task's. So `mac-runner` (the
   dispatcher) needs `required_capabilities ⊇ union of all role
   capabilities`. §7 v2 makes the runner Deployment's init container
   responsible for keeping `AGENT_CAPABILITIES` in sync with the set
   of declared roles. Documented constraint, not a code change.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Runner crashes during renewal → lease expires mid-Job | Acceptable: a new runner replica claims the stuck Job's task on next lease expiry; idempotency on evidence + transitions handles the duplicate-execution case |
| Capability alias points to a role with no registered agent | `_resolve_agent_id_for_role` falls back to `cfg.agent_id`; log a warning. Bad config never crashes the runner |
| Codex-runner image too large / slow to pull | Use a thin base (python:3.13-slim); pre-pull on nodes with `imagePullPolicy: IfNotPresent` + nodeAffinity for warm caches |
| Reviewer agent is busy, review stalls | Register multiple reviewer agents (`mac-worker-python-reviewer-1/2/3` in `mac-seed`) |
| Operator forgets to update dispatcher `AGENT_CAPABILITIES` | Tasks for the new role never get claimed. Surface as an `agent.missing_capability` finding in `mac-k8s-controller` (§9) |
| All Jobs use admin token | Acceptable for v1; per-Job mint deferred (§13 Q2) |

## 15. Acceptance criteria for v1

Both must work end-to-end:

1. **Default path unchanged.** A task with empty `required_capabilities`
   and no `metadata.required_role` claims and executes under
   `mac-runner` agent on the default mac image, identical to today.
2. **Role-specialised path works.** A task with `metadata.required_role
   = "python-coder"` claims, then spawns a Job that:
   - runs the `mac-codex-runner` image
   - has `MAC_AGENT_ID = mac-worker-python-coder` in env
   - runs the configured executor command
   - records evidence under `created_by = mac-worker-python-coder`
   - on `submit_for_review`, the picked reviewer is
     `mac-worker-python-reviewer` (not the coder)

Plus regression: all existing `tests/test_k8s_runner.py` tests pass
unchanged; new tests cover the role-resolution + role-image map paths.

## 16. What's NOT in v1

- Webhook-driven runner (still polls every 5s)
- Per-Job token minting (still uses shared admin token)
- Auto-scaling of agent count (`mac-seed` declares a fixed set)
- Role autodetection from task description (operator/Hermes sets
  `metadata.required_role` explicitly)
- `mac-codex-runner` image internals (it's a separate workstream;
  this spec just consumes whatever image is configured)

Each is a follow-up if/when needed; none block v1 from delivering
specialised execution.
