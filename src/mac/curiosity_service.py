"""Hub-mediated access to a host's curiosity quarantine ledger.

The ledger is not reachable by the agents that need it. The real CLI lives at
``/usr/local/bin/curiosity`` INSIDE the ``mac-openclaw-<agent>`` sandbox, and
its store is ``<state_dir>/mac-curiosity`` inside that same sandbox. A
dispatched task executes in a freshly created ``mac-task-*`` sandbox, which is
a different namespace with neither the CLI nor the store, and cannot reach the
host's ``openshell`` to get them -- that isolation is the point.

So every adjudication task ever filed against the quarantine was unsatisfiable.
``curiosity_reviewer`` pins its tasks to the owning agent via
``metadata.target_agent_id``, which fixes the HOST and not the NAMESPACE; three
attempts on 2026-08-05 failed for exactly this reason, including one that ran
on the correct host. See task_3a4503f0.

The hub is the one process that sits in the right place: it runs on the agent's
host, so it can invoke the ``~/.mac/bin/curiosity`` wrapper (which execs into
the OpenClaw sandbox), and every task sandbox can already reach the hub over
HTTP. Mediating here makes adjudication runnable from any sandbox on any host
and removes the need to pin at all.

A read-only copy uploaded into the task sandbox would NOT have been enough:
adjudication is a write against the real ledger, so enumeration would have
started working while approve/reject stayed broken.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mac import mac_paths

CURIOSITY_SCHEMA = "mac.curiosity_candidates.v1"
CURIOSITY_DECISION_SCHEMA = "mac.curiosity_decision.v1"

#: Statuses the sidecar CLI accepts for ``list --status``.
CURIOSITY_STATUSES = ("quarantined", "approved", "rejected")

#: Decisions the sidecar exposes. Submission is deliberately NOT proxied: the
#: sidecar withholds approve/reject from the submitting agent on purpose, and
#: this service exists to supply the external judgment, not to widen the
#: submission path.
CURIOSITY_DECISIONS = ("approve", "reject")

DEFAULT_TIMEOUT_SECONDS = 60.0


class CuriosityUnavailable(RuntimeError):
    """The host has no reachable curiosity wrapper.

    Distinct from a failing command: an agent host that never ran an OpenClaw
    gateway has no ledger at all, and that is not an error to retry.
    """


class CuriosityCommandError(RuntimeError):
    """The wrapper ran and failed. Carries the CLI's own stderr."""


@dataclass(frozen=True)
class CuriosityConfig:
    """Where the wrapper is and how long it may take."""

    wrapper_path: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "CuriosityConfig":
        env = os.environ if environ is None else environ
        raw = str(env.get("MAC_CURIOSITY_WRAPPER") or "").strip()
        if raw:
            wrapper = Path(raw).expanduser()
        else:
            # Resolve through mac_paths rather than a home literal: the fleet
            # has four different homes and tests/test_mac_paths_no_hardcode.py
            # exists to stop new code assuming one of them.
            override = str(env.get("MAC_HOME") or "").strip()
            root = Path(override).expanduser() if override else mac_paths.mac_home()
            wrapper = root / "bin" / "curiosity"
        timeout = DEFAULT_TIMEOUT_SECONDS
        raw_timeout = str(env.get("MAC_CURIOSITY_TIMEOUT_SECONDS") or "").strip()
        if raw_timeout:
            try:
                parsed = float(raw_timeout)
            except ValueError:
                parsed = DEFAULT_TIMEOUT_SECONDS
            # A hung sandbox exec must not pin a hub request open.
            timeout = min(600.0, max(1.0, parsed))
        return cls(wrapper_path=wrapper, timeout_seconds=timeout)


