# QA ledger

## Version 2.0 — 10 August 2026

Content review:

- Decision is explicit on page 1: HGX-Runner is the durable control plane.
- MAC is described as the executable reference, data source, behavioral oracle,
  and migration parity baseline to port and retire.
- Current, partial, and proposed claims are separated.
- Mechanisms accompany major claims: allocator snapshot and atomic lease,
  dual-key authorization, exact Component planning, CodeGraph/SBOM gates,
  HGX attestation/onboarding, evidence/review/publication, and failure recovery.
- Negative paths cover no eligible agent, lease loss, Literate lifecycle failure,
  cache/custody mismatch, canonical repository movement, and partial
  cross-repository landing.
- No productivity, ROI, or throughput figure is invented.

Depth review:

- A skeptical practitioner can reconstruct the target authority split and the
  principal state transitions from the document.
- The main sequence is mechanism-led; six figures encode distinct relationships
  rather than repeat one lifecycle.
- Concrete authority appears as exact identities, plans, leases, evidence,
  receipts, and canonical repository proof.
- Cost and containment are addressed through bounded scale-up, concurrency,
  narrow cache invalidation, OpenShell policy, project egress, and the
  model-egress boundary.

Google Docs verification:

- Updated the canonical document in place through the Google Docs API.
- Read-back found the required target-state claims and six native tables.
- Google Drive metadata confirmed the canonical title.
- Google's PDF export produced 15 US Letter pages.
- Every page was rendered to PNG and inspected through a complete contact sheet;
  diagram pages and dense pages were additionally inspected at larger size.
- No clipping, overlap, broken connectors, unreadable tables, unresolved markers,
  awkward near-empty trailing page, or stale conclusion was found.
- A temporary Google Docs copy was used for the first full regeneration and was
  moved to Drive trash after canonical verification.

Future regenerations must append a dated entry rather than silently replacing
this evidence.

## Version 2.1 — 11 August 2026

Source and content review:

- Refreshed evidence against MAC `b09ab1c1`, Literate AI `281652b6`, new
  Horde / OV Agent Farm `307e0450`, and classic Horde `bc62c9e9`.
- Added the existing complementary CLI mechanisms: classic Horde's stable
  JSON/help, create planning, async request IDs, capacity/resource groups,
  service accounts and audited repair; new Horde's session/workspace lifecycle,
  waits, SSH, storage, logs/events, checkpoint/resume, secrets and diagnostics.
- Made the proposed trust boundary explicit: secure NVIDIA GitLab projects use
  classic Horde on-prem runners; non-secure GitHub projects use new Horde
  off-prem runners. Durable project policy authorizes repository, backend,
  runner, credentials, artifacts and egress; ambiguous, conflicting or
  downgraded classification fails closed.
- Replaced the four-step roadmap with M0-M7 complexity, duration, dependency and
  exit gates across three parallel lanes. The stated planning assumption is two
  implementation streams plus continuing security/SRE support; M0-M6 form an
  estimated 21-week critical path and M7 is a controlled follow-on.
- Preserved the target conclusion: HGX-Runner absorbs the MAC control kernel and
  compatible data, cuts over, and retires MAC. Classic and new Horde remain
  distinct capacity fabrics behind one `hgx` contract, not extra work ledgers.

Google Docs verification:

- Regenerated a temporary native Google Docs copy first, then updated the
  canonical document in place through the Google Docs API and restored the
  canonical Drive title.
- Automated read-back found all required migration and route-boundary phrases
  and six native editable tables.
- Google's canonical PDF export produced 17 US Letter pages.
- Inspected the complete canonical contact sheet and full-size pages 13-15 and
  17, covering the three-lane roadmap, every M0-M7 delivery block, success
  criteria and final source ledger.
- The first render exposed an orphaned M4 exit gate and a sparse 18th page.
  Tightened—not removed—the milestone and source text; the accepted render
  keeps every milestone block intact and closes on a substantive 17th page.
- No clipping, overlap, unreadable diagram text, broken table geometry, stale
  conclusion, or nearly empty trailing page was found.
- Moved the temporary Google Docs copy to Drive trash after canonical
  verification.

## Authoring-package integration test — 10 August 2026

- Ran the checked-in `update_google_doc.py --check`: 129 authored blocks, six
  closed diagram definitions, and the required HGX-Runner/MAC target boundaries.
- Ran `regenerate.sh --apply` against a disposable Google Docs copy.
- Automated read-back found six native tables and all required decision phrases.
- Google's export rendered 15 US Letter pages.
- Inspected the complete generated contact sheet and full-size pages 1, 6, 9,
  and 12, covering the title/authority map, Literate lifecycle, identity join,
  and roadmap. No clipping, overflow, broken table geometry, or stale conclusion
  was found.
- Moved the disposable integration document to Google Drive trash after the test.
