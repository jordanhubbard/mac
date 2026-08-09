"""`mac human-interface` — the commands that make porting invocable.

``mac.human_interface_profile`` could port an agent's identity, memory and
messaging credentials between Hermes and OpenClaw in either direction for four
weeks, and nothing ever called it. The operator rule -- port before you switch
-- cannot be followed if there is no command that ports (task_61b3f521).

These exercise the CLI seam specifically: argument wiring, the dry-run default,
and that the gate's refusal reaches the caller. The porting semantics
themselves are covered by tests/test_human_interface_profile.py and the gate by
tests/test_human_interface_switch_gate.py.

Unlike most CLI families these commands are LOCAL: an agent's identity
documents and messaging credentials live in its home directory, so there is no
hub call and no ``--db`` to supply.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.human_interface_profile import ProfilePortError


def _run(tmp_path, *args):
    """Run `mac <args>` and return (rc, parsed_output).

    No ``--db``: these commands never touch the control plane.
    """
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        # --json is prepended here rather than at each call site so the calls
        # read `_run(tmp_path, "<domain>", "<subcommand>", ...)`, which is the
        # form tests/cli/test_cli_coverage_gate.py scans for. A leading
        # "--json" argument makes that gate read the domain as "--json" and
        # the command as uncovered.
        rc = main(["--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


@pytest.fixture()
def agent_home(tmp_path):
    """OpenClaw holding the live profile, as the hub did on 2026-08-04."""
    home = tmp_path / "home"
    (home / ".mac" / "openclaw" / "workspace").mkdir(parents=True)
    (home / ".hermes").mkdir(parents=True)
    (home / ".mac" / "openclaw" / "workspace" / "MEMORY.md").write_text(
        "# MEMORY\n- search state.db before asking the user to repeat context\n",
        encoding="utf-8",
    )
    (home / ".mac" / "openclaw" / "workspace" / "SOUL.md").write_text(
        "# SOUL\n", encoding="utf-8"
    )
    return home


def _state(home):
    return str(home / "port-state.json")


def test_port_is_a_dry_run_unless_apply_is_given(tmp_path, agent_home):
    """The default must not rewrite an agent's identity and credentials."""
    rc, report = _run(
        tmp_path,
        "admin", "human-interface",
        "port",
        "--from",
        "openclaw",
        "--to",
        "hermes",
        "--home",
        str(agent_home),
        "--state-file",
        _state(agent_home),
    )

    assert rc == 0
    assert report["dry_run"] is True
    assert not (agent_home / ".hermes" / "MEMORY.md").exists(), (
        "a dry run wrote to the target"
    )


def test_port_with_apply_moves_the_profile(tmp_path, agent_home):
    rc, report = _run(
        tmp_path,
        "admin", "human-interface",
        "port",
        "--from",
        "openclaw",
        "--to",
        "hermes",
        "--home",
        str(agent_home),
        "--state-file",
        _state(agent_home),
        "--apply",
    )

    assert rc == 0
    assert report["dry_run"] is False
    ported = agent_home / ".hermes" / "MEMORY.md"
    assert ported.is_file()
    assert "search state.db" in ported.read_text(encoding="utf-8")


def test_port_leaves_the_source_untouched(tmp_path, agent_home):
    """Porting is a read of one tree and a write into the other."""
    source = agent_home / ".mac" / "openclaw" / "workspace" / "MEMORY.md"
    before = source.read_text(encoding="utf-8")

    _run(
        tmp_path, "admin", "human-interface", "port",
        "--from", "openclaw", "--to", "hermes",
        "--home", str(agent_home), "--state-file", _state(agent_home), "--apply",
    )

    assert source.read_text(encoding="utf-8") == before


def test_check_reports_an_unported_target(tmp_path, agent_home):
    rc, readiness = _run(
        tmp_path, "admin", "human-interface", "check",
        "--to", "hermes",
        "--home", str(agent_home), "--state-file", _state(agent_home),
    )

    assert rc == 0
    assert readiness["ready"] is False
    assert readiness["reason"] == "never_ported"


def test_check_reports_readiness_after_a_port(tmp_path, agent_home):
    _run(
        tmp_path, "admin", "human-interface", "port",
        "--from", "openclaw", "--to", "hermes",
        "--home", str(agent_home), "--state-file", _state(agent_home), "--apply",
    )

    rc, readiness = _run(
        tmp_path, "admin", "human-interface", "check",
        "--to", "hermes",
        "--home", str(agent_home), "--state-file", _state(agent_home),
    )

    assert rc == 0
    assert readiness["ready"] is True


def test_check_without_assert_ready_does_not_fail(tmp_path, agent_home):
    """Reporting and enforcing are separate, so an operator can look first."""
    rc, readiness = _run(
        tmp_path, "admin", "human-interface", "check",
        "--to", "hermes",
        "--home", str(agent_home), "--state-file", _state(agent_home),
    )

    assert rc == 0
    assert readiness["ready"] is False


def test_assert_ready_raises_when_the_profile_is_not_current(tmp_path, agent_home):
    """The deploy gate's seam. It must not quietly return success."""
    with pytest.raises(ProfilePortError) as excinfo:
        _run(
            tmp_path, "admin", "human-interface", "check",
            "--to", "hermes", "--assert-ready",
            "--home", str(agent_home), "--state-file", _state(agent_home),
        )

    assert "mac admin human-interface port" in str(excinfo.value)


def test_porting_to_the_same_interface_is_refused(tmp_path, agent_home):
    with pytest.raises(ProfilePortError):
        _run(
            tmp_path, "admin", "human-interface", "port",
            "--from", "hermes", "--to", "hermes",
            "--home", str(agent_home), "--state-file", _state(agent_home),
        )


def test_an_unknown_interface_is_rejected_by_the_parser(tmp_path, agent_home):
    """argparse choices, so a typo fails before touching an agent's home."""
    with pytest.raises(SystemExit):
        _run(
            tmp_path, "admin", "human-interface", "port",
            "--from", "slack", "--to", "hermes",
        )
