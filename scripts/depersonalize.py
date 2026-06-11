#!/usr/bin/env python3
"""De-personalize a source tree for the public NVIDIA-dev/mac mirror.

Single source of truth for the placeholder mapping. Two modes:

    depersonalize.py scrub <dir>     # rewrite files in <dir> in place
    depersonalize.py check <dir>     # exit 1 if any personal token remains

`check` is the fail-closed gate used by sync-public-mirror.sh: the mirror is
NEVER pushed if a real name/IP/handle survives the scrub.

This file lives in the PRIVATE source repo only (it lists the real names on the
left-hand side of the mapping); it is itself excluded from the public mirror.
"""
from __future__ import annotations
import os
import re
import sys

# --- mapping (the LHS is the personal data we strip) --------------------------

# Distinctive agent/host names. Replaced UNBOUNDED so we also catch occurrences
# glued to a string escape (e.g. "...\n\nrocky\n" where 'rocky' follows the 'n'
# of \n). The placeholders are valid Python identifiers AND hostname labels, so
# identifier/hostname uses (e.g. `rocky = register_worker(...)`) stay valid.
AGENT_NAMES = {
    "rocky": "hosta",
    "madmax": "hostb",
    "natasha": "hostc",
    "bullwinkle": "hostd",
    "sparky": "hoste",
    "puck": "hostf",
}

# Names with substring / collision risk -> only replaced on token boundaries
# (not flanked by an alphanumeric), so we never chew the middle of an unrelated
# word (e.g. 'horde' must not touch 'chOrder' in a vendored XSD).
BOUNDED_NAMES = {
    "do-host1": "node1",
    "jordanh": "devuser",
    "jkh": "dev",
    "horde": "dev",
    "omgjkh": "teamone",   # Slack workspace slug
    "offtera": "teamtwo",  # Slack workspace slug
}

# Literal, whole-string rewrites applied FIRST, most-specific first. Includes
# the full name, personal domains/handles, infra host, and tailnet/public IPs.
LITERALS = [
    ("Jordan Hubbard", "Dev User"),
    ("jordanhubbard.net", "example.com"),
    ("horde-gke.nvidia.com", "example.com"),
    ("jordanhubbard", "devuser"),        # GitHub username (after the .net rule)
    ("rockyandfriends", "teamchannel"),
    ("MadMax", "Hostb"),                 # camel-case agent spelling
    ("100.125.137.89", "100.64.1.1"),
    ("100.87.229.125", "100.64.1.2"),
    ("146.190.134.110", "203.0.113.10"),
]

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".next",
}


def _case_variants(word: str) -> list[str]:
    """lower / Title / UPPER spellings, de-duplicated, longest first."""
    out = []
    for v in (word, word.capitalize(), word.upper()):
        if v not in out:
            out.append(v)
    return out


def _bounded(word: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])")


def scrub_text(text: str) -> str:
    for src, dst in LITERALS:
        text = text.replace(src, dst)
    for name, tok in AGENT_NAMES.items():
        for v, t in zip(_case_variants(name), _case_variants(tok)):
            text = text.replace(v, t)          # unbounded
    for name, tok in BOUNDED_NAMES.items():
        for v, t in zip(_case_variants(name), _case_variants(tok)):
            text = _bounded(v).sub(t, text)    # token-boundary only
    return text


def _iter_text_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    yield path, f.read()
            except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError):
                continue  # skip binaries / unreadable


def scrub_tree(root: str) -> int:
    changed = 0
    for path, text in _iter_text_files(root):
        new = scrub_text(text)
        if new != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            changed += 1
    return changed


def check_tree(root: str) -> list[tuple[str, int, str]]:
    """Return (path, lineno, token) for every surviving personal token.

    Case-sensitive per known spelling, so vendored false positives like
    'chOrder' (capital O) never trip the 'horde' check.
    """
    tokens: list[str] = []
    for src, _ in LITERALS:
        tokens.append(src)
    for name in list(AGENT_NAMES) + list(BOUNDED_NAMES):
        tokens.extend(_case_variants(name))
    seen = set()
    hits: list[tuple[str, int, str]] = []
    for path, text in _iter_text_files(root):
        for i, line in enumerate(text.splitlines(), 1):
            for tok in tokens:
                if tok in line and (path, i, tok) not in seen:
                    seen.add((path, i, tok))
                    hits.append((os.path.relpath(path, root), i, tok))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("scrub", "check"):
        print("usage: depersonalize.py {scrub|check} <dir>", file=sys.stderr)
        return 2
    mode, root = argv[1], argv[2]
    if not os.path.isdir(root):
        print(f"depersonalize: not a directory: {root}", file=sys.stderr)
        return 2
    if mode == "scrub":
        n = scrub_tree(root)
        print(f"depersonalize: scrubbed {n} file(s) under {root}")
        return 0
    hits = check_tree(root)
    if not hits:
        print(f"depersonalize: clean — no personal tokens under {root}")
        return 0
    print(f"depersonalize: FOUND {len(hits)} residual personal token(s):", file=sys.stderr)
    for path, line, tok in hits[:200]:
        print(f"  {path}:{line}: {tok}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
