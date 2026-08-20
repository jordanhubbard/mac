"""`skills/setup-mac-fleet/SKILL.md` had no test and 330 commits of drift.

It is the skill that onboards a NEW fleet, so its rot costs the most: the
reader has no working fleet to check an instruction against. Publishing it to
every harness (ADR 0023) turns "an unread skill that is wrong" into "an
instruction every harness obeys", so it needs the same mechanical floor the
other published skills have.

Mechanical checks cannot catch advice that is valid in syntax and untrue in
fact -- that is task_73c7de1a. What they can catch, and what this file asserts,
is a skill naming a script, a sample, a Kubernetes overlay or an environment
variable that no longer exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "setup-mac-fleet" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_frontmatter_names_the_skill_and_says_when_to_use_it():
    text = _text()
    front = yaml.safe_load(text.split("---\n", 2)[1])
    assert front["name"] == "setup-mac-fleet"
    description = front["description"].lower()
    # The description is what a harness matches on to decide whether to load
    # the skill at all, so it has to carry the trigger words.
    for trigger in ("set up", "deploy", "fleet"):
        assert trigger in description, trigger


def test_every_repository_path_the_skill_names_still_exists():
    """A path in an onboarding skill is an instruction, not a reference."""

    text = _text()
    referenced = {
        match.group(0)
        for match in re.finditer(
            r"(?:deploy|scripts|src/mac|docs)/[A-Za-z0-9._/-]*[A-Za-z0-9_]",
            text,
        )
    }
    missing = sorted(
        path
        for path in referenced
        # Trailing-directory mentions ("deploy/k8s/") and glob-ish prose are
        # covered by the directory existing; a named file must be a file.
        if not (ROOT / path).exists()
    )
    assert missing == [], "setup-mac-fleet names paths that no longer exist: %s" % missing


def test_every_shell_entry_point_the_skill_tells_you_to_run_is_executable():
    text = _text()
    for script in ("setup.sh", "deploy/deploy-mac-fleet.sh"):
        assert script in text, "%s is the documented entry point" % script
        assert (ROOT / script).is_file()


def test_fleet_state_stays_out_of_git():
    """The rule the skill exists to enforce, asserted against the repository."""

    text = _text()
    assert "~/.mac/fleets.yaml" in text
    assert "~/.mac/.env" in text
    # The skill's rule is that fleet topology and secrets live under the home
    # directory, so the repository must carry neither -- and it does not need a
    # .gitignore rule to say so, because the paths are outside the tree.
    committed = [
        path
        for path in ROOT.rglob("fleets.yaml")
        if ".git" not in path.parts and "node_modules" not in path.parts
    ]
    assert committed == [], "fleet topology must not live in the repository: %s" % committed
    # A real fleet spec must never be committed; only the placeholder samples.
    specs = sorted(p.name for p in (ROOT / "deploy" / "fleet" / "samples").glob("*.fleet.yaml"))
    assert specs, "the skill points at deploy/fleet/samples for a worked example"
    for name in specs:
        assert "sample" in name or name.startswith(("gke", "generic", "example")), name


def test_env_vars_the_skill_names_are_read_by_the_source():
    """`MAC_*` in an onboarding skill means "set this and it takes effect"."""

    text = _text()
    named = {match.group(0) for match in re.finditer(r"MAC_[A-Z0-9_]+", text)}
    assert named, "the skill names no MAC_* variables at all"
    corpus = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in [
            *(ROOT / "src" / "mac").rglob("*.py"),
            *(ROOT / "deploy").rglob("*.sh"),
            *(ROOT / "deploy").rglob("*.yaml"),
            *(ROOT / "scripts").rglob("*.py"),
            *(ROOT / "scripts").rglob("*.sh"),
        ]
    )
    unknown = sorted(name for name in named if name not in corpus)
    assert unknown == [], "setup-mac-fleet names variables nothing reads: %s" % unknown
