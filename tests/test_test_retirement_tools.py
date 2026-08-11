"""The retirement gate: what may be deleted, and on what evidence.

The suite grew for a year by adding tests with every change and never removing
one. These two tools are the missing half. The report says WHERE to look; the
mutation check decides. The rules that matter are the conservative ones, so
they are what these tests pin.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REPORT = _load("test-redundancy-report")
DISCRIM = _load("test-discrimination")


def _map(sigs):
    """An impact map whose tests cover the given (file, line) sets."""
    nodeids = sorted(sigs)
    index = {n: i for i, n in enumerate(nodeids)}
    file_line: dict = {}
    for nodeid, pairs in sigs.items():
        for filename, line in pairs:
            file_line.setdefault(filename, {}).setdefault(str(line), []).append(index[nodeid])
    return {"nodeids": nodeids, "file_line_tests": file_line, "file_tests": {}}


def test_identical_coverage_is_reported_as_a_cluster():
    pairs = {("src/mac/a.py", n) for n in range(1, 21)}
    impact = _map({"tests/test_a.py::one": pairs, "tests/test_a.py::two": pairs})

    report = REPORT.build_report(impact, {"tests": []})

    assert report["clusters"] == 1
    assert report["retirement_candidates"] == 1


def test_one_member_of_a_cluster_is_always_kept():
    """The code still has to be exercised. A report that proposes deleting a
    whole cluster is proposing to stop testing that code."""
    pairs = {("src/mac/a.py", n) for n in range(1, 21)}
    impact = _map({"tests/test_a.py::%d" % i: pairs for i in range(5)})

    cluster = REPORT.build_report(impact, {"tests": []})["top_clusters"][0]

    assert len(cluster["candidates"]) == cluster["size"] - 1
    assert cluster["keep"] not in cluster["candidates"]


def test_an_incidental_signature_is_not_reported():
    """The largest raw cluster was 49 tests sharing 3 lines across 6 unrelated
    files -- tests that barely touch mapped source, matching by accident.
    Ranking those first sends the reviewer where there is nothing to find."""
    tiny = {("src/mac/a.py", 1), ("src/mac/a.py", 2)}
    impact = _map({"tests/test_a.py::one": tiny, "tests/test_b.py::two": tiny})

    report = REPORT.build_report(impact, {"tests": []})

    assert report["clusters"] == 0
    assert report["clusters_all"] == 1


def test_saving_counts_the_fixture_cost_not_just_the_assertions():
    """Test bodies are 34 minutes of a 63-minute job and half finish under
    10ms. What a test actually costs is its own Postgres schema, so a 1ms test
    is nearly as expensive to keep as a 500ms one."""
    pairs = {("src/mac/a.py", n) for n in range(1, 21)}
    impact = _map({"tests/test_a.py::one": pairs, "tests/test_a.py::two": pairs})

    cluster = REPORT.build_report(impact, {"tests": []})["top_clusters"][0]

    assert cluster["seconds_saved_if_retired"] >= REPORT.FIXTURE_COST_SECONDS


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def _analysis(kills, nodeids):
    """Drive the cover/verdict logic with a known kill matrix."""
    import types

    captured = {}

    def fake_run(ids, *, timeout):
        mutant = captured["mutant"]
        return {n for n in ids if mutant in kills.get(n, [])}

    return fake_run


def test_a_test_that_catches_nothing_unique_is_retirable(monkeypatch, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("def f(a, b):\n    return a == b\n", encoding="utf-8")
    monkeypatch.setattr(DISCRIM, "ROOT", tmp_path)
    monkeypatch.setattr(DISCRIM, "run_tests", lambda ids, timeout: {"a", "b"})

    result = DISCRIM.analyse(["a", "b"], target, {2}, limit=4, timeout=5)

    assert result["retirable"] == ["b"] or result["retirable"] == ["a"]
    assert len(result["must_keep"]) == 1


def test_a_test_nothing_else_can_replace_is_kept(monkeypatch, tmp_path):
    """The protective half. Coverage said these were interchangeable; only the
    mutation shows one of them is the reason a fault is caught."""
    target = tmp_path / "mod.py"
    target.write_text("def f(a, b):\n    return a == b\n", encoding="utf-8")
    monkeypatch.setattr(DISCRIM, "ROOT", tmp_path)
    monkeypatch.setattr(DISCRIM, "run_tests", lambda ids, timeout: {"only_this_one"})

    result = DISCRIM.analyse(["only_this_one", "other"], target, {2}, limit=4, timeout=5)

    assert result["must_keep"] == ["only_this_one"]
    assert result["retirable"] == ["other"]


def test_catching_nothing_withholds_the_verdict(monkeypatch, tmp_path):
    """Nothing caught means the mutations were too weak OR the tests are, and
    those need different answers. It must not read as "delete them all"."""
    target = tmp_path / "mod.py"
    target.write_text("def f(a, b):\n    return a == b\n", encoding="utf-8")
    monkeypatch.setattr(DISCRIM, "ROOT", tmp_path)
    monkeypatch.setattr(DISCRIM, "run_tests", lambda ids, timeout: set())

    result = DISCRIM.analyse(["a", "b"], target, {2}, limit=4, timeout=5)

    assert result["verdict"] == "no_evidence"


def test_thin_evidence_is_labelled_weak(monkeypatch, tmp_path):
    """27 tests reduced to a cover of 1 by three mutations is not proof that 26
    are worthless -- it is proof the operators cannot tell them apart, which is
    the normal case for parametrised tests over a mapping table."""
    target = tmp_path / "mod.py"
    target.write_text("def f(a, b):\n    return a == b\n", encoding="utf-8")
    monkeypatch.setattr(DISCRIM, "ROOT", tmp_path)
    monkeypatch.setattr(DISCRIM, "run_tests", lambda ids, timeout: {"a"})

    result = DISCRIM.analyse(["a", "b"], target, {2}, limit=4, timeout=5)

    assert result["evidence"] == "weak"


def test_the_source_file_is_restored(monkeypatch, tmp_path):
    """A tool that leaves the tree mutated is a tool nobody runs twice."""
    target = tmp_path / "mod.py"
    original = "def f(a, b):\n    return a == b\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(DISCRIM, "ROOT", tmp_path)
    monkeypatch.setattr(DISCRIM, "run_tests", lambda ids, timeout: {"a"})

    DISCRIM.analyse(["a", "b"], target, {2}, limit=4, timeout=5)

    assert target.read_text(encoding="utf-8") == original


def test_the_file_is_restored_even_when_the_run_explodes(monkeypatch, tmp_path):
    target = tmp_path / "mod.py"
    original = "def f(a, b):\n    return a == b\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(DISCRIM, "ROOT", tmp_path)

    def boom(ids, timeout):
        raise RuntimeError("pytest died")

    monkeypatch.setattr(DISCRIM, "run_tests", boom)

    try:
        DISCRIM.analyse(["a"], target, {2}, limit=4, timeout=5)
    except RuntimeError:
        pass

    assert target.read_text(encoding="utf-8") == original
