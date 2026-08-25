"""Safe garbage collection for orphaned MAC-owned OpenShell sandboxes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


DEFAULT_STALE_AFTER_SECONDS = 24 * 60 * 60
#: An errored sandbox is already terminal, so it only needs long enough for
#: its creator to read the logs -- not the full stale window a working
#: sandbox is given.
DEFAULT_ERROR_GRACE_SECONDS = 15 * 60
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
    error_grace_seconds: float = DEFAULT_ERROR_GRACE_SECONDS,
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
        # A sandbox that FAILED is the common leak, and it was the one phase
        # nothing could ever collect: this filter accepted "ready" only, so a
        # sandbox that died on its way up stayed forever. Measured on the hub:
        # 77 sandboxes, 60 of them in Error, 49 of those from hub verification
        # -- none collectable at any age.
        #
        # An errored sandbox will not become useful, so it needs no age
        # heuristic to prove abandonment; it needs only a grace window long
        # enough for whoever created it to read its logs.
        phase = str(row.get("phase") or "").strip().lower()
        if phase not in {"ready", "error", "failed"}:
            continue
        errored = phase in {"error", "failed"}
        created = _created_at(row.get("created_at"))
        if created is None:
            continue
        age_seconds = max(0.0, (current - created).total_seconds())

        labels_value = row.get("labels")
        labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}
        owner = str(labels.get("mac.owner") or "").strip().lower()
        # A dead creator is PROOF the sandbox is garbage; age is only a guess
        # for the sandboxes we cannot prove anything about. Testing age first
        # inverted that: a sandbox whose creator had demonstrably exited was
        # protected for a full day anyway.
        #
        # That is a leak, not an inefficiency. The hub creates one sandbox per
        # verification, ticks every 30 seconds, and kills the creator when the
        # gate exceeds its timeout -- so every tick left a Ready sandbox that
        # nothing would collect for 24 hours. Observed on the hub: 10
        # abandoned sandboxes, then 87 within a few hours of the tick
        # resuming, each holding a container.
        proven_abandoned = False
        if owner:
            if owner != "mac" or _truthy(labels.get("mac.keep")):
                continue
            raw_pid = str(labels.get("mac.pid") or "").strip()
            if raw_pid:
                try:
                    if pid_is_alive(int(raw_pid)):
                        # A live creator protects work in progress -- but an
                        # errored sandbox is terminal, and the hub's creator PID
                        # is the long-lived hub itself, so honouring it there
                        # would protect every failure for the hub's whole life.
                        if not errored:
                            continue
                    else:
                        proven_abandoned = True
                except ValueError:
                    continue
        elif not include_legacy:
            continue

        # Unlabelled or still-unproven sandboxes keep the age heuristic: a
        # creator we cannot identify might yet be working. An errored sandbox
        # is not working by definition, so it waits only the grace window.
        threshold = error_grace_seconds if errored else minimum_age
        if not proven_abandoned and age_seconds < threshold:
            continue

        row["age_seconds"] = int(age_seconds)
        row["legacy"] = not bool(owner)
        candidates.append(row)
    return candidates


def reconcile_stale_sandboxes(
    *,
    openshell_bin: str = "openshell",
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    error_grace_seconds: float = DEFAULT_ERROR_GRACE_SECONDS,
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
        error_grace_seconds=error_grace_seconds,
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

MANAGED_KINDS = frozenset({"task", "hubverify", "codingcap", "runtime-smoke", "security-probe"})

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
            classify_orphan_task_sandbox(row, pid_is_alive=pid_is_alive) for row in sandboxes
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


# --- Durable lease-authority orphan reconciler -------------------------------
#
# The dead-PID reaper above proves orphanhood from the *local* creator process.
# That signal is host-local: a Ready task sandbox left behind by an executor
# that exited cleanly, crashed, or ran on a now-unreachable host has a recorded
# ``mac.pid`` that is either dead-but-unprovable or belongs to an unrelated
# process on the reaping host. The authoritative source of truth for whether a
# task is still being worked is the durable lease store, exactly as the k8s
# controller reconciles stuck Jobs (see ``mac.k8s.controller``).
#
# This reconciler stamps the same fail-closed discipline onto lease authority: a
# task sandbox is reaped only when the lease store *positively* proves the work
# is gone. A sandbox is reaped when, for its recorded ``mac.task.id``:
#
#   * the task is in a terminal state (completed / failed / cancelled), OR
#   * the task has no active lease, OR
#   * the task's active lease differs from the sandbox's ``mac.lease.id``
#     (the lease was superseded by a newer claim), OR
#   * the task's lease has expired (``leased_until`` is in the past).
#
# Anything that cannot be proven — a missing ``mac.task.id`` label, a task the
# store cannot resolve, ``mac.keep`` truthy, or a live matching lease — is left
# untouched. Task lookup is injected so this stays decoupled from any concrete
# store/API and secret-free in its evidence.

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled", "canceled"})


def _lease_expired(leased_until: Any, now: datetime) -> bool:
    parsed = _created_at(leased_until)
    if parsed is None:
        return False
    return parsed < now


def classify_lease_orphan_sandbox(
    sandbox: Mapping[str, Any],
    task: Optional[Mapping[str, Any]],
    *,
    now: datetime,
) -> Dict[str, Any]:
    """Classify a single task sandbox against durable lease authority.

    ``task`` is the authoritative task record (or ``None`` when the store could
    not resolve the recorded ``mac.task.id``). Returns a secret-free record with
    ``reap`` (bool), the ownership signals used, and a ``reason``. Label *values*
    beyond the ownership/identity fields the decision is based on are never
    recorded, so the result is safe to emit as evidence.
    """

    row = dict(sandbox)
    name = str(row.get("name") or "").strip()
    labels_value = row.get("labels")
    labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}

    owner = str(labels.get("mac.owner") or "").strip().lower()
    kind = str(labels.get("mac.kind") or "").strip().lower()
    keep_raw = labels.get("mac.keep")
    task_id = str(labels.get("mac.task.id") or "").strip()
    lease_id = str(labels.get("mac.lease.id") or "").strip()
    phase = str(row.get("phase") or "").strip()

    record: Dict[str, Any] = {
        "name": name,
        "phase": phase,
        "owner": owner,
        "kind": kind,
        "keep": str(keep_raw if keep_raw is not None else "").strip().lower(),
        "task_id": task_id,
        "lease_id": lease_id,
        "reap": False,
        "reason": "",
    }

    if not name or not MANAGED_NAME_RE.fullmatch(name):
        record["reason"] = "name is not an exact MAC-managed sandbox"
        return record
    if owner != "mac":
        record["reason"] = "mac.owner is not exactly 'mac'"
        return record
    if kind != "task":
        record["reason"] = "mac.kind is not 'task'"
        return record
    if _truthy(keep_raw):
        record["reason"] = "mac.keep is truthy (protected)"
        return record
    if not task_id:
        record["reason"] = "mac.task.id label is missing"
        return record
    if task is None:
        record["reason"] = "task could not be resolved from the lease store"
        return record

    state = str(task.get("state") or "").strip().lower()
    if state in TERMINAL_TASK_STATES:
        record["reap"] = True
        record["reason"] = "task is in terminal state %r" % state
        return record

    active_lease_id = str(task.get("lease_id") or "").strip()
    if not active_lease_id:
        record["reap"] = True
        record["reason"] = "task has no active lease"
        return record
    if lease_id and active_lease_id != lease_id:
        record["reap"] = True
        record["reason"] = "sandbox lease was superseded by a newer task lease"
        return record
    if _lease_expired(task.get("leased_until"), now):
        record["reap"] = True
        record["reason"] = "task lease has expired"
        return record

    record["reason"] = "task has a live matching lease"
    return record


def lease_orphan_task_sandbox_candidates(
    sandboxes: Iterable[Mapping[str, Any]],
    lookup_task: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return reap-eligible records for task sandboxes orphaned per lease authority.

    ``lookup_task`` resolves a ``mac.task.id`` to its authoritative task record
    (or ``None`` when unknown). Lookup failures fail closed: the sandbox is kept
    and its reason records the lookup error.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    records: List[Dict[str, Any]] = []
    for raw in sandboxes:
        if not isinstance(raw, Mapping):
            continue
        labels_value = raw.get("labels")
        labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}
        task_id = str(labels.get("mac.task.id") or "").strip()
        task: Optional[Mapping[str, Any]] = None
        lookup_error = ""
        if task_id:
            try:
                task = lookup_task(task_id)
            except Exception as exc:  # noqa: BLE001 - fail closed on lookup failure
                lookup_error = str(exc)
                task = None
        record = classify_lease_orphan_sandbox(raw, task, now=current)
        if lookup_error and not record["reap"]:
            record["reason"] = "task lookup failed: %s" % lookup_error[-200:]
        if record["reap"]:
            records.append(record)
    return records


def reconcile_task_sandboxes_from_lease_authority(
    lookup_task: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    openshell_bin: str = "openshell",
    apply: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """List and optionally delete task sandboxes orphaned per durable lease authority.

    A task sandbox is reaped only when the lease store proves the work is gone:
    the task is terminal, unleased, its lease was superseded, or its lease has
    expired. Missing identity labels, unresolvable tasks, ``mac.keep=true``, and
    live matching leases are always preserved (fail-closed). ``lookup_task``
    resolves ``mac.task.id`` to the authoritative task record; the returned
    evidence is secret-free.
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

    candidates = lease_orphan_task_sandbox_candidates(payload, lookup_task, now=now)
    protected = sum(1 for row in payload if isinstance(row, Mapping)) - len(candidates)

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
        "schema": "mac.openshell.sandbox_lease_reconcile.v1",
        "dry_run": not apply,
        "scanned": len(payload),
        "protected": max(0, protected),
        "candidates": candidates,
        "deleted": deleted,
        "failures": failures,
    }


