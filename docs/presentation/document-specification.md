# HGX-Runner document specification

## Communication job

- Audience: architecture, platform, security, and engineering leaders deciding
  how MAC, Literate AI, and HGX/Horde converge.
- Decision: make HGX-Runner the one durable organization-scale work control
  plane; absorb MAC's validated contracts and compatible data; prove behavioral
  parity; cut traffic over; and retire MAC.
- Supporting architecture: Literate AI is the exact semantic derivation and
  qualification engine. The existing HGX/Horde service is the elastic session,
  workspace, and GPU-capacity substrate.
- Form: a claim-led architecture proposal with enough mechanism, failure
  behavior, boundaries, and source evidence for an implementing reader.
- Publication target: the native Google Doc in `current-deliverables.md`.

## Non-negotiable distinctions

- MAC is mature current behavior and therefore the migration parity baseline;
  it is not the target runtime.
- HGX 0.8.0 is current session infrastructure; HGX-Runner is the proposed
  organization-scale work control plane built above and into that substrate.
- Literate AI Standard-core mechanisms exist, while the portable
  HGX-Runner/Literate run envelope remains a design contract.
- Git remains authoritative for accepted code and project specifications.
- Large generated artifacts and forensic journals belong in immutable,
  content-addressed storage and are joined by identity.

## Narrative sequence

1. State the decision and target authority map.
2. Explain what changed since the previous edition and label implemented,
   partial, and proposed claims.
3. Establish independent fleet-execution and semantic-operation grants.
4. Inventory the MAC operational kernel to port and retire.
5. Explain Literate AI's exact planning, custody, evidence, and qualification.
6. Separate current HGX session capability from the HGX-Runner target.
7. Define the minimal identity join between HGX-Runner and Literate AI.
8. Preserve a free-text fast lane while adding optional Component actions.
9. Walk the end-to-end operating flow.
10. Cover negative paths and conservative cleanup behavior.
11. Deliver through inventory, port, cutover, and extension milestones.
12. End with measurable success criteria and an explicit boundary appendix.

## Native diagram contract

All figures are editable Google Docs tables with fixed column geometry, dark
panels, restrained accent colors, centered labels, and captions.

1. Target authority map: HGX-Runner target; MAC-to-HGX migration; Literate AI;
   Git and content-addressed storage.
2. Dual-key rule: HGX-Runner execution grant plus Literate AI content-bound
   authorization.
3. Literate lifecycle: authority, lock/plan, source, index/admit, authorize,
   build, verify, admit.
4. Capacity flow: no eligible agent, stabilize/bound, create, attest, onboard,
   allocate.
5. Identity join: HGX-Runner request envelope and Literate result envelope.
6. Roadmap: inventory, port, cutover, extend.

## Design tokens

- Page: US Letter portrait, Google Docs-native margins.
- Typography: Arial; 26 pt title; 16 pt subtitle; 20 pt H1; 16 pt H2;
  11 pt body at 110% line spacing; 9 pt captions.
- Palette: ink `#101317`, panel `#23282F`, steel `#65707C`, fog `#EEF1F3`,
  orange `#FF6B35`, blue `#72B7D6`, green `#76B900`, red `#F47C7C`.
- Lists: real Google Docs bullets and numbering, not glyph-prefixed prose.
- Figures: native tables, fixed width, expandable rows, centered text, explicit
  padding and borders.

## Acceptance

- The opening decision, roadmap, success criteria, and conclusion all state the
  same target: merge MAC into HGX-Runner and retire MAC after verified cutover.
- Every major current capability maps to a mechanism or source in
  `source-notes.md`.
- Roadmap items remain labeled partial or proposed.
- Google Docs read-back contains six native tables and the required target-state
  phrases.
- Google's PDF export renders 15 clean pages with no clipping, overlap, broken
  captions, unreadable diagrams, or nearly empty trailing page.
