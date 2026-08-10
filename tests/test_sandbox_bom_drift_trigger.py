"""Registering or deleting a project must not leave the image manifest stale.

The derivation is only worth having if something runs it. Registering a repo
whose contract needs a tool the image lacks is exactly the moment the fleet
becomes wrong, and it is silent -- the gap shows up later as a coding agent
provisioning a tool per task, or editing source until it builds without one.

These cover the trigger and, more importantly, the two ways it could be worse
than nothing: by failing a registration over a diagnostic, and by filing the
same report often enough that nobody reads it.
"""

from __future__ import annotations

import subprocess

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp(tmp_path):
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _repo(tmp_path, name, commands, project="mac"):
    """A real checkout with a real contract.

    Registration reads .mac/project.yaml from the repo rather than trusting
    metadata handed to it, which is the right call and means a test that fakes
    the metadata would be testing a path production never takes.
    """
    repo = tmp_path / name
    (repo / ".mac").mkdir(parents=True)
    (repo / ".mac" / "project.yaml").write_text(
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: %s" % project,
                "platforms: [linux]",
                "toolchain:",
                "  required_commands: [%s]" % ", ".join(commands),
                "bootstrap:",
                "  command: '/bin/true'",
                "test:",
                "  command: '/bin/true'",
                "evidence:",
                "  required: [log]",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _drift_tasks(cp):
    return [
        task
        for task in cp.list_tasks()
        if "sandbox_bom_drift" in (task.metadata or {})
    ]


def test_registering_a_repo_with_a_new_tool_reports_drift(cp, tmp_path):
    repo = _repo(tmp_path, "newrepo", ["zig"])
    cp.register_project_repository("newrepo", str(repo), project="mac")

    assert _drift_tasks(cp), "a contract requiring an unshipped tool went unreported"


def test_the_report_names_the_tool_and_the_next_step(cp, tmp_path):
    """A drift report that does not say what to do gets read once."""
    repo = _repo(tmp_path, "newrepo", ["zig"])
    cp.register_project_repository("newrepo", str(repo), project="mac")

    description = _drift_tasks(cp)[0].description
    assert "zig" in description
    assert "mac admin sandbox rollout" in description


def test_the_same_drift_is_not_filed_twice(cp, tmp_path):
    """Re-filing per registration buries the one report that matters."""
    for name in ("repo_a", "repo_b"):
        cp.register_project_repository(
            name, str(_repo(tmp_path, name, ["zig"])), project="mac"
        )

    assert len(_drift_tasks(cp)) == 1


def test_registering_a_repo_the_image_already_covers_adds_no_requirement(cp, tmp_path):
    """The common case must not claim the image needs anything new.

    Asserted on added_commands rather than on "no task was filed": drift is
    reported in BOTH directions, and this control plane holds only the repo the
    test registered, so every OTHER command in the reviewed manifest correctly
    reads as no-longer-required. That is the removed half doing its job, not a
    false positive on the added half.
    """
    repo = _repo(tmp_path, "covered", ["make", "git"])
    cp.register_project_repository("covered", str(repo), project="mac")

    report = cp.check_sandbox_bom_drift()

    assert report["checked"], report
    assert report["drift"]["added_commands"] == []


def test_a_newly_required_tool_shows_up_as_added(cp, tmp_path):
    """The half that breaks work if it is missed."""
    repo = _repo(tmp_path, "needy", ["zig"])
    cp.register_project_repository("needy", str(repo), project="mac")

    report = cp.check_sandbox_bom_drift()

    assert report["drift"]["added_commands"] == ["zig"]


def test_a_failing_drift_check_never_fails_the_registration(cp, tmp_path, monkeypatch):
    """A diagnostic must not be why a project cannot be registered."""
    monkeypatch.setattr(
        cp, "list_project_repositories", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    repo = _repo(tmp_path, "newrepo", ["zig"])

    registered = cp.register_project_repository("newrepo", str(repo), project="mac")

    assert registered.id


def test_deleting_a_project_also_checks(cp, tmp_path):
    """A deleted project may have been the only thing requiring a package, and
    a tool nothing asks for is still sitting in the security boundary."""
    cp.create_project("doomed", dispatch_paused=False)
    repo = _repo(tmp_path, "gone", ["make"], project="doomed")
    cp.register_project_repository("gone", str(repo), project="doomed")

    cp.delete_project("doomed", force=True)

    assert isinstance(cp.check_sandbox_bom_drift(), dict)


def test_the_drift_report_is_staged_not_dispatchable(cp, tmp_path):
    """An open task is fleet-claimed within minutes, and what an agent would do
    with this one is edit the Containerfile and publish an image -- the
    unreviewed supply-chain path the whole design exists to keep closed.

    The existing dispatch suite caught this: registering a repository started
    handing the fleet work as a side effect.
    """
    repo = _repo(tmp_path, "needy", ["zig"])
    cp.register_project_repository("needy", str(repo), project="mac")

    assert _drift_tasks(cp)[0].metadata.get("no_dispatch") is True


def test_deleting_a_project_does_not_leave_an_ownerless_task(cp, tmp_path):
    """Deletion can only REMOVE requirements, and there is no project left to
    own a ticket about it.

    Filing anyway put an unowned task in the "unassigned" bucket on every
    project deletion -- work nothing would claim or close. CI caught this;
    review did not.
    """
    cp.create_project("doomed", dispatch_paused=False)
    repo = _repo(tmp_path, "doomed-repo", ["make"], project="doomed")
    cp.register_project_repository("doomed-repo", str(repo), project="doomed")
    before = len(_drift_tasks(cp))

    cp.delete_project("doomed", force=True)

    assert len(_drift_tasks(cp)) == before


def test_removal_only_drift_is_reported_even_though_it_is_not_filed(cp):
    """Not filing is not the same as not noticing. `mac sandbox bom --compare`
    exits non-zero on drift in either direction, so CI still catches a stale
    manifest; this just does not manufacture a ticket for it."""
    report = cp.check_sandbox_bom_drift()

    assert report["checked"]
    assert report["drift"]["removed_commands"], "removal drift went unreported"
    assert report["filed"] is None
