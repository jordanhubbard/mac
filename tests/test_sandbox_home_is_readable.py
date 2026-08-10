"""The sandbox user's own home must be readable inside the sandbox.

The policy sets `run_as_user: sandbox`, so bash starts with
HOME=/home/sandbox and a login shell sources ~/.profile before the task's
first command. /home/sandbox was in no granted path set, so Landlock denied
the read and every command in every sandbox began with

    /bin/bash: /home/sandbox/.profile: Permission denied

after which the repository verifier exited non-zero and the gate recorded
`repository_test_failed` -- failing work that had already been done correctly.
Measured on 2026-08-10: 19 events across 5 tasks, including canary children
that probed their toolchain successfully and were failed anyway.

An earlier fix installed a readable .profile in the image. It could not have
worked: the file is mode 0644, world-readable. The denial was never about file
permissions. A path outside the granted set returns EACCES, and bash reports
that identically -- which is exactly why the wrong fix looked plausible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

POLICY = Path(__file__).resolve().parents[1] / "deploy/openshell/mac-hermes-policy.yaml"


@pytest.fixture(scope="module")
def policy():
    # The template carries __PLACEHOLDER__ tokens; they are valid YAML scalars.
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def test_the_sandbox_home_is_granted(policy):
    read_only = policy["filesystem_policy"]["read_only"]

    assert "/home/sandbox" in read_only, (
        "run_as_user is sandbox, so a login shell reads /home/sandbox/.profile; "
        "outside the granted set Landlock denies it and the gate fails the task"
    )


def test_the_home_matches_the_user_the_policy_runs_as(policy):
    """If run_as_user ever changes, the granted home must change with it --
    otherwise this regresses silently and looks like a file-permission bug."""
    user = policy["process"]["run_as_user"]
    read_only = policy["filesystem_policy"]["read_only"]

    assert "/home/%s" % user in read_only


def test_the_grant_is_read_only_not_write(policy):
    """The home is read to source a profile. Nothing needs to write there --
    caches and config are already redirected elsewhere by this policy."""
    assert "/home/sandbox" not in policy["filesystem_policy"]["read_write"]
