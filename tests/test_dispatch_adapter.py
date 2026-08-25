"""Contract tests for the Literate AI execution-dispatch adapter.

These assert the properties that fail SILENTLY if broken: a recomputed request
identity, a refusal before an unroutable task exists, and the exit-code split
between "the dispatcher broke" and "the work failed".
"""

from __future__ import annotations

import json

import pytest

from mac.dispatch_adapter import (
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    DispatchAdapterError,
    build_result,
    canonical_bytes,
    content_identity,
    correlation_metadata,
    read_request,
    refuse_unroutable,
    required_capabilities,
)

BASE_REQUEST = {
    "schema": REQUEST_SCHEMA,
    "action": "test",
    "component": "samples/hello-component",
    "worker_identity": "sha256:" + "a" * 64,
    "requirements": {
        "schema": "urn:literate-ai:schema:v2:execution-requirements",
        "os_family": None,
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
    "artifact_reference": None,
    "timeout_seconds": 3600,
}


def request_with(**overrides):
    request = json.loads(json.dumps(BASE_REQUEST))
    request.update(overrides)
    return request


def requirements_with(**overrides):
    requirements = dict(BASE_REQUEST["requirements"])
    requirements.update(overrides)
    return request_with(requirements=requirements)


class _Agent:
    def __init__(self, capabilities):
        self.capabilities = set(capabilities)


class _Plane:
    def __init__(self, agents):
        self._agents = agents

    def list_agents(self):
        return self._agents


# --- canonicalization -------------------------------------------------------


def test_canonical_bytes_match_literate_ai_canonical_json():
    """Sorted keys, no whitespace, unicode preserved.

    litai compares our request_identity against its own digest of the bytes it
    sent. Any deviation here is rejected as a tampered request, so this mirrors
    literate_ai.contracts.identity.canonical_json_bytes exactly.
    """
    value = {"b": 1, "a": [3, 2, 1], "u": "café", "n": None}
    assert canonical_bytes(value) == b'{"a":[3,2,1],"b":1,"n":null,"u":"caf\xc3\xa9"}'
    assert content_identity(value).startswith("sha256:")
    assert len(content_identity(value)) == len("sha256:") + 64


def test_content_identity_ignores_key_order():
    assert content_identity({"a": 1, "b": 2}) == content_identity({"b": 2, "a": 1})


# --- request validation -----------------------------------------------------


def test_read_request_rejects_a_foreign_schema(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_with(schema="urn:something:else")))
    with pytest.raises(DispatchAdapterError, match="must declare schema"):
        read_request(str(path))


def test_read_request_names_every_missing_field(tmp_path):
    incomplete = request_with()
    del incomplete["timeout_seconds"]
    del incomplete["artifact_reference"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(incomplete))
    with pytest.raises(DispatchAdapterError) as error:
        read_request(str(path))
    assert "timeout_seconds" in str(error.value)
    assert "artifact_reference" in str(error.value)


def test_read_request_rejects_unreadable_input():
    with pytest.raises(DispatchAdapterError):
        read_request("/nonexistent/dispatch-request.json")


# --- translation ------------------------------------------------------------


def test_numeric_requirements_are_enforced_not_refused():
    """They were refused because the allocator "does set membership only".

    That was true of CAPABILITIES and never true of hardware:
    machine_hardware_satisfies has compared cpu_count_min and memory_gb_min all
    along, against facts every worker already publishes. Refusing the request
    was rejecting work the fleet could have routed.
    """
    from mac.dispatch_adapter import required_hardware

    assert required_hardware(requirements_with(minimum_cpu_cores=8))["cpu_count_min"] == 8
    # litai speaks MiB and the allocator's minimum is in GB.
    assert required_hardware(requirements_with(minimum_memory_mib=4096))["memory_gb_min"] == 4.0
    # And they are no longer smuggled into the capability set, where nothing
    # would have matched them.
    assert required_capabilities(requirements_with(minimum_cpu_cores=8)) == ()


def test_a_host_constraint_is_hardware_not_a_capability():
    """THE bug this replaces. os_family became a required capability named
    "linux", which no agent advertises and none ever will -- capabilities are a
    declared vocabulary, os and cpu_arch are probed facts. The result was a task
    created and never claimed."""
    from mac.dispatch_adapter import required_hardware

    assert required_capabilities(requirements_with(os_family="linux")) == ()
    assert required_hardware(requirements_with(os_family="linux"))["os"] == ["linux"]


def test_both_spellings_of_an_architecture_match():
    """Both are live in the fleet right now: rocky reports arm64, natasha
    reports aarch64, for the same architecture family. Matching one spelling
    silently excludes half the hosts that satisfy the constraint."""
    from mac.dispatch_adapter import required_hardware

    assert set(required_hardware(requirements_with(cpu_architecture="arm64"))["cpu_arch"]) == {
        "arm64",
        "aarch64",
    }
    assert set(required_hardware(requirements_with(os_family="macos"))["os"]) == {
        "darwin",
        "macos",
    }


def test_os_version_is_refused_because_it_is_a_comparison():
    with pytest.raises(DispatchAdapterError, match="os_version"):
        required_capabilities(requirements_with(os_version="14.5"))


def test_nvidia_gpu_requires_both_gpu_and_cuda():
    request = requirements_with(
        gpu={
            "schema": "urn:literate-ai:schema:v1:gpu-requirement",
            "vendor": "nvidia",
            "model": None,
            "minimum_count": 1,
            "minimum_memory_mib": None,
            "capabilities": None,
        }
    )
    assert required_capabilities(request) == ("cuda", "gpu")


