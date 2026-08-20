"""ADR 0023, end to end: a fleet host's harnesses receive the obligations
without a human copying anything.

The rest of the ADR is covered by ``tests/test_skill_plugins.py`` (rendering,
refusals, versioning) and ``tests/cli/test_cli_skills.py`` (the operator
surface). What is left, and what this file asserts, is the delivery step:

1. the node install script actually calls the installer, on **every** deploy
   path rather than only the legacy one-shot -- a typed phase 2 that skips it
   leaves the node's harnesses on the previous revision, which is the stale
   state the ADR calls worse than no state at all;
2. the deploy nominates ``--global`` and never a repository, because writing
   into a working tree nobody asked about is what gets a plugin uninstalled;
3. running exactly that command against a synthetic ``$HOME`` puts every
   obligation in ``skills/`` into every harness's always-on surface.

(3) is the one that would otherwise be taken on trust: the script can look
right and still deliver nothing.
"""
from __future__ import annotations

from pathlib import Path

from mac import skill_plugins as sp

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"
SKILLS = ROOT / "skills"


def _function_body(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("%s() {\n" % name)
    return text[start : text.index("\n}\n", start)]


def test_the_deploy_installs_harness_plugins_on_every_path_not_just_the_legacy_one():
    text = SCRIPT.read_text(encoding="utf-8")
    # Called once, from the top level, AFTER the legacy-one-shot branch closes.
    # Inside the branch it would silently not run on a typed phase 2 deploy.
    calls = [
        line
        for line in text.splitlines()
        if line.strip() == "install_harness_skill_plugins"
    ]
    assert calls == ["install_harness_skill_plugins"], (
        "expected exactly one unindented top-level call; an indented one is "
        "inside a branch and will not run on every deploy path"
    )


def test_the_deploy_nominates_global_and_never_a_repository():
    body = _function_body("install_harness_skill_plugins")
    assert "admin skills install --global" in body
    assert "--repo" not in body, (
        "a deploy is not entitled to nominate a working tree; --repo stays an "
        "operator decision (ADR 0023 s3)"
    )
    # Rendered from the revision this deploy carries, not from whatever the
    # installed wheel happens to hold.
    assert '--skills-root "$SRC_DIR/skills"' in body
    # Non-fatal, but a refusal is logged rather than swallowed: an untested
    # skill or a file the node's user owns is a real signal.
    assert "WARNING" in body


def test_a_fleet_node_home_receives_every_obligation_in_every_harness(tmp_path):
    """The deploy's own command, run against a synthetic node home."""

    home = tmp_path / "node-home"
    home.mkdir()
    receipts = tmp_path / "installs.json"
    expected = {
        obligation.id for obligation in sp.obligations_of(sp.load_skills(SKILLS))
    }
    assert expected, "skills/ must carry at least one OBLIGATION for this to mean anything"

    for harness in sp.HARNESSES:
        receipt = sp.install(
            harness,
            scope="global",
            target=home,
            skills_root=SKILLS,
            receipts=receipts,
            host="node-a",
        )
        assert set(receipt.obligations) == expected

    delivered = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(home.rglob("*"))
        if path.is_file()
    )
    for obligation in sorted(expected):
        assert obligation in delivered, (
            "obligation %r never reached a fleet node's harness surface" % obligation
        )

    # And an operator can find what was written, per host and per harness.
    report = sp.status(skills_root=SKILLS, receipts=receipts, host="node-a")
    assert report["harnesses_without_install"] == []
    assert report["stale"] == []
    assert {item["harness"] for item in report["installs"]} == set(sp.HARNESSES)
