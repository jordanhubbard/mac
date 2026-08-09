"""Unit tests for the ``hgx`` provider adapter (``mac.hgx_provider``).

These cover the adapter's hard requirements:

- create (including the explicit ``standard-dind`` path),
- list/status parsing into secret-free dataclasses,
- current global-JSON/type argv and wrapped status payloads,
- real SSH command execution and nonce attestation,
- immutable-ID-only selection for lifecycle verbs,
- display-name resolution that refuses zero/multiple matches,
- that ``hgx info`` is never invoked, and
- that credential-bearing provider fields are scrubbed, never surfaced.

No real ``hgx`` binary is required; ``subprocess.run`` is stubbed so each verb's
argv and output are controlled and asserted.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from mac.fleet_deploy import SshTarget
from mac.hgx_provider import (
    HGX_PROVIDER_SCHEMA,
    STANDARD_DIND_FLAVOR,
    HgxAmbiguousSessionError,
    HgxCommandError,
    HgxError,
    HgxProvider,
    HgxSession,
    HgxSshEndpoint,
    _parse_ssh_from_text,
)


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _install_run(monkeypatch, handler):
    """Replace subprocess.run in the module with ``handler`` and record calls."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return handler(list(argv), kwargs)

    monkeypatch.setattr("mac.hgx_provider.subprocess.run", fake_run)
    return calls


# -- create --------------------------------------------------------------
def test_create_returns_structured_session(monkeypatch):
    payload = {
        "id": "sess-abc123",
        "display_name": "worker-1",
        "agent_type": "standard",
        "status": "running",
        "ssh": "ubuntu@10.0.0.5",
        "port": 2201,
    }
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    provider = HgxProvider()
    session = provider.create(flavor="standard", name="worker-1")

    assert isinstance(session, HgxSession)
    assert session.session_id == "sess-abc123"
    assert session.name == "worker-1"
    assert session.flavor == "standard"
    assert session.state == "running"
    assert isinstance(session.ssh, HgxSshEndpoint)
    assert session.ssh.target == SshTarget(user_host="ubuntu@10.0.0.5", port=2201)
    # hgx 0.9 expects the global JSON flag before the verb and --type.
    assert calls[0]["argv"] == [
        "admin", "hgx",
        "--json",
        "create",
        "--type",
        "standard",
        "--name",
        "worker-1",
    ]


def test_create_standard_dind_uses_named_flavor(monkeypatch):
    payload = {
        "id": "sess-dind",
        "agent_type": STANDARD_DIND_FLAVOR,
        "status": "running",
    }
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().create_standard_dind(name="dind-box")

    assert session.flavor == STANDARD_DIND_FLAVOR
    assert session.is_dind is True
    assert STANDARD_DIND_FLAVOR in calls[0]["argv"]


