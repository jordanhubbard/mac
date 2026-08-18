!!! warning "Historical field note"
    This design/spec note is retained for provenance only. It is not a current operating contract; the premises or implementation path it describes have been superseded or never shipped.

# Review: docs/job-per-task-roles-spec.md

> Source: `codex exec` (gpt-5.5), 2026-05-28. Saved verbatim plus an
> action-tracker showing which findings were applied to v2 of the spec.

## Summary

Verdict: blockers exist. The job-per-task direction is sound, especially
versus Deployment scaling, but the selected Option B has unresolved
ownership and authorization semantics at the exact point where
correctness depends on them. I would not treat this as PR1-ready unless
PR1 is strictly inert and the spec is revised to make the runtime path
unambiguous.

## BLOCKER

### B1. Authorization model for Option B is unspecified

- **Spec section:** §6.1, §6.2, §8, §13 Q1
- **Problem:** Option B claims as dispatcher but executes as role agent,
  and the spec does not establish that mac-api permits the role agent
  to renew, start, submit, transition, or write evidence for a
  lease/task claimed by another agent. This is not safe to defer as a
  vague open question because it decides whether the design works.
- **Evidence:** §6.1 says "keep claiming as `cfg.agent_id`" and
  "re-attribute to the role's agent in the Job spec." §6.2 admits "If
  mac-api enforces 'only the agent who claimed can renew,' renew will
  fail." §8 then says review excludes agents that have "owned this
  task," but under Option B the owner remains the dispatcher, not the
  coder.
- **Recommended fix:** Before PR1, add an authorization matrix for
  `/leases/{id}/renew`, `/tasks/{id}/start`,
  `/tasks/{id}/submit-for-review`, `/tasks/{id}/evidence`, and
  transition endpoints: bearer-only, path-agent, task-owner, or
  evidence-author. Then pick one model: Option A, lease/task
  reassignment before Job start, or explicit dispatcher-owned renewal
  plus role-authored evidence with review logic based on evidence
  author.
- **Status: APPLIED to spec v2** — see §6.1 "Authorization matrix" and
  §6.3 "Lease renewal under dispatcher identity."

## MAJOR

### M1. Role inference from `required_capabilities[0]` is inconsistent

- **Spec section:** §6.1, §8, §13 Q3
- **Problem:** `_resolve_task_role` falling back to
  `required_capabilities[0]` is both fragile and internally inconsistent
  with the role names shown elsewhere. A capability like `python` is
  not the same namespace as a role like `python-coder`.
- **Evidence:** The role maps are keyed by `"python-coder"` /
  `"python-reviewer"`, but the fallback for
  `required_capabilities=[python]` returns `"python"`. §8 claims such
  tasks execute under `mac-worker-python-coder`, which would not happen
  with the shown maps.
- **Recommended fix:** Do not infer role directly from the first
  capability. Use explicit `metadata.required_role`, or add a separate
  configured map such as
  `MAC_RUNNER_CAPABILITY_ROLE_ALIASES={"python":"python-coder"}` with
  deterministic conflict handling.
- **Status: APPLIED to spec v2** — added
  `MAC_RUNNER_CAPABILITY_ROLE_ALIASES`; removed first-capability
  fallback. Tasks with no `metadata.required_role` and no alias hit
  default agent/image.

### M2. Seeder ownership of dispatcher capabilities is contradictory

- **Spec section:** §7, §12, §13 Q5
- **Problem:** Seeder ownership is underspecified, and there is a
  contradiction around who manages the dispatcher identity. §7 says
  `mac-seed` does not register `mac-runner`; §13 says the dispatcher's
  union capabilities are "easy to ensure in `mac-seed`."
- **Evidence:** Option B requires `mac-runner` to have the union of all
  role capabilities to claim work, but the seed example only creates
  role agents.
- **Recommended fix:** Make a minimum viable seeder explicit:
  idempotently create/update machine, role agents, role capabilities,
  and either the dispatcher agent or a documented dependency on the
  runner init container updating dispatcher capabilities. This should
  probably be PR0 or PR2a before wiring role env live.
