"""Answer one Literate AI execution-dispatch request from the MAC fleet.

Literate AI is a derivation engine; MAC is the control plane. It deliberately
refuses to be a scheduler -- its own roadmap says "do not make Literate AI a
scheduler ... a future system such as `mac` can consume that request" -- and
hands work over as one JSON document, expecting one JSON document back.

The transport is a process, not a socket: `litai` runs this command directly,
substituting `{request_file}` if the worker declares it and otherwise piping
the canonical request on stdin. Four properties matter, and each fails silently
if it is wrong.

**Exit 0 even when the work failed.** A non-zero exit means "the dispatcher
malfunctioned", not "the build failed". The work's outcome belongs in `status`
and `exit_status`.

**stdout carries the result document and nothing else.** Diagnostics go to
stderr, which litai bounds and otherwise ignores.

**`request_identity` is recomputed, never echoed.** litai compares it against
its own canonical digest of the request it sent, so echoing a field would let a
tampered request through. The canonicalization here is byte-identical to
`literate_ai.contracts.identity`: sorted keys, no whitespace, no NaN.

**The observed environment describes the machine that actually ran the work.**
litai does no matching -- it verifies afterwards and rejects a result whose
environment contradicts the request. Reporting what a host advertised rather
than what it had is the defect 4c53732f fixed for GPUs, arriving after the work
is already paid for.

One asymmetry is worth stating because it constrains what this can promise.
litai's `ExecutionRequirements` carries OS, CPU, memory and GPU and no toolchain
at all, while its result *requires* `toolchain_identities` -- reported and never
checked. MAC's allocator, meanwhile, matches only on a flat capability set
(`allocator.py`: `task.required_capabilities.issubset(agent.capabilities)`);
there is no numeric or platform matching anywhere in it. So a request can be
satisfiable on paper and unroutable in practice. This module refuses those at
submission rather than discovering it after a build.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import time
from typing import Any, Dict, Mapping, Optional, Tuple

REQUEST_SCHEMA = "urn:literate-ai:schema:v2:execution-dispatch-request"
RESULT_SCHEMA = "urn:literate-ai:schema:v2:execution-dispatch-result"
OBSERVED_SCHEMA = "urn:literate-ai:schema:v2:observed-execution-environment"

REQUEST_FIELDS = (
    "schema",
    "action",
    "component",
    "worker_identity",
    "requirements",
    "parameters",
    "arguments",
    "project_identity",
    "source_identity",
    "specification_identity",
    "flavor_identity",
    "toolchain_identity",
    "codegraph_identity",
    "artifact_reference",
    "timeout_seconds",
)

#: MAC advertises toolchains as bare capability strings, probed inside the
#: sandbox (see the toolchain block in deploy/fleet-node-install.sh). Only the
#: probed set can be required; asking for anything else produces a task no agent
#: can claim, which is how "permanently undispatchable while eight idle agents
#: watched" happened once already.
PROBED_TOOLCHAIN_CAPABILITIES = frozenset({"c", "make", "python3"})

#: State the ledger reaches and stops at, mapped to litai's closed status set.
TERMINAL_STATUS = {
    "completed": "passed",
    "failed": "failed",
    "cancelled": "cancelled",
}


class DispatchAdapterError(RuntimeError):
    """The dispatcher could not answer at all. Exits non-zero, deliberately."""


# --------------------------------------------------------------- protocol


def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON v1. Byte-identical to literate_ai's canonical_json_bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_request(source: Optional[str]) -> Dict[str, Any]:
    """Read one dispatch request from a path, or from stdin when none is given."""
    if source is None:
        raw = sys.stdin.buffer.read()
    else:
        try:
            with open(source, "rb") as handle:
                raw = handle.read()
        except OSError as error:
            raise DispatchAdapterError(f"cannot read dispatch request: {error}") from error
    if not raw.strip():
        raise DispatchAdapterError("no dispatch request supplied on stdin or as a file")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DispatchAdapterError(f"dispatch request is not valid JSON: {error}") from error
    if not isinstance(request, dict):
        raise DispatchAdapterError("dispatch request must be a JSON object")
    if request.get("schema") != REQUEST_SCHEMA:
        raise DispatchAdapterError(
            f"dispatch request must declare schema {REQUEST_SCHEMA}, "
            f"not {request.get('schema')!r}"
        )
    missing = [field for field in REQUEST_FIELDS if field not in request]
    if missing:
        raise DispatchAdapterError(
            "dispatch request is missing required fields: " + ", ".join(missing)
        )
    return request


# ------------------------------------------------------------ translation


def required_capabilities(request: Mapping[str, Any]) -> Tuple[str, ...]:
    """Translate what litai asked for into capabilities MAC can actually match.

    Refuses rather than approximates. MAC matches a flat capability set and
    nothing else, so a requirement it cannot express is not a requirement it can
    quietly drop: litai re-checks the observed environment afterwards and
    rejects the result, so an unenforceable constraint costs a whole execution
    to discover.
    """
    requirements = request.get("requirements") or {}
    capabilities: set[str] = set()
    unenforceable: list[str] = []

    if requirements.get("os_version") is not None:
        # A version is a comparison against a string whose ordering is
        # platform-specific; hardware matching does numeric minimums only.
        unenforceable.append("os_version")
    # os_family, cpu_architecture, minimum_cpu_cores and minimum_memory_mib are
    # NOT capabilities and are no longer translated into any -- see
    # required_hardware() below for why, and for where they go instead.

    gpu = requirements.get("gpu")
    if gpu:
        capabilities.add("gpu")
        vendor = str(gpu.get("vendor") or "").lower()
        if vendor == "nvidia":
            capabilities.add("cuda")
        if gpu.get("minimum_count") is not None and int(gpu["minimum_count"]) > 1:
            unenforceable.append("gpu.minimum_count > 1")
        if gpu.get("minimum_memory_mib") is not None:
            unenforceable.append("gpu.minimum_memory_mib")

    if unenforceable:
        raise DispatchAdapterError(
            "MAC's allocator matches a capability set only; it cannot enforce "
            + ", ".join(sorted(set(unenforceable)))
            + ". Dispatching anyway would burn an execution that litai then "
            "rejects for contradicting the request."
        )
    return tuple(sorted(capabilities))


#: litai and mac spell the same host two ways, and BOTH spellings are live in
#: the fleet right now: rocky reports cpu_arch "arm64" while natasha reports
#: "aarch64" for the same architecture family. A constraint therefore has to
#: match every spelling, or it silently excludes half the hosts that satisfy it.
_OS_ALIASES = {
    "macos": ("darwin", "macos"),
    "darwin": ("darwin", "macos"),
    "linux": ("linux",),
    "windows": ("windows",),
}
_ARCH_ALIASES = {
    "x86_64": ("x86_64", "amd64"),
    "amd64": ("x86_64", "amd64"),
    "arm64": ("arm64", "aarch64"),
    "aarch64": ("arm64", "aarch64"),
}


def required_hardware(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Translate host constraints into what the allocator ALREADY matches.

    These were previously turned into required CAPABILITIES, which is why a
    routable request became a task nobody could claim: capabilities are set
    membership over a DECLARED vocabulary -- agents advertise python, testing,
    review -- while os and cpu_arch are PROBED FACTS in resources.hardware. No
    agent will ever advertise "linux", so the match could not succeed however
    many Linux machines sat idle.

    machine_hardware_satisfies has matched os, cpu_arch and numeric minimums
    all along, and every worker already publishes those facts. Nothing new is
    being taught to the allocator here; the requirements are simply being sent
    to the field that evaluates them.

    That also makes minimum_cpu_cores and minimum_memory_mib enforceable rather
    than grounds for refusal -- they map to cpu_count_min and memory_gb_min.
    """
    requirements = request.get("requirements") or {}
    hardware: Dict[str, Any] = {}

    os_family = requirements.get("os_family")
    if os_family is not None:
        key = str(os_family).strip().lower()
        hardware["os"] = list(_OS_ALIASES.get(key, (key,)))

    arch = requirements.get("cpu_architecture")
    if arch is not None:
        key = str(arch).strip().lower()
        hardware["cpu_arch"] = list(_ARCH_ALIASES.get(key, (key,)))

    cores = requirements.get("minimum_cpu_cores")
    if cores is not None:
        hardware["cpu_count_min"] = int(cores)

    memory_mib = requirements.get("minimum_memory_mib")
    if memory_mib is not None:
        # The allocator's minimum is in GB; litai speaks MiB.
        hardware["memory_gb_min"] = float(memory_mib) / 1024.0

    return hardware


