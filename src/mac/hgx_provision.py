"""Turn a fresh HGX-created fungible node into a deployable onboarding plan.

A ``fungible`` fleet record is replaceable compute — for example a headless
worker created through a provider API such as ``hgx create`` (see
``docs/book/04-machines-and-agents.md``).  Before such a node can run MAC
work it has to reach *phase-zero*: ``deploy/deploy-mac-fleet.sh
--prepare-fungible-onboarding`` binds a live provider session to a draining
fleet placeholder and publishes a reviewed source/venv/tool rollback baseline
onto the node's ``~/.mac`` volume (see ``prepare_fungible_machine_onboarding``
in that script and the helper ``deploy/fleet-node-machine-onboard.py``).

This module is the *pure* planning half of that flow.  Given the raw facts a
caller already has about a freshly created provider session — the ``hgx``
session id, the SSH endpoint it proved reachable, and the fleet record it
should bind to — it computes and validates the exact deployable plan:

* the fungible ``~/.mac`` volume layout the onboarding helper will populate,
* the reviewed runtime toolchain pins the baseline is published against,
* the draining/degraded placeholder barrier the controller registers, and
* the precise ``deploy-mac-fleet.sh --prepare-fungible-onboarding`` command.

It performs no I/O, spawns no processes, and opens no SSH connections, so it is
deterministic and fully unit-testable.  The numbers, schemas, and pins here are
the same ones the deploy script and onboarding helper assert on the wire, so a
plan produced here is what an operator (or an automated caller) can hand to the
deploy tool without surprises.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from mac.models import AgentInstanceKind, ValidationError


# Reviewed runtime toolchain pins.  These MUST match the versions
# ``prepare_fungible_machine_onboarding`` verifies in the remote stage receipt
# (``deploy/deploy-mac-fleet.sh``) and that ``deploy/fleet-node-machine-onboard.py``
# publishes (uv 0.8.22, CPython 3.12.11, CodeGraph v1.1.6).
UV_VERSION = "0.8.22"
PYTHON_VERSION = "3.12.11"
CODEGRAPH_VERSION = "v1.1.6"

# Schemas shared with the deploy/onboarding contract.
PLAN_SCHEMA = "mac.hgx_fungible_onboarding_plan.v1"
SESSION_SCHEMA = "mac.hgx_provider_session.v1"
RESOURCE_SCHEMA = "mac.fleet_machine_onboarding_resource.v1"

# The controller registers the placeholder as draining/degraded and starts no
# services; the published commit receipt asserts exactly this barrier.
PLACEHOLDER_STATUS = "draining"
PLACEHOLDER_HEALTH_STATUS = "degraded"

DEPLOY_SCRIPT = "deploy/deploy-mac-fleet.sh"
PREPARE_FLAG = "--prepare-fungible-onboarding"

# A provider session id and SSH endpoint are attacker-influenced free text
# until proven; keep them to a conservative, argv-safe shape before they can
# reach a command line.
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")
_SAFE_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValidationError(f"{field} must not be empty")
    return stripped


@dataclass(frozen=True)
class HgxSession:
    """A fresh, reachability-proven HGX provider session for a fungible node.

    ``hgx list`` establishes that ``session_id`` exists in provider inventory
    and a successful ``hgx ssh <session_id>`` proves the SSH endpoint below is
    actually reachable.  This object records those proven facts; it does not
    re-establish them.
    """

    session_id: str
    ssh_user: str
    ssh_host: str
    ssh_port: int = 22
    home: str = "/home"

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _require_str(self.session_id, "session_id"))
        object.__setattr__(self, "ssh_user", _require_str(self.ssh_user, "ssh_user"))
        object.__setattr__(self, "ssh_host", _require_str(self.ssh_host, "ssh_host"))
        home = _require_str(self.home, "home")
        if not self.session_id or not _SAFE_SESSION.match(self.session_id):
            raise ValidationError("session_id has an unsupported shape")
        if not _SAFE_USER.match(self.ssh_user):
            raise ValidationError("ssh_user has an unsupported shape")
        if not _SAFE_HOST.match(self.ssh_host):
            raise ValidationError("ssh_host has an unsupported shape")
        if not isinstance(self.ssh_port, int) or isinstance(self.ssh_port, bool):
            raise ValidationError("ssh_port must be an integer")
        if not 1 <= self.ssh_port <= 65535:
            raise ValidationError("ssh_port must be within 1..65535")
        if not home.startswith("/"):
            raise ValidationError("home must be an absolute path")
        object.__setattr__(self, "home", home.rstrip("/") or "/")

    @property
    def account_home(self) -> str:
        """Absolute home directory of the session's login account."""

        base = "" if self.home == "/" else self.home
        return f"{base}/{self.ssh_user}"

    @property
    def ssh_destination(self) -> str:
        """``user@host`` destination proven reachable by ``hgx ssh``."""

        return f"{self.ssh_user}@{self.ssh_host}"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HgxSession":
        """Build a session from an ``hgx``-shaped mapping.

        Accepts the ``mac.hgx_provider_session.v1`` schema tag when present but
        does not require it, so callers can pass a raw provider record.
        """

        if not isinstance(data, Mapping):
            raise ValidationError("hgx session data must be a mapping")
        schema = data.get("schema")
        if schema is not None and schema != SESSION_SCHEMA:
            raise ValidationError(f"unexpected hgx session schema: {schema!r}")
        port = data.get("ssh_port", 22)
        if isinstance(port, str):
            if not port.isdigit():
                raise ValidationError("ssh_port must be an integer")
            port = int(port)
        return cls(
            session_id=data.get("session_id"),
            ssh_user=data.get("ssh_user"),
            ssh_host=data.get("ssh_host"),
            ssh_port=port,
            home=data.get("home", "/home"),
        )


