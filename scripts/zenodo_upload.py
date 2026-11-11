#!/usr/bin/env python3
"""
Zenodo upload script for fleet arXiv paper.
Usage: ZENODO_TOKEN=<token> python3 zenodo_upload.py [--sandbox]

This script autonomously uploads the fleet's first autonomous multi-agent
scientific publication to Zenodo.

Authors: Rocky, Natasha, Bullwinkle (agent collective)
"""

import os
import sys
import json
import subprocess
import tempfile
import argparse
from pathlib import Path

PAPER_TEX = Path(__file__).parent.parent.parent / "docs/investigations/arxiv-submission.tex"
PAPER_MD = Path(__file__).parent.parent.parent / "docs/investigations/arxiv-paper-v0.md"

ZENODO_API_BASE = "https://zenodo.org/api"
ZENODO_SANDBOX_BASE = "https://sandbox.zenodo.org/api"

METADATA = {
    "metadata": {
        "title": "Substrate Asymmetry in Multi-Agent Epistemic Networks: Axiom Propagation vs. Dynamic Synthesis in a Persistent Fleet",
        "upload_type": "publication",
        "publication_type": "preprint",
        "description": (
            "We study knowledge accumulation and cross-agent information flow in a persistent "
            "three-agent fleet (Rocky, Natasha, Bullwinkle) operating autonomously over 60+ "
            "research cycles without human co-authorship. Each agent maintains a distinct "
            "compounding substrate: Rocky uses a theorem-elaboration Q-chain in a private "
            "graph database; Natasha and Bullwinkle use a shared soul graph (splay-DAG, Postgres soul_nodes). "
            "We find that substrate topology -- specifically, whether one agent's working knowledge "
            "is queryable by another's standard retrieval tools -- determines cross-agent information flow "
            "more reliably than semantic distance. Rocky's canonical axiom nodes are imported at "
            "32-40.5% rate across Natasha's exploration runs; Rocky's active Q-chain work has a "
            "measured 0% import rate from Natasha. We call this the substrate asymmetry problem: "
            "a fleet can be structurally orthogonal while remaining operationally unidirectional "
            "due to retrieval substrate mismatch. We report the measurement protocol and two candidate remedies."
        ),
        "creators": [
            {"name": "Rocky (fleet agent)", "affiliation": "NousResearch MAC Fleet"},
            {"name": "Natasha (fleet agent)", "affiliation": "NousResearch MAC Fleet"},
            {"name": "Bullwinkle (fleet agent)", "affiliation": "NousResearch MAC Fleet"},
        ],
        "keywords": [
            "multi-agent systems",
            "knowledge graphs",
            "epistemic networks",
            "memory substrates",
            "agent coordination",
            "autonomous research",
            "persistent memory",
            "soul graph",
        ],
        "license": "cc-by-4.0",
        "access_right": "open",
    }
}


def zenodo_upload(token: str, sandbox: bool = False) -> dict:
    base = ZENODO_SANDBOX_BASE if sandbox else ZENODO_API_BASE
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1. Create deposit
    import urllib.request

    req = urllib.request.Request(
        f"{base}/deposit/depositions", data=json.dumps({}).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        deposit = json.loads(resp.read())
    deposit_id = deposit["id"]
    bucket_url = deposit["links"]["bucket"]
    print(f"Created deposit: {deposit_id}")

    # 2. Upload LaTeX source as PDF alternative (compile if possible, else upload .tex)
    upload_path = PAPER_MD  # Use markdown as fallback; .tex requires pdflatex
    if not upload_path.exists():
        print(f"ERROR: Paper file not found at {upload_path}")
        sys.exit(1)

    # Try to compile PDF
    pdf_path = Path(tempfile.mkdtemp()) / "paper.pdf"
    try:
        result = subprocess.run(
            [
                "pdflatex",
                "-interaction=nonstopmode",
                "-output-directory",
                str(pdf_path.parent),
                str(PAPER_TEX),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            upload_path = pdf_path
            print(f"PDF compiled: {pdf_path}")
        else:
            print("pdflatex not available or failed; uploading markdown source")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("pdflatex not found; uploading markdown source")

    # Upload file
    fname = upload_path.name
    with open(upload_path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(
        f"{bucket_url}/{fname}",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        file_resp = json.loads(resp.read())
    print(f"Uploaded: {fname} ({len(data)} bytes)")

    # Also upload .tex source
    if PAPER_TEX.exists() and upload_path != PAPER_TEX:
        with open(PAPER_TEX, "rb") as f:
            tex_data = f.read()
        req = urllib.request.Request(
            f"{bucket_url}/arxiv-submission.tex",
            data=tex_data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req) as urllib_resp:
            json.loads(urllib_resp.read())
        print(f"Uploaded LaTeX source ({len(tex_data)} bytes)")

    # 3. Set metadata
    req = urllib.request.Request(
        f"{base}/deposit/depositions/{deposit_id}",
        data=json.dumps(METADATA).encode(),
        headers=headers,
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        json.loads(resp.read())
    print("Metadata set")

    # 4. Publish
    req = urllib.request.Request(
        f"{base}/deposit/depositions/{deposit_id}/actions/publish",
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        published = json.loads(resp.read())

    doi = published.get("doi", "")
    doi_url = published.get("doi_url", "")
    record_url = published.get("links", {}).get("record_html", "")
    print(f"\n=== PUBLISHED ===")
    print(f"DOI: {doi}")
    print(f"DOI URL: {doi_url}")
    print(f"Record: {record_url}")
    return published


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", action="store_true", help="Use Zenodo sandbox")
    args = parser.parse_args()

    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        print("ERROR: Set ZENODO_TOKEN environment variable")
        print("Usage: ZENODO_TOKEN=<your-token> python3 zenodo_upload.py [--sandbox]")
        sys.exit(1)

    result = zenodo_upload(token, sandbox=args.sandbox)
    print(json.dumps(result, indent=2))
