from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import urllib.error
import urllib.request

import certifi


DOCS = "https://docs.googleapis.com/v1/documents"
DRIVE = "https://www.googleapis.com/drive/v3/files"
TAB_ID = "t.0"
CANONICAL_DOCUMENT_ID = "1iinPBrxuP8YtGYsdGCwZ0vlQRgIzU_fCl-CcqnvGnPE"
CANONICAL_TITLE = "Project HGX-Runner: Unified Control Plane and Literate Software Foundry"


def access_token() -> str:
    return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()


TOKEN = None
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def api(method: str, url: str, payload=None):
    global TOKEN
    if TOKEN is None:
        TOKEN = access_token()
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google API {exc.code}: {detail}") from exc
    return json.loads(raw) if raw else {}


def get_doc(doc_id: str):
    return api("GET", f"{DOCS}/{doc_id}?includeTabsContent=true")


def batch(doc_id: str, requests):
    if not requests:
        return {}
    revision = get_doc(doc_id)["revisionId"]
    return api(
        "POST",
        f"{DOCS}/{doc_id}:batchUpdate",
        {"requests": requests, "writeControl": {"requiredRevisionId": revision}},
    )


def rgb(hex_value: str):
    value = hex_value.lstrip("#")
    return {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }


INK = "101317"
PANEL = "23282F"
STEEL = "65707C"
FOG = "EEF1F3"
WHITE = "FFFFFF"
ORANGE = "FF6B35"
ORANGE2 = "FF9B66"
BLUE = "72B7D6"
GREEN = "76B900"
GREEN2 = "7BC6A4"
RED = "F47C7C"
LINE = "D8DEE3"


blocks = []


def add(kind, text, **meta):
    blocks.append({"kind": kind, "text": text, **meta})


def bullets(items):
    add("bullets", "\n".join(items))


def numbers(items):
    add("numbers", "\n".join(items))


def diagram(key, caption):
    add("diagram", f"[[DIAGRAM:{key}]]", key=key)
    add("caption", caption)


add("title", "HGX-Runner")
add("subtitle", "Merging MAC control semantics, Literate AI derivation, and HGX capacity")
add("meta", "Architecture refresh | Version 2.0 | 10 August 2026")
add(
    "lead",
    "Decision: Make HGX-Runner the durable, organization-scale control plane. Port MAC's validated task, allocator, fencing, evidence, review, sandbox, publication, and operational-learning semantics into HGX; migrate compatible data with behavioral parity; cut traffic over; and retire MAC. Integrate Literate AI as the exact derivation and qualification engine, while the existing HGX/Horde session platform remains the elastic capacity substrate beneath the unified service.",
)
add(
    "link",
    "Source document | HGX / Horde repository",
    links=[
        (
            "Source document",
            f"https://docs.google.com/document/d/{CANONICAL_DOCUMENT_ID}/edit?tab=t.0",
        ),
        ("HGX / Horde repository", "https://github.com/NVIDIA-Omniverse/ov-agent-farm"),
    ],
)
diagram(
    "AUTHORITY",
    "Figure 1. Target authority map: MAC feeds the HGX-Runner control plane and retires after verified cutover.",
)

add("h1", "1. Executive summary")
add(
    "p",
    "The earlier proposal correctly chose HGX-Runner as the destination and MAC as the migration source, but it now understates what must be absorbed. MAC has become a multi-user, PostgreSQL-backed fleet control plane with one authoritative hub allocator, first-class projects and task groups, caller and agent ownership, durable leases, task-event streaming, structured AgentBus coordination, project-scoped sandbox egress, exact evidence and review gates, managed repository references, and bounded HGX autoscaling. These are no longer isolated experiments; they are the current parity baseline for the HGX migration.",
)
add(
    "p",
    "Literate AI has also moved far beyond a basic specification-to-code prototype. Its current Standard core plans arbitrary Component DAGs before model execution; binds exact Components, Flavors, Skills, Workflows, routes, models, authored assets, and public interfaces; schedules provider-before-consumer layers with bounded concurrency; preserves source and build custody; requires a current CodeGraph sidecar; emits pre-build and post-build CycloneDX evidence; separates build intent from authorization; reruns current trust gates on cache hits; and keeps source-to-specification promotion behind independent, current qualification.",
)
add(
    "p",
    "HGX 0.8.0, as implemented by the Horde DGXC repository, is currently an authenticated API-only client for creating and operating GPU-backed development sessions. It owns session, workspace, profile, resource, SSH, transfer, forwarding, log, event, stop/resume, and deletion operations. HGX-Runner extends that platform upward with the MAC-derived durable work control plane. During migration, an HGX session is still only provider capacity: it is not a task lease, reviewer, or completion proof until the unified service onboards and fences it.",
)
add("h2", "The operating thesis")
bullets(
    [
        "HGX-Runner answers: who asked, what work exists, which worker may act, under which runtime boundary, what evidence arrived, who reviewed it, and whether accepted work reached the canonical repository. MAC supplies the proven semantics and migration data for this authority.",
        "Literate AI answers: what exact behavior and target are authoritative, which derivation inputs and model route apply, what source/build/test/acceptance evidence belongs to the exact content, and whether source has earned regenerative authority.",
        "The existing HGX/Horde substrate answers: which authenticated session exists, where it runs, which resources and workspace profile it has, and how the unified controller reaches and operates it.",
        "Git repositories remain authoritative for accepted code and project specifications. Large generated artifacts and forensic journals belong in immutable content-addressed storage, referenced by identity from both control planes.",
    ]
)

