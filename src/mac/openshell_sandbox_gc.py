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
