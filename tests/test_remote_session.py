"""Unit tests for the secret-free remote-execution interface.

These cover the module's hard requirements:

- ``run`` never shell-joins its argv (each token is quoted, not interpolated),
- every operation is bounded and its timeout is propagated to ``subprocess``,
- attestation fails closed on a missing or partial marker,
- ``repr``/``to_dict`` are secret-free (identity file/ref never surface),
- the failure-class taxonomy maps SSH failures deterministically, and
- telemetry counters are injectable and record operations/timeouts/failures.

No real ``ssh``/``scp`` binary is required; ``subprocess.run`` in the module is
stubbed so each call's argv, stdin, and timeout are controlled and asserted.
"""

from __future__ import annotations

import subprocess

import pytest

from mac.fleet_ssh import FleetSshSpec
from mac.remote_session import (
    DEFAULT_OPERATION_TIMEOUT,
    REMOTE_SESSION_SCHEMA,
    Capability,
    RemoteAuthError,
    RemoteCommandError,
    RemoteConnectTimeout,
    RemoteEndpoint,
    RemoteError,
    RemoteHostKeyError,
    RemoteOperationTimeout,
    RemoteResult,
    RemoteTelemetry,
    RemoteTransport,
    RemoteTransportDead,
    SshTransport,
)


class _FakeCompleted:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_spec(**overrides) -> FleetSshSpec:
    base = dict(
        fleet="rocky",
        fleet_name="mac",
        agent="worker",
        target="ops@worker.example",
        port=2201,
        proxy_jump=None,
        identity_file="/home/op/.ssh/fleet-key",
        identity_ref="file:/home/op/.ssh/fleet-key",
        known_hosts_file="/home/op/.ssh/fleet-known-hosts",
        host_key_policy="strict",
        host_key_fingerprint=None,
        host_ca=None,
        supervisor="systemd",
        os_kind="linux",
        control_port=8789,
    )
    base.update(overrides)
    return FleetSshSpec(**base)