add("h1", "2. What changed since Version 1.1")
add("h2", "Implemented in MAC and therefore mandatory migration parity")
bullets(
    [
        "Global ledger baseline: PostgreSQL is MAC's supported control-plane authority; spokes are API clients and do not keep private ledgers. HGX-Runner must preserve this single-authority property.",
        "Authoritative allocation: one hub-owned allocator computes legal task-agent pairs and atomically creates the fenced lease in the same round.",
        "First-class identity: the hub binds caller identity, records the filer, assigns human owners, and keeps privately owned hardware out of general fleet capacity.",
        "Task groups and control: first-class groups, bulk selection, joins, event following, opt-in decomposition, bounded retries, and in-flight AgentBus messages are durable surfaces.",
        "Sandbox contract: project-declared egress, reviewed allowlists, runtime BOMs, drained-worker rollouts, and separation of image build from certification now exist.",
        "Literate Standard core: exact per-Component planning, bounded source generation, typed build boundaries, CodeGraph, SBOMs, current acceptance, source caching, and receipts exist.",
        "HGX capacity controller: MAC already converts durable no-eligible-agent demand into bounded HGX session creation and nonce SSH attestation; this loop moves into HGX-Runner rather than surviving as an external MAC dependency.",
    ]
)
add("h2", "Partial or proposed")
bullets(
    [
        "Partial: Standard-bound Literate projects use the ordinary lifecycle; some repository samples and qualification paths still retain transitional drivers.",
        "Proposed: the portable HGX-Runner/Literate run envelope that exchanges exact semantic identities is still a design contract, not a finished production protocol.",
        "Proposed: pinned baseline vectors, cohort barriers, ordered multi-repository landing, and compensation remain new capability, not an implemented property of current work packages.",
    ]
)
add(
    "p",
    "This status discipline is load-bearing. A polished diagram does not promote a roadmap item into current capability.",
)

add("h1", "3. Authority boundaries and the dual-key rule")
add(
    "p",
    "The fusion succeeds only if the three systems remain independently usable and no system treats another system's record as a universal grant. Organizational consent, fleet execution privilege, semantic build authorization, and artifact acceptance are different questions.",
)
diagram(
    "DUAL_KEY",
    "Figure 2. One execution requires two independent grants: fleet permission and content-bound authorization.",
)
add("h2", "Key 1: HGX-Runner's fleet execution grant")
add(
    "p",
    "A task in the ledger proves that authorized work was requested; it is not permission to run arbitrary commands. HGX-Runner must port MAC's narrowing sequence: route scope, actor binding, tenant and agent ownership, allocator eligibility, a lease and fence, OpenShell policy, project egress, device/resource availability, and only when explicitly authorized, a single-use lease-bound break-glass record.",
)
add("h2", "Key 2: Literate AI's semantic operation grant")
add(
    "p",
    "Literate AI fixes the operation before generated bytes exist. A BuildRequestDeclaration names builder, toolchain, sandbox class, privileges, and outputs but cannot name a future source bundle. After the exact generated tree, current test manifest, source SBOM, and CodeGraph evidence pass admission, the lifecycle realizes a source-bound request and obtains a current authorization binding the exact intent, request, and index.",
)
add("h2", "The composed invariant")
add(
    "p",
    "An HGX-Runner worker may launch the Literate lifecycle only inside its current lease and confinement envelope. The Literate lifecycle may execute a content-bound build or test only when its own exact authorization passes. The resulting evidence returns to HGX-Runner as identities and typed relationships. HGX-Runner review and publication policy then decide whether that accepted derivation satisfies the organizational task. MAC follows this rule during migration; the final authority is HGX-Runner.",
)

add("h1", "4. MAC: the operational kernel to absorb and retire")
add(
    "p",
    "MAC is not the target runtime. It is the current executable reference, data source, fixture corpus, and behavioral oracle for HGX-Runner. Every capability below must be inventoried as a versioned contract, ported behind HGX APIs, validated with shared parity tests and shadow reads, migrated through stable identity mappings, and removed from MAC only after cutover and recovery are proven.",
)
add("h2", "Projects, tasks, groups, and actors")
add(
    "p",
    "In MAC today, a project is the scheduling and policy boundary around related work and may bind a repository contract. A task is a durable unit of intent. New tasks may be staged with no_dispatch and released later; project pause is a separate gate. The submitter can choose whether decomposition occurs. Task groups provide a first-class collection and join surface without forcing every request into a heavyweight work package. HGX-Runner should port these semantics rather than invent a second lifecycle.",
)
bullets(
    [
        "Caller identity is bound at the hub; the ledger records who filed the work.",
        "Agents have owners, and privately owned hardware is not advertised as general fleet capacity.",
        "Free-text tasks remain a supported fast lane. Current evidence does not justify forcing every task into a Component transition.",
        "Optional structured action fields are a pilot seam for work that truly is a Component lifecycle transition.",
    ]
)
add("h2", "One allocator, one atomic claim")
add(
    "p",
    "MAC's hub now owns the complete scheduling snapshot: runnable task state, canonical dependency joins, project activation, task holds, attempt budget, agent health and capacity, machine trust, tenant authorization, platform and hardware requirements, and explicit target-agent restrictions. The allocator's successful output is an atomic task-and-agent lease, not an advisory candidate list. This hub-owned atomic-claim contract is a hard HGX-Runner parity gate.",
)
add(
    "p",
    "Hard constraints fail closed. Repository locality, warm caches, prior success, load balance, reviewer diversity, and preferred tools are affinities. Missing checkouts, stale caches, or installable tools are repair observations rather than silent reasons to strand work. Allocation decisions and no-match reasons are retained for diagnosis and autoscaling.",
)
add("h2", "Evidence, review, and canonical completion")
add(
    "p",
    "MAC currently separates execution from acceptance. Workers submit structured evidence under a fenced attempt. An independent reviewer evaluates that exact attempt and its evidence. Repository tasks additionally require remotely verified proof that the accepted commit reached the canonical branch. HGX-Runner must reproduce this behavior before MAC writers can be cut over; a worker's statement that tests passed or that it pushed is never sufficient by itself.",
)
add("h2", "Runtime containment and project policy")
add(
    "p",
    "OpenShell is part of the execution contract. The project declares intended egress; the hub combines it with a reviewed registry allowlist. Repository-controlled configuration is untrusted and cannot grant network access. The sandbox image is derived from a versioned BOM, built separately from certification, and rolled out to drained workers so a mutable image tag cannot silently change a live execution boundary.",
)
add("h2", "Coordination and observability")
add(
    "p",
    "mac task wait and the hub event-follow stream support durable completion waiting without polling every object. AgentBus provides bounded, typed, self-only inbox delivery to an agent that is already working; it surfaces at harness step boundaries and does not pretend to interrupt an in-flight tool call. Throughput spans, stranded-work episodes, resource contention, queue latency, review time, rework, and canonical publication provide the operational scorecard.",
)