def refuse_unroutable(
    control_plane: Any,
    capabilities: Tuple[str, ...],
    *,
    hardware: Optional[Mapping[str, Any]] = None,
) -> None:
    """Refuse a request no agent can claim, naming the capability nobody has.

    The allocator matches a capability subset, so a requirement no agent
    advertises does not fail -- it produces a task that sits forever. That is a
    measured failure mode, not a hypothetical: exactly one agent once advertised
    `c`, and when a transient failure excluded it "the work became permanently
    undispatchable while eight idle agents watched".

    A blocking dispatch makes it worse, because litai is holding a deadline. So
    the check happens before the task exists, and the error names the missing
    capability rather than reporting a timeout an hour later.
    """
    hardware = dict(hardware or {})
    if not capabilities and not hardware:
        return
    try:
        agents = control_plane.list_agents()
    except Exception:  # noqa: BLE001 - an unreadable registry is not a refusal
        return

    # Judged with the SAME rules the allocator will apply, capabilities and
    # hardware together. Checking them separately would pass a request whose
    # halves are individually satisfiable by different hosts and which no single
    # host satisfies -- a task that is created and never claimed, which is the
    # exact failure this function exists to prevent.
    from mac.dispatch_preflight import explain, preflight

    result = preflight(
        agents, required_capabilities=capabilities, required_hardware=hardware
    )
    if result["dispatchable"]:
        return
    raise DispatchAdapterError(
        # Fleet question only: this caller states capabilities and hardware and
        # never a scope packet, so a scope clause here would report a field it
        # was never given the chance to supply.
        explain(result, include_scope=False)
        + ". The task would be created and never claimed, and a blocking "
        "dispatch would wait out its timeout."
    )