# --- Exact-identity single-sandbox delete ------------------------------------
#
# When a task completes, its executor owns exactly one sandbox: its own, whose
# name it already knows. Cleaning that up is an *exact-identity* operation — the
# caller names the sandbox and this helper deletes only that name. It never
# lists, never scans by age, and never touches anything the caller did not name,
# so it cannot race with or clobber another task's live sandbox.
#
# ``openshell sandbox delete`` is treated as idempotent: a delete that fails
# because the sandbox is already gone is a success (nothing left to reap). Only
# transient failures are retried, a small bounded number of times with linear
# backoff. The returned record is secret-free: it carries the sandbox name, the
# number of attempts, whether it is now deleted, and a truncated terminal error
# tail only.

DEFAULT_DELETE_ATTEMPTS = 3
DEFAULT_DELETE_BACKOFF_SECONDS = 0.5

_ALREADY_GONE_MARKERS = ("not found", "no such", "does not exist", "notfound")


def _looks_already_gone(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ALREADY_GONE_MARKERS)


def delete_named_sandbox(
    name: str,
    *,
    openshell_bin: str = "openshell",
    attempts: int = DEFAULT_DELETE_ATTEMPTS,
    backoff_seconds: float = DEFAULT_DELETE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    runner: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
) -> Dict[str, Any]:
    """Delete exactly one named sandbox, with bounded retry on transient failure.

    Only the sandbox named by the caller is deleted (``openshell sandbox delete
    <name>``); nothing is listed and nothing is age-scanned. A delete that
    reports the sandbox is already gone counts as success. Other failures are
    retried up to ``attempts`` times with linear backoff. Returns a secret-free
    record: ``name``, ``attempts`` made, ``deleted`` (bool), and a truncated
    terminal ``error`` tail (empty on success).
    """

    clean_name = str(name or "").strip()
    max_attempts = max(1, int(attempts))
    record: Dict[str, Any] = {
        "schema": "mac.openshell.sandbox_delete.v1",
        "name": clean_name,
        "attempts": 0,
        "deleted": False,
        "error": "",
    }
    if not clean_name:
        record["error"] = "sandbox name is empty"
        return record
    run = runner if runner is not None else subprocess.run
    delay = max(0.0, float(backoff_seconds))
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        record["attempts"] = attempt
        try:
            proc = run(
                [openshell_bin, "sandbox", "delete", clean_name],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - retry on transient runner failure
            last_error = str(exc)
        else:
            if proc.returncode == 0:
                record["deleted"] = True
                record["error"] = ""
                return record
            last_error = (proc.stderr or proc.stdout or "").strip()
            if _looks_already_gone(last_error):
                record["deleted"] = True
                record["error"] = ""
                return record
        if attempt < max_attempts and delay > 0:
            sleep(delay * attempt)

    record["error"] = last_error[-1000:]
    return record


# --- Low-cadence positively-identified leftover reconciler -------------------
#
# The dead-PID reaper and the lease-authority reconciler above each prove
# orphanhood from one signal. A slow background sweep wants the *intersection*:
# a sandbox reaped here must satisfy BOTH the fail-closed dead-PID ownership
# proof (exact name, mac.owner==mac, mac.kind in MANAGED_KINDS, mac.keep
# explicitly falsey, dead mac.pid) AND the lease-authority proof (task terminal,
# unleased, superseded, or expired). Requiring both keeps this the most
# conservative path: anything either guard would preserve is preserved here.
#
# The candidate set is deterministic and idempotent. Classification depends only
# on the input payload and the injected task lookup, results are sorted by name,
# and duplicate names collapse to one record — so repeated runs over the same
# input reap the same names, and a re-run after deletion (the names are gone
# from the payload) is a no-op.


def classify_leftover_task_sandbox(
    sandbox: Mapping[str, Any],
    task: Optional[Mapping[str, Any]],
    *,
    now: datetime,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Dict[str, Any]:
    """Classify a leftover using BOTH the dead-PID and lease-authority proofs.

    A sandbox is reaped only when ``classify_orphan_task_sandbox`` (dead-PID
    ownership proof) AND ``classify_lease_orphan_sandbox`` (lease authority)
    independently agree it is reap-eligible. Returns a secret-free record with
    ``reap`` (bool), the ownership signals, and both underlying reasons.
    """

    pid_record = classify_orphan_task_sandbox(sandbox, pid_is_alive=pid_is_alive)
    lease_record = classify_lease_orphan_sandbox(sandbox, task, now=now)

    record: Dict[str, Any] = {
        "name": pid_record["name"],
        "phase": pid_record["phase"],
        "owner": pid_record["owner"],
        "kind": pid_record["kind"],
        "keep": pid_record["keep"],
        "pid": pid_record["pid"],
        "task_id": lease_record["task_id"],
        "lease_id": lease_record["lease_id"],
        "reap": False,
        "pid_reason": pid_record["reason"],
        "lease_reason": lease_record["reason"],
        "reason": "",
    }

    if not pid_record["reap"]:
        record["reason"] = "dead-PID proof withheld: %s" % pid_record["reason"]
        return record
    if not lease_record["reap"]:
        record["reason"] = "lease-authority proof withheld: %s" % lease_record["reason"]
        return record

    record["reap"] = True
    record["reason"] = "dead-PID and lease-authority proofs both confirm leftover"
    return record


def leftover_task_sandbox_candidates(
    sandboxes: Iterable[Mapping[str, Any]],
    lookup_task: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    now: Optional[datetime] = None,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> List[Dict[str, Any]]:
    """Return a deterministic, idempotent set of positively-identified leftovers.

    A sandbox is included only when both the dead-PID ownership proof and the
    lease-authority proof agree it is reap-eligible. The result is sorted by name
    and de-duplicated by name, so repeated runs over the same input reap the same
    names and a re-run after deletion is a no-op. ``lookup_task`` failures fail
    closed (the sandbox is preserved).
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    by_name: Dict[str, Dict[str, Any]] = {}
    for raw in sandboxes:
        if not isinstance(raw, Mapping):
            continue
        labels_value = raw.get("labels")
        labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}
        task_id = str(labels.get("mac.task.id") or "").strip()
        task: Optional[Mapping[str, Any]] = None
        lookup_error = ""
        if task_id:
            try:
                task = lookup_task(task_id)
            except Exception as exc:  # noqa: BLE001 - fail closed on lookup failure
                lookup_error = str(exc)
                task = None
        record = classify_leftover_task_sandbox(raw, task, now=current, pid_is_alive=pid_is_alive)
        if lookup_error and not record["reap"]:
            record["lease_reason"] = "task lookup failed: %s" % lookup_error[-200:]
            record["reason"] = "lease-authority proof withheld: %s" % record["lease_reason"]
        if record["reap"] and record["name"] not in by_name:
            by_name[record["name"]] = record
    return [by_name[name] for name in sorted(by_name)]


def reconcile_leftover_task_sandboxes(
    lookup_task: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    openshell_bin: str = "openshell",
    apply: bool = False,
    now: Optional[datetime] = None,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Dict[str, Any]:
    """Low-cadence sweep reaping only leftovers proven by BOTH guards.

    Lists sandboxes once and reaps only those that both the dead-PID ownership
    proof and the lease-authority proof agree are leftovers. The candidate set is
    deterministic and idempotent (sorted, de-duplicated by name); re-running
    after deletion is a no-op. Returned evidence is secret-free.
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

    candidates = leftover_task_sandbox_candidates(
        payload, lookup_task, now=now, pid_is_alive=pid_is_alive
    )
    protected = sum(1 for row in payload if isinstance(row, Mapping)) - len(candidates)

    deleted: List[str] = []
    failures: List[Dict[str, str]] = []
    if apply:
        for record in candidates:
            result = delete_named_sandbox(str(record["name"]), openshell_bin=openshell_bin)
            if result["deleted"]:
                deleted.append(result["name"])
            else:
                failures.append({"name": result["name"], "error": result["error"]})

    return {
        "schema": "mac.openshell.sandbox_leftover_reconcile.v1",
        "dry_run": not apply,
        "scanned": len(payload),
        "protected": max(0, protected),
        "candidates": candidates,
        "deleted": deleted,
        "failures": failures,
    }


# --- Controller-owned lifecycle reconciler -----------------------------------
#
# The reconcilers above are triggered from the *executor* side (each worker
# reaps its own leftovers) or from age-based sweeps. Neither closes the gap the
# fleet actually hit: a task's OpenShell sandbox stays *Ready* on its original
# host after the task's ownership moved to another worker, the task was
# finalized or cancelled, or the worker was replaced. The original executor is
# gone (or now owns unrelated work), so nothing on that host ever cleans the
# sandbox up, and a Ready-but-orphaned sandbox blocks the deployment quiescence
# gate -- exactly the observed worker6 / worker7 rollout stall.
#
# This reconciler is meant to run inside the *controller* (the component that
# already owns the authoritative task/lease store), driven by a lifecycle
# trigger rather than by a worker's own shutdown. It is deliberately
# fail-closed on the SAME lease-authority discipline as the k8s controller:
#
#   * a sandbox is reaped only when the hub *positively proves* the recorded
#     lease is no longer live for its ``mac.task.id`` -- the task is terminal,
#     the task has no active lease, the sandbox's ``mac.lease.id`` was superseded
#     by a newer lease, or the task's lease has expired.
#   * orphan status is NEVER inferred from agent idleness, host-local PID
#     liveness, sandbox age, or "the worker looks quiet"; those are advisory
#     signals only. Only the durable task/lease store authorizes a delete.
#
# Every classified sandbox carries the full accountable tuple required by the
# controller audit log: task id, lease id, sandbox name, the ownership
# observation the hub returned, the sandbox age, the ``action`` taken
# (``reap`` / ``keep``), and the ``outcome`` (``deleted`` / ``delete-failed`` /
# ``kept`` / ``dry-run``). The record never carries label values beyond the
# ownership/identity fields, so it is safe to persist as evidence.

LIFECYCLE_TRIGGERS = frozenset(
    {
        "ownership_change",
        "finalization",
        "cancellation",
        "worker_replacement",
        "periodic",
    }
)

_LIFECYCLE_ACTION_REAP = "reap"
_LIFECYCLE_ACTION_KEEP = "keep"


def _sandbox_age_seconds(sandbox: Mapping[str, Any], now: datetime) -> Optional[int]:
    created = _created_at(sandbox.get("created_at"))
    if created is None:
        return None
    return int(max(0.0, (now - created).total_seconds()))


def classify_lifecycle_orphan_sandbox(
    sandbox: Mapping[str, Any],
    task: Optional[Mapping[str, Any]],
    *,
    trigger: str,
    now: datetime,
) -> Dict[str, Any]:
    """Classify a task sandbox for controller-owned lifecycle reaping.

    Reaping is authorized ONLY by the durable task/lease store: the recorded
    lease for ``mac.task.id`` must be provably not-live (task terminal, no active
    lease, superseded lease, or expired lease). Agent idleness, host-local PID
    liveness, and sandbox age are never used to justify a delete -- age is
    recorded for the audit trail only.

    Returns a secret-free, fully accountable record carrying ``task_id``,
    ``lease_id``, ``sandbox_name``, ``ownership`` (the observation the store
    returned), ``age_seconds``, ``trigger``, ``action`` (``reap``/``keep``),
    ``outcome`` (set by the reconciler), and a human ``reason``.
    """

    clean_trigger = str(trigger or "").strip().lower()

    lease_record = classify_lease_orphan_sandbox(sandbox, task, now=now)
    row = dict(sandbox)
    name = lease_record["name"]

    task_state = ""
    task_active_lease = ""
    if isinstance(task, Mapping):
        task_state = str(task.get("state") or "").strip().lower()
        task_active_lease = str(task.get("lease_id") or "").strip()

    if task is None:
        ownership = "task-unresolved"
    elif lease_record["reap"]:
        ownership = "lease-not-live"
    else:
        ownership = "lease-live"

    record: Dict[str, Any] = {
        "schema": "mac.openshell.sandbox_lifecycle_record.v1",
        "trigger": clean_trigger,
        "sandbox_name": name,
        "phase": lease_record["phase"],
        "owner": lease_record["owner"],
        "kind": lease_record["kind"],
        "keep": lease_record["keep"],
        "task_id": lease_record["task_id"],
        "lease_id": lease_record["lease_id"],
        "task_state": task_state,
        "task_active_lease_id": task_active_lease,
        "ownership": ownership,
        "age_seconds": _sandbox_age_seconds(row, now),
        "action": _LIFECYCLE_ACTION_KEEP,
        "outcome": "kept",
        "reason": lease_record["reason"],
    }

    if lease_record["reap"]:
        record["action"] = _LIFECYCLE_ACTION_REAP
        record["outcome"] = "pending"
    return record


def lifecycle_orphan_task_sandbox_candidates(
    sandboxes: Iterable[Mapping[str, Any]],
    lookup_task: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    trigger: str,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return reap-eligible lifecycle records, deterministic and idempotent.

    A sandbox is included only when the hub proves the recorded lease is no
    longer live for its ``mac.task.id``. ``lookup_task`` failures fail closed
    (the sandbox is preserved and the lookup error recorded). Results are sorted
    and de-duplicated by sandbox name, so repeated runs over the same input reap
    the same names and a re-run after deletion is a no-op.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    clean_trigger = str(trigger or "").strip().lower()
    by_name: Dict[str, Dict[str, Any]] = {}
    for raw in sandboxes:
        if not isinstance(raw, Mapping):
            continue
        labels_value = raw.get("labels")
        labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}
        task_id = str(labels.get("mac.task.id") or "").strip()
        task: Optional[Mapping[str, Any]] = None
        lookup_error = ""
        if task_id:
            try:
                task = lookup_task(task_id)
            except Exception as exc:  # noqa: BLE001 - fail closed on lookup failure
                lookup_error = str(exc)
                task = None
        record = classify_lifecycle_orphan_sandbox(raw, task, trigger=clean_trigger, now=current)
        if lookup_error and record["action"] != _LIFECYCLE_ACTION_REAP:
            record["ownership"] = "task-unresolved"
            record["reason"] = "task lookup failed: %s" % lookup_error[-200:]
        if record["action"] == _LIFECYCLE_ACTION_REAP and record["sandbox_name"] not in by_name:
            by_name[record["sandbox_name"]] = record
    return [by_name[name] for name in sorted(by_name)]


def reconcile_task_sandbox_lifecycle(
    lookup_task: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    trigger: str = "periodic",
    openshell_bin: str = "openshell",
    apply: bool = False,
    now: Optional[datetime] = None,
    delete_sandbox: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Controller-owned lifecycle reconcile of orphaned task sandboxes.

    Intended to be called by the controller after a task ownership change,
    finalization, cancellation, or worker replacement (``trigger``). Lists task
    sandboxes once, keeps only those whose recorded lease the hub proves is no
    longer live, and (when ``apply``) deletes exactly those named sandboxes via
    the idempotent single-sandbox delete helper.

    Orphan status is proven from the durable task/lease store only -- never from
    agent idleness or host-local PID liveness. Every candidate carries the full
    accountable tuple (task id, lease id, sandbox name, ownership observation,
    age, action, outcome). The returned evidence is secret-free.
    """

    clean_trigger = str(trigger or "").strip().lower()
    if clean_trigger not in LIFECYCLE_TRIGGERS:
        raise ValueError("unsupported lifecycle trigger: %s" % trigger)

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

    candidates = lifecycle_orphan_task_sandbox_candidates(
        payload, lookup_task, trigger=clean_trigger, now=now
    )
    protected = sum(1 for row in payload if isinstance(row, Mapping)) - len(candidates)

    delete = delete_sandbox
    if delete is None:

        def delete(name: str) -> Dict[str, Any]:
            return delete_named_sandbox(name, openshell_bin=openshell_bin)

    deleted: List[str] = []
    failures: List[Dict[str, str]] = []
    for record in candidates:
        if not apply:
            record["outcome"] = "dry-run"
            continue
        result = delete(str(record["sandbox_name"]))
        if result.get("deleted"):
            record["outcome"] = "deleted"
            deleted.append(str(record["sandbox_name"]))
        else:
            record["outcome"] = "delete-failed"
            error = str(result.get("error") or "").strip()[-1000:]
            record["error"] = error
            failures.append({"name": str(record["sandbox_name"]), "error": error})

    return {
        "schema": "mac.openshell.sandbox_lifecycle_gc.v1",
        "trigger": clean_trigger,
        "dry_run": not apply,
        "scanned": len(payload),
        "protected": max(0, protected),
        "candidates": candidates,
        "deleted": deleted,
        "failures": failures,
    }


# --- Inventory summary for diagnostics ---------------------------------------


def sandbox_inventory_summary(
    sandboxes: Iterable[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Dict[str, Any]:
    """Summarize a listed sandbox payload for diagnostics reuse.

    Returns secret-free counts: total ``scanned`` rows, ``managed`` (exact
    MAC-owned by name + mac.owner==mac), ``reap_eligible`` (managed with the
    dead-PID ownership proof satisfied) and ``protected`` (managed but preserved
    by a fail-closed guard). ``oldest_managed_age_seconds`` is the age in seconds
    of the oldest managed sandbox with a parseable ``created_at`` (``None`` when
    none have one). No label values beyond ownership signals are recorded.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    scanned = 0
    managed = 0
    reap_eligible = 0
    oldest_age: Optional[int] = None
    for raw in sandboxes:
        if not isinstance(raw, Mapping):
            continue
        scanned += 1
        name = str(raw.get("name") or "").strip()
        labels_value = raw.get("labels")
        labels = dict(labels_value) if isinstance(labels_value, Mapping) else {}
        owner = str(labels.get("mac.owner") or "").strip().lower()
        if not MANAGED_NAME_RE.fullmatch(name) or owner != "mac":
            continue
        managed += 1
        record = classify_orphan_task_sandbox(raw, pid_is_alive=pid_is_alive)
        if record["reap"]:
            reap_eligible += 1
        created = _created_at(raw.get("created_at"))
        if created is not None:
            age = int(max(0.0, (current - created).total_seconds()))
            if oldest_age is None or age > oldest_age:
                oldest_age = age

    return {
        "schema": "mac.openshell.sandbox_inventory.v1",
        "scanned": scanned,
        "managed": managed,
        "reap_eligible": reap_eligible,
        "protected": managed - reap_eligible,
        "oldest_managed_age_seconds": oldest_age,
    }