add("h1", "5. Literate AI: exact derivation and qualification")
add(
    "p",
    "Literate AI treats readable specifications as application authority and generated source as disposable output only after the evidence justifies that status. It is provider-neutral: Components define observable behavior and public contracts; Flavors carry target variance; Skills carry reusable conversion practice; Workflows define stage order; routing policies constrain eligible models and tools.",
)
diagram(
    "LIFECYCLE",
    "Figure 3. Exact planning and source reuse feed a complete set of current trust gates.",
)
add("h2", "Exact plans before model egress")
add(
    "p",
    "A locked project becomes a ComponentExecutionPlan before any coding model runs. Arbitrary acyclic Component graphs become deterministic provider-before-consumer layers. Independent nodes in a layer may run concurrently up to an explicit bound; output order remains canonical. Missing public-interface contracts, open graphs, cycles, or mismatched locks fail as stable planning errors.",
)
add("h2", "Narrow generation keys and bounded invalidation")
add(
    "p",
    "A ComponentGenerationKey binds the node's ordered specification set, target and Flavors, exact Skills, Workflow, routing and model choice, authored assets, its own exported interfaces, and the direct public interfaces it consumes. It deliberately excludes a dependency's private specifications, source, tests, build output, route, and skills. That boundary preserves reuse without leaking private dependency authority into a consumer prompt.",
)
add("h2", "Cache hits do less work, not less verification")
add(
    "p",
    "An accepted-source hit may skip current model generation, but it produces a new candidate projection and reruns current indexing, authorization, build, generated tests, execution, and independent acceptance. A hit is not republished as new work and historical runtime measurements are not charged to the new attempt.",
)
add("h2", "CodeGraph and CycloneDX evidence")
add(
    "p",
    "Every canonical project and generated tree requires current CodeGraph evidence before lifecycle effects. The pre-build CycloneDX 1.7 document binds the managed Component and repository-source graph. Post-build evidence must preserve that graph, bind the pre-build identity, and may only add or resolve what observation proves. An SBOM is inventory evidence, not a vulnerability verdict, license approval, signature, or safety claim.",
)
add("h2", "Source-to-specification and regenerative authority")
add(
    "p",
    "Inverse derivation indexes an inert, classified mirror and emits reviewable specifications, Component graphs, Flavors, and complete model-call journals without executing the analyzed source. Human review may accept intent authority. Release implementation authority remains with the exact source baseline until clean empty-workspace regenerations, current tests, independent parity checks, complete surface coverage, and trusted attestations satisfy policy. Historical v1 qualification remains explicitly non-authorizing.",
)
add("h2", "Learning changes authority only through review")
add(
    "p",
    "litai learn can classify typed evidence into the narrowest proposed owner: Component, Flavor, Skill, Workflow, routing policy, or framework rule. It is read-only today. A retry repairs one candidate; a reviewed and pinned authority change is what teaches future derivations. MAC retains recurrence and organizational context during migration; HGX-Runner assumes that role after cutover. Neither ledger may silently edit a generation input.",
)

add("h1", "6. HGX today and the HGX-Runner target")
add(
    "p",
    "The current HGX CLI is an API-only client over Horde DGXC. It authenticates with a registered SSH key and operates sessions without direct kubectl, kubeconfig, or cluster permissions. It supports create, list, info, status, wait, logs, events, SSH, SCP, port forwarding, desktop and Companion launch, checkpoint, stop, resume, delete, workspace management, keys, GPU availability, secrets, and diagnostics. This is the existing infrastructure surface on which HGX-Runner can build; it is not yet the durable organization-scale task ledger described by this proposal.",
)
add(
    "p",
    "The target HGX-Runner service combines that session and workspace substrate with the MAC-derived project, task, group, allocator, lease, evidence, review, sandbox-policy, publication, audit, and learning contracts. The migration must converge on one HGX-owned ledger and API. A permanent HGX-to-MAC facade or dual-write design would preserve two authorities and is explicitly not the destination.",
)
diagram(
    "CAPACITY",
    "Figure 4. Session creation becomes schedulable capacity only after attestation and explicit onboarding.",
)
add("h2", "Bounded scale-up")
add(
    "p",
    "MAC's current provider controller counts only actionable, task-bound no-eligible-agent requests. Terminal or already assigned tasks are cancelled from demand; reviewer and specialized service-role shortages do not create generic coding workers. Desired capacity is bounded by provider-wide max sessions, stabilization time, per-step creation, and cooldown. HGX-Runner should internalize this exact demand-to-capacity contract. Quota errors remain visible and are never misreported as satisfied demand.",
)
add("h2", "Readiness is not creation")
add(
    "p",
    "A successful hgx create returns an immutable provider session ID; it does not prove readiness. The controller waits for status and requires an unpredictable nonce to round-trip through hgx ssh addressed by that immutable ID. Failed sessions remain classified provider failures and do not count toward ready supply.",
)
add("h2", "Onboarding is the trust boundary")
add(
    "p",
    "After attestation the current controller stops at prepare_fungible_onboarding. A reviewed operator or automation path supplies the fleet name, hub agent identity, placeholder state, endpoint-bound worker credentials, and authoritative fleet record. Only then can an agent heartbeat, advertise verified capabilities, receive a lease, and satisfy provisioning demand. HGX-Runner should make this one native onboarding transaction while preserving the trust boundary.",
)
add("h2", "Retirement remains intentionally conservative")
add(
    "p",
    "The current autoscaler may retire only old, surplus, controller-created sessions that never became registered MAC agents. Onboarded workers require a multi-resource lifecycle: drain leases, revoke credentials, tombstone the agent, update the fleet registry, and then delete the provider session. Until that transaction exists, automatic deletion fails closed.",
)

