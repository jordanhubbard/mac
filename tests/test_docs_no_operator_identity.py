"""Guard checked-in docs/skills against per-user / per-fleet operator identity.

The repo must read as generic for ANY fleet owner. Checked-in docs, skills, and
deploy markdown must never name a specific fleet's agents, hosts, or operator —
those belong only in the home-scoped fleet registry (``~/.mac/fleets.yaml`` /
``~/.mac/specs/``), never in Git. This test codifies that so an example fleet's
real names (and the de-personalized ``hostX`` / ``devuser`` placeholders, which
are still per-fleet example identity) cannot leak back into the documentation.

Use generic, readable placeholders/role names instead: ``hub`` / ``<hub-node>``,
``worker-1`` / ``<worker-node>``, ``gpu-worker`` / ``<gpu-node>``, ``<user>``,
``<host>``, ``<mesh-ip>``.

This test is kept IDENTICAL across the mac and mac-dev forks so the two stay
unified.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF_REL = "tests/test_docs_no_operator_identity.py"

# Per-user / per-fleet operator identity that must never appear in a checked-in
# doc/skill. Includes real example-fleet agent/host names AND the
# de-personalized ``hostX`` / ``devuser`` / ``agentuser`` placeholders, which are
# still per-fleet example identity rather than generic role names.
#
# NOTE: ``NVIDIA-dev`` / ``jordanhubbard`` (this fork's real repo/clone org
# identity) are deliberately NOT forbidden.
FORBIDDEN = [
    "rocky",
    "natasha",
    "bullwinkle",
    "madmax",
    "puck",
    "sparky",
    "worker2",
    "jkh",
    "do-host",
    "hosta",
    "hostb",
    "hostc",
    "hostd",
    "hoste",
    "hostf",
    "devuser",
    "agentuser",
]

IDENTITY = re.compile(
    r"(?i)\b(?:%s)\b" % "|".join(re.escape(t) for t in FORBIDDEN)
)


def _is_in_scope(rel: str) -> bool:
    """Doc/markdown/skill files this guard walks.

    Scope: ``docs/``, ``skills/``, ``deploy/skills/``, ``deploy/*.md`` /
    ``deploy/**/*.md``, and root ``*.md``. Excludes vendored Hermes, the ticket
    mirrors, and this test file itself.
    """
    if rel.startswith("src/mac/_hermes/"):
        return False
    if rel.startswith(".tickets/"):
        return False
    if rel == SELF_REL:
        return False
    if rel.startswith("docs/"):
        return True
    if rel.startswith("skills/"):
        return True
    if rel.startswith("deploy/skills/"):
        return True
    if rel.startswith("deploy/") and rel.endswith(".md"):
        return True
    if re.fullmatch(r"[^/]+\.md", rel):
        return True
    return False


def _tracked_docs() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [rel for rel in out.stdout.splitlines() if _is_in_scope(rel)]


def test_docs_carry_no_operator_or_per_fleet_identity():
    docs = _tracked_docs()
    assert docs, "expected to find checked-in docs/skills to scan"
    offenders: list[str] = []
    for rel in docs:
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if IDENTITY.search(line):
                offenders.append("%s:%d: %s" % (rel, lineno, line.strip()))
    assert not offenders, (
        "per-user / per-fleet operator identity leaked into checked-in docs "
        "(use generic placeholders/role names: hub / <hub-node>, worker-1 / "
        "<worker-node>, gpu-worker / <gpu-node>, <user>, <host>, <mesh-ip>):\n"
        + "\n".join(offenders)
    )