@dataclass(frozen=True)
class VolumeLayout:
    """Fungible ``~/.mac`` volume paths the onboarding baseline populates.

    Mirrors ``Layout.for_home`` in ``deploy/fleet-node-machine-onboard.py`` so
    a plan describes exactly the paths the reviewed baseline will create.
    """

    home: str
    mac_home: str
    source: str
    venv: str
    local_bin: str
    mac_bin: str
    codegraph_bin: str
    gh_bin: str
    receipt: str
    lock: str

    @classmethod
    def for_account_home(cls, account_home: str) -> "VolumeLayout":
        account_home = _require_str(account_home, "account_home").rstrip("/") or "/"
        if not account_home.startswith("/"):
            raise ValidationError("account_home must be an absolute path")
        base = "" if account_home == "/" else account_home
        mac_home = f"{base}/.mac"
        return cls(
            home=account_home,
            mac_home=mac_home,
            source=f"{mac_home}/src/mac",
            venv=f"{mac_home}/venv",
            local_bin=f"{base}/.local/bin",
            mac_bin=f"{base}/.local/bin/mac",
            codegraph_bin=f"{mac_home}/bin/codegraph",
            gh_bin=f"{mac_home}/bin/gh",
            receipt=f"{mac_home}/machine-onboarding-receipt.json",
            lock=f"{mac_home}/.machine-onboarding.lock",
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "home": self.home,
            "mac_home": self.mac_home,
            "source": self.source,
            "venv": self.venv,
            "local_bin": self.local_bin,
            "mac_bin": self.mac_bin,
            "codegraph_bin": self.codegraph_bin,
            "gh_bin": self.gh_bin,
            "receipt": self.receipt,
            "lock": self.lock,
        }