def test_create_standard_dind_rejects_wrong_flavor(monkeypatch):
    payload = {"id": "sess-x", "agent_type": "standard", "status": "running"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    with pytest.raises(HgxError):
        HgxProvider().create_standard_dind()


def test_create_extra_args_are_forwarded(monkeypatch):
    payload = {"id": "sess-e", "agent_type": STANDARD_DIND_FLAVOR}
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    HgxProvider().create(flavor=STANDARD_DIND_FLAVOR, extra_args=["--region", "us"])
    assert calls[0]["argv"][-2:] == ["--region", "us"]


def test_create_without_json_object_raises(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted("not json"))
    with pytest.raises(HgxError):
        HgxProvider().create(flavor="standard")


# -- list / status parsing ----------------------------------------------
def test_list_parses_multiple_shapes(monkeypatch):
    payload = {
        "sessions": [
            {"id": "s1", "name": "a", "flavor": "standard", "state": "running"},
            {"id": "s2", "name": "b", "flavor": STANDARD_DIND_FLAVOR, "state": "stopped"},
        ]
    }
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    sessions = HgxProvider().list()
    assert [s.session_id for s in sessions] == ["s1", "s2"]
    assert sessions[1].is_dind is True


def test_list_top_level_array(monkeypatch):
    payload = [{"id": "s1"}, {"id": "s2"}, "junk"]
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    sessions = HgxProvider().list()
    assert [s.session_id for s in sessions] == ["s1", "s2"]
    assert calls[0]["argv"] == ["admin", "hgx", "--json", "list"]


def test_list_non_json_yields_empty(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted("table-ish text"))
    assert HgxProvider().list() == []


def test_status_addresses_by_immutable_id(monkeypatch):
    payload = {
        "session": {
            "id": "sess-9",
            "display_name": "n",
            "agent_type": "standard-dind",
            "status": "running",
        },
        "restore": {},
        "events": {},
    }
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("sess-9")
    assert session.session_id == "sess-9"
    assert session.name == "n"
    assert session.is_dind is True
    assert calls[0]["argv"] == ["admin", "hgx", "--json", "status", "sess-9"]


def test_status_missing_session_raises(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().status("ghost")


def test_status_id_mismatch_raises(monkeypatch):
    payload = {"id": "other", "state": "running"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().status("requested")


def test_status_requires_nonempty_id(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted("{}"))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().status("   ")


def test_payload_without_id_raises(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps({"name": "x"})))
    with pytest.raises(HgxError):
        HgxProvider().create(flavor="standard")


# -- SSH endpoint compatibility + real reachability proof ----------------
def test_ssh_target_uses_structured_status_without_print_probe(monkeypatch):
    payload = {
        "session": {
            "id": "sess-1",
            "ssh": "ubuntu@203.0.113.7:2222",
        }
    }
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    endpoint = HgxProvider().ssh_target("sess-1")
    assert endpoint.user_host == "ubuntu@203.0.113.7"
    assert endpoint.port == 2222
    assert calls[0]["argv"] == ["admin", "hgx", "--json", "status", "sess-1"]


def test_legacy_ssh_invocation_output_remains_parseable():
    endpoint = _parse_ssh_from_text("Run: ssh -p 2201 root@198.51.100.9\n")
    assert endpoint is not None
    assert endpoint.user_host == "root@198.51.100.9"
    assert endpoint.port == 2201


def test_ssh_target_unparseable_raises(monkeypatch):
    _install_run(
        monkeypatch,
        lambda argv, kw: _FakeCompleted(json.dumps({"session": {"id": "sess-1"}})),
    )
    with pytest.raises(HgxError):
        HgxProvider().ssh_target("sess-1")


def test_run_ssh_command_uses_current_noninteractive_syntax(monkeypatch):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted("remote-output"))
    output = HgxProvider().run_ssh_command("sess-1", ["uname", "-a"])

    assert output == "remote-output"
    assert calls[0]["argv"] == ["admin", "hgx", "ssh", "sess-1", "--", "uname", "-a"]


@pytest.mark.parametrize("command", [[], "", [""]])
def test_run_ssh_command_requires_nonempty_argv(monkeypatch, command):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    with pytest.raises(HgxError):
        HgxProvider().run_ssh_command("sess-1", command)
    assert calls == []


def test_attest_ssh_requires_remote_nonce_echo(monkeypatch):
    monkeypatch.setattr("mac.hgx_provider.secrets.token_hex", lambda size: "a1b2")
    calls = _install_run(
        monkeypatch,
        lambda argv, kw: _FakeCompleted(
            "Connecting to sess-1...\nmac-hgx-ssh-attest-a1b2"
        ),
    )

    assert HgxProvider().attest_ssh("sess-1") == "sess-1"
    assert calls[0]["argv"] == [
        "admin", "hgx",
        "ssh",
        "sess-1",
        "--",
        "printf",
        "mac-hgx-ssh-attest-a1b2",
    ]


def test_attest_ssh_fails_when_remote_nonce_is_missing(monkeypatch):
    monkeypatch.setattr("mac.hgx_provider.secrets.token_hex", lambda size: "a1b2")
    _install_run(
        monkeypatch,
        lambda argv, kw: _FakeCompleted("Connecting to sess-1...\n"),
    )
    with pytest.raises(HgxError, match="attestation marker"):
        HgxProvider().attest_ssh("sess-1")


def test_ssh_from_separate_host_user_fields(monkeypatch):
    payload = {"id": "s1", "user": "dev", "hostname": "host.internal", "ssh_port": "2020"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("s1")
    assert session.ssh.user_host == "dev@host.internal"
    assert session.ssh.port == 2020


# -- lifecycle: stop / resume / delete by id ----------------------------
def test_stop_resume_and_delete_use_immutable_id(monkeypatch):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    provider = HgxProvider()
    assert provider.stop("sess-7") == "sess-7"
    assert provider.resume("sess-7") == "sess-7"
    assert provider.delete("sess-7") == "sess-7"
    assert calls[0]["argv"] == ["admin", "hgx", "stop", "sess-7"]
    assert calls[1]["argv"] == ["admin", "hgx", "resume", "sess-7"]
    assert calls[2]["argv"] == ["admin", "hgx", "delete", "sess-7"]


def test_stop_requires_id(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().stop("")


def test_delete_requires_id(monkeypatch):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().delete("")
    assert calls == []


# -- name -> immutable id resolver --------------------------------------
def _list_payload(rows):
    return json.dumps({"sessions": rows})


def test_resolve_name_to_unique_id(monkeypatch):
    rows = [{"id": "s1", "name": "alpha"}, {"id": "s2", "name": "beta"}]
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(_list_payload(rows)))
    assert HgxProvider().resolve_session_id("alpha") == "s1"


def test_resolve_name_zero_matches_raises(monkeypatch):
    rows = [{"id": "s1", "name": "alpha"}]
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(_list_payload(rows)))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().resolve_session_id("missing")


def test_resolve_name_multiple_matches_refused(monkeypatch):
    rows = [{"id": "s1", "name": "dup"}, {"id": "s2", "name": "dup"}]
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(_list_payload(rows)))
    with pytest.raises(HgxAmbiguousSessionError) as excinfo:
        HgxProvider().resolve_session_id("dup")
    assert set(excinfo.value.session_ids) == {"s1", "s2"}


