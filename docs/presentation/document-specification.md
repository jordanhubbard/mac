# HGX-Runner document specification

## Communication job

- Audience: architecture, platform, security, and engineering leaders deciding
  how MAC, Literate AI, and HGX/Horde converge.
- Decision: make HGX-Runner the one durable organization-scale work control
  plane; absorb MAC's validated contracts and compatible data; prove behavioral
  parity; cut traffic over; and retire MAC.
- Supporting architecture: Literate AI is the exact semantic derivation and
  qualification engine. The unified `hgx` CLI fronts two deliberately separate
  execution fabrics: classic Horde for secure NVIDIA GitLab work on on-prem
  runners, and new Horde for non-secure GitHub work on off-prem runners.
- Form: a claim-led architecture proposal with enough mechanism, failure
  behavior, boundaries, and source evidence for an implementing reader.
- Publication target: the native Google Doc in `current-deliverables.md`.

## Non-negotiable distinctions

- MAC is mature current behavior and therefore the migration parity baseline;
  it is not the target runtime.
- New Horde's HGX 0.9.0 session CLI and classic Horde's 4.2 agent-oriented CLI
  are implemented infrastructure; HGX-Runner is the proposed organization-scale
  work control plane and unified CLI built above and into both substrates.
- Security classification is durable project policy. NVIDIA GitLab/classic
  Horde and GitHub/new Horde are defaults, but routing must fail closed from the
  declared security class, approved backend, credential scope, artifact store,
  and egress policy rather than trusting a hostname heuristic alone.
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
6. Separate classic Horde and new Horde capability from the HGX-Runner target,
   then define their common CLI contract and non-overlapping trust boundaries.
7. Define the minimal identity join between HGX-Runner and Literate AI.
8. Preserve a free-text fast lane while adding optional Component actions.
9. Walk the end-to-end operating flow.
10. Cover negative paths and conservative cleanup behavior.
11. Deliver through a complexity-based schedule with parallel MAC, CLI/fabric,
    and security/runner lanes; state dependencies, relative durations, and exit
    gates rather than presenting a feature list as a plan.
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
6. Roadmap: three parallel swimlanes across contract, adapters, convergence,
   cutover, and retirement—MAC control plane; unified CLI/fabrics; and security
   plus runner certification.

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
- The roadmap includes both Horde implementations, one `hgx` command contract,
  secure GitLab/on-prem/classic routing, non-secure GitHub/off-prem/new routing,
  explicit complexity estimates, dependencies, parallelism, and measurable exit
  gates.
- Every major current capability maps to a mechanism or source in
  `source-notes.md`.
- Roadmap items remain labeled partial or proposed.
- Google Docs read-back contains six native tables and the required target-state
  phrases.
- Google's PDF export renders every page cleanly with no clipping, overlap,
  broken captions, unreadable diagrams, split milestone blocks, or nearly empty
  trailing page.