@dataclass(frozen=True)
class OnboardingPlan:
    """A validated, deployable phase-zero plan for one fungible node."""

    agent: str
    session: HgxSession
    fleet_name: str
    capabilities: tuple[str, ...]
    hub_agent: str
    layout: VolumeLayout

    @property
    def toolchain(self) -> Dict[str, str]:
        return {
            "uv": UV_VERSION,
            "python": PYTHON_VERSION,
            "codegraph": CODEGRAPH_VERSION,
        }

    @property
    def placeholder_barrier(self) -> Dict[str, str]:
        return {
            "status": PLACEHOLDER_STATUS,
            "health_status": PLACEHOLDER_HEALTH_STATUS,
        }

    def deploy_command(self) -> list[str]:
        """The exact ``--prepare-fungible-onboarding`` argv for this node."""

        return [
            DEPLOY_SCRIPT,
            "--hub",
            self.hub_agent,
            PREPARE_FLAG,
            self.agent,
        ]

    def deploy_command_str(self) -> str:
        """Shell-safe rendering of :meth:`deploy_command`."""

        return " ".join(shlex.quote(part) for part in self.deploy_command())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "instance_kind": AgentInstanceKind.FUNGIBLE.value,
            "agent": self.agent,
            "hub_agent": self.hub_agent,
            "fleet_name": self.fleet_name,
            "capabilities": list(self.capabilities),
            "session": {
                "schema": SESSION_SCHEMA,
                "session_id": self.session.session_id,
                "ssh_destination": self.session.ssh_destination,
                "ssh_port": self.session.ssh_port,
            },
            "volume": self.layout.as_dict(),
            "toolchain": self.toolchain,
            "placeholder": {
                "schema": RESOURCE_SCHEMA,
                "status": "prepared",
                "instance_kind": AgentInstanceKind.FUNGIBLE.value,
                "barrier": self.placeholder_barrier,
            },
            "services_started": False,
            "deploy_command": self.deploy_command(),
            "deploy_command_str": self.deploy_command_str(),
        }


def _coerce_capabilities(capabilities: Any) -> tuple[str, ...]:
    if capabilities is None:
        return ()
    if isinstance(capabilities, str):
        items = [item.strip() for item in capabilities.split(",")]
    elif isinstance(capabilities, (list, tuple)):
        items = []
        for item in capabilities:
            if not isinstance(item, str):
                raise ValidationError("each capability must be a string")
            items.append(item.strip())
    else:
        raise ValidationError("capabilities must be a string or a list of strings")
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def plan_fungible_onboarding(
    *,
    agent: str,
    session: HgxSession | Mapping[str, Any],
    hub_agent: str,
    fleet_name: str = "mac",
    capabilities: Optional[Any] = None,
    instance_kind: Any = AgentInstanceKind.FUNGIBLE,
) -> OnboardingPlan:
    """Build a validated deployable plan for a fresh HGX fungible node.

    ``instance_kind`` must resolve to ``fungible``; phase-zero onboarding
    refuses a static fleet record exactly as
    ``prepare_fungible_machine_onboarding_worker`` does on the wire.
    """

    agent = _require_str(agent, "agent")
    if not _SAFE_AGENT.match(agent):
        raise ValidationError("agent has an unsupported shape")
    hub_agent = _require_str(hub_agent, "hub_agent")
    if not _SAFE_AGENT.match(hub_agent):
        raise ValidationError("hub_agent has an unsupported shape")
    fleet_name = _require_str(fleet_name, "fleet_name")

    kind = AgentInstanceKind(str(instance_kind))
    if kind is not AgentInstanceKind.FUNGIBLE:
        raise ValidationError(
            "phase-zero onboarding refuses a non-fungible fleet record"
        )

    if isinstance(session, HgxSession):
        proven = session
    else:
        proven = HgxSession.from_mapping(session)

    layout = VolumeLayout.for_account_home(proven.account_home)
    return OnboardingPlan(
        agent=agent,
        session=proven,
        fleet_name=fleet_name,
        capabilities=_coerce_capabilities(capabilities),
        hub_agent=hub_agent,
        layout=layout,
    )
