"""Porting an agent profile between human interfaces must not lose knowledge.

Grounded in what was measured on the hub 2026-08-04, four weeks after the fleet
switched to OpenClaw:

* SOUL.md was byte-identical on both sides.
* MEMORY.md had DIVERGED AND BECOME DISJOINT -- OpenClaw's copy held April-July
  operational knowledge, Hermes' April copy held the record of the previous
  migration including its fix for Slack tokens not porting. Neither was a
  superset of the other.
* BOTH interfaces already carried BOTH Slack workspaces, `omgjkh` and
  `offtera` -- Hermes through ~/.hermes/slack_accounts.json (the multi-Slack
  patch), OpenClaw through namespaced env keys. An earlier reading of this
  module treated Hermes as single-account; that was wrong, and a port written
  to it would have dropped a workspace while reporting success.

The property under test is therefore not "the target matches the source" but
"no content is lost in either direction" -- for identity documents AND for
Slack workspaces.

Note the signing secret is NOT a gap: Socket Mode verifies no inbound request
signatures, and no ~/.hermes/.env backup has ever contained one.
"""
from __future__ import annotations

import json

import pytest

from mac.human_interface_profile import (
    HERMES,
    OPENCLAW,
    ProfilePortError,
    hermes_layout,
    openclaw_layout,
    parse_env,
    port_profile,
    upsert_env,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(home, *, hermes_memory=None, openclaw_memory=None, hermes_soul=None,
          openclaw_soul=None, hermes_env=None, openclaw_env=None):
    h, o = hermes_layout(home), openclaw_layout(home)
    if hermes_soul is not None:
        _write(h.identity_dir / "SOUL.md", hermes_soul)
    if openclaw_soul is not None:
        _write(o.identity_dir / "SOUL.md", openclaw_soul)
    if hermes_memory is not None:
        _write(h.identity_dir / "MEMORY.md", hermes_memory)
    if openclaw_memory is not None:
        _write(o.identity_dir / "MEMORY.md", openclaw_memory)
    if hermes_env is not None:
        _write(h.env_file, hermes_env)
    if openclaw_env is not None:
        _write(o.env_file, openclaw_env)
    return h, o


def test_disjoint_memory_is_never_overwritten(tmp_path):
    """The case that motivated this module.

    Each side holds knowledge the other lacks. Porting must preserve the
    destination and surface the incoming version, not clobber it.
    """
    openclaw_only = "AgentFS is canonical for all jkh-requested projects.\n"
    hermes_only = "Slack tokens do not port automatically; fix by hand.\n"
    _seed(tmp_path, openclaw_memory=openclaw_only, hermes_memory=hermes_only)

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert report["conflicts"], "divergent MEMORY.md must be reported, not merged silently"
    conflict = [c for c in report["conflicts"] if c["file"] == "MEMORY.md"][0]

    # Destination preserved.
    preserved = hermes_layout(tmp_path).identity_dir / "MEMORY.md"
    assert preserved.read_text(encoding="utf-8") == hermes_only
    # Incoming content is on disk for reconciliation -- nothing is lost.
    assert (tmp_path / conflict["candidate"]).exists() or __import__("pathlib").Path(
        conflict["candidate"]
    ).exists()
    incoming = __import__("pathlib").Path(conflict["candidate"]).read_text(encoding="utf-8")
    assert incoming == openclaw_only
    assert report["clean"] is False


def test_identical_identity_is_a_no_op(tmp_path):
    """SOUL.md was identical on both sides; that must not be reported as work."""
    soul = "You are Rocky.\n"
    _seed(tmp_path, hermes_soul=soul, openclaw_soul=soul)

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SOUL.md" in report["unchanged"]
    assert "SOUL.md" not in report["ported"]
    assert not [c for c in report["conflicts"] if c["file"] == "SOUL.md"]


def test_a_missing_target_file_is_ported(tmp_path):
    """The ordinary case: the target has never seen this document."""
    _seed(tmp_path, openclaw_soul="You are Rocky.\n")

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SOUL.md" in report["ported"]
    assert (hermes_layout(tmp_path).identity_dir / "SOUL.md").read_text(
        encoding="utf-8"
    ) == "You are Rocky.\n"


def test_dry_run_writes_nothing(tmp_path):
    _seed(tmp_path, openclaw_soul="You are Rocky.\n")

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=True)

    assert report["dry_run"] is True
    assert "SOUL.md" in report["ported"]
    assert not (hermes_layout(tmp_path).identity_dir / "SOUL.md").exists()