add("h1", "7. The HGX-Runner / Literate AI integration contract")
add(
    "p",
    "The target system needs one portable request/result envelope, not a shared internal schema. HGX-Runner launches a Literate derivation as a fenced task operation and receives a trusted result envelope containing semantic identities. During migration MAC may originate the same envelope for parity testing, but the production endpoint moves to HGX and the MAC route is retired. Large blobs remain in immutable storage. Each system stores only the relations it owns.",
)
diagram(
    "JOIN",
    "Figure 5. A minimal HGX-Runner/Literate identity join exchanges operational context and content-bound result identities.",
)
add("h2", "Request envelope")
bullets(
    [
        "HGX-Runner task, task-group, project, actor, tenant, and correlation identifiers, with mapped MAC source IDs retained only as migration provenance.",
        "Exact repository URL, ref and base SHA, or accepted specification baseline.",
        "Requested Component coordinate, transition, and terminal outcome when structured; otherwise a bounded free-text objective.",
        "Current agent, lease and fence, runtime policy, project egress, device and resource grant, deadline, and cancellation channel.",
        "Retention and disclosure policy, including whether model egress is allowed.",
    ]
)
add("h2", "Result envelope")
bullets(
    [
        "Exact Component lock, execution plan, generation-key, Skill, Workflow, route, model, and tool identities.",
        "Prompt, response, and decision blob identities visible at the framework boundary; no claim to private provider chain of thought.",
        "Generated tree, source bundle, CodeGraph, source and resolved SBOM, build, test, execution, acceptance, cache-membership, artifact, and receipt identities.",
        "Terminal outcome, bounded diagnostics, and identity of the complete retained journal.",
    ]
)
add("h2", "Identity exclusion rule")
add(
    "p",
    "Task IDs, user IDs, lease IDs, hostnames, and mutable URLs are correlation metadata. They do not enter generation, source-cache, build-cache, or artifact identity. Retrying the same exact semantic derivation under another task must not create different content merely because the operational wrapper changed.",
)

add("h1", "8. Optional tasks as actions on Components")
add(
    "p",
    "MAC's current work-package node already carries a deterministic structure axis: node kind, expected outputs, and declared read/write/exclusive/external effects. HGX-Runner should port that contract, then add Literate AI's orthogonal domain axis: Component, exact revision, lifecycle transition, and target state. The seam is additive:",
)
numbers(
    [
        "Add an optional action object to one real task or work-package workflow: Component, exact revision, transition, and target state.",
        "Derive resource effects and lifecycle dependencies from that transition where the mapping is closed and reviewed.",
        "Define completion as the exact terminal lifecycle evidence existing and being accepted under MAC policy.",
        "Measure completion yield, rework, latency, and stranding against the free-text cohort before expanding adoption.",
    ]
)
add(
    "p",
    "This must remain optional. Many valuable tasks are investigations, fleet operations, migration coordination, measurements, broad refactors, or incident response and do not reduce to one Component transition. MAC's own production data found direct human tasks outperforming machine-generated dependency trees. Structure is useful only where it removes ambiguity without manufacturing fragile work.",
)

add("h1", "9. End-to-end operating flow")
numbers(
    [
        "A human, API, or approved adapter files an HGX-Runner task. The service binds the actor, tenant, origin, project, owner, dispatch hold, and optional task group. During cutover, migrated MAC identities remain durable provenance rather than a second writable ledger.",
        "HGX-Runner resolves repository and policy context, estimates scope, and either uses the free-text fast lane or validates an optional Component action.",
        "The MAC-derived authoritative allocator matches the task against healthy capacity. If no legal worker exists, it records durable demand; sustained eligible demand may trigger one bounded HGX scale-up step.",
        "The HGX provider creates the requested session. HGX-Runner addresses it by immutable ID, verifies status, and attests SSH with a nonce. A separate onboarding transaction registers the real agent and credentials.",
        "HGX-Runner atomically creates the agent lease and launches the task inside the current OpenShell runtime, project egress, hardware, and cancellation envelope.",
        "For a Literate task, the worker verifies project authority, locks, and CodeGraph; produces the exact plan; then executes the Standard lifecycle under the dual-key rule.",
        "Literate AI returns content and evidence identities. HGX-Runner stores the run-envelope relation, structured evidence, bounded diagnostics, and operational timeline, not duplicate source trees or private prompts in task metadata.",
        "An independent HGX-Runner reviewer evaluates the exact executor attempt and the Literate acceptance evidence. Rejection preserves evidence and returns bounded rework; approval authorizes the next policy stage.",
        "Repository publication uses the task branch and canonical remote contract. Completion requires remote verification of the accepted commit; moving bases or conflicting refs cause rebase and review or explicit failure, never an implied atomic write.",
        "Operational learning remains secret-free and evidence-scoped. Reusable semantic lessons become Literate proposals and affect future generation only after review, validation, refreshed locks, and clean rebuilds.",
    ]
)

add("h1", "10. Failure handling and negative paths")
add("h2", "No eligible agent")
add(
    "p",
    "Keep the task open and explain the missing hard capability. Create one task-bound provisioning request. Stabilize demand before scaling. Never count a created or merely SSH-reachable HGX session as a task-capable agent until onboarding and heartbeat prove the boundary.",
)
add("h2", "Lease loss or worker failure")
add(
    "p",
    "Fence the stale attempt, retain output and command/evidence artifacts, classify the cause, and prefer another compatible worker for the single narrow retry. The same deterministic failure fingerprint twice stops. Bare lease expiry without useful telemetry remains a visible failure rather than generating recursive repair noise.",
)
add("h2", "Literate planning or lifecycle failure")
add(
    "p",
    "A graph, lock, interface, budget, CodeGraph, SBOM, authorization, build, test, execution, or acceptance failure blocks downstream authority. Dependents are cancelled before their adapters run; independent branches may finish. Repair the narrow authority owner or failed stage, then rerun current gates.",
)
add("h2", "Cache or custody mismatch")
add(
    "p",
    "Fail closed before reuse. A cache entry grants only eligibility to materialize immutable source. It cannot carry forward current authorization, acceptance, or publication authority.",
)
add("h2", "Canonical repository moved")
add(
    "p",
    "The landing controller compares expected base and candidate identity against the canonical remote. If the compare-and-swap premise fails, no completion proof is recorded. Rebase, repeat required tests and review, or replan.",
)
add("h2", "Partial cross-repository landing")
add(
    "p",
    "Current MAC work packages are repository-scoped and the full cohort protocol is proposed. Until implemented, do not describe multiple Git writes as atomic. A future cohort must pin a baseline vector, compatibility edges, readiness barrier, landing order, publication receipts, and compensate, roll-forward, or replan policy.",
)