def _install_run(monkeypatch, handler):
    """Replace subprocess.run in the module and record each call."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return handler(list(argv), kwargs)

    monkeypatch.setattr("mac.remote_session.subprocess.run", fake_run)
    return calls


def _transport(monkeypatch, handler, **kwargs):
    calls = _install_run(monkeypatch, handler)
    telemetry = RemoteTelemetry()
    transport = SshTransport(_make_spec(), telemetry=telemetry, **kwargs)
    return transport, calls, telemetry


# -- endpoint / capabilities --------------------------------------------------
def test_endpoint_is_capability_feature_tested():
    endpoint = RemoteEndpoint.for_ssh(_make_spec())
    assert endpoint.logical_identity == "worker"
    assert endpoint.address == "ops@worker.example:2201"
    assert endpoint.transport == "ssh"
    assert endpoint.provider == "ssh"
    assert endpoint.has(Capability.ARGV_EXEC)
    assert endpoint.has(Capability.FILE_PUT)
    assert endpoint.has(Capability.FILE_GET)
    # Feature-test, not provider branching.
    assert Capability.MULTIPLEX not in endpoint.capabilities
    assert endpoint.identity == "ops@worker.example:2201"


def test_transport_conforms_to_protocol(monkeypatch):
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    assert isinstance(transport, RemoteTransport)


# -- argv is never shell-joined ----------------------------------------------
def test_run_never_shell_joins_argv(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.run(["ls", "-la", "a b; rm -rf /"])

    argv = calls[0]["argv"]
    # The transport shells out to the ssh binary with a fixed route, never a
    # shell wrapper (no "sh -c" / "bash -c" prefix on the local side).
    assert argv[0] == "ssh"
    assert "sh" not in argv[:1]
    remote_command = argv[-1]
    # The dangerous token is quoted as a single argument, not interpolated.
    assert "'a b; rm -rf /'" in remote_command
    # There is exactly one remote-command element; argv was not flattened into
    # separate positional shell words.
    assert remote_command.startswith("ls -la ")


def test_run_rejects_string_argv(monkeypatch):
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    with pytest.raises(TypeError):
        transport.run("ls -la")


def test_run_rejects_empty_argv(monkeypatch):
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    with pytest.raises(ValueError):
        transport.run([])


def test_run_shell_is_the_explicit_escape_hatch(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    script = "if [ -f /etc/hosts ]; then cat /etc/hosts; else exit 7; fi"
    transport.run_shell(script)
    # The script is passed verbatim as the remote command, unlike run().
    assert calls[0]["argv"][-1] == script


# -- timeouts are always bounded + propagated --------------------------------
def test_default_timeout_is_bounded_and_propagated(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.run(["true"])
    assert calls[0]["kwargs"]["timeout"] == DEFAULT_OPERATION_TIMEOUT
    assert calls[0]["kwargs"]["timeout"] is not None


def test_explicit_timeout_is_propagated(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.run(["true"], timeout=5.0)
    assert calls[0]["kwargs"]["timeout"] == 5.0


def test_non_positive_timeout_is_refused(monkeypatch):
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    with pytest.raises(ValueError):
        transport.run(["true"], timeout=0)
    with pytest.raises(ValueError):
        transport.run(["true"], timeout=-1)


def test_put_and_get_are_bounded(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.put("/tmp/local", "/tmp/remote", timeout=9.0)
    transport.get("/tmp/remote", "/tmp/local")
    assert calls[0]["argv"][0] == "scp"
    assert calls[0]["kwargs"]["timeout"] == 9.0
    assert calls[1]["kwargs"]["timeout"] == DEFAULT_OPERATION_TIMEOUT


def test_scp_remote_paths_are_quoted(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.put("/tmp/local", "/tmp/a b; touch /tmp/pwned")
    transport.get("/tmp/a b; touch /tmp/pwned", "/tmp/local")
    assert calls[0]["argv"][-1] == ("ops@worker.example:'/tmp/a b; touch /tmp/pwned'")
    assert calls[1]["argv"][-2] == ("ops@worker.example:'/tmp/a b; touch /tmp/pwned'")


def test_operation_timeout_maps_to_typed_error(monkeypatch):
    def handler(argv, kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))

    transport, _, telemetry = _transport(monkeypatch, handler)
    with pytest.raises(RemoteOperationTimeout) as excinfo:
        transport.run(["sleep", "999"])
    assert excinfo.value.failure_class == "operation_timeout"
    assert telemetry.timeouts == 1
    assert telemetry.failure_classes["operation_timeout"] == 1


# -- attestation fails closed -------------------------------------------------
def test_attest_returns_identity_on_exact_marker(monkeypatch):
    def handler(argv, kw):
        marker = argv[-1].split()[-1]
        return _FakeCompleted(stdout=(marker + "\n").encode())

    transport, _, _ = _transport(monkeypatch, handler)
    assert transport.attest() == transport.endpoint.identity


def test_attest_fails_closed_on_missing_marker(monkeypatch):
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted(stdout=b""))
    with pytest.raises(RemoteError):
        transport.attest()


def test_attest_fails_closed_on_partial_marker(monkeypatch):
    def handler(argv, kw):
        marker = argv[-1].split()[-1]
        return _FakeCompleted(stdout=marker[:-4].encode())

    transport, _, _ = _transport(monkeypatch, handler)
    with pytest.raises(RemoteError):
        transport.attest()


# -- secret-free repr / to_dict ----------------------------------------------
def test_endpoint_to_dict_is_secret_free():
    endpoint = RemoteEndpoint.for_ssh(_make_spec())
    payload = endpoint.to_dict()
    text = repr(payload)
    assert payload["schema"] == REMOTE_SESSION_SCHEMA
    assert "identity_file" not in payload["spec"]
    assert "identity_ref" not in payload["spec"]
    assert "/home/op/.ssh/fleet-key" not in text
    # Non-secret route facts remain observable.
    assert payload["spec"]["target"] == "ops@worker.example"
    assert payload["spec"]["host_key_policy"] == "strict"


def test_endpoint_repr_is_secret_free():
    endpoint = RemoteEndpoint.for_ssh(_make_spec())
    text = repr(endpoint)
    assert "/home/op/.ssh/fleet-key" not in text
    assert "identity_file" not in text


def test_result_carries_identity_not_credentials(monkeypatch):
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted(stdout=b"hi\n"))
    result = transport.run(["echo", "hi"])
    assert isinstance(result, RemoteResult)
    assert result.endpoint == transport.endpoint.identity
    assert result.ok
    assert "/home/op/.ssh/fleet-key" not in repr(result.to_dict())


# -- failure-class mapping ----------------------------------------------------
@pytest.mark.parametrize(
    "stderr, returncode, expected",
    [
        (b"Permission denied (publickey).", 255, RemoteAuthError),
        (b"Host key verification failed.", 255, RemoteHostKeyError),
        (b"ssh: connect to host: Connection timed out", 255, RemoteConnectTimeout),
        (b"ssh: connect to host: Connection refused", 255, RemoteTransportDead),
        (b"unknown ssh failure", 255, RemoteTransportDead),
        (b"cat: /nope: No such file or directory", 1, RemoteCommandError),
        (b"cat: /private: Permission denied", 1, RemoteCommandError),
    ],
)
def test_failure_class_mapping(monkeypatch, stderr, returncode, expected):
    transport, _, telemetry = _transport(
        monkeypatch,
        lambda a, k: _FakeCompleted(stderr=stderr, returncode=returncode),
    )
    with pytest.raises(expected) as excinfo:
        transport.run(["cat", "/nope"])
    assert excinfo.value.failure_class == expected.failure_class
    assert telemetry.failure_classes[expected.failure_class] == 1
    # stderr is retained on the exception but never in the observable identity.
    assert excinfo.value.endpoint is transport.endpoint


def test_missing_binary_maps_to_transport_dead(monkeypatch):
    def handler(argv, kw):
        raise FileNotFoundError(argv[0])

    transport, _, telemetry = _transport(monkeypatch, handler)
    with pytest.raises(RemoteTransportDead):
        transport.run(["true"])
    assert telemetry.failure_classes["transport_dead"] == 1


# -- telemetry ----------------------------------------------------------------
def test_telemetry_is_injectable_and_records(monkeypatch):
    transport, _, telemetry = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.run(["true"])
    transport.run(["true"])
    assert telemetry.operations == 2
    assert telemetry.timeouts == 0
    assert telemetry.to_dict()["operations"] == 2


# -- strict host-key verification is never weakened ---------------------------
def test_strict_host_key_options_are_preserved(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted())
    transport.run(["true"])
    argv = calls[0]["argv"]
    assert "StrictHostKeyChecking=yes" in argv
    assert "UserKnownHostsFile=/home/op/.ssh/fleet-known-hosts" in argv


def test_streaming_transfer_uses_exec_channel(monkeypatch):
    transport, calls, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted(stdout=b"data"))
    assert transport.open_read("/tmp/file") == b"data"
    # open_read streams via the argv exec path, never a shell string.
    assert calls[0]["argv"][0] == "ssh"
    assert "cat -- /tmp/file" in calls[0]["argv"][-1]


def test_streaming_read_preserves_binary_bytes(monkeypatch):
    payload = b"\x00\xff\xfe\x80data"
    transport, _, _ = _transport(monkeypatch, lambda a, k: _FakeCompleted(stdout=payload))
    assert transport.open_read("/tmp/file") == payload
