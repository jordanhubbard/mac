"""Runtime-dependency and footprint management helpers extracted from worker.py.

Contains:
  - REQUIRED_RUNTIME_PIP: list of required pip packages
  - RuntimeDepsMixin: mixin that provides footprint management, pip/npm install
    helpers, and reconcile_runtime_deps to MacWorker

These are imported back into worker.py; callers that import from mac.worker
see no change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mac import mac_paths
from mac.atomic_file import atomic_write_text
from typing import Any, Dict, List, Optional
from urllib.parse import quote

REQUIRED_RUNTIME_PIP: List[str] = [
    # NeMo Relay observability seam (src/mac/relay_observability.py). Present on
    # every agent so MAC_RELAY_OBSERVABILITY=1 actually activates rather than
    # silently no-opping on a missing import.
    "nemo-relay==0.3.0",
]

JsonDict = Dict[str, Any]


class RuntimeDepsMixin:
    """Mixin that provides runtime-dependency management to MacWorker.

    Relies on the following attributes being set by MacWorker.__init__:
      self.client, self.agent_id
    Also uses self._record_command_audit (defined elsewhere in MacWorker).
    """

    def _mac_home(self) -> Path:
        return mac_paths.mac_home()

    def _agent_venv_python(self) -> str:
        py = self._mac_home() / "venv" / "bin" / "python"
        return str(py) if py.exists() else sys.executable

    def _footprint_path(self) -> Path:
        return self._mac_home() / "agent-footprint.json"

    def _load_footprint(self) -> JsonDict:
        try:
            return json.loads(self._footprint_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_footprint(self, footprint: JsonDict) -> None:
        # ``~/.mac`` is per-USER, not per-agent: every worker, CLI invocation and
        # agent startup writes this file. A fixed ``agent-footprint.json.tmp``
        # therefore has multiple concurrent owners that truncate and splice each
        # other, and _load_footprint swallows the resulting parse error and
        # returns {} — silently erasing the record of what is installed in the
        # shared venv. atomic_write_text gives each writer a private temp name.
        path = self._footprint_path()
        atomic_write_text(path, json.dumps(footprint, indent=2, sort_keys=True), mode=0o600)

    def _report_footprint(self, footprint: JsonDict) -> None:
        try:
            self.client.post(
                "/agents/%s/installed-packages" % quote(self.agent_id, safe=""),
                {"installed_packages": footprint},
            )
        except Exception:
            pass

    @staticmethod
    def _pip_base_name(spec: str) -> str:
        return re.split(r"[\[<>=!~;\s]", spec.strip(), 1)[0].strip().lower().replace("_", "-")

    @staticmethod
    def _npm_base_name(spec: str) -> str:
        spec = spec.strip()
        if spec.startswith("@"):
            parts = spec.split("@")  # ['', 'scope/name', 'ver'?]
            return ("@" + parts[1]).lower() if len(parts) >= 2 else spec.lower()
        return spec.split("@", 1)[0].lower()

    def _pip_installed(self, py: str) -> Dict[str, str]:
        """Map of installed pip package name -> version (normalized names).

        ``pip list --format=json`` already carries the version; we keep it so the
        probe can compare name+version tuples instead of presence-only.
        """
        try:
            out = subprocess.run(
                [py, "-m", "pip", "list", "--format=json"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            ).stdout
            return {
                str(p.get("name", "")).lower().replace("_", "-"): str(p.get("version", ""))
                for p in json.loads(out or "[]")
            }
        except Exception:
            return {}

    @classmethod
    def _pip_spec_satisfied(cls, spec: str, installed: Dict[str, str]) -> bool:
        """True when *spec* (name + optional version constraint) is already met.

        ``installed`` is name->version (see :meth:`_pip_installed`). The probe is
        version-aware: a present-but-out-of-range package is NOT satisfied, so it
        gets reinstalled/upgraded to match. Uses ``packaging`` for correct PEP 440
        comparison when available, with a conservative fallback (exact ``==``
        match, else presence) so a missing ``packaging`` can never wrongly skip an
        install of something absent.
        """
        name = cls._pip_base_name(spec)
        have = installed.get(name)
        if have is None:
            return False  # absent -> must install
        try:
            from packaging.requirements import Requirement

            req = Requirement(spec)
            if not req.specifier:
                return True  # no version pin -> presence is enough
            return req.specifier.contains(have, prereleases=True)
        except Exception:
            # Fallback without packaging: honor an exact "==" pin, else accept
            # presence (can't reason about ranges safely).
            marker = "=="
            if marker in spec:
                want = spec.split(marker, 1)[1].strip().split(",")[0].strip()
                return have == want
            return True

    def _npm_installed(self, prefix: str) -> set:
        try:
            out = subprocess.run(
                ["npm", "ls", "--prefix", prefix, "--depth", "0", "--json"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            ).stdout
            deps = json.loads(out or "{}").get("dependencies") or {}
            return {str(k).lower() for k in deps}
        except Exception:
            return set()

    def _run_install(
        self, argv: List[str], *, manager: str, reason: str, specs: List[str]
    ) -> JsonDict:
        command_id = secrets.token_hex(8)
        cwd = str(self._mac_home())
        meta = {"self_install": True, "package_manager": manager, "reason": reason, "specs": specs}
        started = datetime.now(timezone.utc).isoformat()
        self._record_command_audit(
            {
                "command_id": command_id,
                "phase": "started",
                "argv": argv,
                "cwd": cwd,
                "started_at": started,
                "metadata": meta,
            }
        )
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=1800, check=False
            )
            rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except Exception as exc:  # noqa: BLE001 - install failures are reported, not raised.
            self._record_command_audit(
                {
                    "command_id": command_id,
                    "phase": "failed",
                    "argv": argv,
                    "cwd": cwd,
                    "started_at": started,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "returncode": -1,
                    "metadata": {**meta, "error": str(exc)},
                }
            )
            return {"ok": False, "error": str(exc), "specs": specs}
        dur_ms = (time.monotonic() - t0) * 1000.0
        self._record_command_audit(
            {
                "command_id": command_id,
                "phase": "completed" if rc == 0 else "failed",
                "argv": argv,
                "cwd": cwd,
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": dur_ms,
                "returncode": rc,
                "stdout_sha256": hashlib.sha256(out.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(err.encode("utf-8")).hexdigest(),
                "stdout_bytes": len(out.encode("utf-8")),
                "stderr_bytes": len(err.encode("utf-8")),
                "metadata": meta,
            }
        )
        return {
            "ok": rc == 0,
            "returncode": rc,
            "stdout": out[-4000:],
            "stderr": err[-4000:],
            "specs": specs,
        }

    def _update_footprint(
        self, manager: str, specs: List[str], *, index_url: Optional[str] = None
    ) -> JsonDict:
        """Read-modify-write the shared footprint. Caller MUST hold _install_lock.

        Two agents that interleave load -> mutate -> write here lose one of the
        two updates outright, so serialization is not optional. The hub report
        deliberately happens outside this method: it is network I/O and must not
        run while the host-wide install lock is held.
        """

        fp = self._load_footprint()
        entries = fp.get(manager) if isinstance(fp.get(manager), list) else []
        by_name = {e.get("name"): dict(e) for e in entries if isinstance(e, dict) and e.get("name")}
        now = datetime.now(timezone.utc).isoformat()
        base = self._pip_base_name if manager == "pip" else self._npm_base_name
        for spec in specs:
            entry = {"name": base(spec), "spec": spec, "installed_at": now}
            if index_url:
                entry["index_url"] = index_url
            by_name[entry["name"]] = entry
        fp[manager] = [by_name[k] for k in sorted(by_name)]
        fp["updated_at"] = now
        self._write_footprint(fp)
        return fp

    def _update_footprint_serialized(
        self, manager: str, specs: List[str], *, index_url: Optional[str] = None
    ) -> None:
        """Take the install lock, update the footprint, report after releasing."""

        lock = self._install_lock()
        try:
            fp = self._update_footprint(manager, specs, index_url=index_url)
        finally:
            lock.close()
        if isinstance(fp, dict):
            self._report_footprint(fp)

    def _install_lock(self):
        """Take the shared-venv install lock, or fail loudly.

        Swallowing a flock error here meant running ``pip install`` into the
        shared ``~/.mac/venv`` unserialized while believing the process was
        holding a lock — the exact scenario that leaves a half-installed
        distribution behind. A failed lock now raises so the caller reports the
        install as failed instead of racing.
        """

        lock_path = self._mac_home() / ".install.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")  # noqa: SIM115 - held until caller closes
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:
            # Platform without fcntl (Windows). There is no shared venv there;
            # log rather than hard-fail, but never claim the lock silently.
            self._observe_install_lock_unavailable("fcntl unavailable on this platform")
        except OSError as exc:
            fh.close()
            raise RuntimeError(
                "could not take the shared install lock at %s: %s" % (lock_path, exc)
            ) from exc
        return fh

    def _observe_install_lock_unavailable(self, reason: str) -> None:
        observe = getattr(self, "_observe_log", None)
        if observe is None:
            return
        try:
            observe(
                "worker.runtime_deps.install_lock_unavailable",
                level="warning",
                detail={"reason": reason},
            )
        except Exception:  # noqa: BLE001 - telemetry must not break installs
            pass

    def ensure_pip(
        self,
        specs: List[str],
        *,
        reason: str = "agent self-install",
        index_url: Optional[str] = None,
    ) -> JsonDict:
        # reject flag-smuggling specs (e.g. "-rfile", "--upgrade"); only real pkgs.
        specs = [s.strip() for s in (specs or []) if s and not s.strip().startswith("-")]
        if not specs:
            return {"ok": True, "skipped": "no specs"}
        py = self._agent_venv_python()
        installed = self._pip_installed(py)
        # Version-aware probe: install/upgrade only the (name, version) tuples
        # that are missing OR present at an unsatisfying version. pip moves a
        # present-but-wrong version to satisfy the constraint (no --upgrade, so
        # we don't churn transitive deps).
        pending = [s for s in specs if not self._pip_spec_satisfied(s, installed)]
        if not pending:
            # The footprint update is a read-modify-write against a file shared
            # by every agent on the host, so it belongs inside the install lock
            # on the fast path exactly as much as on the install path.
            self._update_footprint_serialized("pip", specs, index_url=index_url)
            return {"ok": True, "skipped": "already satisfied", "specs": specs}
        argv = [py, "-m", "pip", "install", *pending]
        if index_url:
            argv += ["--index-url", index_url]
        lock = self._install_lock()
        footprint: Optional[JsonDict] = None
        try:
            result = self._run_install(argv, manager="pip", reason=reason, specs=pending)
            if result.get("ok"):
                footprint = self._update_footprint("pip", pending, index_url=index_url)
        finally:
            lock.close()
        if isinstance(footprint, dict):
            self._report_footprint(footprint)
        return result

    def ensure_npm(self, packages: List[str], *, reason: str = "agent self-install") -> JsonDict:
        packages = [p.strip() for p in (packages or []) if p and not p.strip().startswith("-")]
        if not packages:
            return {"ok": True, "skipped": "no packages"}
        prefix = str(self._mac_home())
        installed = self._npm_installed(prefix)
        pending = [p for p in packages if self._npm_base_name(p) not in installed]
        if not pending:
            self._update_footprint_serialized("npm", packages)
            return {"ok": True, "skipped": "already satisfied", "packages": packages}
        argv = ["npm", "install", "--prefix", prefix, *pending]
        lock = self._install_lock()
        footprint: Optional[JsonDict] = None
        try:
            result = self._run_install(argv, manager="npm", reason=reason, specs=pending)
            if result.get("ok"):
                footprint = self._update_footprint("npm", pending)
        finally:
            lock.close()
        if isinstance(footprint, dict):
            self._report_footprint(footprint)
        return result

    def reconcile_runtime_deps(self, specs: Optional[List[str]] = None) -> JsonDict:
        """Probe + install the agent's declared runtime deps (idempotent).

        Version-aware via :meth:`ensure_pip`: installs/upgrades only the
        (name, version) tuples that are missing or unsatisfied, and is a fast
        no-op when everything already matches. Invoked at lifecycle startup so a
        fresh or stale agent self-converges to the required dependency versions
        on demand — no redeploy needed. ``specs`` defaults to
        :data:`REQUIRED_RUNTIME_PIP`.
        """
        specs = list(REQUIRED_RUNTIME_PIP) if specs is None else list(specs)
        if not specs:
            return {"ok": True, "skipped": "no runtime deps"}
        return self.ensure_pip(specs, reason="runtime-deps reconcile")

    def _reconcile_runtime_deps_best_effort(self) -> None:
        """Run :meth:`reconcile_runtime_deps` without ever breaking the loop.

        Gated by ``MAC_AGENT_RECONCILE_RUNTIME_DEPS`` (default on); set to a
        falsey value to skip (e.g. air-gapped hosts that provision deps out of
        band).
        """
        if os.environ.get("MAC_AGENT_RECONCILE_RUNTIME_DEPS", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return
        try:
            result = self.reconcile_runtime_deps()
            self._observe_log(
                "worker.runtime_deps.reconciled",
                level="debug",
                detail={k: result.get(k) for k in ("ok", "skipped", "specs") if k in result},
            )
        except Exception as exc:  # noqa: BLE001 - dep reconcile must never crash the loop
            self._observe_log(
                "worker.runtime_deps.error", level="warning", detail={"error": str(exc)}
            )