def test_the_source_is_never_modified(tmp_path):
    original = "You are Rocky.\n"
    _seed(tmp_path, openclaw_soul=original, hermes_soul="different\n")

    port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert openclaw_layout(tmp_path).identity_dir.joinpath("SOUL.md").read_text(
        encoding="utf-8"
    ) == original


def _hermes_accounts(home):
    path = hermes_layout(home).accounts_file
    return {a["name"]: a for a in json.loads(path.read_text(encoding="utf-8"))}


def test_every_openclaw_account_reaches_hermes(tmp_path):
    """BOTH interfaces are multi-account; a port must not collapse them.

    deploy/hermes/multi-slack-mvp.patch gives Hermes true multi-workspace
    Socket Mode via ~/.hermes/slack_accounts.json -- one AsyncApp and one
    websocket per account. Writing only the active account into the flat
    SLACK_BOT_TOKEN would silently drop every other workspace. Verified on the
    hub 2026-08-04: both sides carry `omgjkh` AND `offtera`.
    """
    _seed(
        tmp_path,
        openclaw_env=(
            "MAC_OPENCLAW_SLACK_ACCOUNT_ID=omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN=xoxb-omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN=xapp-omgjkh\n"
            "MAC_OPENCLAW_SLACK_OFFTERA_BOT_TOKEN=xoxb-offtera\n"
            "MAC_OPENCLAW_SLACK_OFFTERA_APP_TOKEN=xapp-offtera\n"
        ),
    )

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    accounts = _hermes_accounts(tmp_path)
    assert set(accounts) == {"omgjkh", "offtera"}, "a workspace was lost"
    assert accounts["offtera"]["bot_token"] == "xoxb-offtera"
    assert accounts["offtera"]["app_token"] == "xapp-offtera"
    assert set(report["accounts_ported"]) == {"omgjkh", "offtera"}


def test_an_account_only_the_target_knows_is_preserved(tmp_path):
    """The union property, stated directly.

    The source is authoritative for accounts it HAS -- it is the interface the
    agent used last. It is not authoritative for accounts it has never heard
    of, and porting must not delete those.
    """
    h = hermes_layout(tmp_path)
    h.accounts_file.parent.mkdir(parents=True, exist_ok=True)
    h.accounts_file.write_text(
        json.dumps([
            {"name": "omgjkh", "bot_token": "xoxb-old", "app_token": "xapp-old"},
            {"name": "legacy", "bot_token": "xoxb-legacy", "app_token": "xapp-legacy"},
        ]),
        encoding="utf-8",
    )
    _seed(
        tmp_path,
        openclaw_env=(
            "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN=xoxb-new\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN=xapp-new\n"
        ),
    )

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    accounts = _hermes_accounts(tmp_path)
    assert accounts["omgjkh"]["bot_token"] == "xoxb-new", "source wins where both know it"
    assert accounts["legacy"]["bot_token"] == "xoxb-legacy", "target-only survives"
    assert report["accounts_preserved"] == ["legacy"]


def test_hermes_accounts_port_back_to_openclaw_namespaced_keys(tmp_path):
    """The reverse direction, account for account."""
    h = hermes_layout(tmp_path)
    h.accounts_file.parent.mkdir(parents=True, exist_ok=True)
    h.accounts_file.write_text(
        json.dumps([
            {"name": "omgjkh", "bot_token": "xoxb-1", "app_token": "xapp-1"},
            {"name": "offtera", "bot_token": "xoxb-2", "app_token": "xapp-2"},
        ]),
        encoding="utf-8",
    )

    report = port_profile(HERMES, OPENCLAW, home=tmp_path, dry_run=False)

    env = parse_env(openclaw_layout(tmp_path).env_file)
    assert env["MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN"] == "xoxb-1"
    assert env["MAC_OPENCLAW_SLACK_OFFTERA_APP_TOKEN"] == "xapp-2"
    assert env["MAC_OPENCLAW_SLACK_ACCOUNT_ID"] == "omgjkh"
    assert set(report["accounts_ported"]) == {"omgjkh", "offtera"}


