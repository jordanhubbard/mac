"""Derive the sandbox bill of materials from repository contracts.

The OpenShell sandbox image is shared by every agent, and an agent does not
know which project will land on it. So the image must cover the union of EVERY
registered project's contract -- not the projects with work in flight, which
would make the BOM move as the backlog moves and destroy reproducibility.

Today that union is maintained by hand. deploy/openshell/mac-hermes.Containerfile
carries a package list whose comments are a ledger of incidents transcribed
after the fact:

    "build-essential: for repos that compile native code (e.g. nanolang's
     3-stage `make build`)"
    "libssl-dev: nanolang's src/sign.c #includes <openssl/evp.h> ... without it
     a coding agent will destructively stub sign.c just to compile"
    "clang/llvm/lld/qemu: the RISC-V validation floor used by c26"

Each names a project whose contract already said what it needed. The contract
was authoritative and nobody read it, so the answer to "when do we permute the
environment" was "when someone notices a repo broke".

This module makes the union mechanical. A contract gains a command, the derived
BOM changes, and the difference is a fact a test can assert rather than a thing
somebody has to remember.

Two deliberate limits:

* A required COMMAND is not a package name. The mapping is CURATED, and a
  command with no mapping is REPORTED, never guessed. Guessing an apt package
  from a binary name is how you install the wrong thing quietly -- and the
  sandbox is the security boundary, so the wrong thing is expensive.
* Deriving the BOM does not install anything. It produces a manifest to compare
  against the image; publishing a new image stays a reviewed step, because the
  frozen-input hash is what makes the deployed sandbox auditable.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

BOM_SCHEMA = "mac.sandbox_bom.v1"

#: What mac itself needs in every sandbox, independent of any project.
#:
#: These are the tools the executor and its policy layer use to do their own
#: work: fetch and verify the repo, run the contract, enforce egress. A project
#: contract never has to declare them, and removing one breaks every task
#: regardless of project.
MAC_CORE_COMMANDS: Tuple[str, ...] = (
    "bash",
    "ca-certificates",
    "curl",
    "git",
    "iproute2",
    "iptables",
    "procps",
    "python3",
    "tar",
    "uv",
    "xz-utils",
)

#: Curated command -> Debian package. A command is what a contract declares; a
#: package is what installs it, and the two are not the same word often enough
#: to infer. Unmapped commands are reported by :func:`derive_bom` so the gap is
#: visible instead of silently absent from the image.
COMMAND_PACKAGES: Dict[str, Tuple[str, ...]] = {
    # C/C++ toolchain. `cc`, `gcc` and `g++` all come from one package, which
    # is exactly why command != package.
    "cc": ("build-essential",),
    "gcc": ("build-essential",),
    "g++": ("build-essential",),
    "ld": ("build-essential",),
    "make": ("make",),
    # cmake/ninja arrived from isaacsim7-poc's contract, which the first
    # derivation could not read at all -- see _derive_sandbox_bom in cli.py.
    "cmake": ("cmake",),
    "ninja": ("ninja-build",),
    "clang": ("clang",),
    "llvm-objcopy": ("llvm",),
    "ld.lld": ("lld",),
    "qemu-system-riscv64": ("qemu-system-misc",),
    # Runtimes. node/npm map to NO apt package on purpose: the image installs
    # Node v22 LTS from NodeSource and explicitly rejects Debian's v18, because
    # current pnpm refuses Node < v22.13 and silently breaks every `pnpm install`
    # repo bootstrap. Claiming apt supplies node made the gap check demand a
    # `nodejs` package the image had deliberately chosen not to install.
    "node": (),
    "npm": (),
    "java": ("openjdk-17-jre-headless",),
    # Libraries a build links against rather than a binary it invokes. A
    # contract may legitimately name these; nanolang's sign.c needs libcrypto.
    "libssl-dev": ("libssl-dev",),
    "openssl": ("openssl",),
    # Base-image commands: declared by contracts, already present, no package
    # to add. Mapped to nothing so they are neither "unmapped" nor duplicated.
    "bash": (),
    "ca-certificates": (),
    "curl": (),
    "git": ("git",),
    "iproute2": ("iproute2",),
    "iptables": ("iptables",),
    "procps": ("procps",),
    "python3": (),
    "tar": ("tar",),
    "uv": (),
    "xz-utils": ("xz-utils",),
    # Supplied by the image build through a channel that is not apt. Mapping
    # them to no package is not the same as leaving them unmapped: these ARE
    # provided, so reporting them as gaps would cry wolf every run.
    #
    #   gh, codegraph, mac  installed by the build
    #   pnpm                npm install -g, pinned to PNPM_VERSION
    #   lein                installed from the reviewed build assets
    "gh": (),
    "codegraph": (),
    "mac": (),
    "pnpm": (),
    "lein": (),
}


def contract_commands(project_record: Any) -> Set[str]:
    """Required commands declared by one project's repository contracts.

    A project may register several repositories, each with its own contract, so
    this is a union within the project as well as across them.
    """
    commands: Set[str] = set()
    record = project_record.to_dict() if hasattr(project_record, "to_dict") else project_record
    if not isinstance(record, Mapping):
        return commands
    repositories = record.get("project_repositories") or record.get("repositories") or []
    candidates: List[Any] = list(repositories)
    # A contract may also sit directly on the project's own metadata.
    if isinstance(record.get("metadata"), Mapping):
        candidates.append(record)
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        metadata = candidate.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        for key in ("repository_contract", "contract"):
            contract = metadata.get(key)
            if not isinstance(contract, Mapping):
                continue
            toolchain = contract.get("toolchain")
            if not isinstance(toolchain, Mapping):
                continue
            for item in toolchain.get("required_commands") or []:
                text = str(item).strip()
                if text:
                    commands.add(text)
    return commands


def derive_bom(
    project_records: Iterable[Any],
    *,
    core_commands: Sequence[str] = MAC_CORE_COMMANDS,
    command_packages: Optional[Mapping[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """The sandbox BOM implied by every registered project, plus mac's own core.

    ``unmapped`` is the important output. A command no mapping covers cannot be
    installed by this derivation, and saying so is the whole point: the
    alternative is inferring a package name from a binary name and installing
    something plausible into the security boundary.
    """
    packages_for = dict(command_packages or COMMAND_PACKAGES)
    per_project: Dict[str, List[str]] = {}
    commands: Set[str] = set(core_commands)
    for record in project_records:
        raw = record.to_dict() if hasattr(record, "to_dict") else record
        name = ""
        if isinstance(raw, Mapping):
            name = str(raw.get("project") or raw.get("name") or "").strip()
        declared = contract_commands(record)
        if declared:
            per_project[name or "(unnamed)"] = sorted(declared)
        commands |= declared

    packages: Set[str] = set()
    unmapped: List[str] = []
    for command in sorted(commands):
        if command not in packages_for:
            unmapped.append(command)
            continue
        packages.update(packages_for[command])

    return {
        "schema": BOM_SCHEMA,
        "commands": sorted(commands),
        "core_commands": sorted(core_commands),
        "packages": sorted(packages),
        "unmapped_commands": unmapped,
        "contributing_projects": per_project,
    }


#: apt flags to skip when reading an install line, and the ones that consume the
#: token after them (``-t bookworm-backports`` names a suite, not a package).
_APT_VALUE_FLAGS = ("-t", "--target-release", "-o")


def installed_packages(image_text: str) -> Set[str]:
    """The packages the Containerfile actually installs.

    Deliberately NOT a substring search over the file. The first version of this
    was, and it silently passed a mutation that deleted cmake and ninja-build
    from the apt line -- because the COMMENT above it still said "cmake/ninja".
    A check that a package is mentioned somewhere is satisfied by the prose
    explaining why it used to be there, which is the one thing it must not
    accept.
    """
    packages: Set[str] = set()
    for raw_line in image_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            continue
        marker = "apt-get install"
        index = line.find(marker)
        if index < 0:
            continue
        tokens = line[index + len(marker) :].rstrip("\\").split()
        skip_next = False
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            if token in _APT_VALUE_FLAGS:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            if token in ("&&", "|", "||", ";"):
                break
            packages.add(token)
    return packages


def bom_gaps(bom: Mapping[str, Any], image_text: str) -> Dict[str, List[str]]:
    """What the derived BOM requires that the image does not install.

    ``missing_packages`` is the precise signal: it compares against the packages
    the apt lines actually name. ``missing_commands`` stays a containment check
    over non-comment text, because a command can arrive by npm, by COPY from
    build assets, or from the base image, and there is no single line to parse.
    It is a hint; the package list is the assertion.

    Neither answers "does the BUILT image contain this" -- only a build can. The
    drift worth catching in a test is a contract declaring something the
    Containerfile never installs, which is exactly what the hand-maintained
    ledger kept missing.
    """
    installed = installed_packages(image_text)
    body = "\n".join(
        line for line in image_text.splitlines() if not line.strip().startswith("#")
    )
    missing_packages = [
        package for package in bom.get("packages") or [] if package not in installed
    ]
    missing_commands = [
        command for command in bom.get("commands") or [] if command not in body
    ]
    return {
        "missing_packages": missing_packages,
        "missing_commands": missing_commands,
        "unmapped_commands": list(bom.get("unmapped_commands") or []),
    }


#: The reviewed manifest, committed to the repo.
#:
#: The derivation reads the live hub, which is not reproducible from a checkout
#: and must not be: an image build that reaches out to a mutable ledger for its
#: own inputs cannot be audited afterwards. So the flow is deliberately in two
#: steps -- derive from contracts, review the diff, commit the manifest -- and
#: the manifest is what the frozen-input hash covers.
MANIFEST_PATH = "deploy/openshell/sandbox-bom.json"

#: Fields carried into the committed manifest, in a stable order.
_MANIFEST_FIELDS = (
    "schema",
    "commands",
    "core_commands",
    "packages",
    "unmapped_commands",
    "contributing_projects",
)


def manifest(bom: Mapping[str, Any]) -> Dict[str, Any]:
    """The committable form of a derived BOM.

    ``contributing_projects`` is kept even though it churns more than the rest.
    It is the provenance -- why qemu-system-misc is in the image at all -- and
    reconstructing that by hand months later is precisely the work the
    Containerfile's incident comments were doing badly.
    """
    return {field: bom.get(field) for field in _MANIFEST_FIELDS if field in bom}


def manifest_drift(
    committed: Mapping[str, Any], derived: Mapping[str, Any]
) -> Dict[str, List[str]]:
    """How the live contracts differ from the reviewed manifest.

    Drift in EITHER direction is reported. A removed command matters as much as
    an added one: it is a package in the security boundary that no contract asks
    for any more, and nothing else in the system will ever notice it went stale.
    """
    drift: Dict[str, List[str]] = {}
    for field in ("commands", "packages"):
        before = set(committed.get(field) or [])
        after = set(derived.get(field) or [])
        drift["added_" + field] = sorted(after - before)
        drift["removed_" + field] = sorted(before - after)
    return drift


def manifest_has_drift(drift: Mapping[str, Sequence[str]]) -> bool:
    return any(drift.values())
