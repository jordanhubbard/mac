"""Durable hub-mediated self-upgrade orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import pwd
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from mac import mac_paths
from mac.hub_upgrade_supervisor import MANIFEST_SCHEMA, RECEIPT_SCHEMA
from mac.models import (
    JsonDict,
    NotFoundError,
    TransitionError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.source_release_gate import SourceReleaseGate


UPGRADE_SCHEMA = "mac.fleet_upgrade.v1"
PROGRESS_EVENT = "fleet.upgrade.progress"
_PRE_MUTATION_STATES = {"requested", "staging", "staged"}
_TERMINAL_STATES = {"completed", "cancelled", "failed", "rolled_back"}


class FleetUpgradeService:
    """Persist every gate so a replacement hub can resume without chat state."""

    def __init__(
        self,
        control_plane: Any,
        *,
        repository_path: Optional[Path] = None,
        source_gate: Optional[SourceReleaseGate] = None,
    ) -> None:
        self.cp = control_plane
        self.store = control_plane.store
        repository = Path(
            repository_path
            or os.environ.get("MAC_SOURCE_ROOT")
            or Path(__file__).resolve().parents[2]
        )
        self.source_gate = source_gate or SourceReleaseGate(repository)
        self.mac_home = mac_paths.mac_home()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if os.environ.get("MAC_HUB_SELF_UPGRADE_ENABLED", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="mac-fleet-upgrade",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def tick(self) -> JsonDict:
        resumed = self.resume_pending(actor="hub-upgrade-controller")
        row = self.store.query_one(
            """
            SELECT * FROM fleet_upgrades
            WHERE state IN ('requested', 'staged', 'hub_applying')
              AND phase IN ('requested', 'source_staged', 'source_verified',
                            'hub_swap_armed')
            ORDER BY created_at, id LIMIT 1
            """
        )
        if row is None:
            return {"schema": UPGRADE_SCHEMA, "resumed": len(resumed), "action": "idle"}
        upgrade_id = str(row["id"])
        state = str(row["state"])
        actor = "hub-upgrade-controller"
        if state == "requested":
            self.stage(
                upgrade_id,
                actor=actor,
                branch=os.environ.get("MAC_HUB_UPGRADE_BRANCH", "main"),
                explicit_required_checks=[
                    item.strip()
                    for item in os.environ.get("MAC_HUB_UPGRADE_REQUIRED_CHECKS", "").split(",")
                    if item.strip()
                ],
            )
            state = "staged"
        if state == "staged":
            service = (
                os.environ.get("MAC_LAUNCHD_LABEL", "com.mac.control-plane")
                if platform.system() == "Darwin"
                else os.environ.get("MAC_SERVICE_NAME", "mac.service")
            )
            port = os.environ.get("MAC_PORT", "8789")
            self.arm_hub_swap(
                upgrade_id,
                actor=actor,
                service=service,
                health_url="http://127.0.0.1:%s/health" % port,
                attestation_url="http://127.0.0.1:%s/startup-attestation" % port,
            )
            state = "hub_applying"
        if state == "hub_applying":
            current = self._row(upgrade_id)
            if str(current["phase"]) == "hub_swap_armed":
                self.launch_hub_swap(upgrade_id, actor=actor)
                return {
                    "schema": UPGRADE_SCHEMA,
                    "resumed": len(resumed),
                    "action": "supervisor_launched",
                    "upgrade_id": upgrade_id,
                }
        return {
            "schema": UPGRADE_SCHEMA,
            "resumed": len(resumed),
            "action": "advanced",
            "upgrade_id": upgrade_id,
        }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # The transaction records a precise failure. The controller
                # remains alive for later requests and explicit retries.
                pass
            self._stop_event.wait(5)

    def request(
        self,
        *,
        fleet_id: str,
        requested_by_human: str,
        requested_by_principal: str,
        idempotency_key: str,
        target_policy: str,
        reason: str,
        requested_release_id: Optional[str] = None,
        slack_provenance: Optional[JsonDict] = None,
        recovery_policy: str = "retain-upgraded-hub",
    ) -> JsonDict:
        human_id = str(requested_by_human or "").strip()
        principal_id = str(requested_by_principal or "").strip()
        key = str(idempotency_key or "").strip()
        if not human_id or not principal_id or not key or not reason.strip():
            raise ValidationError(
                "fleet upgrade requires bound human, principal, idempotency key, and reason"
            )
        if target_policy not in {"approved-current", "registered-release"}:
            raise ValidationError("unsupported fleet upgrade target policy")
        if target_policy == "registered-release" and not requested_release_id:
            raise ValidationError("registered-release target requires requested_release_id")
        if target_policy == "approved-current" and requested_release_id:
            raise ValidationError("approved-current target cannot assert a release id")
        if recovery_policy not in {
            "retain-upgraded-hub",
            "rollback-hub-on-cohort-failure",
        }:
            raise ValidationError("unsupported fleet upgrade recovery policy")
        provenance = ensure_json_object(slack_provenance or {})
        if provenance and not {
            "workspace_id",
            "channel_id",
            "message_ts",
        }.issubset(provenance):
            raise ValidationError("Slack provenance requires workspace_id, channel_id, message_ts")
        request_material = {
            "fleet_id": fleet_id,
            "requested_by_human": human_id,
            "requested_by_principal": principal_id,
            "target_policy": target_policy,
            "requested_release_id": requested_release_id,
            "reason": reason.strip(),
            "slack_provenance": provenance,
            "recovery_policy": recovery_policy,
        }
        request_sha = self._digest(request_material)
        existing = self.store.query_one(
            "SELECT * FROM fleet_upgrades WHERE idempotency_key = ?", (key,)
        )
        if existing is not None:
            if str(existing["request_sha256"]) != request_sha:
                raise ValidationError(
                    "fleet upgrade idempotency key was reused with another request"
                )
            return self._upgrade(existing)
        if self.store.query_one("SELECT id FROM fleets WHERE id = ?", (fleet_id,)) is None:
            raise NotFoundError("fleet not found: %s" % fleet_id)
        if self.store.get_human(human_id) is None:
            raise NotFoundError("human not found: %s" % human_id)
        if requested_release_id:
            release = self.cp.get_source_release(requested_release_id)
            if release.status != "published":
                raise ValidationError("registered fleet upgrade release is not published")
        upgrade_id = new_id("upgrade")
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO fleet_upgrades (
                id, idempotency_key, request_sha256, fleet_id,
                requested_by_human, requested_by_principal, target_policy,
                requested_release_id, resolved_release_id, reason, slack_provenance,
                recovery_policy, state, phase, generation_id, staged_source_path,
                stage_evidence, stage_evidence_digest, handoff_path, handoff_digest,
                supervisor_receipt, epoch_id, epoch_identity_sha256,
                desired_source_state_id, error_code, error_detail,
                created_at, updated_at, completed_at, cancelled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'requested',
                      'requested', NULL, NULL, '{}', NULL, NULL, NULL, '{}',
                      NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL)
            """,
            (
                upgrade_id,
                key,
                request_sha,
                fleet_id,
                human_id,
                principal_id,
                target_policy,
                requested_release_id,
                reason.strip(),
                json_dumps(provenance),
                recovery_policy,
                now,
                now,
            ),
        )
        self._event(upgrade_id, "requested", "requested", request_material, human_id)
        return self.get(upgrade_id)

    def get(self, upgrade_id: str) -> JsonDict:
        row = self.store.query_one("SELECT * FROM fleet_upgrades WHERE id = ?", (upgrade_id,))
        if row is None:
            raise NotFoundError("fleet upgrade not found: %s" % upgrade_id)
        return self._upgrade(row)

    def list(
        self,
        *,
        fleet_id: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> List[JsonDict]:
        clauses: List[str] = []
        params: List[Any] = []
        if fleet_id:
            clauses.append("fleet_id = ?")
            params.append(fleet_id)
        if state:
            clauses.append("state = ?")
            params.append(state)
        params.append(max(1, min(int(limit), 1000)))
        rows = self.store.query_all(
            "SELECT * FROM fleet_upgrades%s ORDER BY created_at DESC, id DESC LIMIT ?"
            % ((" WHERE " + " AND ".join(clauses)) if clauses else ""),
            tuple(params),
        )
        return [self._upgrade(row) for row in rows]

    def cancel(self, upgrade_id: str, *, actor: str, reason: str) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) not in _PRE_MUTATION_STATES:
            raise TransitionError("fleet upgrade cannot be cancelled after mutation is armed")
        now = utcnow()
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET state = 'cancelled', phase = 'cancelled', cancelled_at = ?,
                error_code = 'cancelled', error_detail = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, reason.strip(), now, upgrade_id),
        )
        self._event(upgrade_id, "cancelled", "cancelled", {"reason": reason}, actor)
        return self.get(upgrade_id)

    def stage(
        self,
        upgrade_id: str,
        *,
        actor: str,
        branch: str = "main",
        explicit_required_checks: Optional[Iterable[str]] = None,
    ) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) == "staged":
            return self._upgrade(row)
        if str(row["state"]) != "requested":
            raise TransitionError("fleet upgrade is not ready for source staging")
        if str(row["target_policy"]) == "registered-release":
            release = self.cp.get_source_release(str(row["requested_release_id"]))
            try:
                staged = self.source_gate.stage_registered_release(
                    transaction_id=upgrade_id,
                    canonical_remote_url=release.canonical_remote_url,
                    commit_sha=release.commit_sha,
                    tree_digest=release.tree_digest,
                    ci_evidence=ensure_json_object(release.metadata.get("ci")),
                )
            except Exception as exc:
                self._fail(upgrade_id, "stage_gate_failed", str(exc), actor)
                raise
            self.store.execute(
                """
                UPDATE fleet_upgrades
                SET resolved_release_id = ?, generation_id = ?, staged_source_path = ?,
                    stage_evidence = ?, stage_evidence_digest = ?, state = 'staged',
                    phase = 'source_staged', updated_at = ?
                WHERE id = ?
                """,
                (
                    release.id,
                    "hub-%s-%s" % (upgrade_id, release.commit_sha[:12]),
                    staged.stage_path,
                    json_dumps(staged.evidence),
                    staged.evidence_digest,
                    utcnow(),
                    upgrade_id,
                ),
            )
            self._event(
                upgrade_id,
                "source_staged",
                "source_staged",
                {
                    "release_id": release.id,
                    "commit_sha": release.commit_sha,
                    "evidence_digest": staged.evidence_digest,
                },
                actor,
            )
            return self.get(upgrade_id)
        self.store.execute(
            "UPDATE fleet_upgrades SET state = 'staging', phase = 'staging', updated_at = ? WHERE id = ?",
            (utcnow(), upgrade_id),
        )
        self._event(upgrade_id, "staging", "staging", {"branch": branch}, actor)
        try:
            staged = self.source_gate.stage_approved_current(
                transaction_id=upgrade_id,
                branch=branch,
                explicit_required_checks=list(explicit_required_checks or ()),
            )
            release = self.cp.register_source_release(
                repository_id="repository:%s" % staged.repository_name,
                repository_name=staged.repository_name,
                canonical_remote_url=staged.canonical_remote_url,
                commit_sha=staged.commit_sha,
                canonical_ref=staged.canonical_ref,
                tree_digest=staged.tree_digest,
                created_by=actor,
                status="reviewed",
                metadata={
                    **staged.evidence,
                    "gate_evidence_digest": staged.evidence_digest,
                    "convergence_action": "full_redeploy_required",
                },
            )
        except Exception as exc:
            self._fail(upgrade_id, "stage_gate_failed", str(exc), actor)
            raise
        generation_id = "hub-%s-%s" % (upgrade_id, staged.commit_sha[:12])
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET resolved_release_id = ?, generation_id = ?, staged_source_path = ?,
                stage_evidence = ?, stage_evidence_digest = ?, state = 'staged',
                phase = 'source_staged', updated_at = ?
            WHERE id = ?
            """,
            (
                release.id,
                generation_id,
                staged.stage_path,
                json_dumps(staged.evidence),
                staged.evidence_digest,
                utcnow(),
                upgrade_id,
            ),
        )
        self._event(
            upgrade_id,
            "source_staged",
            "source_staged",
            {
                "release_id": release.id,
                "commit_sha": release.commit_sha,
                "evidence_digest": staged.evidence_digest,
            },
            actor,
        )
        return self.get(upgrade_id)

    def arm_hub_swap(
        self,
        upgrade_id: str,
        *,
        actor: str,
        service: str,
        health_url: str,
        attestation_url: str,
        authorization_ttl_seconds: int = 900,
    ) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) == "hub_applying":
            return self._upgrade(row)
        if str(row["state"]) != "staged":
            raise TransitionError("fleet upgrade source is not staged")
        release = self.cp.get_source_release(str(row["resolved_release_id"]))
        stage = Path(str(row["staged_source_path"] or ""))
        if not stage.is_dir() or not (stage / ".venv" / "bin" / "python").is_file():
            raise ValidationError("complete staged source and venv generation is required")
        current_root = self.mac_home / "current"
        source_link = current_root / "source"
        venv_link = current_root / "venv"
        if not source_link.is_symlink() or not venv_link.is_symlink():
            raise ValidationError(
                "hub generation links are not initialized; run the compatibility migration first"
            )
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=max(60, min(int(authorization_ttl_seconds), 3600))
        )
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "transaction_id": upgrade_id,
            "generation_id": str(row["generation_id"]),
            "authorization": {
                "status": "authorized",
                "human_id": str(row["requested_by_human"]),
                "principal_id": str(row["requested_by_principal"]),
                "authorized_at": utcnow(),
                "expires_at": expires.isoformat(),
            },
            "source_link": str(source_link),
            "venv_link": str(venv_link),
            "staged_source": str(stage),
            "staged_venv": str(stage / ".venv"),
            "service": service,
            "health_url": health_url,
            "attestation_url": attestation_url,
            "expected_commit_sha": release.commit_sha,
            "required_health_successes": 3,
            "health_timeout_seconds": 180,
            "stage_evidence_digest": str(row["stage_evidence_digest"]),
            "recovery_policy": str(row["recovery_policy"]),
        }
        handoff_root = self.mac_home / "upgrades" / "handoffs"
        handoff_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        handoff_path = handoff_root / ("%s.json" % upgrade_id)
        raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        temporary = handoff_path.with_name(".%s.%d.tmp" % (handoff_path.name, os.getpid()))
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        os.replace(temporary, handoff_path)
        fenced_agents = self._fence_worker_cohort(row)
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET handoff_path = ?, handoff_digest = ?, state = 'hub_applying',
                phase = 'hub_swap_armed', updated_at = ?
            WHERE id = ?
            """,
            (str(handoff_path), digest, utcnow(), upgrade_id),
        )
        self._event(
            upgrade_id,
            "hub_swap_armed",
            "hub_swap_armed",
            {
                "manifest_digest": digest,
                "generation_id": row["generation_id"],
                "fenced_agents": fenced_agents,
            },
            actor,
        )
        result = self.get(upgrade_id)
        result["supervisor"] = {
            "argv": [
                "mac-hub-upgrade-supervisor",
                "--mac-home",
                str(self.mac_home),
                "apply",
                str(handoff_path),
                "--digest",
                digest,
            ]
        }
        return result

    def resume_pending(self, *, actor: str = "hub-startup") -> List[JsonDict]:
        rows = self.store.query_all(
            """
            SELECT * FROM fleet_upgrades
            WHERE state IN ('hub_applying', 'hub_rollback_required')
            ORDER BY created_at, id
            """
        )
        resumed: List[JsonDict] = []
        for row in rows:
            receipt_path = (
                self.mac_home / "upgrades" / "supervisor" / ("%s.receipt.json" % str(row["id"]))
            )
            state_path = (
                self.mac_home / "upgrades" / "supervisor" / ("%s.state.json" % str(row["id"]))
            )
            if not receipt_path.is_file():
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if state.get("phase") in {"rolled_back", "rollback_failed"}:
                        self.store.execute(
                            """
                            UPDATE fleet_upgrades
                            SET state = 'rolled_back', phase = 'hub_rolled_back',
                                error_code = 'hub_swap_failed',
                                error_detail = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                str(state.get("error") or "hub swap rolled back"),
                                utcnow(),
                                row["id"],
                            ),
                        )
                        self._clear_upgrade_holds(str(row["id"]))
                        self._event(
                            str(row["id"]),
                            "hub_rolled_back",
                            "hub_rolled_back",
                            {"supervisor_phase": state.get("phase")},
                            actor,
                        )
                        resumed.append(self.get(str(row["id"])))
                continue
            try:
                resumed.append(
                    self.record_supervisor_receipt(
                        str(row["id"]),
                        receipt=json.loads(receipt_path.read_text(encoding="utf-8")),
                        actor=actor,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one corrupt receipt cannot block another.
                self._fail(str(row["id"]), "supervisor_receipt_invalid", str(exc), actor)
        return resumed

    def _fence_worker_cohort(self, row: Any) -> List[str]:
        members = self.store.query_all(
            "SELECT agent_id FROM fleet_agents WHERE fleet_id = ? ORDER BY agent_id",
            (row["fleet_id"],),
        )
        hub_identity = os.environ.get("MAC_REVIEW_TICK_HUB_AGENT", "").strip()
        reason = "fleet_upgrade:%s:hub_cutover" % row["id"]
        fenced: List[str] = []
        busy: List[str] = []
        for member in members:
            agent = self.cp.get_agent(str(member["agent_id"]))
            if agent.id == hub_identity or agent.name == hub_identity:
                continue
            if agent.deleted_at:
                continue
            resources = agent.resources if isinstance(agent.resources, dict) else {}
            if resources.get("virtual") is True:
                continue
            existing_reason = str(agent.dispatch_hold_reason or "")
            if agent.dispatch_hold and existing_reason != reason:
                # Interactive sessions and other operator holds stay with their
                # issuer. Failing the whole cutover because a laptop session is
                # held made hub-mediated upgrade unusable on the live fleet.
                continue
            if str(agent.current_task_id or "") or str(agent.status) == "busy":
                busy.append(agent.id)
                continue
            self.cp.set_agent_dispatch_hold(agent.id, reason)
            fenced.append(agent.id)
        if busy:
            raise ValidationError("worker cohort is not quiescent: %s" % ", ".join(sorted(busy)))
        return fenced

    def _clear_upgrade_holds(self, upgrade_id: str) -> None:
        reason = "fleet_upgrade:%s:hub_cutover" % upgrade_id
        rows = self.store.query_all(
            "SELECT id, dispatch_hold, dispatch_hold_reason FROM agents "
            "WHERE dispatch_hold = 1 AND dispatch_hold_reason = ?",
            (reason,),
        )
        for row in rows:
            self.cp.clear_agent_dispatch_hold(str(row["id"]))

    def launch_hub_swap(self, upgrade_id: str, *, actor: str) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) != "hub_applying" or str(row["phase"]) not in {
            "hub_swap_armed",
            "supervisor_launched",
        }:
            raise TransitionError("hub swap is not armed")
        if str(row["phase"]) == "supervisor_launched":
            return self._upgrade(row)
        executable = self.mac_home / "current" / "venv" / "bin" / "mac-hub-upgrade-supervisor"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValidationError("host upgrade supervisor executable is not installed")
        argv = [
            str(executable),
            "--mac-home",
            str(self.mac_home),
            "apply",
            str(row["handoff_path"]),
            "--digest",
            str(row["handoff_digest"]),
        ]
        unit_suffix = hashlib.sha256(upgrade_id.encode("utf-8")).hexdigest()[:16]
        if platform.system() == "Linux":
            launcher = [
                "sudo",
                "-n",
                "systemd-run",
                "--unit=mac-hub-upgrade-%s" % unit_suffix,
                "--property=Type=oneshot",
                "--uid=%d" % os.getuid(),
                "--collect",
                "--no-block",
                *argv,
            ]
            launcher_backend = "systemd"
        elif platform.system() == "Darwin":
            launcher = [
                "sudo",
                "-n",
                "launchctl",
                "submit",
                "-l",
                "com.mac.hub-upgrade.%s" % unit_suffix,
                "--",
                "/usr/bin/sudo",
                "-u",
                pwd.getpwuid(os.getuid()).pw_name,
                *argv,
            ]
            launcher_backend = "launchd"
        else:
            raise ValidationError("host upgrade supervisor launcher is unsupported")
        launched = subprocess.run(
            launcher,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if launched.returncode != 0:
            raise ValidationError(
                "host service manager refused upgrade supervisor: %s"
                % (launched.stderr.strip() or launched.returncode)
            )
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET phase = 'supervisor_launched', updated_at = ?
            WHERE id = ?
            """,
            (utcnow(), upgrade_id),
        )
        self._event(
            upgrade_id,
            "supervisor_launched",
            "supervisor_launched",
            {
                "launcher": launcher_backend,
                "manifest_digest": row["handoff_digest"],
            },
            actor,
        )
        return self.get(upgrade_id)

    def record_supervisor_receipt(
        self,
        upgrade_id: str,
        *,
        receipt: Mapping[str, Any],
        actor: str,
    ) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) == "hub_committed":
            return self._upgrade(row)
        if str(row["state"]) != "hub_applying":
            raise TransitionError("fleet upgrade is not awaiting a hub receipt")
        receipt_value = dict(receipt)
        digest = str(receipt_value.pop("receipt_digest", ""))
        if digest != self._digest(receipt_value):
            raise ValidationError("supervisor receipt digest is invalid")
        if (
            receipt_value.get("schema") != RECEIPT_SCHEMA
            or receipt_value.get("status") != "committed"
            or receipt_value.get("manifest_digest") != row["handoff_digest"]
            or receipt_value.get("transaction_id") != upgrade_id
        ):
            raise ValidationError("supervisor receipt does not authorize this transaction")
        release = self.cp.get_source_release(str(row["resolved_release_id"]))
        if receipt_value.get("expected_commit_sha") != release.commit_sha:
            raise ValidationError("supervisor receipt source commit mismatch")
        metadata = dict(release.metadata)
        metadata["supervisor_receipt_digest"] = digest
        metadata["supervisor_receipt"] = receipt_value
        self.store.execute(
            """
            UPDATE source_releases
            SET status = 'published', metadata = ?, updated_at = ?
            WHERE id = ? AND status = 'reviewed'
            """,
            (json_dumps(metadata), utcnow(), release.id),
        )
        now = utcnow()
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET supervisor_receipt = ?, state = 'hub_committed',
                phase = 'hub_health_proved', updated_at = ?
            WHERE id = ?
            """,
            (json_dumps({**receipt_value, "receipt_digest": digest}), now, upgrade_id),
        )
        self._event(
            upgrade_id,
            "hub_health_proved",
            "hub_health_proved",
            {"receipt_digest": digest, "commit_sha": release.commit_sha},
            actor,
        )
        return self.get(upgrade_id)

    def open_worker_epoch(
        self,
        upgrade_id: str,
        participants: Iterable[Mapping[str, Any]],
        *,
        actor: str,
    ) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) == "workers_open":
            return self._upgrade(row)
        if str(row["state"]) != "hub_committed":
            raise TransitionError("upgraded hub has not proved health")
        epoch_id = "upgrade-%s" % upgrade_id
        receipt = self.cp.fleet_release_epochs.open_epoch(
            epoch_id,
            participants,
            successor_hold_reason="source_convergence:fleet_upgrade=%s" % upgrade_id,
            actor=actor,
        )
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET epoch_id = ?, epoch_identity_sha256 = ?, state = 'workers_open',
                phase = 'worker_epoch_open', updated_at = ?
            WHERE id = ?
            """,
            (epoch_id, receipt["identity_sha256"], utcnow(), upgrade_id),
        )
        self._event(
            upgrade_id,
            "worker_epoch_open",
            "worker_epoch_open",
            {"epoch_id": epoch_id, "identity_sha256": receipt["identity_sha256"]},
            actor,
        )
        return self.get(upgrade_id)

    def prove_worker_epoch(
        self,
        upgrade_id: str,
        proofs: Iterable[Mapping[str, Any]],
        *,
        actor: str,
    ) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) == "workers_proved":
            return self._upgrade(row)
        if str(row["state"]) != "workers_open":
            raise TransitionError("worker release epoch is not open")
        self.cp.fleet_release_epochs.prove(
            str(row["epoch_id"]),
            str(row["epoch_identity_sha256"]),
            proofs,
            actor=actor,
        )
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET state = 'workers_proved', phase = 'worker_epoch_proved', updated_at = ?
            WHERE id = ?
            """,
            (utcnow(), upgrade_id),
        )
        self._event(
            upgrade_id,
            "worker_epoch_proved",
            "worker_epoch_proved",
            {"epoch_id": row["epoch_id"]},
            actor,
        )
        return self.get(upgrade_id)

    def commit_worker_epoch(self, upgrade_id: str, *, actor: str) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) == "completed":
            return self._upgrade(row)
        if str(row["state"]) != "workers_proved":
            raise TransitionError("worker release epoch is not proved")
        self.cp.fleet_release_epochs.commit(
            str(row["epoch_id"]),
            str(row["epoch_identity_sha256"]),
            actor=actor,
        )
        desired = self.cp.set_fleet_desired_source(
            fleet_id=str(row["fleet_id"]),
            release_id=str(row["resolved_release_id"]),
            actor=actor,
            reason="fleet upgrade %s committed" % upgrade_id,
            request_id="fleet-upgrade:%s" % upgrade_id,
        )
        now = utcnow()
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET desired_source_state_id = ?, state = 'completed', phase = 'completed',
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (desired.id, now, now, upgrade_id),
        )
        self._event(
            upgrade_id,
            "completed",
            "completed",
            {"epoch_id": row["epoch_id"], "desired_generation": desired.generation},
            actor,
        )
        return self.get(upgrade_id)

    def abort_worker_epoch(
        self,
        upgrade_id: str,
        *,
        actor: str,
        reason: str,
        disposition: str = "restore",
    ) -> JsonDict:
        row = self._row(upgrade_id)
        if str(row["state"]) not in {"workers_open", "workers_proved"}:
            raise TransitionError("worker release epoch is not abortable")
        self.cp.fleet_release_epochs.abort(
            str(row["epoch_id"]),
            str(row["epoch_identity_sha256"]),
            actor=actor,
            reason=reason,
            disposition=disposition,
        )
        rollback_hub = str(row["recovery_policy"]) == "rollback-hub-on-cohort-failure"
        state = "hub_rollback_required" if rollback_hub else "rolled_back"
        phase = "hub_rollback_required" if rollback_hub else "worker_epoch_aborted"
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET state = ?, phase = ?, error_code = 'worker_epoch_aborted',
                error_detail = ?, updated_at = ?
            WHERE id = ?
            """,
            (state, phase, reason, utcnow(), upgrade_id),
        )
        self._event(
            upgrade_id,
            phase,
            phase,
            {"epoch_id": row["epoch_id"], "reason": reason},
            actor,
        )
        if not rollback_hub:
            self._clear_upgrade_holds(upgrade_id)
        result = self.get(upgrade_id)
        if rollback_hub:
            result["supervisor"] = {
                "argv": [
                    "mac-hub-upgrade-supervisor",
                    "--mac-home",
                    str(self.mac_home),
                    "rollback",
                    str(row["handoff_path"]),
                    "--digest",
                    str(row["handoff_digest"]),
                ]
            }
        return result

    def events(self, upgrade_id: str) -> List[JsonDict]:
        self._row(upgrade_id)
        rows = self.store.query_all(
            "SELECT * FROM fleet_upgrade_events WHERE upgrade_id = ? ORDER BY created_at, id",
            (upgrade_id,),
        )
        return [
            {
                **{key: row[key] for key in row.keys()},
                "detail": json_loads(row["detail"], {}),
            }
            for row in rows
        ]

    def _row(self, upgrade_id: str) -> Any:
        row = self.store.query_one("SELECT * FROM fleet_upgrades WHERE id = ?", (upgrade_id,))
        if row is None:
            raise NotFoundError("fleet upgrade not found: %s" % upgrade_id)
        return row

    def _fail(self, upgrade_id: str, code: str, detail: str, actor: str) -> None:
        self.store.execute(
            """
            UPDATE fleet_upgrades
            SET state = 'failed', phase = 'failed', error_code = ?,
                error_detail = ?, updated_at = ?
            WHERE id = ?
            """,
            (code, detail[:2000], utcnow(), upgrade_id),
        )
        self._event(upgrade_id, "failed", "failed", {"code": code, "detail": detail}, actor)

    def _event(
        self,
        upgrade_id: str,
        event_type: str,
        phase: str,
        detail: Mapping[str, Any],
        actor: str,
    ) -> None:
        now = utcnow()
        safe_detail = ensure_json_object(detail)
        self.store.execute(
            """
            INSERT INTO fleet_upgrade_events (
                id, upgrade_id, event_type, phase, detail, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("upgradeevent"),
                upgrade_id,
                event_type,
                phase,
                json_dumps(safe_detail),
                actor,
                now,
            ),
        )
        try:
            self.cp.agentbus_broadcast.publish_system(
                PROGRESS_EVENT,
                payload={
                    "upgrade_id": upgrade_id,
                    "phase": phase,
                    "event_type": event_type,
                    "detail_digest": self._digest(safe_detail),
                },
            )
        except Exception:
            # The SQL event is the durable authority. Broadcast is a wake-up,
            # and an unavailable bus must not corrupt the transaction.
            pass

    @staticmethod
    def _upgrade(row: Any) -> JsonDict:
        result = {key: row[key] for key in row.keys()}
        for key in ("slack_provenance", "stage_evidence", "supervisor_receipt"):
            result[key] = json_loads(result.get(key), {})
        return result

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()