def correlation_metadata(request: Mapping[str, Any]) -> Dict[str, Any]:
    """litai's identities, carried as metadata and nothing more.

    None of these may enter a MAC content identity, and MAC's task id must not
    enter litai's: it travels back in `external_task_id`, the one slot litai
    reserves for correlation and excludes from every identity it computes.
    Retrying the same derivation under a different task must not change content.
    """
    return {
        "schema": "mac.literate_ai_dispatch.v1",
        "action": request["action"],
        "component": request["component"],
        "worker_identity": request["worker_identity"],
        "project_identity": request["project_identity"],
        "source_identity": request["source_identity"],
        "specification_identity": request["specification_identity"],
        "flavor_identity": request["flavor_identity"],
        "toolchain_identity": request["toolchain_identity"],
        "codegraph_identity": request["codegraph_identity"],
        "arguments": list(request.get("arguments") or ()),
        "parameters": list(request.get("parameters") or ()),
    }


def observed_environment(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """Describe the machine that ran the work.

    Sourced from the executing agent's own report where it exists. The local
    fallback is honest only when the work ran here; a hub-side guess about a
    remote host would be the advertised-versus-actual lie again, one layer up.
    """
    reported = evidence.get("observed_environment")
    if isinstance(reported, Mapping):
        return {**reported, "schema": OBSERVED_SCHEMA}
    machine = platform.machine() or "unknown"
    system = platform.system().lower()
    return {
        "schema": OBSERVED_SCHEMA,
        "os_family": {"darwin": "macos"}.get(system, system),
        "os_version": platform.release() or "unknown",
        "cpu_architecture": machine,
        "cpu_cores": _cpu_cores(),
        "memory_mib": _memory_mib(),
        "gpu_vendor": None,
        "gpu_model": None,
        "gpu_count": 0,
        "gpu_memory_mib": None,
        "gpu_capabilities": [],
        "toolchain_identities": _local_toolchain_identities(),
    }


def _cpu_cores() -> int:
    import os as _os

    return max(1, int(_os.cpu_count() or 1))


def _memory_mib() -> int:
    import os as _os

    try:
        return max(
            1,
            int(_os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024 * 1024)),
        )
    except (AttributeError, ValueError, OSError):
        return 1


def _local_toolchain_identities() -> list:
    """Report the toolchains actually resolvable here, not a declared list."""
    found = []
    for tool in sorted(PROBED_TOOLCHAIN_CAPABILITIES | {"cc", "git", "node", "rustc"}):
        path = shutil.which(tool)
        if path:
            found.append({"tool": tool, "path": path})
    return found


# -------------------------------------------------------------- execution