add("h1", "11. Delivery roadmap")
diagram(
    "ROADMAP",
    "Figure 6. A staged integration plan preserves today's authority and measures each new seam.",
)
add("h2", "Milestone 0: freeze the target and parity inventory")
bullets(
    [
        "Keep HGX-Runner as the only target durable ledger and MAC as the migration source to retire.",
        "Inventory MAC schemas, state transitions, allocator decisions, fencing, task groups, identity, sandbox, evidence, review, publication, telemetry, learning, fixtures, and recovery behavior.",
        "Define stable MAC-to-HGX identity mappings, version transforms, retention, rollback, and parity tests before moving writers.",
    ]
)
add("h2", "Milestone 1: port the MAC kernel into HGX")
bullets(
    [
        "Implement HGX-owned projects, tasks, groups, canonical dependency joins, caller/owner/tenant identity, authoritative allocation, atomic leases, attempts, evidence, review, and canonical publication.",
        "Port project-scoped egress, OpenShell and break-glass boundaries, sandbox BOM and certification, ref reconciliation, event streams, AgentBus, retries, throughput, and operational learning.",
        "Run shared fixtures and behavioral parity tests against MAC and HGX implementations; treat any disagreement as a cutover blocker.",
    ]
)
add("h2", "Milestone 2: migrate data, shadow, cut over, and retire MAC")
bullets(
    [
        "Replayably backfill compatible MAC records into HGX with source IDs and mapping provenance; shadow-read and compare state, allocation, review, and completion decisions.",
        "Rehearse rollback, freeze MAC writers, cut writers and readers to HGX, verify audit and recovery, then decommission MAC services and credentials.",
        "Avoid a prolonged dual-write period. Any temporary bridge must have an owner, sunset condition, and one declared authority for each mutable fact.",
    ]
)
add("h2", "Milestone 3: Literate integration and Component-action pilot")
bullets(
    [
        "Define the versioned HGX-Runner/Literate request and result envelopes, identity exclusions, content-addressed references, attestation, replay, cancellation, and bounded diagnostics.",
        "Execute one real Component lifecycle inside an HGX-Runner-leased OpenShell worker and reconcile the task, Literate receipt, canonical SHA, and artifact identities.",
        "Add the optional structured action to one bounded workflow and derive effects where exact.",
        "Compare yield, latency, rework, and stranding with equivalent free-text tasks.",
        "Expand only if the measured cohort improves useful canonical outcomes.",
    ]
)
add("h2", "Milestone 4: cross-repository cohort")
bullets(
    [
        "Introduce a first-class cohort identity and baseline vector without changing per-repository Git authority.",
        "Require compatibility evidence and a ready-to-land barrier, then ordered compare-and-swap publication.",
        "Exercise a forced partial landing and prove compensation, roll-forward, or replan behavior before production claims.",
    ]
)

add("h1", "12. Success criteria")
bullets(
    [
        "One user request produces an HGX-Runner task or group whose actor, tenant, owner, project, policy, lease, and execution history are durable and queryable, with any migrated MAC identity retained as provenance only.",
        "One exact Literate Component plan executes inside that lease; all derivation, CodeGraph, SBOM, build, test, acceptance, and receipt identities survive process restart and reconcile after read-back.",
        "A cache-hit demonstration skips generation while rerunning every current trust gate.",
        "A no-eligible-agent demonstration creates bounded provider capacity, attests by immutable session ID, onboards explicitly into HGX-Runner, and then receives an atomic lease.",
        "A failed gate, lease loss, moving canonical base, and failed HGX attestation each produce bounded, attributable evidence without unsafe publication or recursive work generation.",
        "The operator dashboard reports useful outcomes reaching canonical refs, not merely tasks started, model calls made, sessions created, or internal state transitions completed.",
    ]
)

add("h1", "Appendix A. Current capability boundaries")
add("h2", "Implemented today")
bullets(
    [
        "MAC migration baseline: PostgreSQL hub authority; projects, tasks, groups, actors and owners; authoritative allocation and fenced leases; evidence, independent review, publication proof; OpenShell policy and project egress; sandbox BOM rollout; AgentBus; event following; throughput and learning records; bounded HGX capacity control.",
        "Literate AI: exact locks and plans; arbitrary Component DAGs; bounded per-node generation; narrow cache keys; source custody and current revalidation; CodeGraph preflight; source/resolved CycloneDX evidence; typed build intent and authorization; generated tests, execution, independent acceptance, project admission and receipts; inert source-to-specification drafting and gated qualification machinery.",
        "HGX/Horde: authenticated session lifecycle; GPU, CPU, memory, and profile selection; persistent workspaces; status, wait, logs, events; SSH, SCP and forwarding; stop, resume, checkpoint, and delete; user-facing UI and API-managed cluster access.",
    ]
)
add("h2", "Not yet safe to claim")
bullets(
    [
        "A completed production HGX-Runner/Literate run-envelope protocol with end-to-end replay.",
        "Universal production containment inside Literate AI; its repository states that containment backends are architecture contracts and transitional host execution still exists.",
        "Automatic semantic authority transfer from existing source without current trusted qualification.",
        "A production-enabled general MAC work-package assembly line across the fleet; activation remains gated.",
        "Atomic or fully coordinated multi-repository landing.",
        "Automatic retirement of onboarded HGX-Runner workers.",
        "A proven productivity or ROI percentage. The system should be judged on measured canonical outcomes, latency, cost, failures, and reuse.",
    ]
)

add("h1", "Appendix B. Source ledger")
add(
    "p",
    "Claims were refreshed against current repository authority and implementation on 10 August 2026. The principal sources are listed so future revisions can repeat the audit.",
)
add("h2", "MAC")
bullets(
    [
        "README.md; AGENTS.md; docs/book/01-system.md; 03-projects-and-tasks.md; 05-evidence-review-completion.md; 13-deployment-topologies.md; 15-sandboxed-runtimes.md; 17-learning-evals-scaling.md.",
        "docs/authority-boundary.md; docs/adr/0013-authoritative-hub-allocator.md; docs/work-graph-control-plane.md; docs/structured-task-bodies.md; docs/hgx-elastic-capacity.md; docs/in-flight-agent-messages.md; docs/fleet-operational-learning.md; docs/task-throughput-observability.md; docs/repository-ref-hygiene.md.",
    ]
)
add("h2", "Literate AI")
bullets(
    [
        "SKILL.md; skills/agent/author-presentations-and-documents/SKILL.md; docs/user/framework-flow.md; docs/user/caches-packages-publication.md.",
        "docs/architecture/agent-ledger-boundary.md; component-execution-plans.md; component-authoring-lock-boundary.md; source-promotion.md; sbom-and-dependency-graph.md; authority-learning-loop.md; user-directed-work-loop.md; documentation-artifacts.md.",
        "docs/presentations/literate-ai-manager-overview/source-notes.md and deck-specification.md, 2026-08-10 edition.",
    ]
)
add("h2", "HGX / Horde DGXC")
bullets(
    ["README.md; skills/hgx-cli.md; scripts/hgx (version 0.8.0); DESIGN.md; PRODUCTION-DESIGN.md."]
)


