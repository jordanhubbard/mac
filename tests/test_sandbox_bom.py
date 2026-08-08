"""The sandbox image must be derived from repository contracts, not memory.

deploy/openshell/mac-hermes.Containerfile is a hand-maintained package list
whose comments are a ledger of incidents transcribed after the fact::

    "libssl-dev: nanolang's src/sign.c #includes <openssl/evp.h> ... without it
     a coding agent will destructively stub sign.c just to compile"

Every one of those names a project whose contract already declared the tool.
The contract was authoritative and nobody read it, so the answer to "when do we
permute the environment" was "when someone notices a repo broke".

These tests cover the derivation that replaces that, and -- more usefully -- the
ways it could be worse than the manual list it replaces: by under-deriving
silently, by guessing package names, or by drifting from the image it claims to
describe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac.sandbox_bom import (
    MANIFEST_PATH,
    bom_gaps,
    contract_commands,
    derive_bom,
    installed_packages,
    manifest,
    manifest_drift,
    manifest_has_drift,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = REPO_ROOT / "deploy/openshell/mac-hermes.Containerfile"
MANIFEST = REPO_ROOT / MANIFEST_PATH


def _registration(project, commands, *, key="repository_contract"):
    """A repository registration as the ledger returns it."""
    return {
        "project": project,
        "metadata": {key: {"toolchain": {"required_commands": list(commands)}}},
    }


# --------------------------------------------------------------------------
# Reading contracts
# --------------------------------------------------------------------------


def test_a_contract_supplies_its_required_commands():
    assert contract_commands(_registration("c26", ["qemu-system-riscv64"])) == {
        "qemu-system-riscv64"
    }


@pytest.mark.parametrize("record", [None, {}, "junk", {"metadata": "not-a-mapping"}])
def test_a_record_with_no_contract_contributes_nothing(record):
    assert contract_commands(record) == set()


def test_every_project_contributes_not_only_the_busy_ones():
    """An agent does not know which project will land on it.

    A BOM that tracked projects with work in flight would change as the backlog
    moved, which destroys the reproducibility the frozen hash exists to give.
    """
    bom = derive_bom(
        [_registration("idle-project", ["cmake"]), _registration("busy", ["make"])]
    )

    assert "cmake" in bom["commands"]


def test_several_repositories_in_one_project_are_all_read():
    bom = derive_bom(
        [_registration("ova", ["make"]), _registration("ova", ["node"])]
    )

    assert {"make", "node"} <= set(bom["commands"])


def test_mac_core_commands_are_present_without_any_contract():
    """git and python3 are how the executor does its own work. No contract has
    to ask for them, and removing one breaks every project at once."""
    bom = derive_bom([])

    assert {"git", "python3", "bash", "curl"} <= set(bom["commands"])


def test_the_bom_records_which_project_asked_for_what():
    """Provenance is the thing the Containerfile comments were doing by hand.

    Without it, nobody can answer "why is qemu in the sandbox" a year later,
    which is how a package outlives the contract that justified it.
    """
    bom = derive_bom([_registration("c26", ["qemu-system-riscv64"])])

    assert bom["contributing_projects"]["c26"] == ["qemu-system-riscv64"]


# --------------------------------------------------------------------------
# Commands are not package names
# --------------------------------------------------------------------------


def test_a_command_maps_to_the_package_that_installs_it():
    """`cc` is not a package; build-essential is. This is exactly why the
    mapping cannot be inferred from the binary name."""
    bom = derive_bom([_registration("nanolang", ["cc"])])

    assert "build-essential" in bom["packages"]


def test_an_unmapped_command_is_reported_and_never_guessed():
    """Guessing an apt package from a binary name installs something plausible
    into the security boundary. Reporting is the whole design."""
    bom = derive_bom([_registration("new", ["some-unheard-of-tool"])])

    assert bom["unmapped_commands"] == ["some-unheard-of-tool"]
    assert "some-unheard-of-tool" not in bom["packages"]


def test_a_tool_installed_outside_apt_is_covered_not_unmapped():
    """pnpm comes from `npm install -g` and lein from the reviewed build assets.

    Reporting them as gaps because no apt package supplies them would cry wolf
    on every run, and a gap report nobody trusts is a gap report nobody reads.
    """
    bom = derive_bom([_registration("Aviation", ["pnpm", "lein"])])

    assert bom["unmapped_commands"] == []


# --------------------------------------------------------------------------
# Drift against the image
# --------------------------------------------------------------------------


def test_the_committed_manifest_is_covered_by_the_image():
    """The check that makes the whole thing worth having.

    If this fails, some project's contract requires a tool the sandbox does not
    ship, and the failure mode is a coding agent quietly mutilating source to
    get a build to pass -- which is what the libssl-dev comment records.
    """
    committed = json.loads(MANIFEST.read_text(encoding="utf-8"))
    gaps = bom_gaps(committed, CONTAINERFILE.read_text(encoding="utf-8"))

    assert gaps["missing_packages"] == []
    assert gaps["unmapped_commands"] == []


def test_the_manifest_is_what_the_published_image_hash_covers():
    """Otherwise the BOM is a document beside the image rather than part of its
    identity, and the two can disagree without anything noticing."""
    identity = (REPO_ROOT / "scripts/image-publication-identity.py").read_text(
        encoding="utf-8"
    )

    assert MANIFEST_PATH in identity


def test_a_missing_package_is_reported():
    gaps = bom_gaps({"packages": ["libfoo-dev"], "commands": []}, "FROM debian")

    assert gaps["missing_packages"] == ["libfoo-dev"]


def test_drift_reports_a_newly_required_tool():
    drift = manifest_drift({"commands": ["make"]}, {"commands": ["make", "cmake"]})

    assert drift["added_commands"] == ["cmake"]
    assert manifest_has_drift(drift)


def test_drift_reports_a_tool_no_contract_asks_for_any_more():
    """A package nothing requires is still in the security boundary, and
    nothing else in the system will ever notice it went stale."""
    drift = manifest_drift({"commands": ["make", "lein"]}, {"commands": ["make"]})

    assert drift["removed_commands"] == ["lein"]
    assert manifest_has_drift(drift)


def test_no_drift_when_the_manifest_matches_the_contracts():
    drift = manifest_drift({"commands": ["make"]}, {"commands": ["make"]})

    assert not manifest_has_drift(drift)


def test_the_manifest_form_is_stable_and_committable():
    written = manifest(derive_bom([_registration("c26", ["make"])]))

    assert json.loads(json.dumps(written, sort_keys=True)) == written
    assert "schema" in written


def test_a_package_named_only_in_a_comment_is_not_installed():
    """The mutation that caught this: deleting cmake from the apt line left the
    comment above it saying "cmake/ninja", and a substring check over the file
    passed. A gap test satisfied by the prose explaining a package is worthless.
    """
    image = "# cmake/ninja-build: needed by isaacsim7-poc\nRUN apt-get install -y make\n"

    gaps = bom_gaps({"packages": ["cmake", "make"], "commands": []}, image)

    assert gaps["missing_packages"] == ["cmake"]


def test_an_apt_suite_is_not_read_as_a_package():
    """`-t bookworm-backports` names a suite. Treating it as installed would
    make a BOM entry called "bookworm-backports" look satisfied."""
    installed = installed_packages(
        "RUN apt-get install -y --no-install-recommends -t bookworm-backports qemu-system-misc\n"
    )

    assert installed == {"qemu-system-misc"}
