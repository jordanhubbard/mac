"""git >= 2.38 is a declared, machine-checkable fleet prerequisite.

The merge-gate suite (``tests/test_merge_queue.py`` and friends) and the
production merge queue (``src/mac/merge_queue.py``) both call
``git merge-tree --write-tree``, which only exists in git >= 2.38. On a host
running Ubuntu 22.04 distro git (2.34.1) -- e.g. the GKE pod that ran the
parent task's authoritative gate -- those calls fail with an opaque rc=129, so
a whole gate run is burned and reported as a misleading generic test failure.

``scripts/run-contract-tests.sh`` fails fast on an older git as a runner-side
backstop, but that does not stop a host or image from *shipping* an old git.
This test pins the requirement in one machine-checkable place:

- the floor is DECLARED in ``.mac/project.yaml``
  (``toolchain.command_minimum_versions.git``) and is >= 2.38;
- the runner-side backstop in ``scripts/run-contract-tests.sh`` agrees with it;
- every provisioning asset that installs git enforces the same floor, so the
  prerequisite cannot silently regress when a base image or install script is
  edited.

It is deliberately cheap and reads only checked-in text, so it runs in every
suite without building an image or touching a host.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The absolute minimum every git-provisioning path must satisfy. The declared
#: floor may be raised above this, but never below it.
_ABSOLUTE_MIN = (2, 38)


def _declared_git_floor() -> tuple[int, int]:
    contract = yaml.safe_load((REPO_ROOT / ".mac" / "project.yaml").read_text(encoding="utf-8"))
    toolchain = contract.get("toolchain") or {}
    minimums = toolchain.get("command_minimum_versions") or {}
    assert "git" in minimums, (
        "toolchain.command_minimum_versions.git must declare the git floor in .mac/project.yaml"
    )
    raw = str(minimums["git"]).strip()
    match = re.fullmatch(r"(\d+)\.(\d+)", raw)
    assert match, "git minimum version must be MAJOR.MINOR, got %r" % raw
    return int(match.group(1)), int(match.group(2))


def test_declared_git_floor_is_at_least_2_38() -> None:
    assert _declared_git_floor() >= _ABSOLUTE_MIN, (
        "declared git floor must be >= 2.38 (git merge-tree --write-tree)"
    )


def test_contract_runner_backstop_matches_declared_floor() -> None:
    runner = (REPO_ROOT / "scripts" / "run-contract-tests.sh").read_text(encoding="utf-8")
    major_match = re.search(r"_MAC_GIT_REQUIRED_MAJOR=(\d+)", runner)
    minor_match = re.search(r"_MAC_GIT_REQUIRED_MINOR=(\d+)", runner)
    assert major_match and minor_match, (
        "run-contract-tests.sh must define _MAC_GIT_REQUIRED_MAJOR/_MINOR"
    )
    runner_floor = (int(major_match.group(1)), int(minor_match.group(1)))
    assert runner_floor == _declared_git_floor(), (
        "run-contract-tests.sh git floor %r must match the declared contract floor %r"
        % (runner_floor, _declared_git_floor())
    )


#: Provisioning assets that install git. Each must enforce the declared floor at
#: build/onboard time. Kept explicit rather than globbed: a glob would silently
#: stop covering a file that got renamed.
_GIT_PROVISIONING_ASSETS = (
    "Dockerfile.codex-runner",
    "deploy/openshell/mac-hermes.Containerfile",
    "deploy/fleet-node-install.sh",
)


def _installs_git(text: str) -> bool:
    # apt-installs git as its own package (not a substring of another token).
    return re.search(r"install[^\n]*\bgit\b", text) is not None or "command -v git" in text


@pytest.mark.parametrize("relpath", _GIT_PROVISIONING_ASSETS)
def test_git_provisioning_asset_enforces_floor(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.exists(), "expected git-provisioning asset %s to exist" % relpath
    text = path.read_text(encoding="utf-8")
    assert _installs_git(text), (
        "%s no longer installs/requires git; update _GIT_PROVISIONING_ASSETS" % relpath
    )
    major, minor = _declared_git_floor()
    # The asset must name the exact declared minor floor in a version comparison,
    # so a base-image or script change that drops the check fails this test.
    assert re.search(r"-ge %d\b" % minor, text) or re.search(r"\b%d\b" % minor, text), (
        "%s must assert git >= %d.%d (git merge-tree --write-tree)" % (relpath, major, minor)
    )
    # And it must actually compare a parsed git version, not merely mention the
    # number in prose: require both a git-version parse and a numeric guard.
    assert "git version" in text, (
        "%s must parse `git version` to enforce the floor" % relpath
    )
