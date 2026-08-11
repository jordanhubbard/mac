# HGX-Runner document specification

## Purpose and decision

- Audience: engineering, product, security, SRE, and program leadership deciding how MAC, classic Horde, and agentic Horde converge.
- Decision: merge MAC's work-control behavior and compatible data into HGX-Runner, cut production writers over, and retire MAC as a separate service.
- Execution boundary: classic Horde allocates on-prem resources only; agentic Horde allocates CSP resources only.
- User surface: one `hgx` CLI and one HGX-owned task ledger, with provider-native IDs and diagnostics preserved.
- Security policy: secure NVIDIA GitLab work routes only to classic Horde/on-prem; non-secure GitHub work routes only to agentic Horde/CSP. No capacity shortage authorizes crossover.
- Future infrastructure milestone: after Omniblue and Omnired are fully deployed and certified, drain on-prem runners to Omniblue and off-prem runners to Omnired in bounded cohorts.

## Status discipline

- MAC, classic Horde, and agentic Horde current implementation claims must be supported by repository evidence.
- HGX fusion, unified CLI writes, migration mechanics, and Omniblue/Omnired population migration are proposals until implemented and proven.
- Classic Horde's service deployment on Omniblue does not change its current allocation boundary: its managed capacity remains on-prem vSphere/CloudStack.
- Never describe the three-way sync as three writable copies of one ledger. Every mutable fact has one owner.

## Required narrative

1. State the target decision on page 1.
2. Define the three current systems and their non-overlapping authority.
3. Explain five synchronization planes: policy, work, execution, capacity, and canonical repository proof.
4. Define secure/classic/on-prem and non-secure/agentic/CSP routing with fail-closed negative paths.
5. Inventory the MAC parity baseline and retirement contract.
6. Define a narrow provider adapter and common CLI envelope while preserving native IDs and diagnostics.
7. Walk one routed, fenced task from project classification through reviewed canonical publication.
8. Cover reconciliation, quota, cancellation, lease loss, credential revocation, and moving-repository failures.
9. End with a complexity-based M0-M8 schedule and measurable success criteria.

## Required native diagrams

1. Authority convergence: MAC to HGX-Runner, with classic on-prem and agentic CSP providers.
2. Single-writer synchronization loop: command, provider resource, observation, capacity signal, and repository proof.
3. Security routing: GitLab/classic/on-prem and GitHub/agentic/CSP.
4. Provider adapter contract: one CLI, common envelope, two native adapters.
5. Unified task lifecycle: classify through canonical publication.
6. Three-lane roadmap: control, fabrics, and trust, ending in readiness-gated M8.

All diagrams must be native editable Google Docs tables with concise labels, readable contrast, captions, and no rasterized screenshots.

## Delivery schedule contract

- M0 authority and route freeze — Small — 2 weeks.
- M1 unified read/explain surface — Medium — 3 weeks.
- M2 classic on-prem adapter — Large — 4 weeks.
- M3 agentic CSP adapter — Large — 4 weeks, parallel with M2.
- M4 MAC control-kernel port — Extra large — 6 weeks, parallel with M2-M3.
- M5 three-way routed pilot — Large — 4 weeks.
- M6 MAC backfill, shadow, and writer cutover — Extra large — 5 weeks.
- M7 production hardening and MAC retirement — Large — 3 weeks.
- M8 drain and migrate existing on-prem/off-prem runners to Omniblue/Omnired — Extra large, future and readiness-gated; duration is not estimated before cluster readiness and population inventory.

M0-M7 form an approximately 23-week critical path under the stated staffing assumption. Time never substitutes for exit evidence.

## Acceptance

- The canonical artifact remains a native Google Doc.
- The title contains no unrelated software-foundry framing.
- The body contains no unrelated architecture actor.
- Classic is described as on-prem-only and agentic as CSP-only everywhere.
- MAC is explicitly migrated into HGX and retired, not retained as the durable target.
- M8 uses the exact drain-and-migrate intent and is gated on both clusters being fully deployed and certified.
- All six diagrams render without clipping or overflow.
- Every page is rendered and visually inspected before canonical replacement.
