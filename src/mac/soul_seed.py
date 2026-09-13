"""
mac.soul_seed — Minimal session bootstrap for the soul graph.

The problem Reto identified (2026-09-12):
  A reflexive soul_hot() at every session start is just flat memory with
  extra steps — same tokens, same nodes, same self every time. It defeats
  the point of a splay tree.

Solution: a cheap, context-shaped seed that:
  1. Extracts 3-5 signal words from the incoming session context
  2. Checks whether those words overlap with already-hot nodes
  3. Only queries soul_discover() when the context is novel
  4. Returns a minimal injection — enough to know what to be, not a dump

Token budget target: < 200 tokens at startup. The rest loads on demand
via soul_touch() as the session actually uses nodes.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from mac.soul_graph import SoulGraph

# Words that carry no signal — filtered before seed extraction
_STOP = frozenset({
    "the", "a", "an", "is", "are", "was", "be", "to", "of", "and",
    "or", "in", "on", "at", "for", "with", "this", "that", "it",
    "i", "you", "we", "they", "he", "she", "can", "will", "how",
    "what", "when", "where", "why", "do", "did", "have", "has",
    "please", "just", "get", "let", "me", "my", "your", "so",
})

_SOUL_DIR = Path.home() / ".hermes"


def _extract_signal(text: str, n: int = 5) -> list[str]:
    """Pull n highest-signal words from session context."""
    words = re.findall(r"[a-z]{3,}", text.lower())
    seen: dict[str, int] = {}
    for w in words:
        if w not in _STOP:
            seen[w] = seen.get(w, 0) + 1
    # Sort by frequency, take top n
    return [w for w, _ in sorted(seen.items(), key=lambda x: -x[1])][:n]


def _hot_words(g: SoulGraph, n: int = 8) -> set[str]:
    """Words already present in the hot splay nodes."""
    words: set[str] = set()
    for node in g.hot(n):
        words |= set(re.findall(r"[a-z]{3,}", node.content.lower()))
    return words


def seed(
    session_context: str,
    soul_name: str = "soul",
    novelty_threshold: float = 0.4,
    max_inject_nodes: int = 3,
) -> dict:
    """
    Bootstrap soul context for a new session.

    Returns a dict with:
      "inject"   — list of (content, tags) to prepend to context; may be empty
      "queried"  — whether soul_discover was actually called
      "signal"   — the words extracted from session context
      "reason"   — why inject is what it is

    novelty_threshold: fraction of signal words NOT already in hot nodes
    that triggers a discover() call. Below threshold = already primed,
    skip the query.
    """
    soul_path = _SOUL_DIR / f"{soul_name}_soul.json"
    if not soul_path.exists():
        return {"inject": [], "queried": False, "signal": [], "reason": "no soul file"}

    g = SoulGraph.load(soul_path)
    signal = _extract_signal(session_context)

    if not signal:
        return {"inject": [], "queried": False, "signal": [], "reason": "no signal"}

    # Check overlap with already-hot nodes
    hot = _hot_words(g)
    novel = [w for w in signal if w not in hot]
    novelty = len(novel) / len(signal)

    if novelty < novelty_threshold:
        # Context is familiar — hot nodes are already right, don't query
        return {
            "inject": [],
            "queried": False,
            "signal": signal,
            "reason": f"familiar (novelty={novelty:.2f} < {novelty_threshold})",
        }

    # Novel context — run a targeted discover
    query = " ".join(signal)
    results = g.discover(query, top_k=max_inject_nodes)

    # Save touched nodes
    g.save(soul_path)

    inject = [
        {"content": node.content, "tags": sorted(node.tags), "path_len": len(path)}
        for node, path in results
    ]

    return {
        "inject": inject,
        "queried": True,
        "signal": signal,
        "novel": novel,
        "reason": f"novel context (novelty={novelty:.2f})",
    }


def format_inject(seed_result: dict, max_chars: int = 300) -> Optional[str]:
    """
    Format seed result as a minimal context prefix.
    Returns None if nothing to inject (familiar context).
    Budget: ~200 tokens max. Enough to know what to be, not a dump.
    """
    nodes = seed_result.get("inject", [])
    if not nodes:
        return None

    lines = ["[soul context]"]
    chars = 0
    for n in nodes:
        line = f"• {n['content']}"
        if chars + len(line) > max_chars:
            break
        lines.append(line)
        chars += len(line)

    return "\n".join(lines)
