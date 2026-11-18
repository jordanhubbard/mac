"""
Tests for scripts/lit_review_gate.py

Tests the pure-Python logic using importlib + mock — no network, no disk.
Covers: LaTeX parsing, citation classification, gate evaluation logic,
known-good lookup, arXiv ID extraction, claim query building.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load the script as a module
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "lit_review_gate.py"


def _load_gate() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("lit_review_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Must register before exec_module so @dataclass works on Python 3.14
    sys.modules["lit_review_gate"] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


_gate = _load_gate()


# ---------------------------------------------------------------------------
# Minimal LaTeX fixture
# ---------------------------------------------------------------------------

MINIMAL_TEX = r"""
\title{Multi-Agent Memory Compression via Soul Graphs}
\begin{abstract}
We present a novel approach to memory management in multi-agent systems.
Our method reduces memory footprint by 60\% while preserving recall fidelity.
\end{abstract}
\section{Introduction}
\section{Methodology}
\section{Experiments and Results}
\section{Related Work}
\section{Conclusion}

\begin{thebibliography}{99}
\bibitem{vaswani2017attention} A. Vaswani et al., Attention Is All You Need, arXiv:1706.03762
\bibitem{brown2020gpt3} T. Brown et al., Language Models Are Few-Shot Learners, arXiv:2005.14165
\bibitem{clawxiv.2611.00001} Fleet paper 1, MACFleet Bullwinkle, 2026
\end{thebibliography}
"""

FLEET_TEX = r"""
\title{Fleet-only Paper}
\begin{abstract}Fleet abstract.\end{abstract}
\begin{thebibliography}{99}
\bibitem{clawxiv.2611.00001} Fleet paper 1
\bibitem{clawxiv.2611.00002} Fleet paper 2
\end{thebibliography}
"""


# ---------------------------------------------------------------------------
# LaTeX Parsing
# ---------------------------------------------------------------------------

class TestExtractMetadata(unittest.TestCase):
    def test_extracts_title(self):
        title, _ = _gate.extract_metadata(MINIMAL_TEX)
        assert "Multi-Agent" in title

    def test_extracts_abstract(self):
        _, abstract = _gate.extract_metadata(MINIMAL_TEX)
        assert "memory management" in abstract.lower()

    def test_missing_title_returns_empty(self):
        title, _ = _gate.extract_metadata(r"\begin{abstract}foo\end{abstract}")
        assert title == ""

    def test_missing_abstract_returns_empty(self):
        _, abstract = _gate.extract_metadata(r"\title{Hello}")
        assert abstract == ""


class TestExtractSectionHeaders(unittest.TestCase):
    def test_finds_methodology_and_experiments(self):
        headers = _gate.extract_section_headers(MINIMAL_TEX)
        assert "Methodology" in headers
        assert "Experiments and Results" in headers

    def test_skips_boilerplate_headers(self):
        headers = _gate.extract_section_headers(MINIMAL_TEX)
        boilerplate = {"Introduction", "Conclusion", "Related Work"}
        assert not (boilerplate & set(headers)), \
            f"Boilerplate headers leaked through: {boilerplate & set(headers)}"

    def test_empty_tex_returns_empty_list(self):
        assert _gate.extract_section_headers("no sections here") == []


class TestExtractBibKeys(unittest.TestCase):
    def test_finds_all_bib_entries(self):
        pairs = _gate.extract_bib_keys(MINIMAL_TEX)
        keys = [k for k, _ in pairs]
        assert "vaswani2017attention" in keys
        assert "brown2020gpt3" in keys
        assert "clawxiv.2611.00001" in keys

    def test_returns_tuple_pairs(self):
        pairs = _gate.extract_bib_keys(MINIMAL_TEX)
        assert all(len(p) == 2 for p in pairs)

    def test_empty_tex_returns_empty_list(self):
        assert _gate.extract_bib_keys("no bibliography") == []


# ---------------------------------------------------------------------------
# Citation classification
# ---------------------------------------------------------------------------

class TestIsFleetCitation(unittest.TestCase):
    def test_clawxiv_key_is_fleet(self):
        assert _gate.is_fleet_citation("clawxiv.2611.00001", "fleet paper")

    def test_fleet_prefix_in_raw_is_fleet(self):
        assert _gate.is_fleet_citation("somekey", "clawxiv.2611.00002 Bullwinkle 2026")

    def test_arxiv_key_is_not_fleet(self):
        assert not _gate.is_fleet_citation("vaswani2017", "Attention Is All You Need arXiv:1706.03762")

    def test_rocky_prefix_is_fleet(self):
        assert _gate.is_fleet_citation("rocky_theorem", "rocky's theorem paper")

    def test_natasha_prefix_is_fleet(self):
        assert _gate.is_fleet_citation("natasha_q95", "natasha_q95 paper")


# ---------------------------------------------------------------------------
# Citation verification — known-good list
# ---------------------------------------------------------------------------

class TestVerifyArxivCitationKnownGood(unittest.TestCase):
    def test_brown_gpt3_resolves_from_known_good(self):
        result = _gate.verify_arxiv_citation(
            "brown2020gpt3",
            "T. Brown et al., Language models are few-shot learners"
        )
        assert result.verified is True
        assert result.url is not None
        assert "2005.14165" in result.url or "arxiv" in result.url.lower()

    def test_kirkpatrick_resolves_from_known_good(self):
        result = _gate.verify_arxiv_citation(
            "kirkpatrick2017overcoming",
            "Overcoming catastrophic forgetting"
        )
        assert result.verified is True

    def test_biderman_lora_resolves_from_known_good(self):
        result = _gate.verify_arxiv_citation(
            "biderman2024lora",
            "LoRA Learns Less and Forgets Less"
        )
        assert result.verified is True


# ---------------------------------------------------------------------------
# Citation verification — arXiv ID path (mocked network)
# ---------------------------------------------------------------------------

class TestVerifyArxivCitationArxivID(unittest.TestCase):
    def test_fetches_arxiv_id_from_raw(self):
        fake_html = b'<meta name="citation_title" content="Attention Is All You Need">'
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_html
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _gate.verify_arxiv_citation(
                "vaswani2017attention",
                "A. Vaswani et al., arXiv:1706.03762"
            )
        assert result.verified is True
        assert "1706.03762" in result.url

    def test_network_failure_returns_unverified(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = _gate.verify_arxiv_citation(
                "someref",
                "Some paper arXiv:1234.56789"
            )
        assert result.verified is False
        assert result.error is not None

    def test_no_arxiv_id_no_title_returns_unverified(self):
        result = _gate.verify_arxiv_citation(
            "mystery2024",
            "A paper with no ID and no quoted title"
        )
        # Should fall through to unverified (no network needed)
        assert result.verified is False


# ---------------------------------------------------------------------------
# Claim query building
# ---------------------------------------------------------------------------

class TestBuildClaimQueries(unittest.TestCase):
    def test_includes_abstract_core_claim(self):
        queries = _gate.build_claim_queries(
            "My Paper",
            "We present a novel method. It achieves 90% accuracy.",
            []
        )
        labels = [label for label, _ in queries]
        assert any("Abstract" in label for label in labels)

    def test_includes_section_queries(self):
        queries = _gate.build_claim_queries(
            "My Paper",
            "Abstract sentence.",
            ["Methodology", "Experiments"]
        )
        labels = [label for label, _ in queries]
        assert any("Methodology" in label for label in labels)
        assert any("Experiments" in label for label in labels)

    def test_caps_at_six_sections(self):
        sections = [f"Section {i}" for i in range(10)]
        queries = _gate.build_claim_queries("Title", "Abstract.", sections)
        section_queries = [(l, q) for l, q in queries if l.startswith("Section")]
        assert len(section_queries) <= 6


# ---------------------------------------------------------------------------
# Gate evaluation logic (mocked run_gate internals)
# ---------------------------------------------------------------------------

class TestGateReport(unittest.TestCase):
    def _make_report(self, citations, claim_results=None, min_ext=5):
        """Build a GateReport and manually set pass/fail criteria."""
        report = _gate.GateReport(
            paper_title="Test Paper",
            abstract="Test abstract.",
        )
        report.citations = citations
        report.claim_results = claim_results or []
        # Run the gate decision logic inline
        n_external = len(report.external_citations)
        n_unverified = len(report.unverified_external)
        n_fleet = len(report.fleet_citations)
        n_claims = len(report.claim_results)
        n_grounded = len(report.grounded_claims)
        claim_coverage = n_grounded / n_claims if n_claims > 0 else 1.0

        failures = []
        if n_external < min_ext:
            failures.append(f"Insufficient external citations: {n_external}")
        if n_unverified > 0:
            failures.append(f"{n_unverified} unverified")
        if n_fleet > 0 and n_external == 0:
            failures.append("closed epistemic loop")
        if claim_coverage < _gate.MIN_CLAIM_COVERAGE and n_claims > 0:
            failures.append("insufficient claim grounding")

        report.failure_reasons = failures
        report.passed = len(failures) == 0
        return report

    def _ext(self, key, verified=True):
        return _gate.CitationResult(
            key=key, raw=key, is_fleet=False, verified=verified
        )

    def _fleet(self, key):
        return _gate.CitationResult(
            key=key, raw=key, is_fleet=True, verified=True
        )

    def _claim(self, grounded=True):
        cr = _gate.ClaimSearchResult(claim="test", query="test")
        cr.grounded = grounded
        return cr

    def test_passes_with_enough_verified_external(self):
        cits = [self._ext(f"ref{i}") for i in range(5)]
        report = self._make_report(cits)
        assert report.passed is True

    def test_fails_insufficient_external_citations(self):
        cits = [self._ext("ref1"), self._ext("ref2")]
        report = self._make_report(cits)
        assert report.passed is False
        assert any("Insufficient" in r for r in report.failure_reasons)

    def test_fails_unverified_external_citation(self):
        cits = [self._ext(f"ref{i}") for i in range(5)]
        cits.append(self._ext("unverified_ref", verified=False))
        report = self._make_report(cits)
        assert report.passed is False
        assert any("unverified" in r.lower() for r in report.failure_reasons)

    def test_fails_fleet_only_citations(self):
        cits = [self._fleet(f"clawxiv.{i}") for i in range(3)]
        report = self._make_report(cits, min_ext=1)
        assert report.passed is False
        assert any("epistemic" in r for r in report.failure_reasons)

    def test_external_citations_property_excludes_fleet(self):
        report = _gate.GateReport(paper_title="T", abstract="A")
        report.citations = [
            self._ext("real"),
            self._fleet("fleet1"),
            self._ext("real2"),
        ]
        assert len(report.external_citations) == 2

    def test_fleet_citations_property(self):
        report = _gate.GateReport(paper_title="T", abstract="A")
        report.citations = [self._ext("real"), self._fleet("fleet1")]
        assert len(report.fleet_citations) == 1

    def test_grounded_claims_property(self):
        report = _gate.GateReport(paper_title="T", abstract="A")
        report.claim_results = [
            self._claim(grounded=True),
            self._claim(grounded=False),
            self._claim(grounded=True),
        ]
        assert len(report.grounded_claims) == 2

    def test_unverified_external_property(self):
        report = _gate.GateReport(paper_title="T", abstract="A")
        report.citations = [
            self._ext("ok", verified=True),
            self._ext("bad", verified=False),
            self._fleet("f1"),
        ]
        assert len(report.unverified_external) == 1


# ---------------------------------------------------------------------------
# arxiv_query retries on 429 (mocked)
# ---------------------------------------------------------------------------

class TestArxivQueryRetry(unittest.TestCase):
    def test_retries_on_429(self):
        import urllib.error
        resp_429 = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)

        call_count = [0]

        def _side_effect(*a, **kw):
            call_count[0] += 1
            if call_count[0] < 3:
                raise resp_429
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"<xml/>"
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=_side_effect):
            with patch("time.sleep"):  # don't actually sleep
                result = _gate._arxiv_query("http://fake.arxiv.org", retries=3)
        assert call_count[0] == 3
        assert result == "<xml/>"


if __name__ == "__main__":
    unittest.main()
