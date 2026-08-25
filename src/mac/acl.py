"""Access control as ACEs on a resource tree (ADR 0019).

The model this replaces was nine flat scopes matched by a 204-line ordered
if/elif chain, with two invisible implicit grants (``admin`` implied
everything; ``write`` silently also granted ``roles`` and ``workflow``) and no
resource binding at all -- ``_assert_task_actor`` accepted a ``task_id`` and
never read it. Least privilege was therefore inexpressible: the narrowest
useful credential was a whole fleet worker token, which is why a sandboxed
executor was given none and could not file a child of its own task.

Here, authority is a set of (resource path, permission) grants that inherit
down a tree, as filesystem ACLs do. The properties that matter are all
negative ones -- what this refuses, and refusing predictably:

* deny by default: a path with no matching entry is refused;
* longest path wins, deterministically, NOT by evaluation order;
* an explicit deny beats an allow at the same or any shorter path;
* no permission implies another, ever.

This module is pure decision logic. It performs no I/O and knows nothing about
routes; wiring it to the API is a separate change, so this can be tested
adversarially on its own (ADR 0019 acceptance record).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "Permission",
    "PERMISSIONS",
    "AccessControlEntry",
    "Grant",
    "AclError",
    "fleet_path",
    "project_path",
    "task_path",
    "task_evidence_path",
    "agent_path",
    "machine_path",
    "secret_path",
    "workflow_path",
    "normalize_path",
    "ancestors",
    "AclEvaluator",
]


class AclError(ValueError):
    """Malformed resource path, permission, or entry."""


# --- permissions -----------------------------------------------------------
#
# Listed weakest-first for human reading ONLY. The order carries no semantics:
# holding `write` does not confer `control`, and holding `grant` does not
# confer `read`. Implicit implication is precisely what let the old `write`
# scope silently mean three domains, and it is not reintroduced here under a
# new spelling. A principal that needs several permissions is granted several,
# visibly, where a reviewer can see them.


class Permission:
    READ = "read"  # observe the resource
    APPEND = "append"  # add to it without altering what is there
    CREATE = "create"  # create children beneath it
    UPDATE = "update"  # modify the resource's own fields
    WRITE = "write"  # replace or DELETE the resource itself
    STOP = "stop"  # abort in-flight work and park the resource
    START = "start"  # return a stopped resource to the queue
    CONTROL = "control"  # lifecycle: claim, heartbeat, lease, transition
    GRANT = "grant"  # change the ACL


# `update` is split out of `write`, and `stop`/`start` out of `control`, so the
# common cases can be granted without the destructive or lifecycle ones.
#
# Correcting a task's scope (ADR 0020) needs `update` and nothing else: the
# holder may fix criteria, priority or dependencies, and still may not delete
# the task. Likewise `stop` and `start` are the operator's edit cycle, whereas
# `control` is what an EXECUTOR needs -- claim, heartbeat, lease, transition.
# Folding them together would mean that letting someone correct a bad task
# description also let them claim work and impersonate a worker's lifecycle.
#
# Because no permission implies another, each of these must be granted
# explicitly. That is the point: a reviewer reading an ACE sees exactly what it
# confers, which the old `write`-implies-`roles`-and-`workflow` bridge did not.


PERMISSIONS: FrozenSet[str] = frozenset(
    {
        Permission.READ,
        Permission.APPEND,
        Permission.CREATE,
        Permission.UPDATE,
        Permission.WRITE,
        Permission.STOP,
        Permission.START,
        Permission.CONTROL,
        Permission.GRANT,
    }
)


# --- resource paths --------------------------------------------------------
#
# One function per resource kind, and exactly one canonical path per object.
# Two places that both compute paths is how a scheme like this drifts into
# disagreeing with itself, so callers must come through here.

FLEET = "/fleet"


def normalize_path(path: str) -> str:
    """Canonical form: leading slash, no trailing slash, no empty segments."""

    if not isinstance(path, str) or not path.strip():
        raise AclError("resource path must be a non-empty string")
    parts = [p for p in path.strip().split("/") if p]
    if not parts:
        raise AclError("resource path must not be empty")
    return "/" + "/".join(parts)


def fleet_path() -> str:
    return FLEET


def _segment(kind: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AclError("%s identifier must be a non-empty string" % kind)
    if "/" in value:
        raise AclError("%s identifier must not contain '/': %r" % (kind, value))
    return value.strip()


def project_path(project: str) -> str:
    return "%s/project/%s" % (FLEET, _segment("project", project))


def task_path(project: str, task_id: str) -> str:
    return "%s/task/%s" % (project_path(project), _segment("task", task_id))


def task_evidence_path(project: str, task_id: str) -> str:
    return "%s/evidence" % task_path(project, task_id)


def agent_path(agent_id: str) -> str:
    return "%s/agent/%s" % (FLEET, _segment("agent", agent_id))


def machine_path(machine_id: str) -> str:
    return "%s/machine/%s" % (FLEET, _segment("machine", machine_id))


def secret_path(name: str) -> str:
    return "%s/secret/%s" % (FLEET, _segment("secret", name))


def workflow_path(workflow_id: str) -> str:
    return "%s/workflow/%s" % (FLEET, _segment("workflow", workflow_id))


def ancestors(path: str) -> List[str]:
    """``path`` and every ancestor, longest first.

    Longest-first is the evaluation order, and it is derived from the path
    rather than from the order entries were written or loaded. That is the
    whole point: the model it replaces resolved ties by position in an if/elif
    chain, where a broad early prefix shadowed a narrow later one and nothing
    detected it.
    """

    canonical = normalize_path(path)
    parts = canonical.strip("/").split("/")
    out = []
    for i in range(len(parts), 0, -1):
        out.append("/" + "/".join(parts[:i]))
    return out


# --- entries ---------------------------------------------------------------


@dataclass(frozen=True)
class AccessControlEntry:
    """One (principal, resource, permission, allow|deny) statement.

    ``principal`` is a principal id or a role id; the evaluator resolves role
    membership before matching, so a role is simply a principal that other
    principals inherit from -- users and groups, as on a filesystem.
    """

    principal: str
    resource: str
    permission: str
    allow: bool = True

    def __post_init__(self) -> None:
        if not self.principal or not isinstance(self.principal, str):
            raise AclError("entry requires a principal")
        if self.permission not in PERMISSIONS:
            raise AclError(
                "unknown permission %r (known: %s)"
                % (self.permission, ", ".join(sorted(PERMISSIONS)))
            )
        object.__setattr__(self, "resource", normalize_path(self.resource))


@dataclass(frozen=True)
class Grant:
    """Why a decision came out the way it did.

    ``inherited_from`` is the path the deciding entry was written at, which is
    the ancestor of the requested resource when the grant was inherited. ADR
    0019 requires this: inheritance makes a high grant powerful and easy to
    make carelessly, so "you may do this, because of an entry at /fleet" has to
    be answerable without reading storage by hand.
    """

    allowed: bool
    permission: str
    resource: str
    inherited_from: Optional[str] = None
    via_role: Optional[str] = None
    reason: str = ""

    @property
    def inherited(self) -> bool:
        return bool(self.inherited_from) and self.inherited_from != self.resource


class AclEvaluator:
    """Decides (principal, resource, permission) against a set of entries."""

    def __init__(
        self,
        entries: Iterable[AccessControlEntry],
        roles: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> None:
        self._entries: Tuple[AccessControlEntry, ...] = tuple(entries)
        self._roles: Dict[str, Tuple[str, ...]] = {
            str(k): tuple(str(v) for v in vals) for k, vals in (roles or {}).items()
        }

    # -- principal expansion ------------------------------------------------

    def _principals_for(self, principal: str) -> List[str]:
        """``principal`` plus its roles, transitively, without cycling."""

        seen: List[str] = []
        stack = [str(principal)]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.append(current)
            stack.extend(self._roles.get(current, ()))
        return seen

    # -- decision -----------------------------------------------------------

    def check(self, principal: str, resource: str, permission: str) -> Grant:
        if permission not in PERMISSIONS:
            raise AclError("unknown permission %r" % permission)
        target = normalize_path(resource)
        holders = self._principals_for(principal)

        # Longest path first. The first path with ANY matching entry decides,
        # and within that path a deny beats an allow. Nothing below this loop
        # depends on the order entries were supplied.
        for path in ancestors(target):
            matches = [
                e
                for e in self._entries
                if e.permission == permission and e.resource == path and e.principal in holders
            ]
            if not matches:
                continue
            denial = next((m for m in matches if not m.allow), None)
            decided = denial or matches[0]
            return Grant(
                allowed=decided.allow,
                permission=permission,
                resource=target,
                inherited_from=path,
                via_role=(decided.principal if decided.principal != principal else None),
                reason=(
                    "explicit deny at %s" % path
                    if denial is not None
                    else "allowed by entry at %s" % path
                ),
            )

        # Deny by default. An unmapped resource is refused, not defaulted to
        # some ambient scope.
        return Grant(
            allowed=False,
            permission=permission,
            resource=target,
            inherited_from=None,
            reason="no matching entry (deny by default)",
        )

    def allows(self, principal: str, resource: str, permission: str) -> bool:
        return self.check(principal, resource, permission).allowed

    # -- required tooling (ADR 0019 makes these part of the mechanism) ------

    def effective_permissions(self, principal: str, resource: str) -> Dict[str, Grant]:
        """Every permission on one resource, each with its provenance."""

        return {p: self.check(principal, resource, p) for p in sorted(PERMISSIONS)}

    def who_can_reach(self, resource: str, permission: str) -> List[str]:
        """Principals allowed ``permission`` on ``resource``.

        Role members are expanded, so this answers "who can actually do this",
        not "which entries mention it" -- the question an operator is really
        asking, and the one an entry listing answers misleadingly.
        """

        candidates = {e.principal for e in self._entries}
        for role, members in self._roles.items():
            candidates.add(role)
            candidates.update(members)
        # A principal reaches a resource through its roles, so ask about every
        # known principal rather than only those named on a matching entry.
        return sorted(p for p in candidates if self.allows(p, resource, permission))
