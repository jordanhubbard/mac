"""Lockfiles must name every optionalDependency they declare.

`npm ci` fails closed when a parent lists an optional package that has no
`packages` entry — even on a machine that will never install that binding.
Darwin `make install` hit this for `@rolldown/binding-linux-arm64-musl@1.2.4`
in observe/package-lock.json while ide/ and desktop/ were complete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCKFILES = (
    ROOT / "observe" / "package-lock.json",
    ROOT / "ide" / "package-lock.json",
    ROOT / "desktop" / "package-lock.json",
)


@pytest.mark.parametrize("lockfile", LOCKFILES, ids=lambda path: path.parent.name)
def test_optional_dependencies_have_packages_entries(lockfile: Path):
    document = json.loads(lockfile.read_text(encoding="utf-8"))
    packages = document.get("packages") or {}
    assert isinstance(packages, dict)
    missing = []
    for parent, meta in packages.items():
        if not isinstance(meta, dict):
            continue
        optional = meta.get("optionalDependencies") or {}
        if not isinstance(optional, dict):
            continue
        for name, version in optional.items():
            key = "node_modules/%s" % name
            if key not in packages:
                missing.append("%s optional %s@%s has no %s entry" % (parent, name, version, key))
    assert missing == []
