from __future__ import annotations


def populate(add, bullets, numbers, diagram, canonical_document_id):
    add("title", "HGX-Runner")
    add("subtitle", "One control plane for on-prem and CSP execution")
    add("meta", "Architecture refresh | Version 3.0 | 11 August 2026")
    add(
        "lead",
        "Decision: merge MAC into HGX-Runner and converge three current systems into one operating model. MAC supplies the work-control kernel; classic Horde remains the on-prem capacity authority; agentic Horde remains the CSP capacity authority. One hgx CLI routes secure NVIDIA GitLab work to classic Horde and non-secure GitHub work to agentic Horde, while every mutable fact has exactly one owner. After behavioral parity, migration, and cutover, retire MAC as a separate service.",
    )
    add(
        "link",
        "Source document | Agentic Horde | Classic Horde",
        links=[
            ("Source document", f"https://docs.google.com/document/d/{canonical_document_id}/edit?tab=t.0"),
            ("Agentic Horde", "https://github.com/NVIDIA-Omniverse/ov-agent-farm"),
            ("Classic Horde", "https://gitlab-master.nvidia.com/omniverse/devplat/horde/horde"),
        ],
    )
    diagram("AUTHORITY", "Figure 1. Three current systems converge on one HGX-Runner control plane and two explicitly bounded execution fabrics.")

    add("h1", "1. Executive decision")
    add(
        "p",
        "The target is not a federation of three durable control planes. It is one HGX-Runner service that absorbs MAC's proven project, task, allocator, lease, evidence, review, policy, publication, and operational-learning semantics and then retires MAC. Classic Horde and agentic Horde remain execution providers behind that control plane because they solve different infrastructure problems.",
    )
    add(
        "p",
        "Classic Horde allocates on-prem resources only. Its vSphere and CloudStack integrations, VM templates, capacity views, resource groups, asynchronous requests, service accounts, and repair flows are the secure internal execution substrate. It does not allocate or govern CSP resources. Agentic Horde allocates CSP resources only. Its sessions, Kubernetes or VM cluster profiles, persistent workspaces, SSH, storage, logs, events, checkpoint, stop, and resume flows are the external execution substrate. It does not allocate on-prem resources.",
    )
    add("h2", "The operating thesis")
    bullets([
        "MAC contributes organizational work control: identity, project policy, tasks and groups, authoritative allocation, fenced attempts, evidence, independent review, canonical publication, audit, and bounded capacity demand.",
        "Classic Horde contributes secure on-prem supply: vSphere and CloudStack capacity, VM lifecycle, templates, resource groups, credentials, connectivity, and recovery.",
        "Agentic Horde contributes non-secure CSP supply: session and workspace lifecycle, CSP-backed Kubernetes or VM profiles, GPU resources, connectivity, storage, observability, and recovery.",
        "HGX-Runner becomes the only task authority and hgx becomes the common user surface. Provider-native identifiers and diagnostics stay visible; routing never erases the trust boundary.",
    ])

    add("h1", "2. What synchronization means")
    add(
        "p",
        "Three-way synchronization is an exchange of authoritative state, not three copies of the same ledger. A project policy and task attempt are written once in HGX-Runner. A classic request or agentic session is written once by its owning provider. The common layer stores immutable correlations, observes provider state, and turns verified provider transitions into task events. Conflicts fail closed instead of being resolved by last writer wins.",
    )
    diagram("SYNC", "Figure 2. Each fact has one writer; synchronization carries commands, immutable identities, state, evidence, and capacity signals.")
    add("h2", "Five synchronization planes")
    bullets([
        "Policy: repository authority, security class, approved backend, runner class, credentials, artifacts, network boundary, and egress are bound to the project before dispatch.",
        "Work: project, task, dependency, lease, attempt, cancellation, evidence, review, and publication state move from the MAC contract into HGX-Runner and are never delegated to a provider.",
        "Execution: provider request, instance, session, workspace, cluster, and endpoint IDs are correlated to exactly one fenced attempt; provider events flow back without becoming a second task lifecycle.",
        "Capacity: HGX-Runner emits bounded demand to the authorized fabric; each provider returns available supply, quota, readiness, degradation, and terminal state.",
        "Repository: secure GitLab and non-secure GitHub publication receipts close the task only after remote verification on the canonical authority.",
    ])
    add("h2", "A reconciliation contract, not eventual ambiguity")
    bullets([
        "Every command is idempotent and carries project, task, attempt, lease/fence, backend, provider-handle, correlation, and deadline fields.",
        "Every observed transition records source system, native ID, native version or timestamp, normalized state, and raw diagnostic reference.",
        "Unknown or contradictory state pauses dispatch and raises an operator-visible reconciliation record; it never triggers cross-fabric failover.",
        "Migration bridges have an owner and sunset date. MAC IDs remain durable provenance after cutover, not live routing keys or alternate writers.",
    ])

    add("h1", "3. Authority map and security routing")
    diagram("ROUTING", "Figure 3. Repository security classification selects exactly one execution fabric; capacity shortage cannot downgrade the route.")
    add("h2", "Secure path: GitLab to classic Horde")
    add(
        "p",
        "A secure NVIDIA GitLab project routes only to classic Horde's on-prem capacity. The project binds the internal repository, service-account scope, artifact boundary, allowed egress, required platform and hardware, and eligible runner class. Classic Horde owns the request and VM lifecycle; HGX-Runner owns whether that VM may become an agent, receive a lease, submit evidence, or complete the task.",
    )
    add("h2", "Non-secure path: GitHub to agentic Horde")
    add(
        "p",
        "A non-secure GitHub project routes only to agentic Horde's CSP capacity. The project binds the GitHub authority, CSP account or profile, secret scope, artifact boundary, allowed egress, and eligible runner class. Agentic Horde owns the session, cluster or VM, workspace, and connectivity lifecycle; HGX-Runner owns task authorization, lease fencing, evidence, review, and completion.",
    )
    add("h2", "Fail-closed invariants")
    bullets([
        "Missing, conflicting, or attempted downgraded security classification blocks execution before capacity creation.",
        "Secure credentials, source, logs, artifacts, and caches never enter the CSP trust zone.",
        "A classic Horde outage does not authorize agentic Horde, and a CSP quota failure does not authorize classic Horde.",
        "The CLI can explain a route and show native provider state; it cannot override project policy without an audited policy change.",
    ])

    add("h1", "4. MAC is the migration source, not the durable destination")
    add(
        "p",
        "MAC is not the target runtime. It is the executable reference and data source for the work-control kernel. The migration must port behavior, validate shared fixtures, backfill compatible history, shadow decisions, cut writers over, and retire the separate MAC service. Keeping MAC as a permanent facade or second allocator would preserve the exact split authority this program is intended to remove.",
    )
    add("h2", "Mandatory parity baseline")
    bullets([
        "PostgreSQL-backed global ledger; projects, repository contracts, tasks, dependencies, groups, actors, owners, holds, release, cancellation, and bounded retries.",
        "One hub-owned allocator that evaluates task state, project activation, agent health and capacity, trust, tenant, platform, hardware, ownership, and target restrictions, then atomically creates a fenced lease.",
        "Fenced attempts, structured evidence, independent review, and remotely verified canonical publication before completion.",
        "Project egress, reviewed network allowlists, OpenShell confinement, versioned sandbox BOMs, drained rollouts, and audited single-use break-glass recovery.",
        "Events, task waiting, AgentBus coordination, fleet operational learning, throughput evidence, repository-ref reconciliation, and bounded provider demand.",
    ])
    add("h2", "Cutover rule")
    add(
        "p",
        "For every mutable object type, the migration plan names the current writer, the future HGX writer, the backfill transform, the parity fixture, the shadow comparison, the cutover instant, the rollback boundary, and the bridge removal milestone. There is no open-ended dual write. After cutover, MAC identifiers are read-only provenance and MAC credentials, dispatch, allocator, leases, and APIs are retired.",
    )

    add("h1", "5. The two provider adapters")
    diagram("ADAPTERS", "Figure 4. A narrow common lifecycle preserves two intentionally different infrastructure implementations.")
    add("h2", "Common contract")
    bullets([
        "plan and explain; create; wait; connect; observe; cancel; stop; destroy; and reconcile",
        "stable JSON envelope with normalized status, typed error, correlation ID, provider handle, native IDs, timestamps, retryability, and namespaced diagnostics",
        "idempotency keys and compare-before-mutate recovery for partial creates, stale observations, duplicate commands, and cancellation races",
        "a provider handle that retains every classic request/instance ID or agentic session/workspace/cluster ID rather than inventing a lossy universal resource ID",
    ])
    add("h2", "Classic Horde adapter: on-prem only")
    add(
        "p",
        "The adapter uses classic Horde's server-owned create planning, asynchronous request polling, vSphere or CloudStack VM lifecycle, templates, capacity, resource groups, service accounts, connection hints, and audited repair. It reports on-prem supply and failure only; it contains no CSP routing or governance decision.",
    )
    add("h2", "Agentic Horde adapter: CSP only")
    add(
        "p",
        "The adapter uses agentic Horde's authenticated session lifecycle, CSP-backed Kubernetes or VM profiles, GPU and resource selection, persistent workspaces, wait conditions, SSH, storage, logs, events, secrets, checkpoint, stop, resume, and delete. It reports CSP supply and failure only; it contains no on-prem allocation decision.",
    )

    add("h1", "6. Unified task and capacity lifecycle")
    diagram("LIFECYCLE", "Figure 5. One fenced task lifecycle commands either provider and closes only on independently reviewed repository evidence.")
    numbers([
        "Register a project and bind its canonical repository, security class, permitted backend, runner class, credentials, artifacts, and egress policy.",
        "Create or release a task. HGX-Runner binds the actor, tenant, owner, dependencies, attempt budget, requirements, and optional group.",
        "The authoritative allocator searches currently registered eligible agents. If none exist, it records an attributable no-match reason and task-bound demand.",
        "The route policy selects classic Horde for secure on-prem work or agentic Horde for non-secure CSP work. The provider creates capacity under an idempotent request.",
        "HGX-Runner correlates immutable native IDs, verifies provider readiness and transport, completes explicit agent onboarding, and waits for a capability heartbeat.",
        "The allocator atomically grants the agent a lease and fence. Execution occurs inside the approved repository, sandbox, hardware, credential, artifact, and egress envelope.",
        "Provider events and worker evidence append to the attempt. Lease loss fences stale output; cancellation propagates to both task and provider operations.",
        "An independent reviewer accepts the exact attempt. Repository completion requires remote proof that the accepted commit reached the canonical GitLab or GitHub ref.",
    ])
    add("h2", "Capacity is not an agent")
    add(
        "p",
        "A created VM, cluster, session, workspace, reachable endpoint, or successful SSH nonce is not schedulable capacity. It becomes an agent only after identity, endpoint-bound credentials, fleet registration, policy certification, and heartbeat are authoritative. Retirement reverses that transaction in order: drain leases, revoke credentials, tombstone the agent and registry record, then delete provider capacity.",
    )

    add("h1", "7. Failure handling and operational proof")
    bullets([
        "No eligible agent: keep the task open, preserve the exact missing capability, and create at most bounded demand on the already authorized fabric.",
        "Provider quota or capacity exhaustion: surface the native reason and operator action; never report demand as satisfied and never cross the trust boundary.",
        "Partial create or stale state: reconcile by idempotency key and native immutable IDs before retrying; duplicate capacity must not create duplicate task authority.",
        "Lease loss or worker failure: fence the attempt, retain evidence, cancel or drain provider work, and permit only bounded policy-controlled retry.",
        "Credential revocation or route-policy change: stop new dispatch, fence incompatible attempts, rotate or revoke scoped credentials, and prove no artifact escaped its boundary.",
        "Canonical repository moved: reject completion, rebase or replan, rerun required verification and independent review, then publish with remote proof.",
    ])
    add("h2", "Operational scorecard")
    bullets([
        "useful accepted outcomes reaching canonical refs; queue, allocation, startup, execution, review, and publication latency",
        "no-match episodes, quota failures, provider reconciliation, abandoned capacity, retry and rework rates",
        "secure-route violations attempted and blocked; credential, artifact, network, and egress audit completeness",
        "cost and utilization by project, fabric, profile, task class, and accepted outcome—not merely sessions created or tasks started",
    ])

    add("h1", "8. Complexity-based delivery schedule")
    diagram("ROADMAP", "Figure 6. MAC convergence, CLI/adapters, and trust routing proceed in parallel, then feed cutover and a readiness-gated Kubernetes migration.")
    add(
        "p",
        "Planning basis: two implementation streams plus continuous security and SRE support. Complexity reflects state migration, trust-boundary risk, provider divergence, recovery, and operational proof. M0-M7 form an approximately 23-week critical path if exit gates pass. M8 is intentionally readiness-gated and is not required to prove the functional MAC-to-HGX convergence.",
    )
    add("h2", "M0 | Authority and route freeze — Small — 2 weeks")
    bullets([
        "Freeze HGX-Runner as the target work ledger, MAC as the migration source to retire, classic Horde as on-prem-only, and agentic Horde as CSP-only.",
        "Publish the ownership matrix, project security policy, identity mappings, provider handle, state/error taxonomy, bridge sunsets, and non-negotiable negative paths.",
        "Exit: architecture and security decision signed; MAC parity inventory complete; ambiguous classification and cross-fabric overrides fail closed.",
    ])
    add("h2", "M1 | Unified hgx read and explain surface — Medium — 3 weeks")
    bullets([
        "Ship stable JSON for identity, route explanation, capacity, requests, instances, sessions, workspaces, status, logs, and native diagnostics.",
        "Exit: golden fixtures pass against both providers; existing automation can discover and observe either fabric without guessing provider identity.",
    ])
    add("h2", "M2 | Classic on-prem adapter and secure runner contract — Large — 4 weeks")
    bullets([
        "Normalize classic plan, create, poll, connect, observe, cancel, repair, and destroy around vSphere and CloudStack capacity.",
        "Certify NVIDIA GitLab credentials, artifacts, egress, VM images, runner onboarding, and recovery entirely inside the secure boundary.",
        "Exit: representative secure jobs and forced failure cases complete without CSP access or duplicate requests.",
    ])
    add("h2", "M3 | Agentic CSP adapter and non-secure runner contract — Large — 4 weeks, parallel with M2")
    bullets([
        "Normalize CSP session, cluster or VM, workspace, wait, SSH, storage, logs/events, checkpoint, stop, resume, and delete.",
        "Certify GitHub credentials, artifacts, egress, profiles, runner onboarding, quotas, and recovery entirely inside the CSP boundary.",
        "Exit: representative non-secure jobs and forced failure cases complete without on-prem allocation or credential crossover.",
    ])
    add("h2", "M4 | Port the MAC control kernel — Extra large — 6 weeks, parallel with M2-M3")
    bullets([
        "Implement HGX-owned projects, tasks, groups, actors, allocator, fenced leases, attempts, evidence, review, publication, policy, events, learning, and recovery.",
        "Exit: shared fixtures agree on legal transitions, allocation, lease loss, evidence, review, completion, cancellation, and repository proof.",
    ])
    add("h2", "M5 | Three-way routed pilot — Large — 4 weeks")
    bullets([
        "Drive one secure GitLab project through classic Horde and one non-secure GitHub project through agentic Horde using the same HGX task lifecycle.",
        "Exit: both routes publish accepted commits with complete correlation and audit; forced downgrade, stale lease, quota, timeout, revocation, and wrong-remote tests fail safely.",
    ])
    add("h2", "M6 | MAC backfill, shadow, and writer cutover — Extra large — 5 weeks")
    bullets([
        "Backfill compatible MAC history with transform versions and source IDs; shadow allocation, state, review, and completion; rehearse rollback and reconciliation.",
        "Exit: agreed parity soak passes; MAC writers freeze; HGX is the sole production writer without orphaned leases, lost evidence, or alternate allocation authority.",
    ])
    add("h2", "M7 | Production hardening and MAC retirement — Large — 3 weeks")
    bullets([
        "Operationalize SLOs, quotas, costs, credentials, audits, runbooks, disaster recovery, provider-specific diagnostics, and bounded retirement.",
        "Exit: security/SRE approval; recovery exercises pass on both fabrics; MAC dispatch, credentials, allocator, leases, and APIs are retired, with only policy-approved read-only archive access.",
    ])
    add("h2", "M8 | Drain and migrate agents to Omniblue and Omnired — Extra large — future, readiness-gated")
    bullets([
        "Prerequisite: Omniblue, the internal Kubernetes cluster, and Omnired, the external Kubernetes cluster, are fully deployed, certified, observable, and sized for the existing populations.",
        "Inventory every on-prem and off-prem runner, lease, credential, cache, artifact path, capability, workload class, and dependency; establish rollback and coexistence windows.",
        "Drain and migrate existing on-prem agents and runners to Omniblue, and existing off-prem agents and runners to Omnired, in bounded cohorts with no trust-boundary crossover.",
        "Exit: no active leases on legacy populations; workload, security, performance, cost, recovery, and capacity acceptance pass; legacy runner substrates are decommissioned under an approved rollback horizon.",
    ])

    add("h1", "9. Success criteria")
    bullets([
        "One task authority: after cutover, HGX-Runner is the only writer for projects, tasks, allocation, leases, attempts, evidence, review, and completion; MAC is retired as a service.",
        "Two capacity authorities: classic Horde allocates only on-prem resources and agentic Horde allocates only CSP resources, each retaining native state and diagnostics.",
        "One CLI: the same hgx workflow explains, creates, observes, cancels, and reconciles either authorized route without hiding backend identity.",
        "One security decision: a secure NVIDIA GitLab project runs only on classic Horde on-prem; a non-secure GitHub project runs only on agentic Horde CSP; ambiguity and downgrade attempts block before creation.",
        "One fenced execution chain: project policy, task, lease, provider handle, agent, evidence, reviewer, accepted commit, and canonical publication receipt remain queryable after restart and reconciliation.",
        "One bounded lifecycle: partial create, quota failure, worker loss, lease expiry, cancellation, credential revocation, moving Git base, and provider outage do not produce duplicate authority or unsafe failover.",
        "A future infrastructure exit: after both clusters are ready, on-prem populations are drained to Omniblue and off-prem populations to Omnired with zero active legacy leases and verified rollback evidence.",
    ])

    add("h1", "Appendix A. Capability boundaries")
    add("h2", "Implemented today")
    bullets([
        "MAC: durable projects/tasks/groups; actor and owner identity; authoritative allocation and fenced leases; evidence, independent review and publication proof; confinement and project egress; events, AgentBus, throughput, learning, repository refs, and bounded capacity control.",
        "Classic Horde: on-prem vSphere and CloudStack VM orchestration; templates, capacity, resource groups and service accounts; stable automation output; server-owned create planning; asynchronous requests; lifecycle, connection, and audited repair.",
        "Agentic Horde: CSP session and workspace lifecycle; Kubernetes or VM profiles; GPU and other resources; wait, SSH, storage, logs, events, secrets, checkpoint, stop, resume, delete, and diagnostics.",
    ])
    add("h2", "Proposed—not yet safe to claim")
    bullets([
        "A production HGX-owned work ledger containing full MAC behavioral parity and migrated history.",
        "One production hgx write surface and provider adapter contract across both Horde implementations.",
        "Transparent failover between secure and non-secure fabrics; it is explicitly forbidden by the target policy.",
        "Automatic migration of existing agents or runners merely because provider capacity exists.",
        "Full Omniblue and Omnired workload readiness, complete cohort migration, or decommissioning of current runner substrates.",
        "A proven productivity or ROI percentage; success is measured by accepted canonical outcomes, security, latency, utilization, cost, and recovery.",
    ])

    add("h1", "Appendix B. Source ledger")
    add("p", "Claims were refreshed against the three current local repositories on 11 August 2026. Future revisions should repeat the audit and distinguish implementation evidence from stakeholder-approved target policy.")
    bullets([
        "MAC 36315a43: README.md, AGENTS.md, docs/book, authority and allocator decisions, project/task/group contracts, evidence/review, sandbox/egress, capacity, AgentBus, learning, throughput, and repository-ref implementation.",
        "Agentic Horde 98137d40: README.md, hgx client and API, session/workspace/cluster lifecycle, CSP profiles, resources, SSH, storage, logs/events, secrets, checkpoint, stop/resume, delete, and diagnostics.",
        "Classic Horde 21aa53d7: README.md, CLI and API, vSphere and CloudStack adapters, instances, templates, capacity, resource groups, service accounts, asynchronous requests, connectivity, and repair.",
        "Stakeholder target policy: classic Horde is on-prem-only; agentic Horde is CSP-only; secure GitLab and non-secure GitHub use distinct runners; M8 begins only after Omniblue and Omnired are fully deployed and certified.",
    ])