def test_flat_hermes_env_is_read_when_the_accounts_file_is_absent(tmp_path):
    """The patch keeps the flat env as a single-account fallback."""
    _seed(tmp_path, hermes_env="SLACK_BOT_TOKEN=xoxb-h\nSLACK_APP_TOKEN=xapp-h\n")

    report = port_profile(HERMES, OPENCLAW, home=tmp_path, dry_run=False)

    env = parse_env(openclaw_layout(tmp_path).env_file)
    assert env["MAC_OPENCLAW_SLACK_DEFAULT_BOT_TOKEN"] == "xoxb-h"
    assert report["accounts_ported"] == ["default"]


def test_no_signing_secret_is_required_for_socket_mode(tmp_path):
    """Socket Mode carries no inbound HTTP request, so there is nothing to
    verify -- the absence of a signing secret is not a gap in the port.

    Confirmed on the hub: SLACK_SIGNING_SECRET appears in no ~/.hermes/.env
    backup going back to 2026-05-13, and Hermes served both workspaces anyway.
    """
    _seed(
        tmp_path,
        openclaw_env=(
            "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN=xoxb-omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN=xapp-omgjkh\n"
        ),
    )

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SLACK_SIGNING_SECRET" in report["credentials_not_required"]
    assert "SLACK_SIGNING_SECRET" not in report["credentials_missing"]
    assert report["accounts_ported"] == ["omgjkh"]


def test_an_account_missing_a_token_is_reported_not_silently_dropped(tmp_path):
    """The gateway skips such accounts. If the port dropped them quietly, a
    partial port would read as a complete one."""
    _seed(
        tmp_path,
        openclaw_env=(
            "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN=xoxb-omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN=xapp-omgjkh\n"
            "MAC_OPENCLAW_SLACK_HALFWAY_BOT_TOKEN=xoxb-halfway\n"
        ),
    )

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "halfway" in report["accounts_incomplete"]
    assert "halfway" not in report["accounts_ported"]
    assert set(_hermes_accounts(tmp_path)) == {"omgjkh"}


def test_an_openclaw_account_with_no_tokens_reports_missing(tmp_path):
    _seed(tmp_path, openclaw_env="MAC_OPENCLAW_SLACK_ACCOUNT_ID=omgjkh\n")

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SLACK_BOT_TOKEN" in report["credentials_missing"]
    assert not report["accounts_ported"]


def test_porting_is_idempotent(tmp_path):
    _seed(tmp_path, openclaw_soul="You are Rocky.\n")

    first = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)
    second = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SOUL.md" in first["ported"]
    assert "SOUL.md" in second["unchanged"]
    assert not second["conflicts"]


def test_a_re_port_of_managed_content_is_not_a_conflict(tmp_path):
    """Content this port previously wrote may be updated, not flagged."""
    _seed(tmp_path, openclaw_memory="v1\n")
    port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    _write(openclaw_layout(tmp_path).identity_dir / "MEMORY.md", "v2\n")
    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "MEMORY.md" in report["ported"]
    assert not report["conflicts"]
    assert hermes_layout(tmp_path).identity_dir.joinpath("MEMORY.md").read_text(
        encoding="utf-8"
    ) == "v2\n"


def test_both_directions_are_supported(tmp_path):
    """The reverse direction is the one that did not exist before."""
    _seed(tmp_path, hermes_soul="from hermes\n")
    report = port_profile(HERMES, OPENCLAW, home=tmp_path, dry_run=False)
    assert "SOUL.md" in report["ported"]
    assert openclaw_layout(tmp_path).identity_dir.joinpath("SOUL.md").read_text(
        encoding="utf-8"
    ) == "from hermes\n"


def test_porting_to_the_same_interface_is_refused(tmp_path):
    with pytest.raises(ProfilePortError):
        port_profile(HERMES, HERMES, home=tmp_path)


def test_an_unknown_interface_is_refused(tmp_path):
    with pytest.raises(ProfilePortError):
        port_profile("nemoclaw", HERMES, home=tmp_path)


