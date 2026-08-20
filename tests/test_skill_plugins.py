"""ADR 0023: one skill source, a thin adapter per harness.

The four properties that make this worth having, each asserted here:

1. **Every harness receives every obligation.** A rule present in one harness
   and absent from another is the silent fork the ADR exists to prevent, so it
   fails the build rather than showing up weeks later as an agent behaving
   differently on one CLI.
2. **Adapters render, they never author.** Proven twice: rendering from a
   synthetic source proves the words follow the source, and diffing the real
   render against a render of an EMPTY source isolates the structural skeleton
   so every remaining line can be traced back to `skills/`.
3. **Installing does not clobber a human's configuration**, and refuses this
   repository and any tree the caller did not nominate.
4. **The artifact names its source revision**, and `status` can say which
   harness on which host carries which version -- because a harness carrying
   STALE rules is worse than one carrying none.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mac import skill_plugins as sp
from mac.coding_agent import AGENT_PRIORITY

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"

FIXED = sp.SourceVersion(revision="abcdef123456", digest="0f0f0f0f0f0f")


def _skill_document(name: str, obligations: dict[str, str], prose: str = "") -> str:
    body = ["---", "name: %s" % name, "description: %s synthetic source" % name, "---", ""]
    body.append("# %s" % name)
    body.append("")
    if prose:
        body.extend([prose, ""])
    for identifier, text in obligations.items():
        body.append("**OBLIGATION `%s`** — %s" % (identifier, text))
        body.append("")
    return "\n".join(body)


def _synthetic_root(tmp_path: Path, documents: dict[str, str], *, with_tests: bool = True) -> Path:
    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        target = root / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if with_tests:
        tests = tmp_path / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test_synthetic.py").write_text(
            "\n".join('# covers skills/%s/SKILL.md' % name for name in documents) + "\n",
            encoding="utf-8",
        )
    return root


# --- 1. one source, every harness ------------------------------------------


def test_the_harness_list_is_the_one_mac_already_routes_work_to():
    """Not a second list. A second list is how the two start to disagree."""

    assert sp.HARNESSES is AGENT_PRIORITY
    assert set(sp.HARNESSES) == {"opencode", "pi", "claude", "codex", "cursor"}


def test_every_harness_receives_every_obligation():
    skills = sp.load_skills(SKILLS)
    obligations = sp.obligations_of(skills)
    assert obligations, "skills/ marks no obligations at all"

    for harness in sp.HARNESSES:
        for scope in ("global", "repo"):
            plugin = sp.render_plugin(harness, scope=scope, skills_root=SKILLS, version=FIXED)
            always_on = "\n".join(
                rendered.content
                for rendered in plugin.files
                # The always-on surface is the one that arrives whether or not
                # the session goes looking; a reference copy does not count.
                if rendered.mode == "block" or "obligations" in rendered.path
            )
            assert always_on.strip(), "%s/%s has no always-on surface" % (harness, scope)
            for obligation in obligations:
                assert obligation.id in always_on, (
                    "%s/%s is missing obligation %s" % (harness, scope, obligation.id)
                )
                assert obligation.text in always_on, (
                    "%s/%s carries the id but not the rule for %s"
                    % (harness, scope, obligation.id)
                )


def test_the_obligations_the_fleet_already_wrote_down_are_marked_as_such():
    """The rules an operator had to ask for by hand on 2026-08-20."""

    marked = {obligation.id for obligation in sp.obligations_of(sp.load_skills(SKILLS))}
    for required in (
        "claim-before-working",
        "check-for-existing-pr",
        "triage-against-branch-head",
        "mention-is-not-evidence",
        "reread-state-after-compaction",
        "never-edit-a-running-task-by-hand",
    ):
        assert required in marked, "%s is an obligation that must be delivered" % required


def test_every_harness_receives_every_skill_as_reference():
    names = {skill.name for skill in sp.load_skills(SKILLS)}
    for harness in sp.HARNESSES:
        plugin = sp.render_plugin(harness, scope="global", skills_root=SKILLS, version=FIXED)
        assert set(plugin.skills) == names
        rendered = "\n".join(item.path for item in plugin.files)
        for name in names:
            assert name in rendered, "%s does not deliver %s" % (harness, name)


def test_an_obligation_id_cannot_be_claimed_by_two_skills(tmp_path):
    root = _synthetic_root(
        tmp_path,
        {
            "alpha": _skill_document("alpha", {"shared-id": "one rule."}),
            "beta": _skill_document("beta", {"shared-id": "a different rule."}),
        },
    )
    with pytest.raises(sp.SkillPluginError, match="claimed by both"):
        sp.load_skills(root)


# --- 2. adapters render, they never author ---------------------------------


def test_a_rule_added_to_the_source_reaches_every_harness_unedited(tmp_path):
    """Change the source; every harness changes. That is the whole design."""

    nonce = "zqx-provenance-nonce"
    rule = "Do the %s thing before anything else." % nonce
    root = _synthetic_root(
        tmp_path, {"synthetic": _skill_document("synthetic", {nonce: rule})}
    )
    for harness in sp.HARNESSES:
        plugin = sp.render_plugin(harness, scope="global", skills_root=root, version=FIXED)
        blob = "\n".join(item.content for item in plugin.files)
        assert nonce in blob
        assert rule in blob


def test_adapter_output_that_is_not_skeleton_comes_from_the_source(tmp_path):
    """Isolate the structure, then trace everything else back to skills/.

    Rendering an EMPTY source yields exactly the adapters' own scaffolding.
    Any line the real render adds beyond that scaffolding must be traceable to
    the skill sources -- otherwise an adapter is authoring guidance, which is
    the failure mode that makes five copies of a rule drift apart.
    """

    # The skeleton is rendered from a source whose every word is a nonce, so
    # each artifact the adapter emits is present with none of its content: what
    # survives the diff is exactly the adapter's own scaffolding.
    probe = "\n".join(
        [
            "---",
            "name: zzprobe",
            "description: zzprobedescription",
            "---",
            "",
            "# zzprobe",
            "",
            "**OBLIGATION `zzprobeid`** — zzprobetext.",
            "",
        ]
    )
    empty_root = _synthetic_root(tmp_path, {"zzprobe": probe}, with_tests=True)
    source = " ".join(
        " ".join(path.read_text(encoding="utf-8").split()).lower()
        for path in sorted(SKILLS.glob("*/SKILL.md"))
    )

    for harness in sp.HARNESSES:
        skeleton = sp.render_plugin(
            harness, scope="global", skills_root=empty_root, version=FIXED
        )
        skeleton_lines = {
            line.strip()
            for item in skeleton.files
            for line in item.content.splitlines()
        }
        real = sp.render_plugin(harness, scope="global", skills_root=SKILLS, version=FIXED)
        for item in real.files:
            if item.path.endswith(".json"):
                continue  # structural metadata, asserted field-by-field below
            for line in item.content.splitlines():
                stripped = line.strip()
                if not stripped or stripped in skeleton_lines:
                    continue
                for fragment in re.split(r"[*`|—\-#>:]{1,}", stripped):
                    fragment = " ".join(fragment.split()).lower()
                    if len(fragment) < 4:
                        continue
                    assert fragment in source, (
                        "%s renders %r, which is in no skill source -- an adapter "
                        "is authoring content" % (harness, fragment)
                    )


def test_the_manifest_reports_exactly_what_the_source_holds():
    skills = sp.load_skills(SKILLS)
    for harness in sp.HARNESSES:
        plugin = sp.render_plugin(harness, scope="global", skills_root=SKILLS, version=FIXED)
        manifest = json.loads(
            next(item for item in plugin.files if item.path.endswith(".json")).content
        )
        assert manifest["schema"] == sp.PLUGIN_SCHEMA
        assert manifest["harness"] == harness
        assert [entry["name"] for entry in manifest["skills"]] == [s.name for s in skills]
        assert manifest["obligations"] == [
            obligation.id for obligation in sp.obligations_of(skills)
        ]


# --- 3. installing into somebody else's world ------------------------------


def test_install_preserves_a_humans_existing_configuration(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # claude's GLOBAL always-on surface, the same directory coding_agent probes
    # for credentials -- and a file a human very likely already owns.
    human = home / ".claude" / "CLAUDE.md"
    human.write_text("# My own rules\n\nAlways use tabs.\n", encoding="utf-8")

    receipts = tmp_path / "receipts.json"
    receipt = sp.install(
        "claude",
        scope="global",
        target=home,
        skills_root=SKILLS,
        receipts=receipts,
        host="test-host",
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    updated = human.read_text(encoding="utf-8")
    assert "# My own rules" in updated and "Always use tabs." in updated
    assert sp.BLOCK_BEGIN in updated and sp.BLOCK_END in updated
    assert "claim-before-working" in updated
    assert receipt.blocks == (".claude/CLAUDE.md",)

    # Re-installing replaces the block rather than appending a second copy.
    sp.install(
        "claude",
        scope="global",
        target=home,
        skills_root=SKILLS,
        receipts=receipts,
        host="test-host",
    )
    again = human.read_text(encoding="utf-8")
    assert again.count(sp.BLOCK_BEGIN) == 1
    assert "Always use tabs." in again

    # And uninstall gives the human their file back, block removed.
    sp.uninstall("claude", target_root=home, receipts=receipts, host="test-host")
    restored = human.read_text(encoding="utf-8")
    assert sp.BLOCK_BEGIN not in restored
    assert "Always use tabs." in restored
    assert not (home / ".claude" / "skills" / "mac-cli").exists()
    assert sp.read_receipts(receipts) == ()


def test_install_refuses_to_overwrite_a_file_mac_did_not_write(tmp_path):
    home = tmp_path / "home"
    theirs = home / ".claude" / "skills" / "mac-cli" / "SKILL.md"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("my own skill, thanks\n", encoding="utf-8")
    with pytest.raises(sp.SkillPluginError, match="refusing to overwrite"):
        sp.install(
            "claude",
            scope="global",
            target=home,
            skills_root=SKILLS,
            receipts=tmp_path / "receipts.json",
            host="test-host",
        )
    assert theirs.read_text(encoding="utf-8") == "my own skill, thanks\n"


def test_repo_local_install_refuses_this_repository(tmp_path):
    with pytest.raises(sp.SkillPluginError, match="source of skills/"):
        sp.install(
            "claude",
            scope="repo",
            target=ROOT,
            skills_root=SKILLS,
            receipts=tmp_path / "receipts.json",
            host="test-host",
        )


def test_repo_local_install_never_guesses_a_target(tmp_path):
    with pytest.raises(sp.SkillPluginError, match="never guesses"):
        sp.install(
            "claude",
            scope="repo",
            target=None,
            skills_root=SKILLS,
            receipts=tmp_path / "receipts.json",
            host="test-host",
        )


def test_repo_local_install_writes_where_the_harness_reads(tmp_path):
    repo = tmp_path / "someone-elses-project"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Their conventions\n", encoding="utf-8")
    sp.install(
        "codex",
        scope="repo",
        target=repo,
        skills_root=SKILLS,
        receipts=tmp_path / "receipts.json",
        host="test-host",
    )
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "# Their conventions" in agents
    assert "triage-against-branch-head" in agents
    assert (repo / ".codex" / "skills" / "mac-cli" / "SKILL.md").exists()


# --- 4. versioning and reporting -------------------------------------------


def test_every_rendered_artifact_names_its_source_revision():
    for harness in sp.HARNESSES:
        plugin = sp.render_plugin(harness, scope="global", skills_root=SKILLS, version=FIXED)
        for item in plugin.files:
            assert str(FIXED) in item.content or FIXED.digest in item.content, (
                "%s renders %s without naming the source revision" % (harness, item.path)
            )


def test_a_changed_source_produces_a_changed_version(tmp_path):
    root = _synthetic_root(tmp_path, {"alpha": _skill_document("alpha", {"a-rule": "Do it."})})
    before = sp.source_version(root)
    (root / "alpha" / "SKILL.md").write_text(
        _skill_document("alpha", {"a-rule": "Do it, differently."}), encoding="utf-8"
    )
    after = sp.source_version(root)
    assert before.digest != after.digest


def test_status_reports_which_host_and_harness_carries_which_version(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    receipts = tmp_path / "receipts.json"
    sp.install(
        "claude",
        scope="global",
        target=home,
        skills_root=SKILLS,
        receipts=receipts,
        host="node-a",
    )
    report = sp.status(skills_root=SKILLS, receipts=receipts)
    assert len(report["installs"]) == 1
    record = report["installs"][0]
    assert record["host"] == "node-a"
    assert record["harness"] == "claude"
    assert record["version"] == report["source_version"]
    assert record["stale"] is False
    assert sorted(report["harnesses_without_install"]) == sorted(
        set(sp.HARNESSES) - {"claude"}
    )


def test_status_calls_an_install_stale_when_the_source_moved(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = _synthetic_root(tmp_path, {"alpha": _skill_document("alpha", {"a-rule": "Do it."})})
    receipts = tmp_path / "receipts.json"
    sp.install(
        "cursor", scope="global", target=home, skills_root=root, receipts=receipts, host="node-a"
    )
    assert sp.status(skills_root=root, receipts=receipts)["stale"] == []

    (root / "alpha" / "SKILL.md").write_text(
        _skill_document("alpha", {"a-rule": "Do it sooner."}), encoding="utf-8"
    )
    report = sp.status(skills_root=root, receipts=receipts)
    assert len(report["stale"]) == 1, "a harness carrying stale rules must be visible"


# --- the publishing guard --------------------------------------------------


def test_publishing_refuses_a_skill_that_has_no_test(tmp_path):
    root = _synthetic_root(
        tmp_path,
        {"unguarded": _skill_document("unguarded", {"a-rule": "Trust me."})},
        with_tests=False,
    )
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_other.py").write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(sp.SkillPluginError, match="untested skill"):
        sp.render_plugin("claude", scope="global", skills_root=root)


def test_every_published_skill_in_this_repository_has_a_test():
    assert sp.untested_skills(SKILLS) == (), (
        "a published skill with no test is an instruction every harness obeys"
    )


def test_publishing_refuses_a_source_whose_tests_cannot_be_read(tmp_path):
    root = _synthetic_root(
        tmp_path, {"alpha": _skill_document("alpha", {"a-rule": "Do it."})}, with_tests=False
    )
    with pytest.raises(sp.SkillPluginError, match="no tests/ tree"):
        sp.render_plugin("claude", scope="global", skills_root=root)