DIAGRAMS = {
    "AUTHORITY": {
        "rows": 1,
        "cols": 4,
        "cells": [
            (
                "HGX-RUNNER TARGET\nGlobal tasks, tenants, allocator, leases, confinement, review, publication, audit",
                PANEL,
                ORANGE2,
            ),
            ("MAC -> HGX\nPort contracts and data; prove parity; cut over; retire MAC", PANEL, RED),
            (
                "LITERATE AI\nComponents, Flavors, exact plans, generation keys, build intent, SBOMs, acceptance",
                PANEL,
                BLUE,
            ),
            (
                "GIT + CAS\nAccepted code and specs; immutable trees, prompts, receipts, and evidence blobs",
                FOG,
                INK,
            ),
        ],
        "height": 126,
    },
    "DUAL_KEY": {
        "rows": 1,
        "cols": 3,
        "cells": [
            (
                "KEY 1 | HGX-RUNNER\nActor + lease + host + sandbox + egress + devices + time",
                PANEL,
                ORANGE2,
            ),
            ("BOTH KEYS\nrequired for one exact execution", ORANGE, INK),
            (
                "KEY 2 | LITERATE AI\nComponent + source + index + build intent + privileges + outputs",
                PANEL,
                BLUE,
            ),
        ],
        "height": 118,
    },
    "LIFECYCLE": {
        "rows": 2,
        "cols": 4,
        "cells": [
            ("1 | AUTHORITY\nSpecs + Flavors + Skills + Workflow", PANEL, ORANGE2),
            ("2 | LOCK + PLAN\nExact graph, keys, layers, budgets", PANEL, BLUE),
            ("3 | SOURCE\nGenerate or materialize an untrusted candidate", PANEL, BLUE),
            ("4 | INDEX + ADMIT\nCodeGraph + source SBOM", PANEL, GREEN2),
            ("5 | AUTHORIZE\nIntent + request + exact index", PANEL, ORANGE2),
            ("6 | BUILD\nResolved SBOM + realized exports", PANEL, GREEN2),
            ("7 | VERIFY\nTests + execute + independent acceptance", PANEL, GREEN),
            ("8 | ADMIT\nProject receipt + artifact custody", PANEL, ORANGE2),
        ],
        "height": 94,
    },
    "CAPACITY": {
        "rows": 2,
        "cols": 3,
        "cells": [
            ("1 | NO ELIGIBLE AGENT\nTask-bound durable demand", PANEL, RED),
            ("2 | STABILIZE + BOUND\nStep, cooldown, quota", PANEL, ORANGE2),
            ("3 | HGX CREATE\nProfile, GPU, CPU, memory", PANEL, GREEN2),
            ("4 | ATTEST\nImmutable ID + nonce SSH", PANEL, BLUE),
            ("5 | ONBOARD\nCredentials + fleet registry", PANEL, ORANGE2),
            ("6 | ALLOCATE\nAtomic task + agent lease", PANEL, GREEN),
        ],
        "height": 94,
    },
    "JOIN": {
        "rows": 1,
        "cols": 2,
        "cells": [
            (
                "HGX-RUNNER REQUEST ->\nTask, tenant, actor, baseline, lease, sandbox grant, correlation, retention, requested outcome",
                PANEL,
                ORANGE2,
            ),
            (
                "<- LITERATE RESULT ENVELOPE\nComponent lock, plan, skills/models, tree, CodeGraph, SBOM, build, test, acceptance, journal identities",
                PANEL,
                BLUE,
            ),
        ],
        "height": 128,
    },
    "ROADMAP": {
        "rows": 1,
        "cols": 4,
        "cells": [
            (
                "INVENTORY\nFreeze HGX target; map MAC contracts, data, identities, and parity tests",
                PANEL,
                GREEN,
            ),
            (
                "PORT\nImplement MAC semantics behind HGX APIs and one PostgreSQL authority",
                PANEL,
                ORANGE2,
            ),
            ("CUTOVER\nBackfill, shadow, prove parity, switch traffic, retire MAC", PANEL, BLUE),
            ("EXTEND\nLiterate join and cross-repo cohorts with explicit recovery", PANEL, ORANGE2),
        ],
        "height": 126,
    },
}


def color_style(hex_value):
    return {"color": {"rgbColor": rgb(hex_value)}}


def make_text_and_ranges():
    pieces = []
    ranges = []
    cursor = 1
    for block in blocks:
        text = block["text"] + "\n"
        start = cursor
        end = cursor + len(text)
        ranges.append({**block, "start": start, "end": end})
        pieces.append(text)
        cursor = end
    return "".join(pieces), ranges


def paragraph_style_request(start, end, style, fields):
    return {
        "updateParagraphStyle": {
            "range": {"startIndex": start, "endIndex": end, "tabId": TAB_ID},
            "paragraphStyle": style,
            "fields": fields,
        }
    }


def text_style_request(start, end, style, fields):
    return {
        "updateTextStyle": {
            "range": {"startIndex": start, "endIndex": end, "tabId": TAB_ID},
            "textStyle": style,
            "fields": fields,
        }
    }