def submit_and_wait(
    control_plane: Any,
    request: Mapping[str, Any],
    *,
    project: Optional[str],
    poll_seconds: float = 2.0,
) -> Tuple[str, str, Dict[str, Any]]:
    """Create one task, wait for it to settle, and return what the fleet saw.

    Returns ``(task_id, litai_status, evidence)``.
    """
    from mac.task_wait import TaskWait

    timeout_seconds = int(request["timeout_seconds"])
    capabilities = required_capabilities(request)
    hardware = required_hardware(request)
    refuse_unroutable(control_plane, capabilities, hardware=hardware)
    task = control_plane.create_task(
        title=f"litai {request['action']}: {request['component']}",
        description=(
            "Literate AI execution dispatch.\n\n"
            f"action={request['action']} component={request['component']}\n"
            f"source={request['source_identity']}\n"
        ),
        project=project,
        required_capabilities=capabilities,
        # Host constraints go here, where machine_hardware_satisfies evaluates
        # them against facts the fleet already publishes.
        required_hardware=hardware,
        metadata=correlation_metadata(request),
        actor="literate-ai",
        # The same derivation submitted twice is the same work. litai's request
        # identity is content-derived, so it is the right idempotency key --
        # and using it means a retried dispatch rejoins the original task
        # instead of forking a second execution of identical content.
        idempotency_key=content_identity(request),
    )
    task_id = str(getattr(task, "id", None) or getattr(task, "task_id", ""))
    if not task_id:
        raise DispatchAdapterError("control plane did not return a task id")

    wait = TaskWait({task_id: str(getattr(task, "state", "open"))}, follow_new=False)
    deadline = time.monotonic() + timeout_seconds
    state = str(getattr(task, "state", "open"))
    while not wait.done:
        if time.monotonic() >= deadline:
            return task_id, "timed-out", {}
        time.sleep(poll_seconds)
        current = control_plane.get_task(task_id)
        state = str(getattr(current, "state", "") or "")
        if state == "needs_input":
            # A parked task is waiting for a person, and litai is blocking on a
            # deadline. Say so now rather than burning the timeout in silence.
            raise DispatchAdapterError(
                f"task {task_id} parked for human input; a blocking dispatch "
                "cannot wait for an answer. Resolve it and re-dispatch."
            )
        wait.observe(task_id, state)

    return task_id, TERMINAL_STATUS.get(state, "failed"), _task_evidence(control_plane, task_id)


def _task_evidence(control_plane: Any, task_id: str) -> Dict[str, Any]:
    """Best available evidence for a settled task; absence is not failure."""
    try:
        records = control_plane.list_evidence(task_id=task_id)
    except Exception:  # noqa: BLE001 - evidence is reported, never load-bearing here
        return {}
    for record in records or ():
        payload = getattr(record, "payload", None) or getattr(record, "data", None)
        if isinstance(payload, Mapping):
            return dict(payload)
    return {}


# ----------------------------------------------------------------- result


def build_result(
    request: Mapping[str, Any],
    *,
    task_id: Optional[str],
    status: str,
    evidence: Mapping[str, Any],
) -> Dict[str, Any]:
    stdout = str(evidence.get("stdout") or "")
    stderr = str(evidence.get("stderr") or "")
    return {
        "schema": RESULT_SCHEMA,
        "request_identity": content_identity(request),
        "worker_identity": request["worker_identity"],
        "external_task_id": task_id,
        "status": status,
        "observed_environment": observed_environment(evidence),
        "exit_status": int(evidence.get("exit_status") or (0 if status == "passed" else 1)),
        "artifact_reference": evidence.get("artifact_reference"),
        "evidence_identity": (
            evidence.get("evidence_identity") or content_identity(dict(evidence))
        ),
        "stdout": stdout,
        "stderr": stderr,
        "diagnostic_digest": (
            content_identity({"stdout": stdout, "stderr": stderr})
            if stdout or stderr
            else None
        ),
    }


def dispatch_submit(
    control_plane: Any,
    source: Optional[str],
    *,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Read one request, run it on the fleet, and return one result document."""
    request = read_request(source)
    task_id, status, evidence = submit_and_wait(control_plane, request, project=project)
    result = build_result(request, task_id=task_id, status=status, evidence=evidence)
    if request["action"] == "build" and result["artifact_reference"] is None and status == "passed":
        # litai asserts on a non-null artifact_reference for a passing build and
        # would raise AssertionError rather than report. Fail here, where the
        # message can say which task produced nothing.
        raise DispatchAdapterError(
            f"build dispatch {task_id} passed without an artifact_reference; "
            "litai asserts on it and would crash instead of reporting"
        )
    return result
