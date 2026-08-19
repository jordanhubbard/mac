"""A repository contract has never been checked against the repository.

The sandbox image is the union of what contracts DECLARE. What a repo NEEDS is
observed by mac.environment_contract, per task, in a worktree that is deleted
when the task ends. Nothing compared the two, so under-declaration was silent:
the image omits the tool and the task dies inside the sandbox on a missing
binary that reads as the task's fault.

These cover the comparison and, more importantly, the three ways it could be
worse than nothing: by missing the gap it exists to find, by inventing findings
for repos that have none, and by "helpfully" guessing which apt package supplies
a command it has never seen.
"""

from __future__ import annotations

import json

import pytest

from mac.contract_coverage import (
    COVERAGE_METADATA_KEY,
    MANIFEST_COMMANDS,
    coverage_has_findings,
    coverage_report,
    coverage_task_description,
    coverage_task_metadata,
    declared_commands_from_checkout,
    evidence_lines,
    suggest_required_commands,
)
from mac.environment_contract import derive_environment_contract
from mac.sandbox_bom import COMMAND_PACKAGES, MAC_CORE_COMMANDS
from mac.services import ControlPlane


def _repo(tmp_path, name, files, *, declared=None):
    """A checkout with real manifests and, optionally, a real contract."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    for filename, body in files.items():
        (repo / filename).write_text(body, encoding="utf-8")
    if declared is not None:
        (repo / ".mac").mkdir()
        (repo / ".mac" / "project.yaml").write_text(
            "\n".join(
                [
                    "schema: mac.repository_contract.v1",
                    "project: %s" % name,
                    "toolchain:",
                    "  required_commands: [%s]" % ", ".join(declared),
                ]
            ),
            encoding="utf-8",
        )
    return repo


# --------------------------------------------------------------------------
# Suggestion: reuse the scanner, and carry why
# --------------------------------------------------------------------------


def test_the_suggestion_comes_from_the_environment_contract_scanner():
    """One place learns to read repos.

    Asserted by feeding a contract in rather than a path: if the suggestion had
    its own scanner it would ignore this and go read the (nonexistent) disk.
    """
    suggestion = suggest_required_commands(
        "/does/not/exist",
        contract={"manifests": ["Cargo.toml"], "native_build": {"required": False}},
    )

    assert suggestion["suggested_commands"] == ["cargo"]


def test_a_manifest_implies_a_command_and_says_which_manifest(tmp_path):
    """Evidence is what makes a suggestion arguable instead of an assertion."""
    repo = _repo(tmp_path, "web", {"package.json": '{"name": "web"}'})

    suggestion = suggest_required_commands(repo)

    assert "node" in suggestion["suggested_commands"]
    assert suggestion["evidence"]["node"] == ["package.json"]
    assert "node <- package.json" in evidence_lines(suggestion)


def test_a_compiling_repo_implies_a_compiler_with_the_signal_that_said_so(tmp_path):
    repo = _repo(tmp_path, "native", {"Cargo.toml": "[package]\nname='x'\n"})

    suggestion = suggest_required_commands(repo)

    assert "cc" in suggestion["suggested_commands"]
    assert any("Cargo.toml" in item for item in suggestion["evidence"]["cc"])


def test_the_scanner_records_which_manifests_exist(tmp_path):
    """The evidence trail the coverage report reads, on the contract itself."""
    repo = _repo(tmp_path, "mixed", {"package.json": "{}", "pyproject.toml": ""})

    assert derive_environment_contract(repo)["manifests"] == [
        "package.json",
        "pyproject.toml",
    ]


# --------------------------------------------------------------------------
# The gap this exists to find
# --------------------------------------------------------------------------


def test_a_needed_command_the_contract_omits_is_reported(tmp_path):
    """The silent failure: the image will not carry it, and nothing said so."""
    repo = _repo(
        tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=["make"]
    )

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )

    undeclared = {entry["command"] for entry in report["undeclared_commands"]}
    assert "cargo" in undeclared
    assert coverage_has_findings(report)


def test_the_report_carries_the_evidence_for_each_gap(tmp_path):
    repo = _repo(tmp_path, "web", {"package.json": "{}"}, declared=[])

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )
    entry = next(e for e in report["undeclared_commands"] if e["command"] == "node")

    assert entry["evidence"] == ["package.json"]


def test_a_declared_command_no_manifest_implies_is_reported_as_unused(tmp_path):
    """The other direction: a package in the security boundary nothing asks for."""
    repo = _repo(
        tmp_path,
        "web",
        {"package.json": "{}"},
        declared=["node", "qemu-system-riscv64"],
    )

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )

    assert report["unused_declared_commands"] == ["qemu-system-riscv64"]
    assert report["satisfied_commands"] == ["node"]


def test_a_declared_command_that_is_needed_is_not_a_finding(tmp_path):
    repo = _repo(tmp_path, "web", {"package.json": "{}"}, declared=["node"])

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )

    assert report["undeclared_commands"] == []
    assert report["unused_declared_commands"] == []
    assert not coverage_has_findings(report)


# --------------------------------------------------------------------------
# Noise is the only way this fails
# --------------------------------------------------------------------------


def test_a_repo_whose_manifests_imply_nothing_produces_no_finding(tmp_path):
    """A docs repo must not become a ticket."""
    repo = _repo(tmp_path, "docs", {"README.md": "# docs\n"}, declared=[])

    suggestion = suggest_required_commands(repo)
    report = coverage_report(suggestion, declared_commands_from_checkout(repo))

    assert suggestion["suggested_commands"] == []
    assert not coverage_has_findings(report)


def test_commands_every_sandbox_already_has_are_not_reported(tmp_path):
    """python3 is in every sandbox; calling it undeclared trains people to stop reading."""
    assert "python3" in MAC_CORE_COMMANDS
    repo = _repo(tmp_path, "lib", {"pyproject.toml": "[project]\nname='x'\n"}, declared=[])

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )

    assert report["undeclared_commands"] == []
    assert report["core_supplied_commands"] == ["python3"]


def test_declaring_a_core_command_is_not_dead_weight(tmp_path):
    """`git` in a contract is redundant, not a package to go delete."""
    repo = _repo(tmp_path, "docs", {"README.md": "x"}, declared=["git"])

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )

    assert report["unused_declared_commands"] == []


# --------------------------------------------------------------------------
# The invariant: reported, never guessed
# --------------------------------------------------------------------------


def test_a_suggested_command_with_no_curated_package_is_reported_not_mapped(tmp_path):
    """The line this feature must not cross.

    `cargo` is a command sandbox_bom has no package for. Inferring one from the
    binary name would install something plausible into the security boundary --
    so the report says "I do not know", loudly, and stops.
    """
    assert "cargo" not in COMMAND_PACKAGES
    repo = _repo(tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=[])

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )
    entry = next(e for e in report["undeclared_commands"] if e["command"] == "cargo")

    assert entry["package_mapping_known"] is False
    assert entry["packages"] == []
    assert "cargo" in report["unmapped_commands"]


def test_the_report_never_invents_a_package_name(tmp_path):
    """No entry may carry a package the curated table did not supply."""
    repo = _repo(
        tmp_path,
        "polyglot",
        {
            "Cargo.toml": "[package]\nname='x'\n",
            "go.mod": "module x\n",
            "Gemfile": "source 'x'\n",
        },
        declared=[],
    )

    report = coverage_report(
        suggest_required_commands(repo), declared_commands_from_checkout(repo)
    )

    for entry in report["undeclared_commands"]:
        curated = set(COMMAND_PACKAGES.get(entry["command"], ()))
        assert set(entry["packages"]) <= curated


def test_deriving_coverage_does_not_mutate_the_curated_mapping(tmp_path):
    before = json.dumps({k: list(v) for k, v in COMMAND_PACKAGES.items()}, sort_keys=True)
    repo = _repo(tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=[])

    coverage_report(suggest_required_commands(repo), declared_commands_from_checkout(repo))

    assert (
        json.dumps({k: list(v) for k, v in COMMAND_PACKAGES.items()}, sort_keys=True)
        == before
    )


def test_every_suggestible_command_is_a_command_not_a_package():
    """The tables must not quietly converge.

    A manifest maps to COMMANDS. If someone starts writing package names here
    ('build-essential' instead of 'cc') the two curated layers merge and the
    guess-free property is gone.
    """
    packages = {pkg for pkgs in COMMAND_PACKAGES.values() for pkg in pkgs}
    suggested = {cmd for cmds in MANIFEST_COMMANDS.values() for cmd in cmds}

    assert not (suggested & (packages - set(COMMAND_PACKAGES)))


# --------------------------------------------------------------------------
# The review surface
# --------------------------------------------------------------------------


def test_a_filed_report_is_staged_and_not_dispatchable(tmp_path):
    """What an agent would do with this is rewrite a contract from an inference."""
    plane = ControlPlane.in_memory()
    plane.create_project("rustproj", dispatch_paused=False)
    repo = _repo(tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=[])
    report = coverage_report(
        suggest_required_commands(repo),
        declared_commands_from_checkout(repo),
        project="rustproj",
    )

    result = plane.file_contract_coverage_report(report, project="rustproj")

    task = plane.get_task(result["filed"])
    assert task.metadata["no_dispatch"] is True
    assert COVERAGE_METADATA_KEY in task.metadata
    assert "cargo" in task.description


def test_the_same_finding_is_not_filed_twice(tmp_path):
    """Re-filing per run buries the one report that matters."""
    plane = ControlPlane.in_memory()
    plane.create_project("rustproj", dispatch_paused=False)
    repo = _repo(tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=[])
    report = coverage_report(
        suggest_required_commands(repo),
        declared_commands_from_checkout(repo),
        project="rustproj",
    )

    first = plane.file_contract_coverage_report(report, project="rustproj")
    second = plane.file_contract_coverage_report(report, project="rustproj")

    assert first["filed"]
    assert second["filed"] is None


def test_nothing_is_filed_when_there_is_nothing_to_report(tmp_path):
    plane = ControlPlane.in_memory()
    plane.create_project("docsproj", dispatch_paused=False)
    repo = _repo(tmp_path, "docs", {"README.md": "x"}, declared=[])
    report = coverage_report(
        suggest_required_commands(repo),
        declared_commands_from_checkout(repo),
        project="docsproj",
    )

    assert plane.file_contract_coverage_report(report, project="docsproj")["filed"] is None


def test_a_malformed_report_is_rejected_at_the_boundary():
    """The facade must name the schema, not fail three frames deep."""
    from mac.models import ValidationError

    plane = ControlPlane.in_memory()

    with pytest.raises(ValidationError):
        plane.file_contract_coverage_report("not a report")
    with pytest.raises(ValidationError):
        plane.file_contract_coverage_report({"undeclared_commands": [{"command": "x"}]})


def test_the_description_states_both_answers_and_forbids_automation(tmp_path):
    repo = _repo(tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=["zig"])
    report = coverage_report(
        suggest_required_commands(repo),
        declared_commands_from_checkout(repo),
        project="rustproj",
    )

    description = coverage_task_description(report)

    assert "cargo" in description and "zig" in description
    assert "never guessed" in description
    assert coverage_task_metadata(report)["no_dispatch"] is True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli(argv):
    from mac.cli import main

    return main(argv)


def test_the_cli_reports_the_gap_and_can_gate_on_it(tmp_path, capsys):
    repo = _repo(tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=[])

    with pytest.raises(SystemExit) as exit_info:
        _cli(
            [
                "--json",
                "admin",
                "sandbox-image",
                "contract-coverage",
                "--repo",
                str(repo),
                "--check",
            ]
        )

    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert [e["command"] for e in payload["undeclared_commands"]] == [
        "cargo",
        "cc",
        "make",
    ]
    assert payload["declared_source"] == "checkout .mac/project.yaml"


def test_the_cli_is_quiet_when_the_contract_is_complete(tmp_path, capsys):
    repo = _repo(
        tmp_path, "rust", {"Cargo.toml": "[package]\nname='x'\n"}, declared=["cargo", "cc", "make"]
    )

    _cli(
        [
            "--json",
            "admin",
            "sandbox-image",
            "contract-coverage",
            "--repo",
            str(repo),
            "--check",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["undeclared_commands"] == []


def test_the_cli_can_diff_against_an_explicit_declaration(tmp_path, capsys):
    """No hub, no contract file -- the report still works from a checkout."""
    repo = _repo(tmp_path, "web", {"package.json": "{}"})

    _cli(
        [
            "--json",
            "admin",
            "sandbox-image",
            "contract-coverage",
            "--repo",
            str(repo),
            "--declared",
            "node,qemu-system-riscv64",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["undeclared_commands"] == []
    assert payload["unused_declared_commands"] == ["qemu-system-riscv64"]
    assert payload["declared_source"] == "--declared"
