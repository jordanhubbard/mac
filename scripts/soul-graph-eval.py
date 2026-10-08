#!/usr/bin/env python3
"""Replay a task ledger through SoulGraph and measure what it recalls.

SoulGraph's premise is that recency (the splay order), causal links (the DAG)
and cross-cutting tags help an agent recall the experience that matters now.
This harness tests that premise on real history instead of a hand-built toy.

The input is a task export (``mac task list --all --all-states --full-ids``)
or a fixture in the same shape: a list of objects with ``id``, ``title``,
``project``, ``created_at``, ``last_updated_at`` and ``dependencies``.

Replay, in timestamp order, with SoulGraph's clock pinned to the event time:

* a task is created  -> before it is added, every method is asked two
  questions from the task's title alone, scored separately:
    ``deps``        "what earlier work is this built on?" -- truth is the
                    dependencies that already exist;
    ``dependents``  "what existing work is waiting on this?" -- truth is the
                    earlier tasks that declared a dependency on this one
                    before it existed (an integrating task filed first, its
                    parts filed later: more than half the ledger's edges);
  then the task is added with its project as a tag, linked into the DAG in
  both directions, and each dependency it names is touched, because the new
  work used it;
* a task is updated  -> it is touched.
 Each method returns ten ids; the report gives recall@5,
recall@10, hit@10 (any dependency found) and MRR, plus mean latency.

Two splits are scored. ``all`` uses every dependency. Most of those were
created seconds earlier, by the same plan decomposition, so pure recency finds
them and they say little about memory. ``long_range`` keeps only dependencies
created at least ``--gap-hours`` (default 6) before the task: the work an agent
has stopped thinking about, which is what SoulGraph claims to recall.

Methods:

  recent       the ten most recently created tasks (pure-recency baseline)
  keyword      word overlap, newest first on ties (no recency, no DAG)
  bm25         BM25 over titles (strong lexical baseline)
  sg_search    SoulGraph.semantic_search: overlap boosted by splay recency
  sg_tag       sg_search restricted to the query's project tag
  sg_related   SoulGraph.related: sg_search hits, each followed by its DAG
               parents, then children
  sg_related0  SoulGraph.related(recency_weight=0): the DAG without recency
  bm25_dag     bm25 hits expanded the same way (DAG over a stronger ranker)
  sg_discover  SoulGraph.discover(touch=False): hits plus their ancestor paths
  sg_hot       SoulGraph.hot(10) (sampled: it re-ranks the whole graph)

Usage:
  scripts/soul-graph-eval.py TASKS.json [--project P] [--limit N] [--gap-hours H] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

from mac.soul_graph import SoulGraph, tokens

K = 10
HOT_SAMPLE_EVERY = 25


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _deps(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = ast.literal_eval(raw)
    return [str(d) for d in raw]


def load_tasks(path: Path, project: str | None = None, limit: int | None = None) -> List[dict]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("tasks", [])
    tasks = []
    for t in data:
        if not t.get("title") or not t.get("created_at"):
            continue
        if project and t.get("project") != project:
            continue
        created = _ts(t["created_at"])
        updated = _ts(t.get("last_updated_at") or t.get("updated_at") or t["created_at"])
        tasks.append(
            {
                "id": str(t["id"]),
                "title": str(t["title"]),
                "project": t.get("project") or "none",
                "created": created,
                "updated": max(created, updated),
                "deps": _deps(t.get("dependencies")),
            }
        )
    tasks.sort(key=lambda t: t["created"])
    if limit:
        tasks = tasks[:limit]
    return tasks


class _Lexical:
    """Keyword and BM25 baselines over the same tokens SoulGraph uses."""

    def __init__(self) -> None:
        self.postings: Dict[str, set] = defaultdict(set)
        self.length: Dict[str, int] = {}
        self.order: Dict[str, int] = {}
        self.total = 0

    def add(self, nid: str, text: str) -> None:
        words = tokens(text)
        for w in words:
            self.postings[w].add(nid)
        self.length[nid] = max(1, len(words))
        self.order[nid] = len(self.order)
        self.total += self.length[nid]

    def keyword(self, query: str) -> List[str]:
        overlap: Dict[str, int] = defaultdict(int)
        for w in tokens(query):
            for nid in self.postings.get(w, ()):
                overlap[nid] += 1
        ranked = sorted(overlap, key=lambda n: (overlap[n], self.order[n]), reverse=True)
        return ranked[:K]

    def bm25(self, query: str, k1: float = 1.2, b: float = 0.75) -> List[str]:
        n = len(self.length)
        if not n:
            return []
        avg = self.total / n
        score: Dict[str, float] = defaultdict(float)
        for w in tokens(query):
            ids = self.postings.get(w)
            if not ids:
                continue
            idf = math.log(1 + (n - len(ids) + 0.5) / (len(ids) + 0.5))
            for nid in ids:
                tf = 1.0  # titles: a word appears once
                score[nid] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * self.length[nid] / avg))
        ranked = sorted(score, key=lambda x: (score[x], self.order[x]), reverse=True)
        return ranked[:K]


def _with_links(graph: SoulGraph, ranked: Iterable[str]) -> List[str]:
    """Each hit, then its DAG parents, then its children, until K ids."""
    out: List[str] = []
    for nid in ranked:
        node = graph.nodes[nid]
        for candidate in [nid] + list(node.parents) + list(node.children):
            if candidate not in out:
                out.append(candidate)
        if len(out) >= K:
            break
    return out[:K]


def run(tasks: List[dict], gap_hours: float = 6.0) -> dict:
    now = [0.0]
    graph = SoulGraph(name="ledger", clock=lambda: now[0])
    lexical = _Lexical()
    created_order: List[str] = []

    events = []
    for t in tasks:
        events.append((t["created"], 0, "create", t))
        if t["updated"] > t["created"]:
            events.append((t["updated"], 1, "update", t))
    events.sort(key=lambda e: (e[0], e[1]))

    methods: Dict[str, Callable[[dict], List[str]]] = {
        "recent": lambda t: created_order[-K:][::-1],
        "keyword": lambda t: lexical.keyword(t["title"]),
        "bm25": lambda t: lexical.bm25(t["title"]),
        "sg_search": lambda t: [n.id for n, _ in graph.semantic_search(t["title"], K)],
        "sg_tag": lambda t: [
            n.id
            for n, _ in graph.semantic_search(
                t["title"], K, candidates=graph.tagged("project:" + t["project"])
            )
        ],
        "sg_related": lambda t: [n.id for n in graph.related(t["title"], K)],
        "sg_related0": lambda t: [n.id for n in graph.related(t["title"], K, recency_weight=0)],
        "bm25_dag": lambda t: _with_links(graph, lexical.bm25(t["title"])),
        "sg_discover": lambda t: _discover_ids(graph, t["title"]),
    }
    names = list(methods) + ["sg_hot"]
    totals: Dict[str, Dict[str, Dict[str, float]]] = {
        split: {
            m: {"recall5": 0.0, "recall10": 0.0, "hit10": 0.0, "mrr": 0.0, "seconds": 0.0, "n": 0}
            for m in names
        }
        for split in ("deps/all", "deps/long_range", "dependents/all", "dependents/long_range")
    }
    gap = gap_hours * 3600.0
    queries = 0
    edges = 0
    ingest_seconds = 0.0
    waiting_on: Dict[str, List[str]] = defaultdict(list)  # dep id -> earlier tasks needing it

    def evaluate(question: str, t: dict, truth: List[str], ts: float) -> None:
        old = [d for d in truth if graph.nodes[d].created_at <= ts - gap]
        for name, method in methods.items():
            started = time.perf_counter()
            ranked = method(t)
            elapsed = time.perf_counter() - started
            _score(totals[question + "/all"][name], ranked, truth, elapsed)
            if old:
                _score(totals[question + "/long_range"][name], ranked, old, elapsed)
        if queries % HOT_SAMPLE_EVERY == 0:
            started = time.perf_counter()
            ranked = [n.id for n in graph.hot(K)]
            elapsed = time.perf_counter() - started
            _score(totals[question + "/all"]["sg_hot"], ranked, truth, elapsed)
            if old:
                _score(totals[question + "/long_range"]["sg_hot"], ranked, old, elapsed)

    for ts, _, kind, t in events:
        now[0] = ts
        if kind == "update":
            if t["id"] in graph.nodes:
                started = time.perf_counter()
                graph.touch(t["id"])
                ingest_seconds += time.perf_counter() - started
            continue
        truth = [d for d in t["deps"] if d in graph.nodes]
        for dep in t["deps"]:
            if dep not in graph.nodes:
                waiting_on[dep].append(t["id"])
        dependents = [w for w in waiting_on.pop(t["id"], []) if w in graph.nodes]
        if truth:
            queries += 1
            evaluate("deps", t, truth, ts)
        if dependents:
            queries += 1
            evaluate("dependents", t, dependents, ts)
        started = time.perf_counter()
        graph.add(t["title"], tags={"project:" + t["project"]}, parents=truth, node_id=t["id"])
        for child in dependents:
            if graph.link(t["id"], child):
                edges += 1
        for dep in truth:
            graph.touch(dep, propagate_parents=False)
        ingest_seconds += time.perf_counter() - started
        lexical.add(t["id"], t["title"])
        created_order.append(t["id"])
        edges += len(truth)

    report = {
        "tasks": len(tasks),
        "edges": edges,
        "queries": queries,
        "ingest_seconds": round(ingest_seconds, 3),
        "gap_hours": gap_hours,
        "splits": {},
    }
    for split, by_method in totals.items():
        report["splits"][split] = {}
        for name, agg in by_method.items():
            n = agg["n"] or 1
            report["splits"][split][name] = {
                "queries": int(agg["n"]),
                "recall@5": round(agg["recall5"] / n, 4),
                "recall@10": round(agg["recall10"] / n, 4),
                "hit@10": round(agg["hit10"] / n, 4),
                "mrr": round(agg["mrr"] / n, 4),
                "mean_ms": round(1000 * agg["seconds"] / n, 3),
            }
    return report


def _discover_ids(graph: SoulGraph, query: str) -> List[str]:
    out: List[str] = []
    for node, path in graph.discover(query, top_k=K, touch=False):
        for candidate in [node.id] + [p for p in reversed(path) if p != node.id]:
            if candidate not in out:
                out.append(candidate)
        if len(out) >= K:
            break
    return out[:K]


def _score(agg: Dict[str, float], ranked: List[str], truth: List[str], seconds: float) -> None:
    truth_set = set(truth)
    agg["n"] += 1
    agg["seconds"] += seconds
    agg["recall5"] += len(truth_set & set(ranked[:5])) / len(truth_set)
    agg["recall10"] += len(truth_set & set(ranked[:K])) / len(truth_set)
    agg["hit10"] += 1.0 if truth_set & set(ranked[:K]) else 0.0
    for rank, nid in enumerate(ranked, start=1):
        if nid in truth_set:
            agg["mrr"] += 1.0 / rank
            break


def _table(report: dict) -> str:
    lines = ["tasks=%(tasks)d edges=%(edges)d queries=%(queries)d ingest=%(ingest_seconds)ss" % report]
    for split, by_method in report["splits"].items():
        label = split if split.endswith("/all") else "%s (truth >= %gh old)" % (split, report["gap_hours"])
        lines += [
            "",
            label,
            "%-12s %8s %9s %10s %7s %7s %9s" % ("method", "queries", "recall@5", "recall@10", "hit@10", "mrr", "mean_ms"),
        ]
        for name, m in by_method.items():
            lines.append(
                "%-12s %8d %9.3f %10.3f %7.3f %7.3f %9.3f"
                % (name, m["queries"], m["recall@5"], m["recall@10"], m["hit@10"], m["mrr"], m["mean_ms"])
            )
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tasks", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--gap-hours", type=float, default=6.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(load_tasks(args.tasks, project=args.project, limit=args.limit), gap_hours=args.gap_hours)
    print(json.dumps(report, indent=2) if args.json else _table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
