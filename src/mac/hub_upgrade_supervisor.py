"""Finite host-native hub generation swap and rollback supervisor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence


MANIFEST_SCHEMA = "mac.hub_upgrade_handoff.v1"
RECEIPT_SCHEMA = "mac.hub_upgrade_receipt.v1"
_SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class SupervisorError(RuntimeError):
    pass


class ServiceController(Protocol):
    def stop(self, service: str) -> None: ...
    def start(self, service: str) -> None: ...


@dataclass(frozen=True)
class AppliedGeneration:
    source_target: str
    venv_target: str


class HostServiceController:
    """Fixed launchd/systemd command adapter; never accepts arbitrary commands."""

    def __init__(self, *, system: Optional[str] = None) -> None:
        self.system = (system or platform.system()).lower()

    def stop(self, service: str) -> None:
        self._validate(service)
        if self.system == "darwin":
            domain = "system/%s" % service
            self._run(["sudo", "-n", "launchctl", "kill", "SIGTERM", domain], allow_absent=True)
            return
        if self.system == "linux":
            self._run(["sudo", "-n", "systemctl", "stop", service])
            return
        raise SupervisorError("unsupported service manager platform")

    def start(self, service: str) -> None:
        self._validate(service)
        if self.system == "darwin":
            domain = "system/%s" % service
            self._run(["sudo", "-n", "launchctl", "kickstart", "-k", domain])
            return
        if self.system == "linux":
            self._run(["sudo", "-n", "systemctl", "start", service])
            return
        raise SupervisorError("unsupported service manager platform")

    @staticmethod
    def _validate(service: str) -> None:
        if not _SERVICE.fullmatch(service):
            raise SupervisorError("invalid service identity")

    @staticmethod
    def _run(argv: Sequence[str], *, allow_absent: bool = False) -> None:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0 and not allow_absent:
            raise SupervisorError(
                "service manager failed: %s" % (completed.stderr.strip() or completed.returncode)
            )


class HubUpgradeSupervisor:
    """Apply only an already-authorized, digest-bound generation manifest."""

    def __init__(
        self,
        *,
        mac_home: Path,
        service_controller: Optional[ServiceController] = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.mac_home = Path(mac_home).expanduser().resolve()
        self.service_controller = service_controller or HostServiceController()
        self.sleep = sleep
        self.state_root = self.mac_home / "upgrades" / "supervisor"

    def apply(self, manifest_path: Path, expected_digest: str) -> Mapping[str, Any]:
        manifest, manifest_digest = self._load_manifest(manifest_path, expected_digest)
        transaction_id = str(manifest["transaction_id"])
        state_path = self._state_path(transaction_id)
        receipt_path = self._receipt_path(transaction_id)
        state = self._read_json(state_path, default={})
        if receipt_path.exists():
            receipt = self._read_json(receipt_path)
            if receipt.get("manifest_digest") == manifest_digest:
                return receipt
            raise SupervisorError("receipt exists for different manifest material")

        source_link = self._managed_path(manifest["source_link"])
        venv_link = self._managed_path(manifest["venv_link"])
        staged_source = self._managed_path(manifest["staged_source"])
        staged_venv = self._managed_path(manifest["staged_venv"])
        self._require_generation(staged_source, staged_venv, str(manifest["expected_commit_sha"]))
        previous = AppliedGeneration(
            source_target=str(
                state.get("previous_source_target") or self._read_managed_link(source_link)
            ),
            venv_target=str(
                state.get("previous_venv_target") or self._read_managed_link(venv_link)
            ),
        )
        self._write_state(
            state_path,
            {
                "schema": RECEIPT_SCHEMA,
                "transaction_id": transaction_id,
                "manifest_digest": manifest_digest,
                "phase": "armed",
                "previous_source_target": previous.source_target,
                "previous_venv_target": previous.venv_target,
                "target_source": str(staged_source),
                "target_venv": str(staged_venv),
                "supervisor_pid": os.getpid(),
                "updated_at": self._now(),
            },
        )
        service = str(manifest["service"])
        try:
            self.service_controller.stop(service)
            self._write_phase(state_path, "stopped")
            self._atomic_link(source_link, staged_source)
            self._atomic_link(venv_link, staged_venv)
            self._write_runtime_attestation(
                str(manifest["generation_id"]),
                str(manifest["expected_commit_sha"]),
            )
            self._write_phase(state_path, "swapped")
            self.service_controller.start(service)
            self._write_phase(state_path, "started")
            health_proof = self._prove_health(manifest)
        except Exception as exc:
            rollback_error = ""
            try:
                self._restore(previous, source_link, venv_link, service)
            except Exception as rollback_exc:  # noqa: BLE001 - both failures belong in state.
                rollback_error = str(rollback_exc)
            self._write_state(
                state_path,
                {
                    **self._read_json(state_path, default={}),
                    "phase": "rolled_back" if not rollback_error else "rollback_failed",
                    "error": str(exc),
                    "rollback_error": rollback_error,
                    "updated_at": self._now(),
                },
            )
            raise SupervisorError("hub generation failed health proof and was rolled back") from exc

        receipt = {
            "schema": RECEIPT_SCHEMA,
            "transaction_id": transaction_id,
            "manifest_digest": manifest_digest,
            "generation_id": str(manifest["generation_id"]),
            "expected_commit_sha": str(manifest["expected_commit_sha"]),
            "previous_source_target": previous.source_target,
            "previous_venv_target": previous.venv_target,
            "source_target": str(staged_source),
            "venv_target": str(staged_venv),
            "health_proof": health_proof,
            "status": "committed",
            "created_at": self._now(),
        }
        receipt["receipt_digest"] = self._digest_json(receipt)
        self._write_private_json(receipt_path, receipt)
        self._write_phase(state_path, "committed")
        return receipt

    def rollback(self, manifest_path: Path, expected_digest: str) -> Mapping[str, Any]:
        manifest, manifest_digest = self._load_manifest(
            manifest_path, expected_digest, allow_expired=True
        )
        transaction_id = str(manifest["transaction_id"])
        state_path = self._state_path(transaction_id)
        state = self._read_json(state_path)
        if state.get("manifest_digest") != manifest_digest:
            raise SupervisorError("supervisor state does not match manifest")
        previous = AppliedGeneration(
            source_target=str(state["previous_source_target"]),
            venv_target=str(state["previous_venv_target"]),
        )
        self._restore(
            previous,
            self._managed_path(manifest["source_link"]),
            self._managed_path(manifest["venv_link"]),
            str(manifest["service"]),
        )
        self._write_phase(state_path, "rolled_back")
        return self.status(manifest_path, expected_digest, allow_expired=True)

    def status(
        self,
        manifest_path: Path,
        expected_digest: str,
        *,
        allow_expired: bool = False,
    ) -> Mapping[str, Any]:
        manifest, manifest_digest = self._load_manifest(
            manifest_path,
            expected_digest,
            allow_expired=allow_expired,
        )
        transaction_id = str(manifest["transaction_id"])
        state = self._read_json(self._state_path(transaction_id), default={})
        receipt = self._read_json(self._receipt_path(transaction_id), default={})
        return {
            "schema": RECEIPT_SCHEMA,
            "transaction_id": transaction_id,
            "manifest_digest": manifest_digest,
            "phase": state.get("phase", "not_started"),
            "receipt": receipt or None,
        }

    def recover(self, manifest_path: Path, expected_digest: str) -> Mapping[str, Any]:
        """Resume a crash only from durable state, manifest, and receipt."""
        status = self.status(manifest_path, expected_digest, allow_expired=True)
        if status["phase"] in {"not_started", "committed", "rolled_back"}:
            return status
        manifest, _ = self._load_manifest(manifest_path, expected_digest, allow_expired=True)
        state = self._read_json(self._state_path(str(manifest["transaction_id"])))
        previous = AppliedGeneration(
            source_target=str(state["previous_source_target"]),
            venv_target=str(state["previous_venv_target"]),
        )
        self._restore(
            previous,
            self._managed_path(manifest["source_link"]),
            self._managed_path(manifest["venv_link"]),
            str(manifest["service"]),
        )
        self._write_phase(self._state_path(str(manifest["transaction_id"])), "rolled_back")
        return self.status(manifest_path, expected_digest, allow_expired=True)

    def recover_all(self) -> Mapping[str, Any]:
        """Startup hook: restore interrupted swaps before the hub process execs."""
        recovered = []
        skipped_active = []
        if not self.state_root.exists():
            return {
                "schema": RECEIPT_SCHEMA,
                "recovered": recovered,
                "skipped_active": skipped_active,
            }
        for state_path in sorted(self.state_root.glob("*.state.json")):
            state = self._read_json(state_path, default={})
            phase = str(state.get("phase") or "")
            if phase in {"committed", "rolled_back"}:
                continue
            transaction_id = self._safe_id(str(state.get("transaction_id") or ""))
            receipt_path = self._receipt_path(transaction_id)
            if receipt_path.exists():
                self._write_phase(state_path, "committed")
                continue
            supervisor_pid = int(state.get("supervisor_pid") or 0)
            if supervisor_pid > 0 and self._pid_alive(supervisor_pid):
                skipped_active.append(transaction_id)
                continue
            manifest_path = self.mac_home / "upgrades" / "handoffs" / ("%s.json" % transaction_id)
            manifest, _ = self._load_manifest(
                manifest_path,
                str(state.get("manifest_digest") or ""),
                allow_expired=True,
            )
            previous = AppliedGeneration(
                source_target=str(state["previous_source_target"]),
                venv_target=str(state["previous_venv_target"]),
            )
            previous_source = self._managed_path(previous.source_target)
            self._atomic_link(self._managed_path(manifest["source_link"]), previous_source)
            self._atomic_link(
                self._managed_path(manifest["venv_link"]),
                self._managed_path(previous.venv_target),
            )
            self._write_runtime_attestation(
                previous_source.name,
                self._source_commit(previous_source),
            )
            self._write_phase(state_path, "rolled_back")
            recovered.append(transaction_id)
        return {
            "schema": RECEIPT_SCHEMA,
            "recovered": recovered,
            "skipped_active": skipped_active,
        }

    def _load_manifest(
        self,
        manifest_path: Path,
        expected_digest: str,
        *,
        allow_expired: bool = False,
    ) -> tuple[Mapping[str, Any], str]:
        path = Path(manifest_path).expanduser()
        self._assert_private_regular_file(path)
        raw = path.read_bytes()
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not _DIGEST.fullmatch(expected_digest) or actual != expected_digest:
            raise SupervisorError("handoff manifest digest mismatch")
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SupervisorError("handoff manifest is not valid JSON") from exc
        required = {
            "schema",
            "transaction_id",
            "generation_id",
            "authorization",
            "source_link",
            "venv_link",
            "staged_source",
            "staged_venv",
            "service",
            "health_url",
            "attestation_url",
            "expected_commit_sha",
        }
        if not isinstance(manifest, dict) or required - set(manifest):
            raise SupervisorError("handoff manifest is missing required fields")
        if manifest["schema"] != MANIFEST_SCHEMA:
            raise SupervisorError("unsupported handoff manifest schema")
        authorization = manifest["authorization"]
        if (
            not isinstance(authorization, dict)
            or authorization.get("status") != "authorized"
            or not authorization.get("human_id")
        ):
            raise SupervisorError("handoff manifest is not authorized")
        expires_at = self._parse_time(authorization.get("expires_at"))
        if not allow_expired and expires_at <= datetime.now(timezone.utc):
            raise SupervisorError("handoff manifest authorization expired")
        if not _SERVICE.fullmatch(str(manifest["service"])):
            raise SupervisorError("invalid service identity")
        return manifest, actual

    def _prove_health(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        required_successes = max(2, min(int(manifest.get("required_health_successes", 3)), 10))
        timeout = max(5, min(int(manifest.get("health_timeout_seconds", 120)), 900))
        deadline = time.monotonic() + timeout
        consecutive = 0
        last_error = ""
        last_health: Mapping[str, Any] = {}
        last_attestation: Mapping[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                health = self._fetch_json(str(manifest["health_url"]))
                attestation = self._fetch_json(str(manifest["attestation_url"]))
                if health.get("status") != "ok":
                    raise SupervisorError("hub health status is not ok")
                if str(attestation.get("source_commit") or "") != str(
                    manifest["expected_commit_sha"]
                ):
                    raise SupervisorError("startup attestation source commit mismatch")
                if str(attestation.get("generation_id") or "") != str(manifest["generation_id"]):
                    raise SupervisorError("startup attestation generation mismatch")
                consecutive += 1
                last_health = health
                last_attestation = attestation
                if consecutive >= required_successes:
                    return {
                        "consecutive_successes": consecutive,
                        "health": dict(last_health),
                        "attestation": dict(last_attestation),
                        "proved_at": self._now(),
                    }
            except Exception as exc:  # noqa: BLE001 - bounded health retry.
                consecutive = 0
                last_error = str(exc)
            self.sleep(1)
        raise SupervisorError("hub health proof timed out: %s" % last_error)

    @staticmethod
    def _fetch_json(url: str) -> Mapping[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise SupervisorError("health proof URL must be loopback HTTP(S)")
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise SupervisorError("health proof request failed") from exc
        if not isinstance(body, dict):
            raise SupervisorError("health proof response is not an object")
        return body

    def _restore(
        self,
        previous: AppliedGeneration,
        source_link: Path,
        venv_link: Path,
        service: str,
    ) -> None:
        self.service_controller.stop(service)
        previous_source = self._managed_path(previous.source_target)
        self._atomic_link(source_link, previous_source)
        self._atomic_link(venv_link, self._managed_path(previous.venv_target))
        commit = self._source_commit(previous_source)
        self._write_runtime_attestation(previous_source.name, commit)
        self.service_controller.start(service)

    def _require_generation(self, source: Path, venv: Path, expected_sha: str) -> None:
        if not source.is_dir() or not venv.is_dir():
            raise SupervisorError("staged source and venv generations must exist")
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(source),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip() != expected_sha:
            raise SupervisorError("staged source does not match expected commit")
        if not (venv / "bin" / "python").is_file():
            raise SupervisorError("staged venv has no Python interpreter")

    @staticmethod
    def _source_commit(source: Path) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(source),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or len(completed.stdout.strip()) != 40:
            raise SupervisorError("could not attest source generation commit")
        return completed.stdout.strip()

    def _write_runtime_attestation(self, generation_id: str, commit_sha: str) -> None:
        current = self.mac_home / "current"
        current.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, value in (
            ("generation-id", generation_id),
            ("source-commit", commit_sha),
        ):
            path = current / name
            temporary = path.with_name(".%s.%d.tmp" % (name, os.getpid()))
            temporary.write_text(value + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, path)

    def _managed_path(self, value: Any) -> Path:
        path = Path(str(value)).expanduser()
        resolved_parent = path.parent.resolve()
        candidate = resolved_parent / path.name
        try:
            candidate.relative_to(self.mac_home)
        except ValueError as exc:
            raise SupervisorError("manifest path escapes MAC_HOME") from exc
        return candidate

    def _read_managed_link(self, path: Path) -> str:
        if not path.is_symlink():
            raise SupervisorError("managed generation pointer must be a symlink")
        target = (path.parent / os.readlink(path)).resolve()
        return str(self._managed_path(target))

    @staticmethod
    def _atomic_link(link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = link.with_name(".%s.%d.tmp" % (link.name, os.getpid()))
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, link)

    def _state_path(self, transaction_id: str) -> Path:
        return self.state_root / ("%s.state.json" % self._safe_id(transaction_id))

    def _receipt_path(self, transaction_id: str) -> Path:
        return self.state_root / ("%s.receipt.json" % self._safe_id(transaction_id))

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise SupervisorError("invalid transaction identity")
        return value

    def _write_phase(self, state_path: Path, phase: str) -> None:
        state = dict(self._read_json(state_path, default={}))
        state.update({"phase": phase, "updated_at": self._now()})
        self._write_state(state_path, state)

    def _write_state(self, path: Path, value: Mapping[str, Any]) -> None:
        self._write_private_json(path, value)

    def _write_private_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)

    @staticmethod
    def _read_json(path: Path, default: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        if not path.exists():
            if default is not None:
                return default
            raise SupervisorError("supervisor state is missing")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SupervisorError("supervisor state is not an object")
        return value

    @staticmethod
    def _assert_private_regular_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise SupervisorError("handoff manifest must be a regular file")
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise SupervisorError("handoff manifest must be owner-private")

    @staticmethod
    def _digest_json(value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SupervisorError("invalid authorization expiry") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac-home", default=os.environ.get("MAC_HOME", "~/.mac"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("apply", "status", "rollback", "recover"):
        sub = subparsers.add_parser(command)
        sub.add_argument("manifest")
        sub.add_argument("--digest", required=True)
    subparsers.add_parser("recover-all")
    args = parser.parse_args(argv)
    supervisor = HubUpgradeSupervisor(mac_home=Path(args.mac_home))
    try:
        if args.command == "recover-all":
            result = supervisor.recover_all()
        else:
            operation = getattr(supervisor, args.command)
            result = operation(Path(args.manifest), args.digest)
    except SupervisorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