def test_resolve_blank_name_raises(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(_list_payload([])))
    from mac.hgx_provider import HgxSessionNotFoundError

    with pytest.raises(HgxSessionNotFoundError):
        HgxProvider().resolve_session_id("  ")


def test_resolve_exact_id_when_no_name_match(monkeypatch):
    rows = [{"id": "sess-xyz", "name": "alpha"}]
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(_list_payload(rows)))
    assert HgxProvider().resolve_session_id("sess-xyz") == "sess-xyz"


# -- hgx info is banned --------------------------------------------------
def test_info_verb_is_refused(monkeypatch):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    with pytest.raises(HgxError):
        HgxProvider()._run(["info", "sess-1"])
    assert calls == []


def test_global_json_cannot_bypass_info_ban(monkeypatch):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    with pytest.raises(HgxError):
        HgxProvider()._run(["--json", "info", "sess-1"])
    assert calls == []


def test_no_verb_ever_calls_info(monkeypatch):
    seen_argv = []

    def handler(argv, kw):
        seen_argv.append(argv)
        if "create" in argv or "status" in argv:
            return _FakeCompleted(json.dumps({"id": "s1", "ssh": "u@h", "port": 22}))
        if "list" in argv:
            return _FakeCompleted(_list_payload([{"id": "s1", "name": "n"}]))
        return _FakeCompleted("")

    _install_run(monkeypatch, handler)
    provider = HgxProvider()
    provider.create(flavor="standard")
    provider.list()
    provider.status("s1")
    provider.stop("s1")
    provider.resume("s1")
    provider.delete("s1")
    for argv in seen_argv:
        assert "info" not in argv


# -- secret scrubbing ----------------------------------------------------
def test_secret_fields_are_scrubbed_not_surfaced(monkeypatch):
    payload = {
        "id": "sess-secret",
        "name": "n",
        "state": "running",
        "fallback_password": "hunter2",
        "root_password": "p@ss",
        "api_key": "sk-live-123",
    }
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("sess-secret")

    observable = session.observable()
    flat = json.dumps(observable)
    assert "hunter2" not in flat
    assert "p@ss" not in flat
    assert "sk-live-123" not in flat
    assert set(session.scrubbed_fields) == {"fallback_password", "root_password", "api_key"}
    assert observable["credential_present"] is True
    assert observable["schema"] == HGX_PROVIDER_SCHEMA


def test_credential_env_var_recorded_by_name_only(monkeypatch):
    payload = {"id": "s1", "state": "running"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    provider = HgxProvider(credential_env_var="HGX_BOOTSTRAP_PASSWORD")
    session = provider.status("s1")
    obs = session.observable()
    assert obs["credential_env_var"] == "HGX_BOOTSTRAP_PASSWORD"
    assert obs["credential_present"] is True
    # The env var value never appears — only its name.
    assert "credential_present" in obs and obs["credential_present"] is True


def test_observable_ssh_is_secret_free(monkeypatch):
    payload = {"id": "s1", "ssh": "ubuntu@10.0.0.1", "port": 2201}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("s1")
    assert session.observable()["ssh"] == {"user_host": "ubuntu@10.0.0.1", "port": 2201}


# -- subprocess error translation ---------------------------------------
def test_nonzero_exit_raises_command_error(monkeypatch):
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted("", "boom", returncode=1))
    with pytest.raises(HgxCommandError) as excinfo:
        HgxProvider().list()
    assert excinfo.value.returncode == 1
    assert excinfo.value.stderr == "boom"


