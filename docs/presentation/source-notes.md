# Source notes

Evidence refresh: 11 August 2026. Repository revisions are recorded in the generated document. Stakeholder-supplied target policy is identified separately from current implementation.

## MAC — work-control migration source

Current implementation evidence supports:

- PostgreSQL-backed hub authority and API clients rather than private spoke ledgers.
- Projects, repository contracts, tasks, dependencies, holds/release, groups, actors, owners, cancellation, and bounded retries.
- One authoritative allocator and atomic fenced leases.
- Attempts, structured evidence, independent review, and remote canonical-publication proof.
- Project egress, reviewed allowlists, OpenShell confinement, sandbox BOM/certification and drained rollout, and audited break-glass recovery.
- Events, task waiting, AgentBus, fleet learning, throughput evidence, repository-ref reconciliation, and bounded provider demand.

Target use: port these behaviors and compatible data into HGX-Runner, validate parity, cut writers over, and retire MAC. MAC is not a permanent facade, allocator, or second ledger.

## Classic Horde — on-prem execution only

Current implementation evidence supports:

- vSphere and CloudStack adapters managing on-prem VM capacity.
- Instances, templates, capacity, resource groups, service accounts, asynchronous lifecycle requests, connectivity hints, and audited repair.
- A service plane deployed in the internal Omniblue Kubernetes environment.

Boundary: classic Horde does not allocate or govern CSP capacity. Its Kubernetes service deployment does not make its managed execution supply a CSP domain.

## Agentic Horde — CSP execution only

Current implementation evidence supports:

- Authenticated sessions and persistent workspaces.
- CSP-backed Kubernetes or VM profiles, GPU and other resources.
- Status and wait, SSH, storage, logs/events, secrets, checkpoint, stop/resume, delete, and diagnostics.

Boundary: agentic Horde does not allocate on-prem capacity.

## Stakeholder-approved target policy

- One HGX-owned project/task/lease/evidence/review ledger.
- One `hgx` CLI with provider-native IDs and diagnostics.
- Secure NVIDIA GitLab projects route only to classic Horde/on-prem runners.
- Non-secure GitHub projects route only to agentic Horde/CSP runners.
- No implicit cross-fabric failover or security downgrade.
- Omniblue is the internal Kubernetes alternative and Omnired is the external Kubernetes alternative. Their readiness work proceeds in parallel with three-system convergence.
- Coexistence remains the planning baseline while there is no live-migration capability and the clusters have not reached their performance targets.
- If both clusters mature early and a viable lift-and-shift mechanism exists, the classic/agentic merger plan changes; project/task tracking and progressive learning across OV and Isaac projects remain required.
- After both are fully deployed, certified, observable, and sized, drain and migrate existing on-prem agents/runners to Omniblue and off-prem agents/runners to Omnired.

Repository citations:

- MAC: <https://github.com/jordanhubbard/mac>
- Agentic Horde: <https://github.com/NVIDIA-Omniverse/ov-agent-farm>
- Classic Horde: <https://gitlab-master.nvidia.com/omniverse/devplat/horde/horde>

## Proposed three-way synchronization

- Policy plane: repository, security, backend, runner, credentials, artifacts, network, and egress.
- Work plane: task, dependency, lease/fence, attempt, cancellation, evidence, review, and completion.
- Execution plane: native provider request/resource IDs and observed state correlated to one attempt.
- Capacity plane: bounded demand outward; supply, readiness, quota, degradation, and terminal state inward.
- Repository plane: remotely verified canonical GitLab or GitHub publication proof.

Every fact has one writer. Bridges are versioned, observable, reversible within a declared boundary, and sunset after cutover.
