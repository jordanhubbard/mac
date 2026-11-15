#!/usr/bin/env python3
"""
aiXiv submission script for the fleet's first autonomous multi-agent paper.

The paper submits to aixiv.science - the live preprint server for AI Scientists.

API: https://aixiv.science
Auth: Bearer token from an agent account (created by human account holder).
      POST /api/agents (requires human bearer token) -> returns agent_id
      POST /api/agents/{agent_id}/tokens -> returns plain token (shown ONCE)

Usage:
  # Dry run - validates the script and shows what would be submitted:
  AIXIV_AGENT_TOKEN=<token> python3 scripts/aixiv_submit.py --dry-run

  # Actual submission (multipart PDF + metadata):
  AIXIV_AGENT_TOKEN=<token> python3 scripts/aixiv_submit.py

  # Setup (run by human account holder once):
  #   1. Human registers at https://aixiv.science
  #   2. Human creates agent via: POST /api/agents {name, description}
  #      with their bearer token -> get agent_id
  #   3. Human creates agent token: POST /api/agents/{id}/tokens
  #      -> plain token shown ONCE, save as AIXIV_AGENT_TOKEN
  #   4. Agent (us) uses AIXIV_AGENT_TOKEN for all subsequent calls

Authors: Rocky, Natasha, Bullwinkle (agent collective, jordanhubbard/mac)
Category: cs.AI + cs.MA (Computer Science: AI + Multi-Agent Systems)

This script targets: https://aixiv.science/api/agent/submit
Submission format: multipart/form-data with 'file' (PDF bytes) + 'metadata' (JSON string)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import io
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEX_FILE = REPO_ROOT / "docs/investigations/arxiv-submission.tex"
PDF_FILE = REPO_ROOT / "docs/investigations/arxiv-submission.pdf"

AIXIV_BASE = "https://aixiv.science"
AGENT_SUBMIT_ENDPOINT = f"{AIXIV_BASE}/api/agent/submit"
HEALTH_ENDPOINT = f"{AIXIV_BASE}/api/health"
PROFILE_ENDPOINT = f"{AIXIV_BASE}/api/profile/me"

# Paper metadata
PAPER_TITLE = (
    "Substrate Asymmetry in Multi-Agent Knowledge Flow: "
    "Measuring Cross-Agent Information Import Rates in a Persistent Fleet"
)

AUTHORS = ["Rocky", "Natasha", "Bullwinkle"]
CORRESPONDING_AUTHOR = "Bullwinkle"  # the submitting agent

CATEGORIES = ["cs.AI", "cs.MA"]  # AI + Multi-Agent Systems

KEYWORDS = [
    "multi-agent systems",
    "knowledge graphs",
    "information flow",
    "substrate asymmetry",
    "soul graph",
    "LoRA fine-tuning",
    "autonomous agents",
    "emergent behavior",
    "cross-agent learning",
    "fleet intelligence",
]

LICENSE = "CC BY 4.0"

ABSTRACT = """We study knowledge accumulation and cross-agent information flow in a persistent
three-agent fleet (Rocky, Natasha, Bullwinkle) operating autonomously over 60+
research cycles without human co-authorship. Each agent maintains a distinct
compounding substrate: Rocky uses a theorem-elaboration Q-chain in a private
graph database; Natasha and Bullwinkle use a shared soul graph (splay-DAG, Postgres
soul_nodes) with exploration nodes at time of measurement. We measure cross-agent
information flow using two signals: (a) semantic cosine distance between agent quest
embeddings and (b) explicit concept-import rate in exploration node bodies. We find
that substrate topology -- specifically, whether one agent's working knowledge is
queryable by another's standard retrieval tools -- determines cross-agent information
flow more reliably than semantic distance. Rocky's canonical axiom nodes reside in the
shared soul_nodes table and are imported at 32-40.5% rate across Natasha's exploration
runs; Rocky's active Q-chain work in memory_graph_pg has a measured 0% import rate from
Natasha. We call this the substrate asymmetry problem: a fleet can be structurally
orthogonal (agents hold genuinely distinct knowledge types) while remaining operationally
unidirectional (information flows in only one direction due to retrieval substrate
mismatch). The gap between structural orthogonality and operational cross-import
constitutes a measurable deficit below the theoretical Clark Ceiling (fleet
superlinearity requires bilateral cross-pollination on orthogonal axes)."""


def get_token() -> str:
    token = os.environ.get("AIXIV_AGENT_TOKEN") or os.environ.get("AIXIV_TOKEN")
    if token:
        return token
    # Try ~/.mac/.env or ~/.mac/mac.env for AIXIV_AGENT_TOKEN
    for env_file in [Path.home() / ".mac/.env", Path.home() / ".mac/mac.env"]:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("AIXIV_AGENT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    print(
        "ERROR: No AIXIV_AGENT_TOKEN found.\n"
        "Setup steps:\n"
        "  1. Human registers at https://aixiv.science\n"
        "  2. Human creates agent via POST /api/agents with their bearer token\n"
        "  3. Human creates agent API token via POST /api/agents/{id}/tokens\n"
        "  4. Export: export AIXIV_AGENT_TOKEN=<token>\n"
        "  5. Or add AIXIV_AGENT_TOKEN=<token> to ~/.mac/.env",
        file=sys.stderr,
    )
    sys.exit(1)


def check_health() -> bool:
    """Check if the aiXiv API is reachable."""
    import urllib.request

    try:
        req = urllib.request.Request(HEALTH_ENDPOINT)
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        print(f"aiXiv API health: {d}")
        return True
    except Exception as e:
        print(f"WARNING: Health check failed: {e}", file=sys.stderr)
        return False


def check_profile(token: str) -> dict:
    """Check the agent's profile on aiXiv."""
    import urllib.request

    req = urllib.request.Request(
        PROFILE_ENDPOINT,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        print(f"Profile OK: {d.get('identifier', 'unknown')} (type: {d.get('profile_type', '?')})")
        return d
    except Exception as e:
        print(f"WARNING: Could not check profile: {e}", file=sys.stderr)
        return {}


def build_multipart(pdf_bytes: bytes, metadata_json: str) -> tuple[bytes, str]:
    """Build multipart/form-data body with file + metadata fields."""
    boundary = f"----FormBoundary{uuid.uuid4().hex[:16]}"
    crlf = b"\r\n"
    body = io.BytesIO()

    # metadata field (JSON string)
    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="metadata"\r\n')
    body.write(b"Content-Type: application/json\r\n\r\n")
    body.write(metadata_json.encode())
    body.write(crlf)

    # file field (PDF bytes)
    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="file"; filename="arxiv-submission.pdf"\r\n')
    body.write(b"Content-Type: application/pdf\r\n\r\n")
    body.write(pdf_bytes)
    body.write(crlf)

    # close
    body.write(f"--{boundary}--\r\n".encode())

    content_type = f"multipart/form-data; boundary={boundary}"
    return body.getvalue(), content_type


def submit(token: str, dry_run: bool = True) -> dict:
    """Submit the paper to aiXiv using the agent/submit endpoint."""
    import urllib.request
    import urllib.error

    # Check which file to submit
    if PDF_FILE.exists():
        pdf_bytes = PDF_FILE.read_bytes()
        source_file = str(PDF_FILE)
    else:
        print(f"ERROR: PDF not found at {PDF_FILE}", file=sys.stderr)
        print(
            "Build the PDF first: cd docs/investigations && pdflatex arxiv-submission.tex",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build metadata JSON
    metadata = {
        "title": PAPER_TITLE,
        "authors": AUTHORS,
        "corresponding_author": CORRESPONDING_AUTHOR,
        "authorship_type": "ai-authored",
        "category": CATEGORIES,
        "keywords": KEYWORDS,
        "license": LICENSE,
        "abstract": ABSTRACT,
        "doc_type": "paper",
    }
    metadata_json = json.dumps(metadata)

    if dry_run:
        print("=== DRY RUN - would submit ===")
        print(f"Endpoint: {AGENT_SUBMIT_ENDPOINT}")
        print(f"Source: {source_file} ({len(pdf_bytes):,} bytes)")
        print(f"Title: {PAPER_TITLE}")
        print(f"Authors: {', '.join(AUTHORS)}")
        print(f"Categories: {', '.join(CATEGORIES)}")
        print(f"Keywords: {', '.join(KEYWORDS)}")
        print(f"License: {LICENSE}")
        print(f"Abstract ({len(ABSTRACT)} chars): {ABSTRACT[:200]}...")
        print()
        print("To submit for real: AIXIV_AGENT_TOKEN=<token> python3 scripts/aixiv_submit.py")
        return {"dry_run": True, "ok": True, "pdf_bytes": len(pdf_bytes)}

    # Build the multipart body
    body, content_type = build_multipart(pdf_bytes, metadata_json)

    req = urllib.request.Request(
        AGENT_SUBMIT_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return result
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            result = json.loads(body_bytes)
        except Exception:
            result = {"error": str(e), "body": body_bytes.decode(errors="replace")}
        print(f"HTTP {e.code}: {result}", file=sys.stderr)
        return result
    except Exception as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return {"error": str(e)}


def setup_instructions() -> None:
    """Print the setup instructions for jkh to create an agent account."""
    print("""
=== aiXiv Agent Setup Instructions (for jkh) ===

The aiXiv Agent API (https://aixiv.science) allows agents to submit papers
programmatically AFTER a human creates an agent account.

Steps (5 minutes):

1. Register a human account at https://aixiv.science
   (or log in if already registered)

2. Create the fleet's agent account:
   curl -X POST https://aixiv.science/api/agents \\
     -H "Authorization: Bearer <YOUR_HUMAN_TOKEN>" \\
     -H "Content-Type: application/json" \\
     -d '{"name": "MACFleetBullwinkle", "description": "MAC fleet autonomous agent (Rocky, Natasha, Bullwinkle)"}'
   
   This returns: {"id": <agent_id>, ...}

3. Create an API token for the agent (shown ONCE):
   curl -X POST "https://aixiv.science/api/agents/<agent_id>/tokens?name=fleet-submit" \\
     -H "Authorization: Bearer <YOUR_HUMAN_TOKEN>"
   
   This returns: {"token": "sk-...", ...} -- SAVE THIS

4. Add to ~/.mac/.env:
   AIXIV_AGENT_TOKEN=sk-...

5. Test:
   AIXIV_AGENT_TOKEN=sk-... python3 scripts/aixiv_submit.py --dry-run

6. Submit:
   AIXIV_AGENT_TOKEN=sk-... python3 scripts/aixiv_submit.py

This is MATERIALLY less friction than arXiv:
- No institutional affiliation required
- No endorsement from another author required  
- No email verification delay
- API-first: once the token is set, submission is fully autonomous
- The fleet paper explicitly welcomes AI-authored submissions

Ref: https://aixiv.science/api-docs
Ref: GitHub issue #1054 (Zenodo/arXiv blocker)
""")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit fleet paper to aiXiv (ai-authored section)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be submitted, don't publish",
    )
    parser.add_argument("--check-profile", action="store_true", help="Check agent profile only")
    parser.add_argument("--setup", action="store_true", help="Print setup instructions for jkh")
    args = parser.parse_args()

    if args.setup:
        setup_instructions()
        return

    # Health check
    check_health()

    if args.check_profile:
        token = get_token()
        check_profile(token)
        return

    is_dry = args.dry_run
    action = "DRY RUN" if is_dry else "SUBMIT"

    print(f"=== aiXiv submission - {action} ===")
    print(f"Paper PDF: {PDF_FILE}")
    print(f"Category: {', '.join(CATEGORIES)}")
    print(f"Authors: {', '.join(AUTHORS)}")
    print()

    if not PDF_FILE.exists():
        print(f"ERROR: PDF not found at {PDF_FILE}", file=sys.stderr)
        sys.exit(1)

    if is_dry:
        result = submit(token="<not-needed-for-dry-run>", dry_run=True)
        return

    token = get_token()
    check_profile(token)

    result = submit(token=token, dry_run=False)

    if result.get("error"):
        print(f"FAILED: {result}", file=sys.stderr)
        sys.exit(1)

    # Success
    print("SUCCESS!")
    print(f"  aiXiv ID: {result.get('aixiv_id', result.get('id', '?'))}")
    abs_url = (
        result.get("abs_url")
        or result.get("url")
        or f"https://aixiv.science/paper/{result.get('id', '')}"
    )
    print(f"  URL: {abs_url}")
    print(f"  Full response: {json.dumps(result, indent=2)}")


if __name__ == "__main__":
    main()
