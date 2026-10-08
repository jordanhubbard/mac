"""The SoulGraph replay harness, on a scrubbed sample of the real task ledger.

tests/fixtures/soul_graph/ledger-sample.json was cut from the MAC task ledger:
the tasks behind dependency edges at least six hours long, the siblings that
share their dependencies, and unrelated tasks from the same period, with ids
renumbered, timestamps shifted and titles scrubbed. The test pins the finding
that justifies SoulGraph's retrieval: walking the DAG from search hits recalls
old related work better than keyword search or BM25 alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "soul_graph" / "ledger-sample.json"


def _harness():
    spec = importlib.util.spec_from_file_location("soul_graph_eval", ROOT / "scripts" / "soul-graph-eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quality(report):
    return {
        split: {m: {k: v for k, v in r.items() if k != "mean_ms"} for m, r in methods.items()}
        for split, methods in report["splits"].items()
    }


def test_dag_retrieval_recalls_old_work_better_than_lexical_search():
    harness = _harness()
    report = harness.run(harness.load_tasks(FIXTURE))
    old = report["splits"]["dependents/long_range"]
    assert old["sg_related"]["queries"] >= 200
    for metric in ("recall@5", "recall@10", "mrr"):
        assert old["sg_related"][metric] > old["keyword"][metric]
        assert old["sg_related"][metric] > old["bm25"][metric]
    # The DAG, not just the recency boost: related() with recency off still
    # beats keyword search on the same question.
    assert old["sg_related0"]["recall@10"] > old["keyword"]["recall@10"]
    # Pure recency cannot find work that is hours old.
    assert old["recent"]["recall@10"] < 0.1


def test_the_replay_is_deterministic():
    harness = _harness()
    tasks = harness.load_tasks(FIXTURE)
    assert _quality(harness.run(tasks)) == _quality(harness.run(tasks))


def test_the_table_renders_every_split():
    harness = _harness()
    table = harness._table(harness.run(harness.load_tasks(FIXTURE, limit=200)))
    for split in ("deps/all", "deps/long_range", "dependents/all", "dependents/long_range"):
        assert split in table
