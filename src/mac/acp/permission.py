"""Permission policy bridge: ACP ``request_permission`` -> OpenShell policy.

This is the Phase-3 replacement for the Phase-1 blanket auto-approve. When mac
drives an external ACP agent, the agent asks for permission before a tool call
via ``session/request_permission``. mac must answer *yes/no* with a reason.

The evaluator here is **pure** (no I/O, no env reads beyond the explicit
``mode`` argument the caller resolves): it maps an ACP ``toolCall.kind`` to an
intent, then consults a parsed OpenShell policy dict to decide. The OpenShell
*kernel* sandbox is the real enforcement gate -- when a run is sandboxed the ACP
prompt is purely advisory, so a sandboxed run short-circuits to *allow*. The
policy consult only matters for the unsandboxed case, where the ACP decision is
the only gate mac has.

Decision matrix (``mode == "policy"``):

==========================  =========  ====================================
ACP toolCall.kind           intent     decision
==========================  =========  ====================================
fetch / *network*           NETWORK    allow iff policy.network_policies != {}
edit/delete/move/create/    FS-WRITE   allow iff policy.filesystem_policy
  write                                  .read_write != []
read / search / think       (benign)   allow
execute / unknown           (gated)    allow iff sandboxed, else deny
==========================  =========  ====================================

Short-circuits, regardless of kind:

* ``sandboxed`` True            -> allow ("sandbox-enforced")
* ``mode == "allow"``           -> allow ("mode-allow")
* ``mode == "deny"``            -> deny ("mode-deny")
* ``mode == "policy"`` + no policy -> allow ("no-policy-default-allow")

The no-policy default is **allow** to preserve the Phase-1 behavior (mac did not
deny tool calls before Phase 3). Flip the whole bridge to deny-by-default with
``MAC_ACP_PERMISSION_MODE=deny`` if you want a hard gate even without a policy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


__all__ = [
    "PermissionDecision",
    "PermissionMode",
    "permission_mode",
    "evaluate_permission",
    "load_openshell_policy",
    "NETWORK_KINDS",
    "FS_WRITE_KINDS",
    "BENIGN_KINDS",
]


@dataclass(frozen=True)
class PermissionDecision:
    """The outcome of evaluating one ACP permission request."""

    allow: bool
    reason: str


class PermissionMode:
    """Values for ``MAC_ACP_PERMISSION_MODE`` (the ``mode`` argument)."""

    POLICY = "policy"  # default: consult the OpenShell policy / sandbox
    ALLOW = "allow"  # always allow (Phase-1 parity, no gate)
    DENY = "deny"  # always deny (hard lockdown)


#: ACP ``toolCall.kind`` values that mean network egress (a NETWORK intent).
NETWORK_KINDS = frozenset({"fetch", "network", "request", "http", "download"})

#: ACP ``toolCall.kind`` values that mean a filesystem mutation (FS-WRITE).
FS_WRITE_KINDS = frozenset({"edit", "delete", "move", "create", "write"})

#: ACP ``toolCall.kind`` values that are read-only / harmless (always allowed).
BENIGN_KINDS = frozenset({"read", "search", "think"})


def permission_mode(raw: Optional[str] = None) -> str:
    """Resolve the active mode from ``raw`` (or ``MAC_ACP_PERMISSION_MODE``).

    Defaults to :data:`PermissionMode.POLICY`. Unknown values fall back to the
    default rather than failing, so a typo can't silently disable the gate in
    an unexpected direction.
    """

    value = (raw if raw is not None else os.environ.get("MAC_ACP_PERMISSION_MODE", "")).strip().lower()
    if value in {PermissionMode.POLICY, PermissionMode.ALLOW, PermissionMode.DENY}:
        return value
    return PermissionMode.POLICY


def _tool_call_kind(tool_call: Dict[str, Any]) -> str:
    """Best-effort extraction of the ACP ``kind`` from a tool_call dict."""

    return str((tool_call or {}).get("kind") or "").strip().lower()


def _network_allowed(policy: Dict[str, Any]) -> bool:
    """A non-empty ``network_policies`` permits egress; empty/absent == lockdown."""

    network = policy.get("network_policies")
    return isinstance(network, dict) and len(network) > 0


def _fs_write_allowed(policy: Dict[str, Any]) -> bool:
    """A non-empty ``filesystem_policy.read_write`` permits writes.

    Path-agnostic: ACP rarely hands us a concrete path on the permission
    request, so this is the best-effort "does the policy allow *any* writable
    location" check. The kernel sandbox still enforces the exact paths.
    """

    fs = policy.get("filesystem_policy")
    if not isinstance(fs, dict):
        return False
    read_write = fs.get("read_write")
    return isinstance(read_write, (list, tuple)) and len(read_write) > 0


def evaluate_permission(
    tool_call: Dict[str, Any],
    *,
    policy: Optional[Dict[str, Any]] = None,
    sandboxed: bool = False,
    mode: Optional[str] = None,
) -> PermissionDecision:
    """Decide whether to allow an ACP tool call. Pure; see the module matrix.

    ``tool_call`` is the ACP ``toolCall`` object (we read its ``kind``).
    ``policy`` is a parsed OpenShell policy dict (see
    :func:`load_openshell_policy`); ``None`` means no policy is available.
    ``sandboxed`` reflects whether the run is confined by the OpenShell kernel
    sandbox (the real gate). ``mode`` is the resolved permission mode (defaults
    to ``MAC_ACP_PERMISSION_MODE`` / ``policy``).
    """

    resolved_mode = permission_mode(mode)

    # The kernel sandbox is the authoritative gate; the ACP prompt is advisory.
    if sandboxed:
        return PermissionDecision(allow=True, reason="sandbox-enforced")

    if resolved_mode == PermissionMode.ALLOW:
        return PermissionDecision(allow=True, reason="mode-allow")
    if resolved_mode == PermissionMode.DENY:
        return PermissionDecision(allow=False, reason="mode-deny")

    # mode == policy from here.
    if not policy:
        # Preserve Phase-1 behavior: no policy means no per-tool gate. Flip with
        # MAC_ACP_PERMISSION_MODE=deny for a hard lockdown.
        return PermissionDecision(allow=True, reason="no-policy-default-allow")

    kind = _tool_call_kind(tool_call)

    if kind in BENIGN_KINDS:
        return PermissionDecision(allow=True, reason="benign-kind:%s" % (kind or "?"))

    if kind in NETWORK_KINDS:
        if _network_allowed(policy):
            return PermissionDecision(allow=True, reason="policy-network-allowed")
        return PermissionDecision(allow=False, reason="policy-network-lockdown")

    if kind in FS_WRITE_KINDS:
        if _fs_write_allowed(policy):
            return PermissionDecision(allow=True, reason="policy-fs-write-allowed")
        return PermissionDecision(allow=False, reason="policy-fs-write-readonly")

    # execute / unknown -> only safe under the kernel sandbox (handled above).
    return PermissionDecision(allow=False, reason="policy-unsandboxed-execute:%s" % (kind or "unknown"))


def load_openshell_policy(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load + parse the OpenShell policy YAML, best-effort.

    Resolution: ``path`` -> ``MAC_OPENSHELL_POLICY`` -> ``~/.mac/openshell-
    policy.yaml`` (mirrors ``task_executor._resolve_openshell_policy`` but never
    raises and never falls back to the bundled default — a missing/unreadable
    policy yields ``None`` so the caller treats it as "no policy"). ``yaml`` is
    already a mac dependency.
    """

    candidate: Optional[Path]
    if path:
        candidate = Path(path)
    else:
        explicit = (os.environ.get("MAC_OPENSHELL_POLICY") or "").strip()
        if explicit:
            candidate = Path(explicit)
        else:
            candidate = Path.home() / ".mac" / "openshell-policy.yaml"

    try:
        if not candidate.is_file():
            return None
        import yaml

        parsed = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - best-effort: any failure == no policy
        return None
    return parsed if isinstance(parsed, dict) else None
