---
name: hgx-runner-google-doc
description: Regenerate and verify the HGX-Runner three-way convergence Google Doc from current MAC, classic Horde, and agentic Horde authority.
---

# Regenerate the HGX-Runner Google Doc

1. Read `document-specification.md`, `source-notes.md`, and `qa-ledger.md`.
2. Revalidate implementation claims against the current MAC, classic Horde, and agentic Horde checkouts; record exact revisions.
3. Preserve the target decision: merge MAC into HGX-Runner, prove parity, cut over, and retire MAC.
4. Preserve the infrastructure boundary: classic Horde is on-prem-only; agentic Horde is CSP-only.
5. Preserve the security route: secure GitLab to classic/on-prem; non-secure GitHub to agentic/CSP; ambiguity fails closed.
6. Preserve Omniblue/Omnired as a parallel readiness-gated alternative: an early lift-and-shift capability may change the Horde merger path, but not the need for unified work control.
7. Preserve clickable source citations for MAC, agentic Horde, and classic Horde.
8. Run `python3 scripts/update_google_doc.py --check` before API writes.
9. Regenerate a temporary Google Docs copy, render every page, inspect the contact sheet and all diagram/dense pages, and iterate until clean.
10. Update the canonical document only after the temporary copy passes.
11. Run `scripts/verify_google_doc.sh`, render the canonical document, inspect it, append `qa-ledger.md`, and trash the temporary copy.

The Google Doc is the deliverable. Do not create a local Word document.
