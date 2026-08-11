# HGX-Runner Google Doc authoring package

This directory is the standalone, reproducible authoring package for the native
Google Doc:

<https://docs.google.com/document/d/1iinPBrxuP8YtGYsdGCwZ0vlQRgIzU_fCl-CcqnvGnPE/edit?tab=t.0>

The document's conclusion is fixed unless the decision itself changes:
HGX-Runner becomes the durable organization-scale control plane; MAC is the
implementation, data-migration, and behavioral-parity source that is cut over
and retired; classic Horde remains the on-prem capacity authority; agentic
Horde remains the CSP capacity authority.

## Package contents

- `document-specification.md` — audience, decision, narrative, and figure contract.
- `source-notes.md` — factual ledger and source revisions used by the current edition.
- `scripts/presentation_content.py` — current architecture narrative and schedule.
- `scripts/update_google_doc.py` — formatting, native table diagrams, and Google Docs
  update logic.
- `regenerate.sh` — guarded update entry point.
- `scripts/verify_google_doc.sh` — read-back, PDF export, page rendering, and contact-sheet
  generation.
- `qa-ledger.md` — recorded content and visual acceptance results.
- `current-deliverables.md` — canonical document identity and title.
- `skills/` — vendored authoring guidance used by this standalone package.

Generated PDFs, page PNGs, contact sheets, API responses, and access tokens must
stay outside the repository. The scripts default to a temporary directory for
verification artifacts.

## Prerequisites

- Python 3.10 or newer
- `certifi` from `requirements.txt`
- authenticated `gcloud` CLI access to the target Google Doc
- `curl`, `jq`, `pdfinfo`, `pdftoppm`, ImageMagick's `magick`, and `rg`

Install the one Python dependency into an external environment, never beneath
this source directory:

```console
python3 -m venv /tmp/hgx-runner-doc-venv
/tmp/hgx-runner-doc-venv/bin/pip install -r docs/presentation/requirements.txt
```

## Validate without changing Google Docs

```console
/tmp/hgx-runner-doc-venv/bin/python \
  docs/presentation/scripts/update_google_doc.py --check
```

## Regenerate and verify the canonical document

The `--apply` flag is intentionally required because regeneration replaces the
document body in place while preserving the Google Doc identity and revision
history.

```console
PYTHON=/tmp/hgx-runner-doc-venv/bin/python \
  docs/presentation/regenerate.sh --apply --rename
```

To test safely, first make a temporary Google Drive copy and pass its ID:

```console
PYTHON=/tmp/hgx-runner-doc-venv/bin/python \
  docs/presentation/regenerate.sh \
  --document-id GOOGLE_DOC_COPY_ID \
  --apply
```

The command updates the document, reads it back, exports Google's PDF render,
renders every page to PNG, and builds a contact sheet. It prints the external QA
directory. A human must inspect the entire contact sheet and dense/diagram pages
at full size before recording acceptance in `qa-ledger.md`.

## Refresh discipline

1. Read this package's `SKILL.md` and the vendored authoring skill.
2. Revalidate `source-notes.md` against current MAC, classic Horde, and agentic
   Horde authority. Use CodeGraph first where a repository index exists.
3. Update `source-notes.md` and `document-specification.md` before changing the
   generator.
4. Keep implemented, partial, and proposed claims visibly distinct.
5. Run the offline check, regenerate a temporary Docs copy, and visually inspect
   every page.
6. Apply the same source to the canonical Doc, read it back, export/render it, and
   record the result in `qa-ledger.md`.
