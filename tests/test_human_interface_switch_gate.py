"""Porting must be invocable, and switching without it must fail loudly.

``mac.human_interface_profile`` could port an agent's identity, memory and
messaging credentials in either direction for four weeks, and none of it ever
ran. Nothing called it: no CLI command, no deploy hook, no gate. The operator
rule -- "whichever interface the agent last used is authoritative, and its
profile must be ported before the switch" -- lived in a ticket, and a ticket
cannot stop a deploy.

That gap is not academic. Measured on the hub 2026-08-04, the two MEMORY.md
files had diverged and become DISJOINT: OpenClaw's held April-July operational
knowledge (a mandatory context-search directive, the AgentFS canonical-storage
rule, safety-filter workarounds), and Hermes' April copy held the record of the
PREVIOUS migration -- including its hard-won fix, that Slack tokens do not port
automatically. The agent had written that exact failure down in April. In
August the same class of failure recurred: a Hermes gateway started with no
Slack signing secret and could not connect. The knowledge existed, in the
agent's own memory, in the copy the live interface could not read.

So these tests cover the two things that were missing rather than the porting
itself (which tests/test_human_interface_profile.py already covers):

  * the port is reachable as a command, and
  * a switch that would lose the profile is refused, with a message naming the
    command that fixes it.

The freshness test is the one that matters most. A completion timestamp cannot
answer the real question: the source keeps accumulating knowledge after a port,
and an hour-old port of a since-changed MEMORY.md loses exactly as much as no
port at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac.human_interface_profile import (
    ProfilePortError,
    assert_switch_ported,
    port_profile,
    switch_readiness,
)


@pytest.fixture()
def home(tmp_path):
    """An agent home with OpenClaw holding the live profile, as the hub did."""
    (tmp_path / ".mac" / "openclaw" / "workspace").mkdir(parents=True)
    (tmp_path / ".hermes").mkdir(parents=True)
    (tmp_path / ".mac" / "openclaw" / "workspace" / "MEMORY.md").write_text(
        "# MEMORY\n- search state.db before asking the user to repeat context\n",
        encoding="utf-8",
    )
    (tmp_path / ".mac" / "openclaw" / "workspace" / "SOUL.md").write_text(
        "# SOUL\n", encoding="utf-8"
    )
    return tmp_path


def _state(home: Path) -> Path:
    return home / "port-state.json"


def _port(home: Path, source="openclaw", target="hermes", **kw):
    return port_profile(source, target, home=home, state_file=_state(home), dry_run=False, **kw)


def _readiness(home: Path, target="hermes", **kw):
    return switch_readiness(target, home=home, state_file=_state(home), **kw)


# --------------------------------------------------------------------------
# Never ported
# --------------------------------------------------------------------------


def test_a_host_that_never_ported_is_not_ready(home):
    readiness = _readiness(home)

    assert readiness["ready"] is False
    assert readiness["reason"] == "never_ported"


def test_the_refusal_names_the_command_that_fixes_it(home):
    """A gate an operator cannot act on gets bypassed, and a bypassed gate is
    worse than none: it costs a deploy and still loses the memory."""
    with pytest.raises(ProfilePortError) as excinfo:
        assert_switch_ported("hermes", home=home, state_file=_state(home))

    message = str(excinfo.value)
    assert "mac admin human-interface port --from openclaw --to hermes --apply" in message
    assert "has ever been ported" in message


def test_the_source_is_inferred_from_the_target(home):
    """The operator names where they are going; the other side is the source."""
    assert _readiness(home, target="hermes")["source"] == "openclaw"
    assert _readiness(home, target="openclaw")["source"] == "hermes"


def test_an_unknown_interface_is_refused(home):
    with pytest.raises(ProfilePortError):
        switch_readiness("slack", home=home, state_file=_state(home))


# --------------------------------------------------------------------------
# Ported, and then not
# --------------------------------------------------------------------------


def test_a_completed_port_makes_the_switch_ready(home):
    _port(home)

    assert _readiness(home)["ready"] is True
    assert assert_switch_ported("hermes", home=home, state_file=_state(home))["ready"]


def test_a_dry_run_does_not_satisfy_the_gate(home):
    """Reporting what a port WOULD do is not doing it.

    Porting is dry-run by default, so a gate that accepted a dry run would be
    satisfied by the safe, read-only invocation an operator reaches for first.
    """
    port_profile("openclaw", "hermes", home=home, state_file=_state(home), dry_run=True)

    assert _readiness(home)["reason"] == "never_ported"


def test_new_knowledge_in_the_source_makes_the_port_stale(home):
    """The condition a timestamp cannot see, and the one that loses memory.

    This is the live shape: OpenClaw kept learning for four months after the
    last time anything moved to Hermes.
    """
    _port(home)
    assert _readiness(home)["ready"] is True

    memory = home / ".mac" / "openclaw" / "workspace" / "MEMORY.md"
    memory.write_text(
        memory.read_text(encoding="utf-8") + "- all jkh projects live in ~/AgentFS/\n",
        encoding="utf-8",
    )

    readiness = _readiness(home)
    assert readiness["ready"] is False
    assert readiness["reason"] == "source_changed"


def test_re_porting_after_a_change_restores_readiness(home):
    """The gate has to be satisfiable, or it is just a wall."""
    _port(home)
    memory = home / ".mac" / "openclaw" / "workspace" / "MEMORY.md"
    memory.write_text(memory.read_text(encoding="utf-8") + "- more\n", encoding="utf-8")
    assert _readiness(home)["ready"] is False

    _port(home)

    assert _readiness(home)["ready"] is True


def test_an_idempotent_re_port_still_counts_as_a_port(home):
    """A port that changed nothing because everything was already in place has
    still run. Recording only file writes would make it look like it had not."""
    _port(home)
    second = _port(home)

    assert not second["ported"], "nothing should have changed on the second pass"
    assert _readiness(home)["ready"] is True


def test_a_port_in_the_other_direction_does_not_satisfy_this_one(home):
    """Direction matters: porting OUT of hermes does not populate hermes."""
    _port(home, source="openclaw", target="hermes")

    assert _readiness(home, target="openclaw")["ready"] is False


def test_staleness_is_reported_when_a_window_is_supplied(home):
    _port(home)

    fresh = _readiness(home, max_age_seconds=3600)
    assert fresh["ready"] is True
    assert fresh["age_seconds"] is not None

    assert _readiness(home, max_age_seconds=0)["reason"] == "stale"


def test_a_corrupt_state_file_reads_as_never_ported(home):
    """Fail closed. An unreadable record is not a record of success."""
    _port(home)
    _state(home).write_text("{not json", encoding="utf-8")

    assert _readiness(home)["reason"] == "never_ported"


def test_an_unclean_port_does_not_satisfy_the_gate(home):
    """Conflicts mean an operator still has candidate files to reconcile.

    The port preserves the destination and writes the candidate aside, so
    nothing is lost -- but the target is not yet whole, and switching onto it
    would present an agent with a half-reconciled profile.
    """
    _port(home)
    state = json.loads(_state(home).read_text(encoding="utf-8"))
    key = next(k for k in state if k.startswith("__port__:"))
    record = json.loads(state[key])
    record["clean"] = False
    state[key] = json.dumps(record, sort_keys=True)
    _state(home).write_text(json.dumps(state), encoding="utf-8")

    assert _readiness(home)["reason"] == "unclean"


# --------------------------------------------------------------------------
# Reachability: the capability existed and could not be invoked
# --------------------------------------------------------------------------


def test_the_cli_exposes_port_and_check():
    """The gap this closes. A library nothing can call is a library nothing
    does call: this module shipped complete and unused for four weeks."""
    from mac import cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else cli.parser()

    def registers(p, name, depth=0):
        """Search into `admin` too: the administrative commands moved there, so
        scanning only the top level reports them as unregistered when they are
        merely re-parented."""
        for action in p._actions:
            choices = getattr(action, "choices", None) or {}
            if name in choices:
                return True
            if depth < 1 and "admin" in choices:
                if registers(choices["admin"], name, depth + 1):
                    return True
        return False

    assert registers(parser, "human-interface"), (
        "no `mac admin human-interface` command is registered"
    )

    admin = next(
        action.choices["admin"]
        for action in parser._actions
        if "admin" in (getattr(action, "choices", None) or {})
    )
    sub = next(
        action.choices["human-interface"]
        for action in admin._actions
        if "human-interface" in (getattr(action, "choices", None) or {})
    )
    names = {name for action in sub._actions for name in (getattr(action, "choices", None) or {})}
    assert {"port", "check"} <= names


def test_the_installer_gates_a_switch():
    """The deploy path is the consumer that was missing.

    Asserted on the script text: reaching the gate for real needs a node
    install, and the property worth protecting is that the call site exists at
    all -- that is precisely what was absent.
    """
    root = Path(__file__).resolve().parents[1]
    installer = (root / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")

    assert "gate_human_interface_switch" in installer
    assert "assert_switch_ported" in installer
    # Only a CHANGE is gated; re-deploying the installed interface must not
    # demand a fresh port, or every routine deploy fails.
    assert '[ "$installed" != "$target" ] || return 0' in installer
