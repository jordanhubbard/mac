#!/usr/bin/env python3
"""
Lit Review Gate — mandatory pre-submission checker for fleet research papers.

Runs before any clawXiv / aiXiv submission. Extracts claims from the paper,
searches arXiv and the web for grounding evidence, verifies every cited
paper is real and retrievable, and refuses submission if the paper is
insufficiently grounded in the external literature.

Exit codes:
  0  — gate passed; submission may proceed
  1  — gate failed; submission blocked (see report)
  2  — usage error

Usage:
  python3 scripts/lit_review_gate.py paper.tex [--min-external N] [--report-only]

  --min-external N   Minimum external (non-fleet) citations required (default: 5)
  --report-only      Print report but exit 0 regardless (for CI info runs)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ARXIV_API = "https://export.arxiv.org/api/query"
FLEET_PREFIXES = ("clawxiv.", "fleet2026", "rocky_", "natasha_", "bullwinkle_")
MIN_EXTERNAL_DEFAULT = 5
MIN_CLAIM_COVERAGE = 0.5   # fraction of major claims that need ≥1 grounding hit
REQUEST_DELAY = 1.5        # seconds between arXiv API calls (rate limit)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CitationResult:
    key: str
    raw: str
    is_fleet: bool
    verified: bool
    title: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ClaimSearchResult:
    claim: str
    query: str
    hits: list[dict] = field(default_factory=list)
    grounded: bool = False


@dataclass
class GateReport:
    paper_title: str
    abstract: str
    citations: list[CitationResult] = field(default_factory=list)
    claim_results: list[ClaimSearchResult] = field(default_factory=list)
    passed: bool = False
    failure_reasons: list[str] = field(default_factory=list)

    @property
    def external_citations(self):
        return [c for c in self.citations if not c.is_fleet]

    @property
    def fleet_citations(self):
        return [c for c in self.citations if c.is_fleet]

    @property
    def unverified_external(self):
        return [c for c in self.external_citations if not c.verified]

    @property
    def grounded_claims(self):
        return [cr for cr in self.claim_results if cr.grounded]


# ---------------------------------------------------------------------------
# LaTeX parsing
# ---------------------------------------------------------------------------

def extract_metadata(tex: str) -> tuple[str, str]:
    """Extract title and abstract from LaTeX source."""
    title = ""
    abstract = ""

    m = re.search(r"\\title\{([^}]+)\}", tex, re.DOTALL)
    if m:
        title = re.sub(r"\\[a-zA-Z]+\{?", "", m.group(1)).replace("}", "").strip()

    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.DOTALL)
    if m:
        abstract = re.sub(r"\\[a-zA-Z]+\{?[^}]*\}?", " ", m.group(1)).strip()
        abstract = re.sub(r"\s+", " ", abstract)

    return title, abstract


def extract_section_headers(tex: str) -> list[str]:
    """Extract section/subsection titles — these are the major claim areas."""
    headers = re.findall(r"\\(?:sub)*section\{([^}]+)\}", tex)
    cleaned = []
    for h in headers:
        h = re.sub(r"\\[a-zA-Z]+\{?[^}]*\}?", " ", h).strip()
        if len(h) > 3 and h.lower() not in (
            "introduction", "conclusion", "related work", "acknowledgments",
            "appendix", "data sources", "references", "background",
            "limitations", "discussion",
        ):
            cleaned.append(h)
    return cleaned


def extract_bib_keys(tex: str) -> list[tuple[str, str]]:
    """
    Extract citation keys and their raw bib entries.
    Returns list of (key, raw_entry) tuples.
    Handles both \\bibitem{key} and natbib \\bibliography inline blocks.
    """
    results = []

    # \bibitem{key} ... next \bibitem or end
    bibitems = re.findall(
        r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}(.*?)(?=\\bibitem|\\end\{thebibliography\})",
        tex, re.DOTALL
    )
    for key, body in bibitems:
        raw = re.sub(r"\s+", " ", body).strip()[:300]
        results.append((key.strip(), raw))

    # @article/@inproceedings/etc blocks
    bib_entries = re.findall(
        r"@\w+\{([^,]+),(.*?)\n\}", tex, re.DOTALL
    )
    seen_keys = {k for k, _ in results}
    for key, body in bib_entries:
        key = key.strip()
        if key not in seen_keys:
            raw = re.sub(r"\s+", " ", body).strip()[:300]
            results.append((key, raw))
            seen_keys.add(key)

    return results


# Known-good non-arXiv citations that cannot be verified via arXiv ID
# but are confirmed real. Format: citation key fragment → (title, url)
KNOWN_GOOD = {
    "park2023generative": (
        "Generative Agents: Interactive Simulacra of Human Behavior",
        "https://dl.acm.org/doi/10.1145/3586183.3606763"
    ),
    "wegner1987transactive": (
        "Transactive Memory: A Contemporary Analysis of the Group Mind",
        "https://link.springer.com/chapter/10.1007/978-1-4612-4634-3_9"
    ),
    "clark1996using": (
        "Using Language",
        "https://www.cambridge.org/core/books/using-language/4E7EBC4EC742C26436F6CF187C43F239"
    ),
    "schmidhuber2003godel": (
        "Gödel Machines: Self-Referential Universal Problem Solvers",
        "https://arxiv.org/abs/cs/0309048"
    ),
    "biderman2024lora": (
        "LoRA Learns Less and Forgets Less",
        "https://arxiv.org/abs/2405.09673"
    ),
    "kirkpatrick2017overcoming": (
        "Overcoming catastrophic forgetting in neural networks",
        "https://arxiv.org/abs/1612.00796"
    ),
    "brown2020gpt3": (
        "Language models are few-shot learners",
        "https://arxiv.org/abs/2005.14165"
    ),
}




def is_fleet_citation(key: str, raw: str) -> bool:
    key_lower = key.lower()
    raw_lower = raw.lower()
    for prefix in FLEET_PREFIXES:
        if prefix in key_lower or prefix in raw_lower:
            return True
    if re.search(r"clawxiv\.\d{4}\.\d{5}", raw_lower):
        return True
    return False


def verify_arxiv_citation(key: str, raw: str) -> CitationResult:
    """Try to verify a citation against arXiv or the known-good list."""
    # Check known-good list first (for non-arXiv classics)
    key_lower = key.lower()
    for known_key, (title, url) in KNOWN_GOOD.items():
        if known_key in key_lower or key_lower in known_key:
            return CitationResult(key=key, raw=raw[:80], is_fleet=False,
                                  verified=True, title=title, url=url)

    # Look for arXiv ID in the raw entry
    arxiv_id = None
    m = re.search(r"arXiv[:\s]*(\d{4}\.\d{4,5}(?:v\d+)?)", raw, re.IGNORECASE)
    if m:
        arxiv_id = m.group(1)
    # Also check for abs/ URLs
    m2 = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", raw, re.IGNORECASE)
    if m2:
        arxiv_id = m2.group(1)

    if arxiv_id:
        return _fetch_arxiv(key, arxiv_id)

    # Try title search as fallback
    title_m = re.search(r'title\s*=\s*\{([^}]{10,})\}', raw, re.IGNORECASE)
    if title_m:
        return _search_arxiv_by_title(key, title_m.group(1))

    # Try extracting quoted title from \bibitem body
    quoted = re.search(r'"([^"]{15,})"', raw)
    if quoted:
        return _search_arxiv_by_title(key, quoted.group(1))

    return CitationResult(
        key=key, raw=raw[:120], is_fleet=False,
        verified=False, error="no arXiv ID or title found in entry"
    )


def _fetch_arxiv(key: str, arxiv_id: str) -> CitationResult:
    clean_id = re.sub(r"v\d+$", "", arxiv_id)
    url = f"https://arxiv.org/abs/{clean_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MACFleetLitGate/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Extract title from og:title or <title>
        m = re.search(r'<meta name="citation_title" content="([^"]+)"', html)
        if not m:
            m = re.search(r'\[[\d.]+\]\s+([^\n<]{10,})', html)
        title = m.group(1).strip() if m else f"arXiv:{clean_id}"
        return CitationResult(key=key, raw=f"arXiv:{clean_id}", is_fleet=False,
                              verified=True, title=title, url=url)
    except Exception as e:
        return CitationResult(key=key, raw=f"arXiv:{clean_id}", is_fleet=False,
                              verified=False, error=str(e))


def _search_arxiv_by_title(key: str, title: str) -> CitationResult:
    time.sleep(REQUEST_DELAY)
    query = urllib.parse.quote(f'ti:"{title[:80]}"')
    url = f"{ARXIV_API}?search_query={query}&max_results=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MACFleetLitGate/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
        entry_m = re.search(r"<entry>(.*?)</entry>", xml, re.DOTALL)
        if not entry_m:
            return CitationResult(key=key, raw=title[:80], is_fleet=False,
                                  verified=False, error="not found on arXiv")
        entry = entry_m.group(1)
        found_title = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        abs_url = re.search(r'<id>(.*?)</id>', entry)
        t = found_title.group(1).strip() if found_title else title
        u = abs_url.group(1).strip() if abs_url else None
        return CitationResult(key=key, raw=title[:80], is_fleet=False,
                              verified=True, title=t, url=u)
    except Exception as e:
        return CitationResult(key=key, raw=title[:80], is_fleet=False,
                              verified=False, error=str(e))


# ---------------------------------------------------------------------------
# Claim grounding search
# ---------------------------------------------------------------------------

def build_claim_queries(title: str, abstract: str, section_headers: list[str]) -> list[tuple[str, str]]:
    """
    Build (claim_label, search_query) pairs from paper metadata.
    One query per major section + one for the core abstract claim.
    """
    queries = []

    # Core abstract claim — first 2 sentences
    sentences = re.split(r'(?<=[.!?])\s+', abstract)
    if sentences:
        core = " ".join(sentences[:2])[:200]
        queries.append(("Abstract core claim", core))

    for header in section_headers[:6]:  # cap at 6 sections
        queries.append((f"Section: {header}", f"{header} {title[:60]}"))

    return queries


def _arxiv_query(url: str, retries: int = 3) -> str:
    """Fetch an arXiv API URL with exponential backoff on 429."""
    delay = REQUEST_DELAY
    for attempt in range(retries):
        time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MACFleetLitGate/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                delay = delay * 3 + 5  # back off hard on rate limit
                if attempt < retries - 1:
                    print(f"      (rate limited, retrying in {delay:.0f}s...)")
                    continue
            raise
        except Exception:
            raise
    return ""


def search_arxiv_for_claim(label: str, query: str) -> ClaimSearchResult:
    """Search arXiv for papers grounding a claim. Returns top hits."""
    q = urllib.parse.quote(query[:200])
    url = f"{ARXIV_API}?search_query=all:{q}&max_results=3&sortBy=relevance"
    result = ClaimSearchResult(claim=label, query=query)
    try:
        xml = _arxiv_query(url)
        entries = re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)
        for entry in entries:
            t = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
            u = re.search(r"<id>(.*?)</id>", entry)
            a = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
            result.hits.append({
                "title": t.group(1).strip() if t else "?",
                "url": u.group(1).strip() if u else None,
                "abstract": (a.group(1).strip()[:200] if a else "")
            })
        result.grounded = len(result.hits) > 0
    except Exception as e:
        result.hits = [{"title": f"search error: {e}", "url": None, "abstract": ""}]
    return result


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def run_gate(tex_path: Path, min_external: int) -> GateReport:
    tex = tex_path.read_text(errors="ignore")
    title, abstract = extract_metadata(tex)
    section_headers = extract_section_headers(tex)
    bib_keys = extract_bib_keys(tex)

    report = GateReport(paper_title=title, abstract=abstract[:300])

    print(f"\n📄 Paper: {title or tex_path.name}")
    print(f"   {len(bib_keys)} citations found, {len(section_headers)} claim sections\n")

    # --- Step 1: Verify every citation ---
    print("🔍 Verifying citations...")
    for key, raw in bib_keys:
        fleet = is_fleet_citation(key, raw)
        if fleet:
            cr = CitationResult(key=key, raw=raw[:80], is_fleet=True,
                                verified=True, title="[fleet paper]")
        else:
            cr = verify_arxiv_citation(key, raw)
            cr.is_fleet = False
            time.sleep(0.3)
        report.citations.append(cr)
        status = "✅" if cr.verified else "❌"
        kind = "fleet" if cr.is_fleet else "external"
        print(f"   {status} [{kind}] {key}: {cr.title or cr.error or '?'}")

    # --- Step 2: Search for grounding on each major claim ---
    print(f"\n🌐 Searching external literature for claim grounding...")
    queries = build_claim_queries(title, abstract, section_headers)
    for label, query in queries:
        cr = search_arxiv_for_claim(label, query)
        report.claim_results.append(cr)
        status = "✅" if cr.grounded else "⚠️ "
        print(f"   {status} {label}")
        for h in cr.hits[:2]:
            print(f"      → {h['title'][:80]}")

    # --- Step 3: Evaluate gate ---
    n_external = len(report.external_citations)
    n_verified = sum(1 for c in report.external_citations if c.verified)
    n_unverified = len(report.unverified_external)
    n_fleet = len(report.fleet_citations)
    n_claims = len(report.claim_results)
    n_grounded = len(report.grounded_claims)
    claim_coverage = n_grounded / n_claims if n_claims > 0 else 0.0

    print(f"\n{'='*60}")
    print(f"📊 Gate Results")
    print(f"   External citations : {n_external} ({n_verified} verified, {n_unverified} unverified)")
    print(f"   Fleet self-citations: {n_fleet}")
    print(f"   Claim grounding    : {n_grounded}/{n_claims} claim areas have external hits ({claim_coverage:.0%})")
    print(f"   Min external req   : {min_external}")
    print(f"{'='*60}")

    failures = []

    if n_external < min_external:
        failures.append(
            f"Insufficient external citations: {n_external} found, {min_external} required. "
            f"Add citations from the external literature for each major claim."
        )

    if n_unverified > 0:
        unverified_keys = [c.key for c in report.unverified_external]
        failures.append(
            f"{n_unverified} external citation(s) could not be verified: {', '.join(unverified_keys)}. "
            f"Check arXiv IDs and titles."
        )

    if n_fleet > 0 and n_external == 0:
        failures.append(
            "Paper cites only fleet papers with zero external citations. "
            "This is a closed epistemic loop — add external grounding."
        )

    if claim_coverage < MIN_CLAIM_COVERAGE and n_claims > 0:
        ungrounded = [cr.claim for cr in report.claim_results if not cr.grounded]
        failures.append(
            f"Only {claim_coverage:.0%} of claim areas have external literature hits. "
            f"Ungrounded sections: {', '.join(ungrounded[:4])}."
        )

    # Warn (not fail) on high self-citation ratio
    total = n_external + n_fleet
    if total > 0 and n_fleet / total > 0.5:
        print(f"⚠️  WARNING: {n_fleet}/{total} citations ({n_fleet/total:.0%}) are fleet self-citations.")
        print(f"   This is high. Consider adding more external grounding.")

    report.failure_reasons = failures
    report.passed = len(failures) == 0
    return report


def print_final_verdict(report: GateReport):
    if report.passed:
        print(f"\n✅ GATE PASSED — submission may proceed")
    else:
        print(f"\n🚫 GATE FAILED — submission blocked\n")
        for i, reason in enumerate(report.failure_reasons, 1):
            print(f"   {i}. {reason}")
        print(f"\nFix the above issues and re-run this gate before submitting.")

    # Always print recommended external papers to add
    if report.claim_results:
        print(f"\n📚 Suggested external papers to cite (from claim search):")
        seen = set()
        for cr in report.claim_results:
            for hit in cr.hits[:1]:
                if hit['url'] and hit['url'] not in seen:
                    seen.add(hit['url'])
                    print(f"   • {hit['title'][:70]}")
                    print(f"     {hit['url']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Lit review gate: verify external grounding before paper submission."
    )
    parser.add_argument("tex_file", help="Path to the LaTeX source file")
    parser.add_argument(
        "--min-external", type=int, default=MIN_EXTERNAL_DEFAULT,
        help=f"Minimum external citations required (default: {MIN_EXTERNAL_DEFAULT})"
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Print report but always exit 0 (non-blocking, for CI info runs)"
    )
    args = parser.parse_args()

    tex_path = Path(args.tex_file)
    if not tex_path.exists():
        print(f"ERROR: File not found: {tex_path}", file=sys.stderr)
        sys.exit(2)

    report = run_gate(tex_path, min_external=args.min_external)
    print_final_verdict(report)

    if args.report_only:
        sys.exit(0)

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
