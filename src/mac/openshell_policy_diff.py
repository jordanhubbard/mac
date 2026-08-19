"""Structural diff of two OpenShell guardrail policies, and its announcement.

Why this exists as a *structural* diff rather than a checksum comparison or a
text diff: the question a running agent has to answer when the sandbox policy
moves under it is not "did it change" but "which DIRECTION did it change in".

* The new policy **expands** — an endpoint, a binary, or a filesystem path was
  added. A sandbox created from the old policy is still compliant with the
  reviewed intent; it is merely less capable than a freshly created one.
* The new policy **restricts** — something the running sandbox still holds was
  revoked. The sandbox is now over-permissioned relative to what a human
  approved, and that is the case a boolean "changed" flag cannot express.

Both can be true at once (a policy that swaps one endpoint for another), and
that is deliberately representable: it is a revocation *and* a grant, and a
consumer that treats it as merely "changed" would keep working under the
revoked capability.

The comparison is over the parsed YAML, never the text. Reordering blocks,
reflowing lists, or editing a comment changes every line of a text diff and
revokes nothing; that is exactly the false alarm that would train a worker to
ignore the signal.

Filesystem paths carry their access mode (``rw:/tmp`` vs ``ro:/tmp``) because
demoting a path from writable to read-only is a revocation, not a rename.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from mac.models import JsonDict

POLICY_DIFF_SCHEMA = "mac.openshell.policy.diff.v1"

#: ``change_kind`` values: what happened to the policy, not what it means.
POLICY_CHANGE_KINDS: Tuple[str, ...] = (
    "published",
    "updated",
    "assigned",
    "unassigned",
    "deleted",
)

#: ``action_hint`` values: what a worker should DO about it. Deliberately a
#: closed, tiny set — a hint a consumer has to parse is not a hint.
ACTION_NO_ACTION = "no_action"
ACTION_RECREATE = "recreate_before_next_task"
ACTION_ABANDON = "abandon_current"
POLICY_ACTION_HINTS: Tuple[str, ...] = (
    ACTION_NO_ACTION,
    ACTION_RECREATE,
    ACTION_ABANDON,
)


def _load(policy_text: Optional[str]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Parse policy YAML. Returns ``(mapping, parsed_ok)``.

    Never raises: this runs on the mutating path (a policy write already
    committed), and an unparseable policy must not turn a successful update
    into a failure. An unparsed side is reported instead, and the caller fails
    SAFE by treating the change as restricting.
    """
    if policy_text is None:
        # "No policy" is a KNOWN state, not an unreadable one: it grants
        # nothing. Conflating it with a parse failure would mark every first
        # assignment as a revocation.
        return {}, True
    try:
        parsed = yaml.safe_load(policy_text)
    except yaml.YAMLError:
        return None, False
    if parsed is None:
        return {}, True
    if not isinstance(parsed, dict):
        return None, False
    return parsed, True


