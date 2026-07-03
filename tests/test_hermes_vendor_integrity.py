"""Guard against silent drift of the vendored Hermes tree (src/mac/_hermes).

Background: `_hermes` is a pinned, pruned *snapshot* of an upstream runtime
(see SNAPSHOT_PIN). The vendoring script (`scripts/vendor-hermes-snapshot.sh`)
copies pristine upstream at the pinned commit and re-applies `deploy/hermes/*.patch`.
The failure mode this test defends against: MAC features/fixes edited files
*inside* `_hermes` directly, WITHOUT capturing a corresponding `.patch`. A
re-vendor would then silently clobber those edits (e.g. the X-MAC billing
attribution headers, the Slack thread-trigger fix — see
`deploy/hermes/LOCAL_PATCHES.md`).

This test pins a content digest of the entire `_hermes` tree. Any change to the
tree — a hand edit or a re-vendor — makes it fail loudly, forcing the author to
either (a) capture the edit as a `deploy/hermes/*.patch` so it survives the next
re-vendor, or (b) knowingly regenerate the baseline. That converts "silent
fork drift" into "a red build you must acknowledge."

To regenerate the baseline after an intentional change:
    python -c "from tests.test_hermes_vendor_integrity import hermes_tree_digest as d; \
        open('deploy/hermes/HERMES_TREE_SHA256','w').write(d()+'\\n')"
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HERMES_DIR = _REPO_ROOT / "src" / "mac" / "_hermes"
_BASELINE = _REPO_ROOT / "deploy" / "hermes" / "HERMES_TREE_SHA256"


def hermes_tree_digest() -> str:
    """Deterministic content digest of the whole vendored tree.

    Hashes sorted (relative-path, content-sha256) pairs so the result is stable
    across machines and independent of filesystem walk order. Byte-compiled
    caches are ignored (not part of the source tree).
    """
    entries = []
    for path in sorted(_HERMES_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(_HERMES_DIR).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        # SNAPSHOT_PIN is vendoring metadata (which upstream base + notes), not
        # upstream source — editing it is not "source drift", so exclude it.
        if rel == "SNAPSHOT_PIN":
            continue
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append("%s\0%s" % (rel, content_hash))
    joined = "\n".join(entries).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def test_hermes_tree_matches_pinned_baseline():
    assert _HERMES_DIR.is_dir(), "vendored _hermes tree is missing"
    assert _BASELINE.is_file(), (
        "missing baseline %s — generate it with the command in this module's "
        "docstring" % _BASELINE
    )
    expected = _BASELINE.read_text(encoding="utf-8").strip()
    actual = hermes_tree_digest()
    assert actual == expected, (
        "Vendored Hermes tree (src/mac/_hermes) changed relative to the pinned "
        "baseline.\n"
        "  expected %s\n  actual   %s\n"
        "If you hand-edited a vendored file: capture it as a deploy/hermes/*.patch "
        "so it survives the next re-vendor (see deploy/hermes/LOCAL_PATCHES.md), "
        "then regenerate the baseline (see this module's docstring). If you "
        "re-vendored, update LOCAL_PATCHES.md and regenerate the baseline." % (
            expected,
            actual,
        )
    )
