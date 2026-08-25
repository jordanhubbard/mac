"""`mac project list` must be readable, and must not list ghosts.

Three defects, all visible in one live run against the hub:

1. **Every project printed as an 18-field key=value dump.** `_one_liner` has a
   real column layout for tasks and for agents; a project summary fell through
   to the generic scalar dump, so one project was one ~340-character line
   carrying `repository_url` and `repository_registration` -- the same URL
   twice, both truncated with an ellipsis mid-hostname.

2. **9 of 19 projects were junk manufactured by our own worktree rule.**
   CLAUDE.md mandates `git worktree add /tmp/mac-<task>` per agent. Filing from
   there inferred the project from `git rev-parse --show-toplevel`, whose
   basename in a linked worktree is the WORKTREE directory. So `mac-bom`,
   `mac-dead1`, `mac-deadtests`, `mac-dev`, `mac-dispatch-fix` and
   `mac-sandbox-loop` were all live projects on the hub, alongside `tmp`,
   `tasks` and `jkh`. The convention that stops agents colliding was quietly
   shredding the project namespace.

3. **Derived projects are unreachable.** They have no `project_id`, so
   `mac project show mac-dev` cannot resolve one, and nothing can be
   dispatched to it. Listing them by default offers an operator rows that no
   other verb accepts.
"""

from __future__ import annotations

import argparse
import os
import subprocess

import pytest

from mac import cli


def _rec(**over):
    base = {
        "project": "mac",
        "project_id": "project_72e4",
        "status": "active",
        "task_count": 6788,
        "active_count": 446,
        "ready_count": 5,
        "blocked_count": 357,
        "review_count": 10,
        "repository_url": "git@github.com:jordanhubbard/mac.git",
        "default_branch": "main",
        "state_counts": {"open": 54, "cancelled": 3524},
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# 1. formatting
# --------------------------------------------------------------------------


def test_a_project_is_not_rendered_as_a_key_value_dump():
    line = cli._one_liner(_rec())

    assert "task_count=" not in line, (
        "the generic scalar dump is still being used for projects; every one "
        "of the 18 fields lands on a single unreadable line"
    )
    assert line.startswith("mac "), "the project name must lead the line"
    assert "active" in line and "446" in line


def test_the_repository_is_named_once_not_twice():
    """`repository_url` and `repository_registration` are the same URL; the
    dump printed both, each truncated mid-hostname."""
    line = cli._one_liner(_rec(repository_registration="git@github.com:jordanhubbard/mac.git#main"))

    assert line.count("jordanhubbard/mac.git") == 1


def test_the_counts_that_describe_live_work_are_all_present():
    line = cli._one_liner(_rec(active_count=98, ready_count=0, blocked_count=84, review_count=7))

    for value in ("98", "84", "7"):
        assert value in line, "count %s is missing from the rendered line" % value


def test_a_project_without_a_repository_still_renders():
    line = cli._one_liner(_rec(project="fleet-maintenance", repository_url="", default_branch=""))

    assert line.startswith("fleet-maintenance")
    assert "#" not in line, "an empty repo must not render as a bare '#branch'"


def test_a_task_is_still_rendered_as_a_task():
    """The project branch keys off `task_count`; a task record must not match
    it. Tasks carry `project` too."""
    line = cli._one_liner({"id": "task_d95bcaee", "state": "open", "project": "mac", "title": "x"})

    assert "open" in line and "x" in line
    assert "act" not in line


# --------------------------------------------------------------------------
# 2. the worktree-basename project factory
# --------------------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo_with_worktree(tmp_path):
    repo = tmp_path / "mac"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "init")
    tree = tmp_path / "mac-dev"
    _git(repo, "worktree", "add", "-q", str(tree), "-b", "wt")
    return repo, tree


def test_filing_from_a_worktree_uses_the_repository_not_the_directory(
    repo_with_worktree, monkeypatch
):
    """This is the bug that put mac-dev, mac-bom and mac-deadtests on the hub."""
    _repo, tree = repo_with_worktree
    monkeypatch.chdir(tree)

    assert cli._default_project_from_cwd() == "mac", (
        "a task filed from the CLAUDE.md-mandated worktree was assigned to a "
        "project named after the worktree directory"
    )


def test_the_main_checkout_is_unchanged(repo_with_worktree, monkeypatch):
    repo, _tree = repo_with_worktree
    monkeypatch.chdir(repo)

    assert cli._default_project_from_cwd() == "mac"


def test_a_subdirectory_of_a_worktree_also_resolves(repo_with_worktree, monkeypatch):
    _repo, tree = repo_with_worktree
    nested = tree / "src" / "mac"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert cli._default_project_from_cwd() == "mac"


def test_outside_a_repository_it_still_falls_back_to_the_directory(tmp_path, monkeypatch):
    """Non-git directories keep bd parity; only the worktree case changes."""
    plain = tmp_path / "somewhere"
    plain.mkdir()
    monkeypatch.chdir(plain)

    assert cli._default_project_from_cwd() == "somewhere"


# --------------------------------------------------------------------------
# 3. default scope
# --------------------------------------------------------------------------


def test_a_derived_project_holding_only_terminal_tasks_is_hidden():
    ghost = _rec(
        project="mac-dev",
        project_id=None,
        status="derived",
        state_counts={"cancelled": 2},
    )

    assert not cli._project_is_reachable(ghost), (
        "`mac project show mac-dev` cannot resolve this and nothing can be "
        "dispatched to it, yet it occupied a row"
    )


def test_a_derived_project_with_live_work_is_kept():
    """Hiding it would hide the tasks, which still have to go somewhere."""
    live = _rec(
        project="unassigned",
        project_id=None,
        status="derived",
        state_counts={"open": 11, "blocked": 16, "cancelled": 473},
    )

    assert cli._project_is_reachable(live)


def test_a_registered_project_survives_an_entirely_terminal_backlog():
    """An operator asked for it; an empty backlog is a fact, not a reason to
    conceal the project."""
    quiet = _rec(
        project="fleet-maintenance",
        project_id="proj_af53",
        state_counts={"cancelled": 14},
    )

    assert cli._project_is_reachable(quiet)


def test_a_registered_project_with_no_tasks_at_all_survives():
    assert cli._project_is_reachable(_rec(project="c26", state_counts={}))


def test_all_shows_everything(monkeypatch, capsys):
    records = [
        _rec(),
        _rec(project="mac-dev", project_id=None, state_counts={"cancelled": 2}),
    ]

    class _Plane:
        def list_projects(self):
            return records

    monkeypatch.setattr(cli, "_plane", lambda args: _Plane())

    cli.cmd_project_list(argparse.Namespace(all=True, selector=None))
    assert "mac-dev" in capsys.readouterr().out

    cli.cmd_project_list(argparse.Namespace(all=False, selector=None))
    assert "mac-dev" not in capsys.readouterr().out


def test_the_flag_is_declared_on_the_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["project", "list", "--all"])

    assert args.all is True
    assert parser.parse_args(["project", "list"]).all is False