def test_missing_binary_raises_command_error(monkeypatch):
    def handler(argv, kw):
        raise FileNotFoundError("no hgx")

    _install_run(monkeypatch, handler)
    with pytest.raises(HgxCommandError):
        HgxProvider(binary="hgx-missing").list()


def test_timeout_raises_command_error(monkeypatch):
    def handler(argv, kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _install_run(monkeypatch, handler)
    with pytest.raises(HgxCommandError):
        HgxProvider(timeout=1).list()


def test_env_is_passed_through(monkeypatch):
    calls = _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(""))
    HgxProvider(env={"HGX_TOKEN_ENV": "x"}).stop("s1")
    assert calls[0]["kwargs"]["env"] == {"HGX_TOKEN_ENV": "x"}


def test_command_error_carries_argv():
    err = HgxCommandError("bad", argv=["admin", "hgx", "list"], returncode=2, stderr="e")
    assert err.argv == ["admin", "hgx", "list"]
    assert err.returncode == 2


def test_ambiguous_error_message_lists_ids():
    err = HgxAmbiguousSessionError("dup", ["b", "a"])
    assert "dup" in str(err)
    assert err.session_ids == ["b", "a"]


# -- misc coercion edges -------------------------------------------------
def test_numeric_id_is_stringified(monkeypatch):
    payload = {"id": 12345, "state": "running"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("12345")
    assert session.session_id == "12345"


def test_bool_port_is_ignored(monkeypatch):
    payload = {"id": "s1", "ssh": "u@h", "port": True}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("s1")
    assert session.ssh.port is None


def test_endpoint_properties_and_is_dind():
    from mac.fleet_deploy import parse_ssh_target

    endpoint = HgxSshEndpoint(target=parse_ssh_target("u@h", port=22), raw="u@h")
    assert endpoint.user_host == "u@h"
    assert endpoint.port == 22
    session = HgxSession(session_id="s", flavor=STANDARD_DIND_FLAVOR)
    assert session.is_dind is True


# -- targeted parser-edge coverage --------------------------------------
def test_ssh_invocation_with_non_p_flag_still_finds_host(monkeypatch):
    # A leading -i keyfile option is skipped; host is still recovered.
    endpoint = _parse_ssh_from_text(
        "connect via: ssh -i key.pem -p 2202 admin@192.0.2.5\n"
    )
    assert endpoint is not None
    assert endpoint.user_host == "admin@192.0.2.5"
    assert endpoint.port == 2202


def test_ssh_invocation_without_host_is_not_parsed():
    # "ssh -v" has only a flag and no host token -> _endpoint_from_ssh_args None.
    assert _parse_ssh_from_text("ssh -v\n") is None


def test_labelled_ssh_with_invalid_value_is_not_parsed():
    assert _parse_ssh_from_text("ssh endpoint: host:0\n") is None


def test_status_raw_ssh_invalid_but_host_field_used(monkeypatch):
    # raw "ssh" value is empty-ish/invalid so the host/user fallback is taken.
    payload = {"id": "s1", "ssh": " ", "hostname": "box.internal", "user": "dev"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("s1")
    assert session.ssh.user_host == "dev@box.internal"


def test_status_with_no_ssh_information(monkeypatch):
    payload = {"id": "s1", "state": "running"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("s1")
    assert session.ssh is None
    assert session.observable()["ssh"] is None


def test_single_object_status_payload(monkeypatch):
    # status returns a lone object (no wrapper key) -> _iter returns [payload].
    payload = {"id": "solo", "state": "running"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    assert HgxProvider().status("solo").session_id == "solo"


def test_wrapper_object_without_list_key_is_single(monkeypatch):
    # A mapping whose "sessions" is not a list falls through to single-object.
    payload = {"id": "wrap", "sessions": "oops"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    sessions = HgxProvider().list()
    assert [s.session_id for s in sessions] == ["wrap"]


def test_status_raw_ssh_invalid_nonblank_uses_host_fallback(monkeypatch):
    # raw ssh is non-blank but invalid (bad port) -> endpoint None, host used.
    payload = {"id": "s1", "ssh": "host:0", "hostname": "box.internal", "user": "dev"}
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps(payload)))
    session = HgxProvider().status("s1")
    assert session.ssh.user_host == "dev@box.internal"


def test_list_scalar_json_yields_empty(monkeypatch):
    # A bare JSON scalar (not list/object) is not session data -> [].
    _install_run(monkeypatch, lambda argv, kw: _FakeCompleted(json.dumps("just-a-string")))
    assert HgxProvider().list() == []