def test_gpu_memory_and_multi_gpu_are_refused():
    for field in ("minimum_memory_mib", "minimum_count"):
        gpu = {
            "schema": "urn:literate-ai:schema:v1:gpu-requirement",
            "vendor": "nvidia",
            "model": None,
            "minimum_count": 1,
            "minimum_memory_mib": None,
            "capabilities": None,
        }
        gpu[field] = 40960 if field == "minimum_memory_mib" else 4
        with pytest.raises(DispatchAdapterError):
            required_capabilities(requirements_with(gpu=gpu))


def test_correlation_metadata_carries_identities_but_not_a_mac_task_id():
    metadata = correlation_metadata(BASE_REQUEST)
    assert metadata["source_identity"] == BASE_REQUEST["source_identity"]
    assert not any("task" in key for key in metadata)


# --- routability preflight --------------------------------------------------


def test_unroutable_request_is_refused_before_the_task_exists():
    """A capability nobody advertises makes work that is never claimed.

    Measured once already: one agent advertised `c`, a transient failure
    excluded it, and the work became permanently undispatchable. A blocking
    dispatch would wait out its whole timeout to learn that.
    """
    fleet = _Plane([_Agent({"cpu", "c", "make"}), _Agent({"cpu", "gpu", "cuda"})])
    with pytest.raises(DispatchAdapterError, match="no agent advertises"):
        refuse_unroutable(fleet, ("linux", "aarch64"))


def test_refusal_names_the_missing_capability():
    fleet = _Plane([_Agent({"cpu", "python3"})])
    with pytest.raises(DispatchAdapterError) as error:
        refuse_unroutable(fleet, ("cpu", "rust"))
    assert "rust" in str(error.value)
    assert "python3" not in str(error.value)


def test_capabilities_split_across_two_agents_are_unroutable():
    """The allocator needs ONE agent holding the whole set, not the union."""
    fleet = _Plane([_Agent({"c"}), _Agent({"gpu"})])
    with pytest.raises(DispatchAdapterError):
        refuse_unroutable(fleet, ("c", "gpu"))


def test_a_satisfiable_set_is_accepted():
    fleet = _Plane([_Agent({"cpu", "c", "make", "python3"})])
    refuse_unroutable(fleet, ("c", "make"))


def test_an_unreadable_registry_is_not_a_refusal():
    """Absence of evidence is not evidence of absence -- let the ledger decide."""

    class Broken:
        def list_agents(self):
            raise RuntimeError("hub unreachable")

    refuse_unroutable(Broken(), ("cpu",))


# --- result -----------------------------------------------------------------


def test_result_recomputes_the_request_identity_rather_than_echoing_one():
    result = build_result(BASE_REQUEST, task_id="task_abc", status="passed", evidence={})
    assert result["request_identity"] == content_identity(BASE_REQUEST)


def test_result_carries_exactly_the_v2_fields():
    result = build_result(BASE_REQUEST, task_id="task_abc", status="passed", evidence={})
    assert result["schema"] == RESULT_SCHEMA
    assert set(result) == {
        "schema",
        "request_identity",
        "worker_identity",
        "external_task_id",
        "status",
        "observed_environment",
        "exit_status",
        "artifact_reference",
        "evidence_identity",
        "stdout",
        "stderr",
        "diagnostic_digest",
    }


def test_the_mac_task_id_travels_only_in_external_task_id():
    """litai excludes external_task_id from every identity it computes.

    Retrying the same derivation under a different task must not change
    content, so the task id must not reach any other field.
    """
    first = build_result(BASE_REQUEST, task_id="task_one", status="passed", evidence={})
    second = build_result(BASE_REQUEST, task_id="task_two", status="passed", evidence={})
    assert first["external_task_id"] != second["external_task_id"]
    assert first["request_identity"] == second["request_identity"]


def test_a_failed_run_still_produces_a_result():
    """Exit code says the dispatcher worked; `status` says the work did not."""
    result = build_result(
        BASE_REQUEST,
        task_id="task_abc",
        status="failed",
        evidence={"exit_status": 2, "stdout": "boom", "stderr": ""},
    )
    assert result["status"] == "failed"
    assert result["exit_status"] == 2
    assert result["diagnostic_digest"].startswith("sha256:")


def test_observed_environment_reports_measured_toolchains():
    result = build_result(BASE_REQUEST, task_id="t", status="passed", evidence={})
    observed = result["observed_environment"]
    assert observed["schema"] == "urn:literate-ai:schema:v2:observed-execution-environment"
    assert isinstance(observed["toolchain_identities"], list)
    assert observed["cpu_cores"] >= 1


def test_an_agent_reported_environment_wins_over_the_local_one():
    """The machine that ran the work describes itself; the hub does not guess."""
    reported = {
        "os_family": "linux",
        "os_version": "6.1",
        "cpu_architecture": "x86_64",
        "cpu_cores": 64,
        "memory_mib": 262144,
        "gpu_vendor": "nvidia",
        "gpu_model": "rtx-6000-ada",
        "gpu_count": 1,
        "gpu_memory_mib": 49140,
        "gpu_capabilities": ["cuda"],
        "toolchain_identities": [{"tool": "rustc", "path": "/usr/bin/rustc"}],
    }
    result = build_result(
        BASE_REQUEST, task_id="t", status="passed", evidence={"observed_environment": reported}
    )
    assert result["observed_environment"]["cpu_cores"] == 64
    assert result["observed_environment"]["gpu_model"] == "rtx-6000-ada"
