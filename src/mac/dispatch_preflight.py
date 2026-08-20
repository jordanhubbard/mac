"""Answer "would this task ever be claimed?" before it is filed.

It answers two questions that are easy to conflate and have opposite remedies:

    could the fleet ever claim this?    -- capabilities and host facts
    is it bounded enough to dispatch?   -- metadata.scope_packet

The first is about the fleet, the second is about the task, and a caller told
only "not dispatchable" cannot tell which one to go and fix. They are reported
as separate findings for that reason: ``missing_capabilities`` /
``hardware_reasons`` on one side, ``scope`` on the other.

WHY THIS EXISTS

A task whose requirements no agent satisfies is accepted, queued, and never
claimed. It does not fail; it waits. On 2026-08-08 one sat undispatchable while
eight idle agents watched, and the installer comment recording that is still in
the tree.

For a caller inside mac that is a slow annoyance. For a caller OUTSIDE mac --
literate-ai submits with a deadline and blocks -- it is the difference between
an error and a timeout, and a timeout says nothing about what was wrong.

THE MISTAKE THIS IS SHAPED TO PREVENT

The obvious mapping for a host constraint like ``os_family: linux`` is a
required CAPABILITY named ``linux``. That produces exactly the undispatchable
task, because capabilities are set membership against a DECLARED vocabulary --
agents advertise ``python``, ``testing``, ``review`` -- while os and cpu_arch
are PROBED FACTS living in ``resources.hardware``. No agent will ever declare
``linux``, so the match cannot succeed no matter how many Linux machines are
idle.

Worse, it can succeed WRONGLY: ``architecture`` is a real advertised capability
meaning the software-architecture skill. A naive ``arch`` mapping matches
agents that have nothing to do with CPU architecture, which is harder to
diagnose than never matching at all.

The correct route is ``required_hardware`` -- ``{"os": ["linux"], "cpu_arch":
["x86_64"]}`` -- which machine_hardware_satisfies already evaluates against
those probed facts, and which every worker already populates.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from mac.roles_service import machine_hardware_satisfies
from mac.task_scope_packet import evaluate as evaluate_scope_packet

SCHEMA = "mac.dispatch_preflight.v1"

#: Capability names that are really host facts. Naming one as a required
#: capability is the mistake above, so it is reported as a mapping error with
#: the correct form rather than as "no agent has this skill" -- the second is
#: true and useless.
_HOST_FACT_CAPABILITIES: Dict[str, str] = {
    "linux": 'required_hardware={"os": ["linux"]}',
    "darwin": 'required_hardware={"os": ["darwin"]}',
    "macos": 'required_hardware={"os": ["darwin"]}',
    "windows": 'required_hardware={"os": ["windows"]}',
    "x86_64": 'required_hardware={"cpu_arch": ["x86_64"]}',
    "amd64": 'required_hardware={"cpu_arch": ["x86_64"]}',
    "arm64": 'required_hardware={"cpu_arch": ["arm64"]}',
    "aarch64": 'required_hardware={"cpu_arch": ["aarch64"]}',
}


def _agent_view(agent: Any) -> Dict[str, Any]:
    """Read an agent however it arrives.

    Three shapes reach this: the Agent model (to_dict), a plain dict from the
    hub, and bare objects with attributes. Assuming any one of them makes the
    preflight fail on callers it exists to serve -- the adapter passes objects.
    """
    if hasattr(agent, "to_dict"):
        record = agent.to_dict()
    elif isinstance(agent, Mapping):
        record = dict(agent)
    else:
        record = {
            field: getattr(agent, field, None)
            for field in (
                "id", "name", "status", "capabilities", "resources",
                "visibility", "owner_human_id",
            )
        }
    resources = record.get("resources") or {}
    if not isinstance(resources, Mapping):
        resources = {}
    hardware = resources.get("hardware") or {}
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "status": record.get("status"),
        "capabilities": {str(c) for c in (record.get("capabilities") or [])},
        "hardware": dict(hardware) if isinstance(hardware, Mapping) else {},
        "visibility": record.get("visibility") or "shared",
        "owner_human_id": record.get("owner_human_id"),
    }


def preflight(
    agents: Iterable[Any],
    *,
    required_capabilities: Optional[Iterable[str]] = None,
    required_hardware: Optional[Mapping[str, Any]] = None,
    created_by_human: Optional[str] = None,
    scope_packet: Any = None,
) -> Dict[str, Any]:
    """Would any agent in ``agents`` be able to claim this task?

    Returns a decision plus the reason per agent. ``dispatchable`` false means
    the task would wait forever as filed, and ``missing_capabilities`` /
    ``hardware_reasons`` say what would have to change.

    ``scope_packet`` is the proposed ``metadata.scope_packet``. Its verdict
    lands in ``scope`` and in ``scope_bounded``, kept out of ``dispatchable``
    on purpose: an unbounded task is a defect in the TASK, a task no agent can
    satisfy is a mismatch with the FLEET, and folding both into one boolean is
    how the caller loses the only thing that tells them which to fix. Omitting
    the argument is itself an answer -- a task filed with no packet is
    unbounded, and saying so is the point.

    Deliberately NOT a claim that the task will be claimed soon: agents may be
    busy, held, or offline. It answers the narrower question that actually
    causes silent hangs -- whether the requirements are satisfiable by the fleet
    that exists.

    ``created_by_human`` is accepted and ignored. It existed to mirror the
    allocator's private-agent gate, which was retired on 2026-08-17: an agent's
    visibility says who the HUB may talk to, not which problems the agent may
    work on. Keeping the parameter means existing callers and the HTTP body do
    not have to change; it simply no longer affects the answer.
    """
    wanted_caps = {str(c).strip() for c in (required_capabilities or []) if str(c).strip()}
    wanted_hw = dict(required_hardware or {})

    # A host fact asked for as a capability can never match. Say so precisely,
    # with the form that would work, instead of reporting an absent skill.
    mapping_errors = [
        {
            "capability": cap,
            "problem": "this is a host fact, not a declared capability",
            "use_instead": _HOST_FACT_CAPABILITIES[cap],
        }
        for cap in sorted(wanted_caps)
        if cap.lower() in _HOST_FACT_CAPABILITIES
    ]

    considered: List[Dict[str, Any]] = []
    eligible: List[str] = []
    fleet_caps: set = set()
    for agent in agents:
        view = _agent_view(agent)
        fleet_caps |= view["capabilities"]
        missing = sorted(wanted_caps - view["capabilities"])
        hardware_ok, hardware_reasons = machine_hardware_satisfies(
            wanted_hw, view["hardware"]
        )
        # Visibility is deliberately not consulted. It describes who the HUB
        # may talk to, not which problems an agent may work on, and mirroring
        # the old dispatch gate here made preflight under-report capacity: on
        # 2026-08-17 it answered "4 agents" for a fleet that had 7, hiding
        # three idle hosts the caller owned. See allocator._eligibility_rejections.
        ok = not missing and hardware_ok
        entry = {
            "agent": view["name"] or view["id"],
            "eligible": ok,
            "missing_capabilities": missing,
            "hardware_reasons": list(hardware_reasons),
        }
        considered.append(entry)
        if ok:
            eligible.append(view["name"] or view["id"])

    # Which requirement is at fault, across the whole fleet. A capability no
    # agent anywhere has is the actionable one; a capability some agent has but
    # this one lacks is ordinary routing.
    unsatisfiable = sorted(wanted_caps - fleet_caps)
    scope = evaluate_scope_packet(scope_packet)
    return {
        "schema": SCHEMA,
        "dispatchable": bool(eligible),
        "scope_bounded": scope.bounded,
        "scope": scope.to_dict(),
        "eligible_agents": sorted(eligible),
        "agents_considered": len(considered),
        # Named so the caller can put it straight into an error message.
        "missing_capabilities": unsatisfiable,
        "mapping_errors": mapping_errors,
        "hardware_reasons": sorted(
            {reason for entry in considered for reason in entry["hardware_reasons"]}
        ),
        "detail": considered,
    }


def explain(result: Mapping[str, Any], *, include_scope: bool = True) -> str:
    """One line a blocking caller can log or raise verbatim.

    The scope finding is appended rather than merged: "no agent advertises
    rust" and "the task never said what done looks like" are two different
    pieces of work for two different people, and a caller that reads only the
    first clause still gets the fleet answer it came for.

    ``include_scope=False`` is for callers that asked ONLY the fleet question
    and never offered a packet -- the litai dispatch adapter is the one in
    tree. Telling them their scope is unbounded is true and useless: they
    cannot supply what they were never asked for, and it would bury the
    capability they actually have to go and fix.
    """
    scope = result.get("scope") if isinstance(result.get("scope"), Mapping) else {}
    scope_clause = (
        ""
        if scope.get("bounded", True) or not include_scope
        else "; scope not bounded: %s" % scope.get("message")
    )
    if result.get("dispatchable"):
        return "dispatchable: %d agent(s) can claim this task%s" % (
            len(result.get("eligible_agents") or []),
            scope_clause,
        )
    parts: List[str] = []
    for error in result.get("mapping_errors") or []:
        parts.append(
            "%r is a host fact, not a capability; use %s"
            % (error["capability"], error["use_instead"])
        )
    missing = result.get("missing_capabilities") or []
    if missing:
        parts.append("no agent advertises: %s" % ", ".join(missing))
    for reason in result.get("hardware_reasons") or []:
        parts.append("hardware: %s" % reason)
    if not result.get("agents_considered"):
        # An empty fleet produced "all candidates are private or otherwise
        # excluded", which describes candidates that do not exist. The caller
        # needs to know there is nothing to route TO.
        parts.append("no agent advertises anything: the fleet is empty")
    if not parts:
        parts.append(
            "no single agent satisfies the whole request (the requirements are "
            "spread across different agents)"
        )
    return "not dispatchable: " + "; ".join(parts) + scope_clause
