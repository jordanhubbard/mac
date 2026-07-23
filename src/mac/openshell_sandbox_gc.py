"""Safe garbage collection for orphaned MAC-owned OpenShell sandboxes."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


DEFAULT_STALE_AFTER_SECONDS = 24 * 60 * 60
MANAGED_NAME_RE = re.compile(
    r"^mac-(?:task|hubverify|codingcap|runtime-smoke|security-probe)-[A-Za-z0-9._-]+$"
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _created_at(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale_sandbox_candidates(
    sandboxes: Iterable[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    include_legacy: bool = True,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> List[Dict[str, Any]]:
    """Return old, inactive, MAC-owned sandboxes that are safe to delete.

    New sandboxes carry ``mac.owner``, ``mac.kind`` and ``mac.pid`` labels.
    A live creator PID or ``mac.keep=true`` protects the sandbox. Older
    deployments did not add labels, so exact historical MAC name prefixes are
    accepted only when ``include_legacy`` is enabled and the age threshold has
    elapsed.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    minimum_age = max(0.0, float(stale_after_seconds))
    candidates: List[Dict[str, Any]] = []
    for raw in sandboxes:
        row = dict(raw)
        name = str(row.get("name") or "").strip()
        if not MANAGED_NAME_RE.fullmatch(name):
            continue
        if str(row.get("phase") or "").strip().lower() != "ready":
            continue
        created = _created_at(row.get("created_at"))
        if created is None:
            continue
        age_seconds = max(0.0, (current - created).total_seconds())
        if age_seconds < minimum_age:
            continue

        labels_value = row.get("labels")
        labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}
        owner = str(labels.get("mac.owner") or "").strip().lower()
        if owner:
            if owner != "mac" or _truthy(labels.get("mac.keep")):
                continue
            raw_pid = str(labels.get("mac.pid") or "").strip()
            if raw_pid:
                try:
                    if pid_is_alive(int(raw_pid)):
                        continue
                except ValueError:
                    continue
        elif not include_legacy:
            continue

        row["age_seconds"] = int(age_seconds)
        row["legacy"] = not bool(owner)
        candidates.append(row)
    return candidates


