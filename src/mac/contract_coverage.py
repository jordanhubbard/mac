"""Compare what a repository NEEDS against what its contract DECLARES.

Two halves of the same question have never been connected.

``mac.environment_contract`` reads a checkout -- package.json, pyproject.toml,
Cargo.toml, .nvmrc, lockfiles -- and works out what the repo actually needs. It
runs per task, in the worker's worktree, and is thrown away when the task ends.

``mac.sandbox_bom`` decides what goes into the shared sandbox image. It reads
exactly one thing: ``metadata.repository_contract.toolchain.required_commands``
-- a list a human TYPED into ``.mac/project.yaml``.

So the image is derived from a memory, while the code that can observe the truth
runs metres away and is discarded. The failure that allows is under-declaration,
and it is silent: a contract that omits a needed tool produces a sandbox without
it, and the task then dies inside the sandbox on a missing binary that reads as
a task problem rather than a contract problem. ``mac.sandbox_excursion`` exists
to paper over exactly this after the fact.

This module closes the loop by REPORTING, never by acting:

* it reuses :func:`mac.environment_contract.derive_environment_contract` rather
  than writing a second scanner -- one place learns to read repos;
* it maps manifests to commands through a CURATED table, and carries the
  evidence for every suggestion ("node <- package.json") so a reviewer can
  disagree with a specific inference instead of the whole report;
* it diffs both directions -- needed-but-undeclared (work breaks) and
  declared-but-unused (dead weight in the security boundary);
* it changes NOTHING. No contract is edited, no package is installed.

The invariant :mod:`mac.sandbox_bom` states -- *a required COMMAND is not a
package name; the mapping is CURATED, and a command with no mapping is
REPORTED, never guessed* -- is strictly preserved here, and is in fact doubly
load-bearing now. A suggestion is an inference about a REPO; turning it into an
apt package would be an inference about the IMAGE stacked on top of it, and the
image is the security boundary. So a suggested command with no curated package
mapping is reported as unmapped, with an empty package list, forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .environment_contract import derive_environment_contract
from .sandbox_bom import COMMAND_PACKAGES, MAC_CORE_COMMANDS

JsonDict = Dict[str, Any]

COVERAGE_SCHEMA = "mac.contract_command_coverage.v1"

#: Marker on a filed report task, so dedupe does not depend on title text.
COVERAGE_METADATA_KEY = "contract_command_coverage"

#: Curated manifest -> commands that manifest implies.
#:
#: Deliberately conservative and deliberately boring. Every entry is a command
#: you cannot run the manifest's own standard workflow without: package.json
#: without node is not a project, Cargo.toml without cargo is not a build. The
#: temptation is to be clever here -- parse scripts, chase toolchain files,
#: guess from directory names -- and the cost of a wrong entry is a report that
#: cries wolf, which is read once and then never again.
#:
#: This table produces SUGGESTIONS for a human to accept or reject. It never
#: edits a contract and never selects a package.
MANIFEST_COMMANDS: Dict[str, Tuple[str, ...]] = {
    "CMakeLists.txt": ("cmake",),
    "Cargo.toml": ("cargo",),
    "Gemfile": ("ruby",),
    "Makefile": ("make",),
    "binding.gyp": ("node", "make", "cc"),
    "build.gradle": ("java",),
    "build.gradle.kts": ("java",),
    "go.mod": ("go",),
    "package-lock.json": ("node", "npm"),
    "package.json": ("node",),
    "pnpm-lock.yaml": ("pnpm", "node"),
    "pnpm-workspace.yaml": ("pnpm", "node"),
    "pom.xml": ("java",),
    "project.clj": ("lein",),
    "pyproject.toml": ("python3",),
    "requirements.txt": ("python3",),
    "setup.cfg": ("python3",),
    "setup.py": ("python3",),
    "uv.lock": ("uv",),
    "yarn.lock": ("yarn", "node"),
}

#: Commands implied by ``native_build.required``, whatever produced the signal.
#: A repo that compiles needs a compiler and something to drive it; which apt
#: package supplies them is sandbox_bom's curated business, not this table's.
NATIVE_BUILD_COMMANDS: Tuple[str, ...] = ("cc", "make")


# ===========================================================================
# Suggestion
# ===========================================================================


def suggest_required_commands(
    repo_path: str | Path,
    *,
    contract: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    """Commands a checkout's manifests imply, each with its evidence.

    Reuses the environment contract derivation rather than re-reading the
    checkout: ``manifests`` is that derivation's inventory of what exists, and
    ``native_build.signals`` is its judgement about compilation. Pass a
    previously derived ``contract`` to avoid scanning twice.

    A repo whose manifests imply nothing returns an EMPTY suggestion. That is
    the common case for a docs repo or a scratch checkout, and inventing a
    baseline for it would make every such repo a finding.
    """
    root = Path(repo_path)
    derived = dict(contract) if contract is not None else derive_environment_contract(root)

    evidence: Dict[str, List[str]] = {}

    for manifest_name in derived.get("manifests") or []:
        for command in MANIFEST_COMMANDS.get(str(manifest_name), ()):
            evidence.setdefault(command, [])
            if manifest_name not in evidence[command]:
                evidence[command].append(str(manifest_name))

    native = derived.get("native_build") or {}
    if native.get("required"):
        signals = [str(item) for item in (native.get("signals") or [])]
        label = "native build (%s)" % ("; ".join(signals) if signals else "detected")
        for command in NATIVE_BUILD_COMMANDS:
            evidence.setdefault(command, [])
            if label not in evidence[command]:
                evidence[command].append(label)

    return {
        "schema": COVERAGE_SCHEMA,
        "repository_path": str(root),
        "suggested_commands": sorted(evidence),
        "evidence": {command: list(sorted(items)) for command, items in evidence.items()},
    }


def evidence_lines(suggestion: Mapping[str, Any]) -> List[str]:
    """``"node <- package.json"`` for each suggested command, sorted."""
    evidence = suggestion.get("evidence") or {}
    return [
        "%s <- %s" % (command, ", ".join(evidence.get(command) or []) or "(no evidence)")
        for command in sorted(suggestion.get("suggested_commands") or [])
    ]


# ===========================================================================
# The diff
# ===========================================================================


def coverage_report(
    suggestion: Mapping[str, Any],
    declared: Iterable[str],
    *,
    project: Optional[str] = None,
    core_commands: Sequence[str] = MAC_CORE_COMMANDS,
    command_packages: Optional[Mapping[str, Sequence[str]]] = None,
) -> JsonDict:
    """Diff a repo's implied commands against the ones its contract declares.

    Both directions are reported because they are different problems:

    ``undeclared``
        A manifest implies a command the contract never names, and the command
        is not one the sandbox core guarantees. This is the silent failure --
        the image will not carry it, and the task dies inside the sandbox on a
        missing binary that looks like the task's fault.

    ``unused``
        The contract names a command nothing in the checkout implies. Dead
        weight in the image, and the image is the security boundary. Worth
        deleting, never urgent -- and explicitly NOT a thing to act on
        automatically, because a manifest table cannot see a tool invoked only
        by a shell script.

    Commands in ``core_commands`` appear in neither list. mac's own executor
    puts them in every sandbox regardless of any contract, so a Python repo
    "failing" to declare python3 is not a finding, it is noise -- and noise is
    the only way this report can fail. They are surfaced separately in
    ``core_supplied`` so the reasoning stays visible.
    """
    packages_for = dict(command_packages or COMMAND_PACKAGES)
    core = {str(item).strip() for item in core_commands if str(item).strip()}

    suggested = {
        str(item).strip() for item in (suggestion.get("suggested_commands") or []) if str(item).strip()
    }
    declared_set = {str(item).strip() for item in declared if str(item).strip()}
    evidence = suggestion.get("evidence") or {}

    undeclared_commands = sorted(suggested - declared_set - core)
    undeclared: List[JsonDict] = []
    unmapped: List[str] = []
    for command in undeclared_commands:
        mapped = command in packages_for
        if not mapped:
            unmapped.append(command)
        undeclared.append(
            {
                "command": command,
                "evidence": list(evidence.get(command) or []),
                # NEVER guessed. An unmapped command carries an empty package
                # list and says so; the alternative is inferring an apt package
                # from a binary name and installing it into the sandbox.
                "packages": sorted(packages_for.get(command, ())) if mapped else [],
                "package_mapping_known": mapped,
            }
        )

    return {
        "schema": COVERAGE_SCHEMA,
        "project": project,
        "repository_path": suggestion.get("repository_path"),
        "suggested_commands": sorted(suggested),
        "declared_commands": sorted(declared_set),
        "undeclared_commands": undeclared,
        "unused_declared_commands": sorted(declared_set - suggested - core),
        "satisfied_commands": sorted(suggested & declared_set),
        "core_supplied_commands": sorted(suggested & core),
        "unmapped_commands": unmapped,
    }


def coverage_has_findings(report: Mapping[str, Any]) -> bool:
    return bool(
        report.get("undeclared_commands") or report.get("unused_declared_commands")
    )


def coverage_signature(report: Mapping[str, Any]) -> JsonDict:
    """The identity of a finding, for dedupe.

    Keyed on the two command sets and the project -- not on evidence text or
    path, which move with checkouts and reword with scanner changes without the
    finding itself being different.
    """
    return {
        "schema": COVERAGE_SCHEMA,
        "project": report.get("project"),
        "undeclared": [entry["command"] for entry in report.get("undeclared_commands") or []],
        "unused": list(report.get("unused_declared_commands") or []),
    }


# ===========================================================================
# The review surface: a staged, non-dispatchable task
# ===========================================================================


def coverage_task_title(report: Mapping[str, Any]) -> str:
    """Name the half that breaks work first, because that is what gets read."""
    project = report.get("project") or "this repository"
    if report.get("undeclared_commands"):
        return (
            "Contract coverage: %s's manifests imply commands its contract does "
            "not declare" % project
        )
    return (
        "Contract coverage: %s declares commands nothing in the repo implies" % project
    )


def coverage_task_description(report: Mapping[str, Any]) -> str:
    """What a reviewer needs to decide -- and what they must decide, not us."""
    project = report.get("project") or "this repository"
    lines = [
        "A static scan of %s's checkout (mac.environment_contract) implies a set "
        "of required commands. Its repository contract declares a different set. "
        "This task reports the difference; nothing has been changed." % project,
        "",
        "NEEDED BUT NOT DECLARED -- the silent failure. The sandbox image is built "
        "from the union of declared contracts, so a command missing here is missing "
        "from the image, and the task fails inside the sandbox on a missing binary "
        "that reads as the task's fault:",
    ]
    if report.get("undeclared_commands"):
        for entry in report["undeclared_commands"]:
            packages = entry.get("packages") or []
            if entry.get("package_mapping_known"):
                supplied = "installed by: %s" % (", ".join(packages) or "(already in the base image)")
            else:
                supplied = (
                    "NO curated package mapping -- sandbox_bom will report it, not "
                    "guess a package for it"
                )
            lines.append(
                "  %s <- %s [%s]"
                % (
                    entry.get("command"),
                    ", ".join(entry.get("evidence") or []) or "(no evidence)",
                    supplied,
                )
            )
    else:
        lines.append("  (none)")
    lines.extend(
        [
            "",
            "DECLARED BUT NOT IMPLIED -- dead weight. Every one of these is a package "
            "in the sandbox, and the sandbox is the security boundary. A manifest scan "
            "cannot see a tool invoked only from a shell script, so this half is a "
            "prompt to look, never a reason to delete:",
        ]
    )
    unused = report.get("unused_declared_commands") or []
    lines.extend(["  %s" % command for command in unused] or ["  (none)"])
    lines.extend(
        [
            "",
            "THE DECISION THIS TASK EXISTS FOR, either answer closes it:",
            "",
            "  1. The scan is RIGHT. Add the missing commands to "
            "toolchain.required_commands in .mac/project.yaml, then re-derive the "
            "image BOM:",
            "       mac admin sandbox-image bom --containerfile deploy/openshell/mac-hermes.Containerfile",
            "",
            "  2. The scan is WRONG for this repo -- the manifest is vendored, the "
            "tool is never invoked, the build runs elsewhere. Say so and close this; "
            "the contract stays as it is.",
            "",
            "NOT a decision anyone should automate: this report never edits a "
            "contract and never selects a package. A command with no curated package "
            "mapping is reported, never guessed, because guessing an apt package from "
            "a binary name installs something plausible into the security boundary.",
        ]
    )
    return "\n".join(lines)


def coverage_task_metadata(report: Mapping[str, Any]) -> JsonDict:
    return {
        COVERAGE_METADATA_KEY: coverage_signature(report),
        # Staged, NOT dispatchable -- the same call sandbox BOM drift makes. An
        # open task is fleet-claimed within minutes, and what an agent would do
        # with this one is rewrite a repository contract from an inference. The
        # whole value here is that a human or an LLM reads the evidence and
        # decides.
        "no_dispatch": True,
    }


def existing_coverage_signatures(tasks: Sequence[Any]) -> Set[str]:
    """Signatures already filed, read from the marker rather than the title."""
    import json as _json

    seen: Set[str] = set()
    for task in tasks:
        record = task.to_dict() if hasattr(task, "to_dict") else task
        if not isinstance(record, Mapping):
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        marker = metadata.get(COVERAGE_METADATA_KEY)
        if isinstance(marker, Mapping):
            seen.add(_json.dumps(marker, sort_keys=True))
    return seen


def declared_commands_from_checkout(repo_path: str | Path) -> Set[str]:
    """``toolchain.required_commands`` as written in the checkout's contract.

    The contract file is what a human typed, and it is the thing this report
    exists to question. Absent or unreadable, the answer is "declares nothing",
    which makes every implied command a finding -- correctly.
    """
    import yaml

    from .services import REPOSITORY_CONTRACT_FILES

    root = Path(repo_path)
    for relative in REPOSITORY_CONTRACT_FILES:
        candidate = root / relative
        if not candidate.is_file():
            continue
        try:
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            return set()
        if not isinstance(raw, Mapping):
            return set()
        toolchain = raw.get("toolchain")
        if not isinstance(toolchain, Mapping):
            return set()
        return {
            str(item).strip()
            for item in (toolchain.get("required_commands") or [])
            if str(item).strip()
        }
    return set()