def replace_body(doc_id):
    doc = get_doc(doc_id)
    body = doc["tabs"][0]["documentTab"]["body"]
    end_index = body["content"][-1]["endIndex"]
    full_text, ranges = make_text_and_ranges()
    requests = []
    if end_index > 2:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1, "tabId": TAB_ID}
                }
            }
        )
    requests.append({"insertText": {"location": {"index": 1, "tabId": TAB_ID}, "text": full_text}})
    requests.append(
        text_style_request(
            1,
            1 + len(full_text),
            {
                "weightedFontFamily": {"fontFamily": "Arial"},
                "fontSize": {"magnitude": 11, "unit": "PT"},
                "foregroundColor": color_style("000000"),
            },
            "weightedFontFamily,fontSize,foregroundColor",
        )
    )

    for item in ranges:
        kind, start, end = item["kind"], item["start"], item["end"]
        if kind == "title":
            requests.append(
                text_style_request(
                    start,
                    end - 1,
                    {
                        "weightedFontFamily": {"fontFamily": "Arial", "weight": 400},
                        "fontSize": {"magnitude": 26, "unit": "PT"},
                        "foregroundColor": color_style("000000"),
                    },
                    "weightedFontFamily,fontSize,foregroundColor",
                )
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {"spaceBelow": {"magnitude": 3, "unit": "PT"}, "lineSpacing": 100},
                    "spaceBelow,lineSpacing",
                )
            )
        elif kind == "subtitle":
            requests.append(
                text_style_request(
                    start,
                    end - 1,
                    {
                        "fontSize": {"magnitude": 16, "unit": "PT"},
                        "foregroundColor": color_style("434343"),
                    },
                    "fontSize,foregroundColor",
                )
            )
            requests.append(
                paragraph_style_request(
                    start, end, {"spaceBelow": {"magnitude": 8, "unit": "PT"}}, "spaceBelow"
                )
            )
        elif kind == "meta":
            requests.append(
                text_style_request(
                    start,
                    end - 1,
                    {
                        "fontSize": {"magnitude": 10, "unit": "PT"},
                        "foregroundColor": color_style(STEEL),
                    },
                    "fontSize,foregroundColor",
                )
            )
            requests.append(
                paragraph_style_request(
                    start, end, {"spaceBelow": {"magnitude": 14, "unit": "PT"}}, "spaceBelow"
                )
            )
        elif kind == "lead":
            requests.append(
                text_style_request(start, min(start + 9, end - 1), {"bold": True}, "bold")
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {"spaceBelow": {"magnitude": 12, "unit": "PT"}, "lineSpacing": 110},
                    "spaceBelow,lineSpacing",
                )
            )
        elif kind == "link":
            requests.append(
                paragraph_style_request(
                    start, end, {"spaceBelow": {"magnitude": 12, "unit": "PT"}}, "spaceBelow"
                )
            )
            for label, url in item.get("links", []):
                offset = item["text"].find(label)
                requests.append(
                    text_style_request(
                        start + offset,
                        start + offset + len(label),
                        {
                            "link": {"url": url},
                            "foregroundColor": color_style("1155CC"),
                            "underline": True,
                        },
                        "link,foregroundColor,underline",
                    )
                )
        elif kind == "h1":
            requests.append(
                text_style_request(
                    start,
                    end - 1,
                    {
                        "fontSize": {"magnitude": 20, "unit": "PT"},
                        "bold": False,
                        "foregroundColor": color_style("000000"),
                    },
                    "fontSize,bold,foregroundColor",
                )
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {
                        "namedStyleType": "HEADING_1",
                        "spaceAbove": {"magnitude": 20, "unit": "PT"},
                        "spaceBelow": {"magnitude": 6, "unit": "PT"},
                        "keepWithNext": True,
                    },
                    "namedStyleType,spaceAbove,spaceBelow,keepWithNext",
                )
            )
        elif kind == "h2":
            requests.append(
                text_style_request(
                    start,
                    end - 1,
                    {
                        "fontSize": {"magnitude": 16, "unit": "PT"},
                        "bold": False,
                        "foregroundColor": color_style("000000"),
                    },
                    "fontSize,bold,foregroundColor",
                )
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {
                        "namedStyleType": "HEADING_2",
                        "spaceAbove": {"magnitude": 18, "unit": "PT"},
                        "spaceBelow": {"magnitude": 6, "unit": "PT"},
                        "keepWithNext": True,
                    },
                    "namedStyleType,spaceAbove,spaceBelow,keepWithNext",
                )
            )
        elif kind == "p":
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {"spaceBelow": {"magnitude": 8, "unit": "PT"}, "lineSpacing": 110},
                    "spaceBelow,lineSpacing",
                )
            )
        elif kind == "bullets":
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": {"startIndex": start, "endIndex": end, "tabId": TAB_ID},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                }
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {
                        "spaceBelow": {"magnitude": 4, "unit": "PT"},
                        "lineSpacing": 110,
                        "indentStart": {"magnitude": 36, "unit": "PT"},
                        "indentFirstLine": {"magnitude": 18, "unit": "PT"},
                    },
                    "spaceBelow,lineSpacing,indentStart,indentFirstLine",
                )
            )
        elif kind == "numbers":
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": {"startIndex": start, "endIndex": end, "tabId": TAB_ID},
                        "bulletPreset": "NUMBERED_DECIMAL_NESTED",
                    }
                }
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {
                        "spaceBelow": {"magnitude": 4, "unit": "PT"},
                        "lineSpacing": 110,
                        "indentStart": {"magnitude": 36, "unit": "PT"},
                        "indentFirstLine": {"magnitude": 18, "unit": "PT"},
                    },
                    "spaceBelow,lineSpacing,indentStart,indentFirstLine",
                )
            )
        elif kind == "caption":
            requests.append(
                text_style_request(
                    start,
                    end - 1,
                    {
                        "fontSize": {"magnitude": 9, "unit": "PT"},
                        "italic": True,
                        "foregroundColor": color_style("555555"),
                    },
                    "fontSize,italic,foregroundColor",
                )
            )
            requests.append(
                paragraph_style_request(
                    start,
                    end,
                    {
                        "alignment": "CENTER",
                        "spaceAbove": {"magnitude": 4, "unit": "PT"},
                        "spaceBelow": {"magnitude": 12, "unit": "PT"},
                        "keepWithNext": True,
                    },
                    "alignment,spaceAbove,spaceBelow,keepWithNext",
                )
            )
    batch(doc_id, requests)
    return ranges


def body_content(doc):
    return doc["tabs"][0]["documentTab"]["body"]["content"]


def find_table(doc, near):
    candidates = [e for e in body_content(doc) if "table" in e]
    return min(candidates, key=lambda e: abs(e["startIndex"] - near))