def reconcile_stale_sandboxes(
    *,
    openshell_bin: str = "openshell",
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    include_legacy: bool = True,
    apply: bool = False,
    now: Optional[datetime] = None,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Dict[str, Any]:
    """List and optionally delete stale MAC-owned OpenShell sandboxes."""

    listed = subprocess.run(
        [
            openshell_bin,
            "sandbox",
            "list",
            "--limit",
            "1000",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()
        raise RuntimeError("OpenShell sandbox list failed: %s" % detail[-1000:])
    try:
        payload = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenShell sandbox list returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("OpenShell sandbox list JSON is not an array")

    candidates = stale_sandbox_candidates(
        payload,
        now=now,
        stale_after_seconds=stale_after_seconds,
        include_legacy=include_legacy,
        pid_is_alive=pid_is_alive,
    )
    deleted: List[str] = []
    failures: List[Dict[str, str]] = []
    if apply:
        for row in candidates:
            name = str(row["name"])
            proc = subprocess.run(
                [openshell_bin, "sandbox", "delete", name],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode == 0:
                deleted.append(name)
            else:
                failures.append(
                    {
                        "name": name,
                        "error": (proc.stderr or proc.stdout or "").strip()[-1000:],
                    }
                )

    return {
        "schema": "mac.openshell.sandbox_gc.v1",
        "dry_run": not apply,
        "stale_after_seconds": int(max(0.0, float(stale_after_seconds))),
        "include_legacy": bool(include_legacy),
        "scanned": len(payload),
        "candidates": candidates,
        "deleted": deleted,
        "failures": failures,
    }


# --- Fail-closed dead-PID orphan reaper --------------------------------------
#
# The stale reaper above is age-gated and opt-in; it exists to sweep old,
# possibly-legacy sandboxes on a slow cadence. It does NOT solve the drain /
# orphan lifecycle gap observed during synchronized static rollouts: a completed
# task can leave a *Ready* OpenShell sandbox whose owning executor has already
# exited, so the recorded creator PID is dead. The deployment quiescence gate
# correctly refuses to delete such a sandbox (the deployer does not own task
# sandboxes) and then waits until timeout.
#
# The reaper below closes that gap the moment any executor starts a sandbox
# lifecycle. It is deliberately FAIL-CLOSED: a sandbox is reaped only when every
# one of the following is positively proven, and is otherwise left untouched.
#
#   * name matches an exact MAC-managed prefix
#   * ``mac.owner`` == ``mac`` (exact, case-insensitive)
#   * ``mac.kind`` is a recognized managed kind
#   * ``mac.keep`` is present and falsey (never reap when missing or truthy)
#   * ``mac.pid`` is a positive integer whose process is dead
#
# Unlike the stale reaper there is NO age threshold and NO legacy (unlabeled)
# acceptance: a sandbox that cannot prove full, valid MAC ownership plus a dead
# recorded PID is never reaped by this path.

MANAGED_KINDS = frozenset(
    {"task", "hubverify", "codingcap", "runtime-smoke", "security-probe"}
)

_FALSEY_KEEP = {"0", "false", "no", "off"}


def _keep_is_falsey(value: Any) -> bool:
    """Return True only when ``mac.keep`` is present and explicitly falsey.

    A missing/blank ``mac.keep`` is treated as protective (not falsey) so the
    reaper fails closed on partially-labeled sandboxes.
    """

    text = str(value if value is not None else "").strip().lower()
    if not text:
        return False
    return text in _FALSEY_KEEP


def classify_orphan_task_sandbox(
    sandbox: Mapping[str, Any],
    *,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Dict[str, Any]:
    """Classify a single sandbox for fail-closed dead-PID reaping.

    Returns a secret-free record with ``reap`` (bool), the observed ownership
    signals, and a ``reason`` describing why it is or is not eligible. The record
    never carries label *values* beyond the ownership fields the decision is
    based on, so it is safe to record as evidence.
    """

    row = dict(sandbox)
    name = str(row.get("name") or "").strip()
    labels_value = row.get("labels")
    labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}

    owner = str(labels.get("mac.owner") or "").strip().lower()
    kind = str(labels.get("mac.kind") or "").strip().lower()
    keep_raw = labels.get("mac.keep")
    pid_raw = str(labels.get("mac.pid") or "").strip()
    phase = str(row.get("phase") or "").strip()

    record: Dict[str, Any] = {
        "name": name,
        "phase": phase,
        "owner": owner,
        "kind": kind,
        "keep": str(keep_raw if keep_raw is not None else "").strip().lower(),
        "pid": pid_raw,
        "reap": False,
        "reason": "",
    }

    if not name or not MANAGED_NAME_RE.fullmatch(name):
        record["reason"] = "name is not an exact MAC-managed sandbox"
        return record
    if owner != "mac":
        record["reason"] = "mac.owner is not exactly 'mac'"
        return record
    if kind not in MANAGED_KINDS:
        record["reason"] = "mac.kind is missing or not a managed kind"
        return record
    if _truthy(keep_raw):
        record["reason"] = "mac.keep is truthy (protected)"
        return record
    if not _keep_is_falsey(keep_raw):
        record["reason"] = "mac.keep is missing or not explicitly falsey"
        return record
    if not pid_raw:
        record["reason"] = "mac.pid label is missing"
        return record
    try:
        pid = int(pid_raw)
    except ValueError:
        record["reason"] = "mac.pid is not an integer"
        return record
    if pid <= 0:
        record["reason"] = "mac.pid is not a positive integer"
        return record
    record["pid"] = pid
    if pid_is_alive(pid):
        record["reason"] = "recorded creator PID is still alive"
        return record

    record["reap"] = True
    record["reason"] = "MAC-owned task sandbox with mac.keep=false and dead recorded PID"
    return record


def orphan_task_sandbox_candidates(
    sandboxes: Iterable[Mapping[str, Any]],
    *,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> List[Dict[str, Any]]:
    """Return fail-closed classification records for reap-eligible sandboxes."""

    return [
        record
        for record in (
            classify_orphan_task_sandbox(row, pid_is_alive=pid_is_alive)
            for row in sandboxes
        )
        if record["reap"]
    ]


def reap_orphaned_task_sandboxes(
    *,
    openshell_bin: str = "openshell",
    apply: bool = False,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Dict[str, Any]:
    """List and optionally delete orphaned MAC-owned task sandboxes.

    Only exact MAC-owned sandboxes with ``mac.keep=false`` and a dead recorded
    ``mac.pid`` are reaped. Live PIDs, ``mac.keep=true``, and missing/invalid
    ownership labels are always preserved (fail-closed). The returned evidence is
    secret-free: it records only names, phases, ownership signals, and reasons.
    """

    listed = subprocess.run(
        [
            openshell_bin,
            "sandbox",
            "list",
            "--limit",
            "1000",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()
        raise RuntimeError("OpenShell sandbox list failed: %s" % detail[-1000:])
    try:
        payload = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenShell sandbox list returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("OpenShell sandbox list JSON is not an array")

    classified = [
        classify_orphan_task_sandbox(row, pid_is_alive=pid_is_alive)
        for row in payload
        if isinstance(row, Mapping)
    ]
    candidates = [record for record in classified if record["reap"]]
    protected = len(classified) - len(candidates)

    deleted: List[str] = []
    failures: List[Dict[str, str]] = []
    if apply:
        for record in candidates:
            name = str(record["name"])
            proc = subprocess.run(
                [openshell_bin, "sandbox", "delete", name],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode == 0:
                deleted.append(name)
            else:
                failures.append(
                    {
                        "name": name,
                        "error": (proc.stderr or proc.stdout or "").strip()[-1000:],
                    }
                )

    return {
        "schema": "mac.openshell.sandbox_orphan_reap.v1",
        "dry_run": not apply,
        "scanned": len(payload),
        "protected": protected,
        "candidates": candidates,
        "deleted": deleted,
        "failures": failures,
    }