- **Status: APPLIED to spec v2** — §7 now says the existing
  `register-mac-agent` init container owns the dispatcher row; the
  seeder owns the role agents. §12 promotes the seeder split into a
  PR2a step before role env wiring goes live.

### M3. "Two-reviewer rule" language is ambiguous

- **Spec section:** §8
- **Problem:** A single `mac-worker-python-reviewer` identity satisfies
  "reviewer != coder" only if the rule requires one distinct reviewing
  agent, not two independent reviewers or two approvals.
- **Evidence:** §8 says operators wanting multiple reviewers can
  register `-1` / `-2`, implying one reviewer identity is enough for v1.
- **Recommended fix:** Rename this to "author/reviewer separation"
  unless the actual invariant is two distinct reviewer agents. If two
  reviewers are required, v1 needs at least two reviewer identities and
  tests proving distinct reviewer selection.
- **Status: APPLIED to spec v2** — §8 retitled "Author/reviewer
  separation" with a sub-note that mac's two-reviewer rule, where
  enforced, requires N≥2 reviewer agents registered in `mac-seed`.

### M4. PR2 is too large for first live enablement

- **Spec section:** §12
- **Problem:** PR2 combines seeder config, role-agent creation, runner
  env wiring, image build, image publication, and live end-to-end
  validation. Too much blast radius for the first live step.
- **Evidence:** PR1 is "no behaviour change"; PR2 makes specialization
  live and introduces new runtime config plus a new image.
- **Recommended fix:** Split seeder/idempotent registration into PR0 or
  PR2a, then wire runner env and image in the next PR.
- **Status: APPLIED to spec v2** — PR phasing is now PR1 (inert
  code), PR2a (seeder + role agent rows live, no runner env), PR2b
  (runner env + role images go live, first specialised task).

### M5. ArgoCD "zero conflict" overclaims

- **Spec section:** §11
- **Problem:** ArgoCD owns the runner Deployment and `mac-seed`
  ConfigMap, while role registrations are DB state that ArgoCD does not
  continuously reconcile.
- **Evidence:** §11 lists those resources as ArgoCD-owned, then says
  ArgoCD does not see agent rows.
- **Recommended fix:** Say there is no `spec.replicas` fight, but
  config changes flow through Git, and DB registration drift is
  handled only when the seeder runs.
- **Status: APPLIED to spec v2** — §11 reworded.

## MINOR

### m1. §4 "specialisation lives in two places" wording

- **Problem:** The table lists `MAC_AGENT_ID`, `MAC_AGENT_ROLE`,
  `image`, and executor command — four fields, not two places.
- **Recommended fix:** Reword to "Specialisation is populated by the
  runner through these fields."
- **Status: APPLIED.**

## NIT

### n1. §12 "reversible" wording is imprecise

- "reversible (just env vars; revert deletes the maps)" — PR1 adds
  code and tests too. Say production *behaviour* is reversible by
  leaving env unset.
- **Status: APPLIED.**

## Open questions worth answering before PR1

1. Does review exclusion use `owner_agent_id`, evidence author, or both?
2. Must task lifecycle endpoints match the task/lease owner agent, or
   is the bearer token sufficient?
3. Who updates `mac-runner` dispatcher capabilities to the union of
   role capabilities?
4. Is first-capability role inference temporary migration behavior or
   part of the API contract?
5. How and when does `mac-seed` rerun to repair DB drift?

(Spec v2 addresses 2, 3, 4 in §6.1/§7. Questions 1 and 5 remain open
and are listed in §13.)

## What's solid

The rejection of Deployment-pool scaling is well argued: avoiding
ArgoCD replica fights, idle-down logic, and a second pod creation path
is the right design pressure. The stable per-role agent identity model
also fits the attestation-key constraint better than ephemeral per-Job
identities. Keeping specialization in per-Job env/image selection is a
clean operational shape once ownership and authorization are pinned
down.
