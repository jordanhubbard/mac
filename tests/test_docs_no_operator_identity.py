"""Guard: checked-in docs/skills must read as generic for ANY fleet owner.

The mac repo documents a *generic* fleet, never the maintainer's specific one.
Operational identity — real agent names, the operator user, concrete hosts —
belongs OUTSIDE git (in ~/.mac/specs / ~/.mac/fleets.yaml), not in the docs that
travel with the repo. This test fails loudly the moment any such token sneaks
back into a checked-in doc, skill, or deploy markdown file.

It forbids BOTH de-personalized example schemes so neither fork's identity can
leak: the maintainer's real fleet names (rocky/natasha/bullwinkle/madmax/puck/
sparky/jkh/do-host...) AND the older mac-dev placeholders (hosta..hostf,
worker2, devuser, agentuser). It does NOT forbid `jordanhubbard` / `NVIDIA-dev`
— those are this fork's legitimate repo-org slug, not operator identity.

Genericize new docs with READABLE role names instead: hub / worker-1 /
worker-2 / gpu-worker, and placeholders like <user> / <host> / <mesh-ip>.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# This test file itself legitimately *names* the forbidden tokens (in the regex
# and docstring), so it must be excluded from the scan.
SELF = Path(__file__).resolve()

# Operational-identity tokens that must never appear in a checked-in doc.
# Matched word-boundary + case-insensitive. Includes the maintainer's real
# fleet identity AND the de-personalized mac-dev example names, so neither
# fork's example identity can come back.
_TOKENS = [
    "rocky",
    "natasha",
    "bullwinkle",
    "madmax",
    "puck",
    "sparky",
    "worker2",
    "jkh",
    "hosta",
    "hostb",
    "hostc",
    "hostd",
    "hoste",
    "hostf",
    "devuser",
    "agentuser",
]
# Boundaries are alphanumeric-only (NOT \b), so an underscore counts as a
# separator: this catches `agent_rocky` / `hermes_hosta` forms that a `\b`
# regex misses because `_` is a word character. `do-host` is matched with the
# same left boundary (the hyphen already terminates it on the right).
IDENTITY = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in _TOKENS) + r")(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])do-host",
    re.IGNORECASE,
)


def _scanned_files() -> list[Path]:
    """git-tracked docs/skills/deploy-md/root-md, minus the excluded trees."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files: list[Path] = []
    for rel in out.stdout.splitlines():
        # Exclusions first.
        if rel.startswith("src/mac/_hermes/") or rel.startswith(".tickets/"):
            continue
        path = (ROOT / rel).resolve()
        if path == SELF:
            continue
        # In-scope globs.
        in_docs = rel.startswith("docs/")
        in_skills = rel.startswith("skills/")
        in_deploy_skills = rel.startswith("deploy/skills/")
        in_deploy_md = rel.startswith("deploy/") and rel.endswith(".md")
        in_root_md = "/" not in rel and rel.endswith(".md")
        if in_docs or in_skills or in_deploy_skills or in_deploy_md or in_root_md:
            files.append(path)
    return files


def test_docs_carry_no_operator_identity():
    scanned = _scanned_files()
    assert scanned, "expected to scan at least one checked-in doc/skill file"
    offenders: list[str] = []
    for path in scanned:
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            match = IDENTITY.search(line)
            if match:
                offenders.append(
                    "%s:%d: %r (matched %r)"
                    % (path.relative_to(ROOT), lineno, line.strip(), match.group(0))
                )
    assert not offenders, (
        "operator/per-fleet identity leaked into checked-in docs — the repo must "
        "read as generic for any fleet owner. Replace with readable role names "
        "(hub / worker-1 / worker-2 / gpu-worker) or placeholders "
        "(<user> / <host> / <mesh-ip>):\n" + "\n".join(offenders)
    )