class CuriosityService:
    """Proxy the host curiosity wrapper for callers that cannot reach it."""

    def __init__(
        self,
        config: Optional[CuriosityConfig] = None,
        *,
        runner: Optional[Any] = None,
    ) -> None:
        self.config = config or CuriosityConfig.from_env()
        # Injectable so tests exercise this module rather than a fake CLI.
        self._runner = runner or subprocess.run

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        path = self.config.wrapper_path
        return bool(path) and path.is_file() and os.access(str(path), os.X_OK)

    def _require_wrapper(self) -> Path:
        path = self.config.wrapper_path
        if not self.available():
            raise CuriosityUnavailable(
                "no curiosity wrapper at %s; this host has no OpenClaw "
                "quarantine ledger" % path
            )
        return path

    # -- invocation --------------------------------------------------------

    def _run(self, args: Sequence[str]) -> str:
        wrapper = self._require_wrapper()
        argv = [str(wrapper), *[str(arg) for arg in args]]
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                # The wrapper resolves its own sandbox and env; inheriting the
                # hub's environment would leak hub credentials into a sandbox
                # exec for no benefit.
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", ""),
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise CuriosityCommandError(
                "curiosity %s timed out after %.0fs"
                % (" ".join(str(a) for a in args), self.config.timeout_seconds)
            ) from exc
        except OSError as exc:
            raise CuriosityUnavailable(
                "could not execute %s: %s" % (wrapper, exc)
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise CuriosityCommandError(
                "curiosity %s failed (exit %s): %s"
                % (
                    " ".join(str(a) for a in args),
                    completed.returncode,
                    detail[:400],
                )
            )
        return completed.stdout or ""

    # -- read --------------------------------------------------------------

    def list_candidates(self, status: Optional[str] = None) -> Dict[str, Any]:
        """Candidates, optionally filtered by status."""
        args: List[str] = ["list"]
        if status is not None:
            normalized = str(status).strip().lower()
            if normalized not in CURIOSITY_STATUSES:
                raise ValueError(
                    "status must be one of %s" % ", ".join(CURIOSITY_STATUSES)
                )
            args += ["--status", normalized]
        raw = self._run(args)
        try:
            payload = json.loads(raw) if raw.strip() else []
        except ValueError as exc:
            raise CuriosityCommandError(
                "curiosity list returned output that is not JSON: %s"
                % raw.strip()[:200]
            ) from exc
        candidates = payload if isinstance(payload, list) else [payload]
        return {
            "schema": CURIOSITY_SCHEMA,
            "status": status,
            "count": len(candidates),
            "candidates": candidates,
        }

    # -- write -------------------------------------------------------------

    def decide(
        self,
        decision: str,
        candidate_id: str,
        *,
        actor: str,
        reason: str,
        approval_id: str,
    ) -> Dict[str, Any]:
        """Approve or reject one candidate.

        actor/reason/approval_id are all required by the sidecar and are the
        whole point of the external-judgment design: every promotion carries an
        auditable trail in the curiosity ledger. They are validated here so a
        bad request fails before it reaches a sandbox exec.
        """
        verb = str(decision).strip().lower()
        if verb not in CURIOSITY_DECISIONS:
            raise ValueError(
                "decision must be one of %s" % ", ".join(CURIOSITY_DECISIONS)
            )
        identifier = str(candidate_id or "").strip()
        if not identifier:
            raise ValueError("candidate_id is required")
        fields = {"actor": actor, "reason": reason, "approval_id": approval_id}
        for name, value in fields.items():
            if not str(value or "").strip():
                raise ValueError("%s is required" % name)
        self._run(
            [
                verb,
                identifier,
                "--actor",
                str(actor).strip(),
                "--reason",
                str(reason).strip(),
                "--approval-id",
                str(approval_id).strip(),
            ]
        )
        return {
            "schema": CURIOSITY_DECISION_SCHEMA,
            "candidate_id": identifier,
            "decision": verb,
            "actor": str(actor).strip(),
            "reason": str(reason).strip(),
            "approval_id": str(approval_id).strip(),
        }
