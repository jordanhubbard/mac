"""`mac admin dispatch submit` answers one Literate AI execution-dispatch request.

The CLI contract is unusual and worth pinning at this level rather than only in
unit tests, because two of its properties are about the PROCESS rather than the
return value:

stdout must carry the result document and nothing else -- litai parses the whole
stream as one JSON value, so a stray heading or progress line makes the result
unreadable.

The exit code reports whether the DISPATCHER worked, not whether the work
passed. litai reads a non-zero exit as `execution.dispatcher_failed`, so an
adapter fault must exit non-zero and print to stderr, while a failed build exits
0 carrying status=failed.

Both refusal paths here return before any task is created, which is deliberate:
a blocking caller is holding a deadline, and work that can never be claimed
would otherwise consume the whole timeout before saying so.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.dispatch_adapter import REQUEST_SCHEMA
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _request(**overrides):
    request = {
        "schema": REQUEST_SCHEMA,
        "action": "test",
        "component": "samples/hello-component",
        "worker_identity": "sha256:" + "a" * 64,
        "requirements": {
            "schema": "urn:literate-ai:schema:v2:execution-requirements",
            # An OS family no agent advertises: the fleet installer publishes
            # cpu/gpu/cuda and probed toolchains, never an OS capability.
            "os_family": "linux",
            "os_version": None,
            "cpu_architecture": None,
            "minimum_cpu_cores": None,
            "minimum_memory_mib": None,
            "gpu": None,
        },
        "parameters": [],
        "arguments": [],
        "project_identity": "sha256:" + "b" * 64,
        "source_identity": "sha256:" + "c" * 64,
        "specification_identity": "sha256:" + "d" * 64,
        "flavor_identity": "sha256:" + "e" * 64,
        "toolchain_identity": "sha256:" + "f" * 64,
        "codegraph_identity": "sha256:" + "0" * 64,
        "artifact_reference": None,
        "timeout_seconds": 3600,
    }
    request.update(overrides)
    return request


def _write(tmp_path, request):
    path = tmp_path / "dispatch-request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_unroutable_request_is_refused_without_creating_a_task(tmp_path, capsys):
    """No agent can claim it, so say so now rather than at the timeout.

    This is the failure measured on the live fleet: one agent advertised `c`,
    a transient exclusion removed it, and the work became permanently
    undispatchable. A blocking dispatch turns that into an hour of silence.
    """
    path = _write(tmp_path, _request())
    rc, payload = _run(tmp_path, "admin", "dispatch", "submit", path)

    assert rc == 1, "an adapter refusal must be a non-zero exit"
    assert payload is None, "stdout must stay empty when no result was produced"
    assert "no agent advertises" in capsys.readouterr().err

    # The refusal happened before any task existed.
    _rc, tasks = _run(tmp_path, "task", "list", "--project", "mac")
    assert not tasks or tasks == []


def test_a_malformed_request_is_refused_on_stderr(tmp_path, capsys):
    path = _write(tmp_path, _request(schema="urn:literate-ai:schema:v1:something-else"))
    rc, payload = _run(tmp_path, "admin", "dispatch", "submit", path)

    assert rc == 1
    assert payload is None
    assert "must declare schema" in capsys.readouterr().err


def test_a_request_missing_fields_names_them(tmp_path, capsys):
    request = _request()
    del request["timeout_seconds"]
    path = _write(tmp_path, request)
    rc, _payload = _run(tmp_path, "admin", "dispatch", "submit", path)

    assert rc == 1
    assert "timeout_seconds" in capsys.readouterr().err


def test_a_numeric_requirement_is_enforced_rather_than_refused(tmp_path, capsys):
    """It used to be refused as unenforceable, because "the allocator matches a
    capability set only". That was true of CAPABILITIES and never of hardware:
    machine_hardware_satisfies has compared cpu_count_min all along, against
    facts every worker publishes. Refusing was rejecting work the fleet could
    route.

    It is still refused HERE -- no agent in this test has 64 cores -- but for
    the honest reason, naming the constraint that failed.
    """
    requirements = dict(_request()["requirements"])
    requirements["minimum_cpu_cores"] = 64
    path = _write(tmp_path, _request(requirements=requirements))
    rc, _payload = _run(tmp_path, "admin", "dispatch", "submit", path)

    assert rc == 1
    message = capsys.readouterr().err
    assert "capability set only" not in message
    assert "not dispatchable" in message
