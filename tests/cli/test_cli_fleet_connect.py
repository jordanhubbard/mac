"""`mac admin fleet connect` -- the hub URL and its bearer token, together.

Both halves already existed: the URL in fleets.yaml, the token as
MAC_API_TOKEN__<FLEET>. Nothing put them side by side, so connecting to a hub
you had just built meant reading a tailscale address off one command and
grepping a token out of a file -- in front of an audience, during the demo this
command exists to serve.

This lives in tests/cli/ deliberately: the coverage gate discovers tested
subcommands by scanning THIS directory for `_run(...)` calls, so a CLI test
filed anywhere else leaves the subcommand looking untested.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main

TOKEN = "yLVmgAsecrettokenvaluedAfU"


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    """A registry and a deploy env file that are not the operator's own."""
    fleets = tmp_path / "fleets.yaml"
    fleets.write_text(
        "fleets:\n  hazel:\n    fleet_name: watership-down\n    hub_url: http://100.64.0.9:8789\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("# a comment\nexport MAC_API_TOKEN__HAZEL='%s'\n" % TOKEN, encoding="utf-8")
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(fleets))
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", str(env_file))
    monkeypatch.delenv("MAC_API_TOKEN__HAZEL", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    monkeypatch.delenv("MAC_FLEET", raising=False)
    return tmp_path


@pytest.fixture
def text_output():
    """tests/conftest.py forces JSON for the whole suite; these assertions are
    about the human layout, so opt back into text explicitly."""
    from mac import cli

    cli._set_output_json(False)
    yield
    cli._set_output_json(True)


def _run(*args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(list(args))
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def test_the_url_and_the_token_arrive_together(fleet_home):
    """The whole point: one command, both halves, ready to paste."""
    _rc, out = _run("--json", "admin", "fleet", "connect", "--show-token")
    doc = json.loads(out)

    assert doc["url"] == "http://100.64.0.9:8789"
    assert doc["token"] == TOKEN


def test_the_token_is_masked_unless_asked_for(fleet_home, text_output):
    """It carries full control-plane authority. A command that printed it
    unasked would leak it into scrollback, shared terminals and CI logs."""
    _rc, out = _run("admin", "fleet", "connect")

    assert TOKEN not in out
    assert "http://100.64.0.9:8789" in out
    assert "yLVmgA" in out and "dAfU" in out  # recognisable, not usable


def test_the_token_is_read_from_the_env_file_not_only_the_environment(fleet_home):
    """setup.sh WRITES the token without exporting it, so the shell you are in
    seconds after building a hub does not have it. That is precisely when this
    command is used."""
    _rc, out = _run("--json", "admin", "fleet", "connect", "--show-token")

    assert json.loads(out)["token"] == TOKEN


def test_a_fleet_can_be_named_by_its_human_label(fleet_home):
    """Fleets are keyed by hub-agent name but carry a separate fleet_name.
    An operator naturally passes either."""
    _rc, out = _run("--json", "--fleet", "watership-down", "admin", "fleet", "connect")

    assert json.loads(out)["url"] == "http://100.64.0.9:8789"


def test_an_unknown_fleet_names_the_ones_that_exist(fleet_home):
    """A bare 'no such fleet' sends the operator back to reading YAML."""
    with pytest.raises(SystemExit) as excinfo:
        _run("--fleet", "efrafa", "admin", "fleet", "connect")

    assert "hazel" in str(excinfo.value)


def test_json_omits_the_token_unless_show_token_is_given(fleet_home):
    _rc, out = _run("--json", "admin", "fleet", "connect")
    doc = json.loads(out)

    assert doc["token"] is None
    assert doc["token_present"] is True
    assert doc["token_var"] == "MAC_API_TOKEN__HAZEL"