def insert_native_diagram(doc_id, item):
    spec = DIAGRAMS[item["key"]]
    start, end = item["start"], item["end"]
    batch(
        doc_id,
        [
            {
                "deleteContentRange": {
                    "range": {"startIndex": start, "endIndex": end, "tabId": TAB_ID}
                }
            },
            {
                "insertTable": {
                    "rows": spec["rows"],
                    "columns": spec["cols"],
                    "location": {"index": start, "tabId": TAB_ID},
                }
            },
        ],
    )
    doc = get_doc(doc_id)
    table = find_table(doc, start)
    table_start = table["startIndex"]
    cells = []
    for row_index, row in enumerate(table["table"]["tableRows"]):
        for col_index, cell in enumerate(row["tableCells"]):
            cells.append((row_index, col_index, cell["startIndex"] + 1))
    cell_texts = spec["cells"]
    insert_requests = []
    for (row, col, index), (text, _bg, _fg) in reversed(list(zip(cells, cell_texts))):
        insert_requests.append(
            {"insertText": {"location": {"index": index, "tabId": TAB_ID}, "text": text}}
        )
    batch(doc_id, insert_requests)
    doc = get_doc(doc_id)
    table = find_table(doc, table_start)
    table_start = table["startIndex"]
    style_requests = []
    width = 468 / spec["cols"]
    style_requests.append(
        {
            "updateTableColumnProperties": {
                "tableStartLocation": {"index": table_start, "tabId": TAB_ID},
                "columnIndices": list(range(spec["cols"])),
                "tableColumnProperties": {
                    "widthType": "FIXED_WIDTH",
                    "width": {"magnitude": width, "unit": "PT"},
                },
                "fields": "widthType,width",
            }
        }
    )
    for row_index in range(spec["rows"]):
        style_requests.append(
            {
                "updateTableRowStyle": {
                    "tableStartLocation": {"index": table_start, "tabId": TAB_ID},
                    "rowIndices": [row_index],
                    "tableRowStyle": {"minRowHeight": {"magnitude": spec["height"], "unit": "PT"}},
                    "fields": "minRowHeight",
                }
            }
        )
    idx = 0
    for row_index, row in enumerate(table["table"]["tableRows"]):
        for col_index, cell in enumerate(row["tableCells"]):
            text, bg, fg = cell_texts[idx]
            idx += 1
            cell_range = {
                "tableCellLocation": {
                    "tableStartLocation": {"index": table_start, "tabId": TAB_ID},
                    "rowIndex": row_index,
                    "columnIndex": col_index,
                },
                "rowSpan": 1,
                "columnSpan": 1,
            }
            border = {
                "color": color_style(LINE),
                "width": {"magnitude": 0.75, "unit": "PT"},
                "dashStyle": "SOLID",
            }
            style_requests.append(
                {
                    "updateTableCellStyle": {
                        "tableRange": cell_range,
                        "tableCellStyle": {
                            "backgroundColor": color_style(bg),
                            "contentAlignment": "MIDDLE",
                            "paddingTop": {"magnitude": 8, "unit": "PT"},
                            "paddingBottom": {"magnitude": 8, "unit": "PT"},
                            "paddingLeft": {"magnitude": 8, "unit": "PT"},
                            "paddingRight": {"magnitude": 8, "unit": "PT"},
                            "borderTop": border,
                            "borderBottom": border,
                            "borderLeft": border,
                            "borderRight": border,
                        },
                        "fields": "backgroundColor,contentAlignment,paddingTop,paddingBottom,paddingLeft,paddingRight,borderTop,borderBottom,borderLeft,borderRight",
                    }
                }
            )
            cstart, cend = cell["startIndex"] + 1, cell["endIndex"] - 1
            style_requests.append(
                text_style_request(
                    cstart,
                    cend,
                    {
                        "weightedFontFamily": {"fontFamily": "Arial"},
                        "fontSize": {"magnitude": 9, "unit": "PT"},
                        "foregroundColor": color_style(fg),
                    },
                    "weightedFontFamily,fontSize,foregroundColor",
                )
            )
            label_end = cstart + len(text.split("\n", 1)[0])
            style_requests.append(
                text_style_request(
                    cstart,
                    label_end,
                    {"bold": True, "fontSize": {"magnitude": 10.5, "unit": "PT"}},
                    "bold,fontSize",
                )
            )
            style_requests.append(
                paragraph_style_request(
                    cstart,
                    cend,
                    {
                        "alignment": "CENTER",
                        "lineSpacing": 105,
                        "spaceAbove": {"magnitude": 0, "unit": "PT"},
                        "spaceBelow": {"magnitude": 0, "unit": "PT"},
                    },
                    "alignment,lineSpacing,spaceAbove,spaceBelow",
                )
            )
    batch(doc_id, style_requests)


def update_document(doc_id, rename=False):
    ranges = replace_body(doc_id)
    for item in sorted(
        (r for r in ranges if r["kind"] == "diagram"), key=lambda r: r["start"], reverse=True
    ):
        insert_native_diagram(doc_id, item)
    if rename:
        api("PATCH", f"{DRIVE}/{doc_id}?fields=id,name", {"name": CANONICAL_TITLE})
    final = get_doc(doc_id)
    content = body_content(final)
    tables = sum(1 for element in content if "table" in element)
    text_chars = sum(
        len(run.get("textRun", {}).get("content", ""))
        for element in content
        for run in element.get("paragraph", {}).get("elements", [])
    )
    print(
        json.dumps(
            {
                "documentId": doc_id,
                "title": final["title"],
                "revisionId": final["revisionId"],
                "tables": tables,
                "bodyTextCharacters": text_chars,
            }
        )
    )


def check_authoring_source():
    diagram_blocks = [block for block in blocks if block["kind"] == "diagram"]
    diagram_keys = [block["key"] for block in diagram_blocks]
    missing = sorted(set(diagram_keys) - set(DIAGRAMS))
    unused = sorted(set(DIAGRAMS) - set(diagram_keys))
    if missing or unused or len(diagram_keys) != len(set(diagram_keys)):
        raise SystemExit(
            f"diagram closure failed: missing={missing}, unused={unused}, keys={diagram_keys}"
        )
    if not any("Make HGX-Runner the durable" in block["text"] for block in blocks):
        raise SystemExit("target-state decision is missing")
    if not any("MAC is not the target runtime" in block["text"] for block in blocks):
        raise SystemExit("MAC migration-and-retirement boundary is missing")
    full_text, ranges = make_text_and_ranges()
    print(
        json.dumps(
            {
                "status": "ok",
                "blocks": len(blocks),
                "characters": len(full_text),
                "diagrams": len(diagram_blocks),
                "rangeCount": len(ranges),
                "canonicalDocumentId": CANONICAL_DOCUMENT_ID,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("document_id", nargs="?")
    parser.add_argument("--rename", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the authoring source without contacting Google",
    )
    args = parser.parse_args()
    if args.check:
        check_authoring_source()
    elif args.document_id:
        update_document(args.document_id, args.rename)
    else:
        parser.error("document_id is required unless --check is used")