def _endpoint_key(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        host = entry.get("host") or entry.get("domain") or entry.get("name")
        if host is None:
            return None
        port = entry.get("port")
        return "%s:%s" % (str(host).strip(), "" if port is None else str(port).strip())
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    return None


def _binary_key(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        path = entry.get("path") or entry.get("binary") or entry.get("name")
        return str(path).strip() if path is not None else None
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    return None


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def policy_capabilities(policy_text: Optional[str]) -> JsonDict:
    """The capability sets a sandbox built from ``policy_text`` would hold.

    Returns ``{"endpoints": set, "binaries": set, "paths": set, "parsed": bool}``.
    Sets are global across ``network_policies`` blocks: renaming a block moves
    no capability, and treating it as one would report a revocation that did
    not happen.
    """
    parsed, ok = _load(policy_text)
    endpoints: Set[str] = set()
    binaries: Set[str] = set()
    paths: Set[str] = set()
    if not ok or parsed is None:
        return {
            "endpoints": endpoints,
            "binaries": binaries,
            "paths": paths,
            "parsed": False,
        }
    network = parsed.get("network_policies")
    if isinstance(network, dict):
        for block in network.values():
            if not isinstance(block, dict):
                continue
            for entry in _as_list(block.get("endpoints")):
                key = _endpoint_key(entry)
                if key:
                    endpoints.add(key)
            for entry in _as_list(block.get("binaries")):
                key = _binary_key(entry)
                if key:
                    binaries.add(key)
    filesystem = parsed.get("filesystem_policy")
    if not isinstance(filesystem, dict):
        filesystem = parsed.get("landlock") if isinstance(parsed.get("landlock"), dict) else {}
    if isinstance(filesystem, dict):
        for mode, key in (("ro", "read_only"), ("rw", "read_write"), ("x", "execute")):
            for entry in _as_list(filesystem.get(key)):
                if isinstance(entry, str) and entry.strip():
                    paths.add("%s:%s" % (mode, entry.strip()))
        if filesystem.get("include_workdir"):
            paths.add("rw:<workdir>")
    return {
        "endpoints": endpoints,
        "binaries": binaries,
        "paths": paths,
        "parsed": True,
    }


def diff_policy_texts(
    old_text: Optional[str], new_text: Optional[str]
) -> JsonDict:
    """Structural diff of two policies, in terms of DIRECTION.

    ``old_text``/``new_text`` may be ``None`` for "no policy" (first
    publication, or an unassignment/deletion that leaves the target with
    nothing). A side that will not parse yields ``parsed=False`` and is treated
    as restricting: an unknown guardrail must never read as safe to keep
    working under.
    """
    before = policy_capabilities(old_text)
    after = policy_capabilities(new_text)
    counts: JsonDict = {}
    restricts = False
    expands = False
    for field, key in (
        ("endpoints", "endpoints"),
        ("binaries", "binaries"),
        ("paths", "paths"),
    ):
        added = after[key] - before[key]
        removed = before[key] - after[key]
        counts["%s_added" % field] = len(added)
        counts["%s_removed" % field] = len(removed)
        restricts = restricts or bool(removed)
        expands = expands or bool(added)
    parsed = bool(before["parsed"] and after["parsed"])
    if not parsed:
        # Fail safe. We cannot show that nothing was revoked, so we must not
        # claim it.
        restricts = True
    return {
        "schema": POLICY_DIFF_SCHEMA,
        "restricts": restricts,
        "expands": expands,
        "parsed": parsed,
        **counts,
    }


def action_hint_for(change_kind: str, diff: JsonDict) -> str:
    """What a worker should do about this change, in one closed-set token.

    * A revocation, an unassignment, or a deletion → ``abandon_current``: the
      running sandbox holds something no longer approved.
    * A pure grant → ``recreate_before_next_task``: the running sandbox is
      still compliant, so nothing has to stop, but the next one should be
      built from the new policy.
    * Neither → ``no_action``: metadata moved, capabilities did not.
    """
    kind = str(change_kind or "")
    if kind in {"unassigned", "deleted"}:
        return ACTION_ABANDON
    if diff.get("restricts"):
        return ACTION_ABANDON
    if diff.get("expands"):
        return ACTION_RECREATE
    return ACTION_NO_ACTION


def policy_change_payload(
    *,
    change_kind: str,
    policy_id: str,
    policy_name: str = "",
    from_text: Optional[str] = None,
    to_text: Optional[str] = None,
    from_version: Optional[int] = None,
    to_version: Optional[int] = None,
    from_checksum: str = "",
    to_checksum: str = "",
    target_type: str = "policy",
    target_id: str = "",
) -> JsonDict:
    """The broadcast payload for one policy change.

    Deliberately all scalars, all small: the bus is announcements, not
    transport. Detail (the policy TEXT) is fetched by the agent through the
    existing policy routes, which already authorise per-agent access to it.
    """
    if change_kind not in POLICY_CHANGE_KINDS:
        raise ValueError("unknown OpenShell policy change_kind: %s" % change_kind)
    diff = diff_policy_texts(from_text, to_text)
    payload: JsonDict = {
        "change_kind": change_kind,
        "policy_id": policy_id,
        "policy_name": policy_name or "",
        "from_version": from_version,
        "to_version": to_version,
        "from_checksum": from_checksum or "",
        "to_checksum": to_checksum or "",
        "target_type": target_type,
        "target_id": target_id or "",
        "restricts": bool(diff["restricts"]),
        "expands": bool(diff["expands"]),
        "diff_parsed": bool(diff["parsed"]),
        "endpoints_added": diff["endpoints_added"],
        "endpoints_removed": diff["endpoints_removed"],
        "binaries_added": diff["binaries_added"],
        "binaries_removed": diff["binaries_removed"],
        "paths_added": diff["paths_added"],
        "paths_removed": diff["paths_removed"],
    }
    payload["action_hint"] = action_hint_for(change_kind, diff)
    return payload
