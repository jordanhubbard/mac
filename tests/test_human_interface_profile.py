"""Porting an agent profile between human interfaces must not lose knowledge.

Grounded in what was measured on the hub 2026-08-04, four weeks after the fleet
switched to OpenClaw:

* SOUL.md was byte-identical on both sides.
* MEMORY.md had DIVERGED AND BECOME DISJOINT -- OpenClaw's copy held April-July
  operational knowledge, Hermes' April copy held the record of the previous
  migration including its fix for Slack tokens not porting. Neither was a
  superset of the other.
* The Slack signing secret was absent from ~/.hermes/.env entirely, which is
  why the Hermes gateway started but could not connect.

The property under test is therefore not "the target matches the source" but
"no content is lost in either direction".
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


def test_openclaw_namespaced_tokens_translate_to_hermes_flat_keys(tmp_path):
    """The interfaces use different credential MODELS, not just different names.

    OpenClaw is multi-account and namespaced
    (MAC_OPENCLAW_SLACK_<ACCOUNT>_BOT_TOKEN); Hermes is single-account and flat
    (SLACK_BOT_TOKEN). The active account is chosen by
    MAC_OPENCLAW_SLACK_ACCOUNT_ID so a multi-account host ports the account it
    actually serves.
    """
    _seed(
        tmp_path,
        openclaw_env=(
            "MAC_OPENCLAW_SLACK_ACCOUNT_ID=omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN=xoxb-omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN=xapp-omgjkh\n"
            "MAC_OPENCLAW_SLACK_OFFTERA_BOT_TOKEN=xoxb-offtera\n"
        ),
        hermes_env="UNRELATED=keepme\n",
    )

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    ported = parse_env(hermes_layout(tmp_path).env_file)
    assert ported["SLACK_BOT_TOKEN"] == "xoxb-omgjkh"
    assert ported["SLACK_APP_TOKEN"] == "xapp-omgjkh"
    # The other account's token must NOT leak into a single-account gateway.
    assert "xoxb-offtera" not in ported.values()
    # Keys this port knows nothing about survive.
    assert ported["UNRELATED"] == "keepme"
    assert "SLACK_BOT_TOKEN" in report["credentials_ported"]


def test_the_signing_secret_is_reported_unavailable_not_missing(tmp_path):
    """OpenClaw has no signing secret to give, and saying "missing" would imply
    the port could have supplied it.

    OpenClaw connects over Socket Mode (app+bot tokens). Hermes' slack_bolt
    additionally verifies request signatures, so SLACK_SIGNING_SECRET must come
    from the hub vault, not from a port. This distinction is what stops an
    operator concluding the port failed when it did all it can.
    """
    _seed(
        tmp_path,
        openclaw_env=(
            "MAC_OPENCLAW_SLACK_ACCOUNT_ID=omgjkh\n"
            "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN=xoxb-omgjkh\n"
        ),
    )

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SLACK_SIGNING_SECRET" in report["credentials_unavailable"]
    assert "SLACK_SIGNING_SECRET" not in report["credentials_missing"]
    assert "SLACK_BOT_TOKEN" in report["credentials_ported"]


def test_hermes_flat_credentials_port_as_is(tmp_path):
    """The reverse direction needs no translation -- Hermes is already flat."""
    _seed(
        tmp_path,
        hermes_env=(
            "SLACK_BOT_TOKEN=xoxb-h\nSLACK_APP_TOKEN=xapp-h\n"
            "SLACK_SIGNING_SECRET=s3cr3t\n"
        ),
    )

    report = port_profile(HERMES, OPENCLAW, home=tmp_path, dry_run=False)

    assert set(report["credentials_ported"]) == {
        "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET",
    }
    assert not report["credentials_unavailable"]


def test_an_openclaw_account_with_no_tokens_reports_missing(tmp_path):
    _seed(tmp_path, openclaw_env="MAC_OPENCLAW_SLACK_ACCOUNT_ID=omgjkh\n")

    report = port_profile(OPENCLAW, HERMES, home=tmp_path, dry_run=False)

    assert "SLACK_BOT_TOKEN" in report["credentials_missing"]
    assert not report["credentials_ported"]


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