def test_upsert_env_preserves_comments_and_unknown_keys(tmp_path):
    path = tmp_path / ".env"
    _write(path, "# a comment\nKEEP=1\nSLACK_BOT_TOKEN=old\n")

    upsert_env(path, {"SLACK_BOT_TOKEN": "new", "SLACK_SIGNING_SECRET": "s"})

    text = path.read_text(encoding="utf-8")
    assert "# a comment" in text
    assert "KEEP=1" in text
    values = parse_env(path)
    assert values["SLACK_BOT_TOKEN"] == "new"
    assert values["SLACK_SIGNING_SECRET"] == "s"


# ---------------------------------------------------------------------------
# A file without a trailing newline could never match itself.
#
# The port appends "\n" to the SOURCE when it lacks one, then compared that
# against the RAW destination. On the hub both MEMORY.md files ended in "." --
# byte-for-byte identical, reported as a conflict on every run, forever. The
# port could not converge, so the interface switch it gates could never be
# cleared. It looked like a corrupt profile and was a missing newline.
# ---------------------------------------------------------------------------


def _profile(home, interface, files):
    from mac.human_interface_profile import layout_for

    layout = layout_for(interface, home)
    layout.identity_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (layout.identity_dir / name).write_text(body, encoding="utf-8")
    return layout


def test_identical_files_without_a_trailing_newline_are_not_a_conflict(tmp_path):
    from mac.human_interface_profile import port_profile

    body = "one fact\ntwo facts, no final newline."
    _profile(tmp_path, "openclaw", {"MEMORY.md": body})
    _profile(tmp_path, "hermes", {"MEMORY.md": body})

    result = port_profile("openclaw", "hermes", home=tmp_path, dry_run=True)

    assert "MEMORY.md" in result["unchanged"]
    assert [c["file"] for c in result["conflicts"]] == []


def test_a_genuine_difference_is_still_a_conflict(tmp_path):
    """The fix must not make the guard permissive: the whole point is that
    unmanaged target content is preserved rather than overwritten."""
    from mac.human_interface_profile import port_profile

    _profile(tmp_path, "openclaw", {"MEMORY.md": "source knowledge"})
    _profile(tmp_path, "hermes", {"MEMORY.md": "different target knowledge"})

    result = port_profile("openclaw", "hermes", home=tmp_path, dry_run=True)

    assert [c["file"] for c in result["conflicts"]] == ["MEMORY.md"]


# ---------------------------------------------------------------------------
# Coverage: what a switch would actually carry.
# ---------------------------------------------------------------------------


def test_coverage_names_every_artefact(tmp_path):
    from mac.human_interface_profile import coverage_report

    _profile(tmp_path, "openclaw", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"})
    _profile(tmp_path, "hermes", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"})

    report = coverage_report("openclaw", "hermes", home=tmp_path)

    named = {item["artefact"] for item in report["items"]}
    assert {"SOUL.md", "USER.md", "MEMORY.md"} <= named
    # Telegram was absent from the port entirely, so a switch moved Slack and
    # dropped Telegram while reporting success.
    assert "telegram accounts" in named
    assert "slack accounts" in named


def test_coverage_reports_solid_when_everything_matches(tmp_path):
    from mac.human_interface_profile import coverage_report

    files = {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"}
    _profile(tmp_path, "openclaw", files)
    _profile(tmp_path, "hermes", files)

    report = coverage_report("openclaw", "hermes", home=tmp_path)

    assert report["solid"] is True
    assert report["unresolved"] == []


def test_an_artefact_missing_at_the_target_is_named_not_omitted(tmp_path):
    """A silent omission reads as "nothing to do", which is how an artefact
    goes missing while the port reports success."""
    from mac.human_interface_profile import coverage_report

    _profile(tmp_path, "openclaw", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"})
    _profile(tmp_path, "hermes", {"SOUL.md": "s"})

    report = coverage_report("openclaw", "hermes", home=tmp_path)

    assert report["solid"] is False
    assert "MEMORY.md" in report["unresolved"]
    states = {i["artefact"]: i["state"] for i in report["items"]}
    assert states["MEMORY.md"] == "missing_at_target"


def test_a_differing_artefact_is_not_reported_as_carried(tmp_path):
    from mac.human_interface_profile import coverage_report

    _profile(tmp_path, "openclaw", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "new"})
    _profile(tmp_path, "hermes", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "old"})

    report = coverage_report("openclaw", "hermes", home=tmp_path)

    assert report["solid"] is False
    assert "MEMORY.md" in report["unresolved"]
