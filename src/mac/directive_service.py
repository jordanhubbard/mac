"""Hub-owned lifecycle for immutable, fleet-wide directives."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from mac.directive_models import (
    DIRECTIVE_ACTIVATION_SCHEMA,
    DIRECTIVE_SCHEMA,
    DIRECTIVE_SNAPSHOT_SCHEMA,
    DirectiveDocument,
    canonical_digest,
    condition_overlap,
    evaluate_directive,
    parse_directive_document,
)
from mac.models import (
    JsonDict,
    NotFoundError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.effect_conflicts import DeclaredEffects, effect_conflicts


SYSTEM_DIRECTIVE_NAME = "system.executor-safety"
SYSTEM_DIRECTIVE_ID = "directive_system_executor_safety"
_ACTIVE_STATES = {"active", "distributing"}
_TARGET_TYPES = {"fleet", "project", "repository"}


class DirectiveService:
    """Own directive proposal, analysis, approval, activation, and snapshots.

    Callbacks keep the policy engine independent from orchestration details.
    A macro expander must return a held work-package description; the service
    never activates the resulting package.
    """

    def __init__(
        self,
        store: Any,
        *,
        enabled: bool = False,
        workflow_resolver: Optional[Callable[[str, int], Any]] = None,
        macro_expander: Optional[
            Callable[[JsonDict, JsonDict, JsonDict, JsonDict], Mapping[str, Any]]
        ] = None,
        activation_notifier: Optional[Callable[[str, JsonDict], None]] = None,
    ) -> None:
        self.store = store
        self.enabled = bool(enabled)
        self.workflow_resolver = workflow_resolver
        self.macro_expander = macro_expander
        self.activation_notifier = activation_notifier
        if self.enabled:
            self._ensure_system_directive()

    # Proposal and immutable versions ---------------------------------

    def propose(self, raw_document: Mapping[str, Any], *, actor: str) -> JsonDict:
        self._require_enabled()
        document = parse_directive_document(raw_document)
        actor_value = self._required_text(actor, "directive actor")
        if document.name == SYSTEM_DIRECTIVE_NAME:
            raise ValidationError("the system executor-safety directive is reserved")
        existing = self.store.query_one(
            "SELECT * FROM fleet_directives WHERE name = ?", (document.name,)
        )
        now = utcnow()
        if existing is None:
            directive_id = new_id("directive")
            version = 1
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO fleet_directives (id, name, description, scope, "
                    "current_version, state, reserved, created_by, updated_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                    (
                        directive_id,
                        document.name,
                        document.description,
                        document.scope,
                        version,
                        "proposed",
                        actor_value,
                        actor_value,
                        now,
                        now,
                    ),
                )
                self._insert_version(conn, directive_id, version, document, actor_value, now)
        else:
            if int(existing["reserved"]):
                raise ValidationError("reserved directives cannot be changed")
            current = self._version_row(str(existing["id"]), int(existing["current_version"]))
            if str(current["digest"]) == document.digest:
                return self.get(str(existing["id"]))
            directive_id = str(existing["id"])
            version = int(existing["current_version"]) + 1
            with self.store.transaction() as conn:
                self._insert_version(conn, directive_id, version, document, actor_value, now)
                updated = conn.execute(
                    "UPDATE fleet_directives SET description = ?, current_version = ?, state = ?, "
                    "updated_by = ?, updated_at = ? WHERE id = ? AND current_version = ?",
                    (
                        document.description,
                        version,
                        "proposed",
                        actor_value,
                        now,
                        directive_id,
                        version - 1,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValidationError("directive version update lost a concurrent race")
        return self.get(directive_id)

    def list(self, *, state: Optional[str] = None) -> List[JsonDict]:
        sql = "SELECT * FROM fleet_directives"
        params: Tuple[Any, ...] = ()
        if state:
            sql += " WHERE state = ?"
            params = (state,)
        sql += " ORDER BY name"
        return [self._directive_dict(row) for row in self.store.query_all(sql, params)]

    def get(self, directive_id_or_name: str) -> JsonDict:
        row = self.store.query_one(
            "SELECT * FROM fleet_directives WHERE id = ? OR name = ? ORDER BY id = ? DESC LIMIT 1",
            (directive_id_or_name, directive_id_or_name, directive_id_or_name),
        )
        if row is None:
            raise NotFoundError("directive not found: %s" % directive_id_or_name)
        result = self._directive_dict(row)
        result["versions"] = self.versions(str(row["id"]))
        result["latest_check"] = self._latest_check(str(row["id"]), int(row["current_version"]))
        result["activation"] = self._latest_activation(str(row["id"]))
        return result

    def versions(self, directive_id_or_name: str) -> List[JsonDict]:
        directive = self._directive_row(directive_id_or_name)
        rows = self.store.query_all(
            "SELECT * FROM fleet_directive_versions WHERE directive_id = ? ORDER BY version DESC",
            (directive["id"],),
        )
        return [self._version_dict(row) for row in rows]

    # Bindings and waivers ---------------------------------------------

    def set_binding(
        self,
        *,
        target_type: str,
        target_id: str,
        key: str,
        value: Any,
        actor: str,
    ) -> JsonDict:
        self._require_enabled()
        target_type_value, target_id_value = self._validate_target(target_type, target_id)
        key_value = self._required_path(key, "directive binding key")
        binding_words = set(key_value.lower().replace("-", "_").replace(".", "_").split("_"))
        if binding_words & {
            "token",
            "secret",
            "password",
            "passwd",
            "credential",
            "credentials",
            "api_key",
            "private_key",
        } or any(
            marker in key_value.lower().replace("-", "").replace("_", "").replace(".", "")
            for marker in ("apikey", "privatekey", "clientsecret", "accesskey")
        ):
            raise ValidationError("directive bindings cannot contain credential material")
        actor_value = self._required_text(actor, "directive binding actor")
        # Reuse document validation's JSON and secret checks by embedding the
        # value in an otherwise harmless variable default.
        parse_directive_document(
            {
                "schema": DIRECTIVE_SCHEMA,
                "name": "binding.validation",
                "description": "Validate a hub-owned directive binding.",
                "scope": "fleet",
                "variables": {
                    "value": {
                        "type": self._value_type(value),
                        "binding": "binding.value",
                        "default": value,
                    }
                },
                "set": {"binding.validation": True},
            }
        )
        prior = self.store.query_one(
            "SELECT COALESCE(MAX(version), 0) AS version FROM fleet_directive_bindings "
            "WHERE target_type = ? AND target_id = ? AND binding_key = ?",
            (target_type_value, target_id_value, key_value),
        )
        version = int(prior["version"] if prior else 0) + 1
        now = utcnow()
        binding_id = new_id("binding")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE fleet_directive_bindings SET active = 0, superseded_at = ? "
                "WHERE target_type = ? AND target_id = ? AND binding_key = ? AND active = 1",
                (now, target_type_value, target_id_value, key_value),
            )
            conn.execute(
                "INSERT INTO fleet_directive_bindings (id, target_type, target_id, binding_key, "
                "binding_value, version, active, created_by, created_at, superseded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)",
                (
                    binding_id,
                    target_type_value,
                    target_id_value,
                    key_value,
                    json_dumps(value),
                    version,
                    actor_value,
                    now,
                ),
            )
        return self._binding_dict(
            self.store.query_one(
                "SELECT * FROM fleet_directive_bindings WHERE id = ?", (binding_id,)
            )
        )

    def list_bindings(
        self,
        *,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        active: bool = True,
    ) -> List[JsonDict]:
        clauses = ["active = ?"]
        params: List[Any] = [1 if active else 0]
        if target_type:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        rows = self.store.query_all(
            "SELECT * FROM fleet_directive_bindings WHERE %s "
            "ORDER BY target_type, target_id, binding_key, version DESC" % " AND ".join(clauses),
            tuple(params),
        )
        return [self._binding_dict(row) for row in rows]

    def create_waiver(
        self,
        directive_id_or_name: str,
        *,
        version: int,
        target_type: str,
        target_id: str,
        reason: str,
        actor: str,
        expires_at: Optional[str] = None,
    ) -> JsonDict:
        self._require_enabled()
        directive = self._directive_row(directive_id_or_name)
        if int(directive["reserved"]):
            raise ValidationError("reserved system constraints cannot be waived")
        self._version_row(str(directive["id"]), int(version))
        target_type_value, target_id_value = self._validate_target(target_type, target_id)
        if target_type_value == "fleet":
            raise ValidationError("fleet-wide waivers are not allowed")
        expires = self._optional_timestamp(expires_at)
        waiver_id = new_id("waiver")
        self.store.execute(
            "INSERT INTO fleet_directive_waivers (id, directive_id, directive_version, "
            "target_type, target_id, reason, created_by, created_at, expires_at, "
            "revoked_by, revoked_at, revoke_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                waiver_id,
                directive["id"],
                int(version),
                target_type_value,
                target_id_value,
                self._required_text(reason, "directive waiver reason"),
                self._required_text(actor, "directive waiver actor"),
                utcnow(),
                expires,
            ),
        )
        return self._waiver_dict(
            self.store.query_one("SELECT * FROM fleet_directive_waivers WHERE id = ?", (waiver_id,))
        )

    def revoke_waiver(self, waiver_id: str, *, actor: str, reason: str) -> JsonDict:
        row = self.store.query_one(
            "SELECT * FROM fleet_directive_waivers WHERE id = ?", (waiver_id,)
        )
        if row is None:
            raise NotFoundError("directive waiver not found: %s" % waiver_id)
        if row["revoked_at"]:
            return self._waiver_dict(row)
        self.store.execute(
            "UPDATE fleet_directive_waivers SET revoked_by = ?, revoked_at = ?, revoke_reason = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (
                self._required_text(actor, "directive waiver actor"),
                utcnow(),
                self._required_text(reason, "directive waiver revoke reason"),
                waiver_id,
            ),
        )
        return self._waiver_dict(
            self.store.query_one("SELECT * FROM fleet_directive_waivers WHERE id = ?", (waiver_id,))
        )

    def list_waivers(self, directive_id_or_name: Optional[str] = None) -> List[JsonDict]:
        params: Tuple[Any, ...] = ()
        sql = "SELECT * FROM fleet_directive_waivers"
        if directive_id_or_name:
            directive = self._directive_row(directive_id_or_name)
            sql += " WHERE directive_id = ?"
            params = (directive["id"],)
        sql += " ORDER BY created_at DESC"
        return [self._waiver_dict(row) for row in self.store.query_all(sql, params)]

    # Check, approval, activation -------------------------------------

    def check(
        self,
        directive_id_or_name: str,
        *,
        version: Optional[int] = None,
        actor: str,
    ) -> JsonDict:
        self._require_enabled()
        directive = self._directive_row(directive_id_or_name)
        version_value = int(version or directive["current_version"])
        version_row = self._version_row(str(directive["id"]), version_value)
        document = parse_directive_document(json_loads(version_row["document"], {}))
        repositories = [
            dict(row)
            for row in self.store.query_all(
                "SELECT * FROM project_repositories WHERE enabled = 1 ORDER BY id"
            )
        ]
        active_documents = self._active_documents(exclude_directive_id=str(directive["id"]))
        blockers: List[JsonDict] = []
        warnings: List[JsonDict] = []
        evaluations: List[JsonDict] = []

        # Abstract overlap catches ambiguous future contexts, not merely the
        # repositories that happen to be registered during this check.
        for active, active_document in active_documents:
            overlap = condition_overlap(document.when, active_document.when)
            for key in sorted(set(document.policy) & set(active_document.policy)):
                if document.policy[key] == active_document.policy[key]:
                    continue
                if overlap != "disjoint":
                    blockers.append(
                        {
                            "code": "policy_conflict"
                            if overlap == "overlap"
                            else "policy_overlap_unproven",
                            "key": key,
                            "other_directive_id": active["directive_id"],
                            "other_version": active["version"],
                            "overlap": overlap,
                        }
                    )
            if (
                document.macro is not None
                and active_document.macro is not None
                and overlap != "disjoint"
            ):
                reasons = effect_conflicts(
                    self._symbolic_effects(document.macro.get("effects") or {}),
                    self._symbolic_effects(active_document.macro.get("effects") or {}),
                )
                if reasons:
                    blockers.append(
                        {
                            "code": (
                                "macro_effect_conflict"
                                if overlap == "overlap"
                                else "macro_effect_overlap_unproven"
                            ),
                            "other_directive_id": active["directive_id"],
                            "other_version": active["version"],
                            "overlap": overlap,
                            "reasons": reasons,
                        }
                    )

        for repository in repositories:
            facts = self._facts_for_repository(repository)
            bindings = self._binding_layers(repository)
            waived = self._matching_waiver(str(directive["id"]), version_value, repository)
            try:
                evaluation = evaluate_directive(document, facts=facts, bindings=bindings)
                entry = {
                    "repository_id": repository["id"],
                    "project": repository["project"],
                    "waived": bool(waived),
                    "waiver_id": waived["id"] if waived else None,
                    **evaluation.to_dict(),
                }
                if evaluation.blocked and not waived:
                    blockers.append(
                        {
                            "code": "binding_resolution_failed",
                            "repository_id": repository["id"],
                            "detail": evaluation.reason,
                        }
                    )
                if evaluation.matched and evaluation.macro is not None and not waived:
                    self._check_macro_workflow(evaluation.macro, repository, blockers)
                    self._check_repository_macro_conflicts(
                        evaluation.macro,
                        repository,
                        active_documents,
                        facts,
                        bindings,
                        blockers,
                    )
                evaluations.append(entry)
            except ValidationError as exc:
                blockers.append(
                    {
                        "code": "evaluation_failed",
                        "repository_id": repository["id"],
                        "detail": str(exc),
                    }
                )
                evaluations.append(
                    {
                        "repository_id": repository["id"],
                        "project": repository["project"],
                        "matched": False,
                        "blocked": True,
                        "reason": str(exc),
                        "waived": bool(waived),
                    }
                )

        context = self._current_context_payload()
        context_digest = canonical_digest(context)
        policy_digest = canonical_digest(
            {
                "directive_id": directive["id"],
                "version": version_value,
                "digest": document.digest,
                "evaluations": evaluations,
                "active_directives": [item[0] for item in active_documents],
            }
        )
        status = "pass" if not blockers else "blocked"
        report: JsonDict = {
            "schema": "mac.directive.check.v1",
            "status": status,
            "directive_id": directive["id"],
            "directive_version": version_value,
            "directive_digest": document.digest,
            "context_digest": context_digest,
            "policy_digest": policy_digest,
            "blockers": blockers,
            "warnings": warnings,
            "evaluations": evaluations,
        }
        check_id = new_id("directive_check")
        checked_at = utcnow()
        self.store.execute(
            "INSERT INTO fleet_directive_checks (id, directive_id, directive_version, directive_digest, "
            "context_digest, policy_digest, status, report, checked_by, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                check_id,
                directive["id"],
                version_value,
                document.digest,
                context_digest,
                policy_digest,
                status,
                json_dumps(report),
                self._required_text(actor, "directive check actor"),
                checked_at,
            ),
        )
        report.update({"id": check_id, "checked_at": checked_at})
        return report

    def approve(
        self,
        directive_id_or_name: str,
        *,
        version: int,
        directive_digest: str,
        check_id: str,
        actor: str,
    ) -> JsonDict:
        self._require_enabled()
        directive = self._directive_row(directive_id_or_name)
        version_row = self._version_row(str(directive["id"]), int(version))
        if str(version_row["digest"]) != str(directive_digest):
            raise ValidationError("directive approval digest does not match immutable version")
        check = self.store.query_one(
            "SELECT * FROM fleet_directive_checks WHERE id = ?", (check_id,)
        )
        if check is None:
            raise NotFoundError("directive check not found: %s" % check_id)
        if (
            check["directive_id"] != directive["id"]
            or int(check["directive_version"]) != int(version)
            or check["directive_digest"] != directive_digest
            or check["status"] != "pass"
        ):
            raise ValidationError("only the exact passing directive check can be approved")
        approval_id = new_id("directive_approval")
        now = utcnow()
        try:
            self.store.execute(
                "INSERT INTO fleet_directive_approvals (id, directive_id, directive_version, "
                "directive_digest, check_id, context_digest, policy_digest, approved_by, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    directive["id"],
                    int(version),
                    directive_digest,
                    check_id,
                    check["context_digest"],
                    check["policy_digest"],
                    self._required_text(actor, "directive approval actor"),
                    now,
                ),
            )
        except Exception:
            existing = self.store.query_one(
                "SELECT * FROM fleet_directive_approvals WHERE directive_id = ? "
                "AND directive_version = ? AND directive_digest = ?",
                (directive["id"], int(version), directive_digest),
            )
            if existing is None:
                raise
            return self._approval_dict(existing)
        self.store.execute(
            "UPDATE fleet_directives SET state = ?, updated_by = ?, updated_at = ? "
            "WHERE id = ? AND current_version = ?",
            ("approved", actor, now, directive["id"], int(version)),
        )
        return self._approval_dict(
            self.store.query_one(
                "SELECT * FROM fleet_directive_approvals WHERE id = ?", (approval_id,)
            )
        )

    def activate(
        self,
        directive_id_or_name: str,
        *,
        version: int,
        directive_digest: str,
        actor: str,
    ) -> JsonDict:
        self._require_enabled()
        directive = self._directive_row(directive_id_or_name)
        if int(directive["reserved"]):
            raise ValidationError("the reserved system directive is already active")
        version_row = self._version_row(str(directive["id"]), int(version))
        if int(directive["current_version"]) != int(version):
            raise ValidationError("only the current directive version can be activated")
        if version_row["digest"] != directive_digest:
            raise ValidationError("directive activation digest does not match immutable version")
        approval = self.store.query_one(
            "SELECT * FROM fleet_directive_approvals WHERE directive_id = ? "
            "AND directive_version = ? AND directive_digest = ?",
            (directive["id"], int(version), directive_digest),
        )
        if approval is None:
            raise ValidationError("directive activation requires exact-version approval")
        fresh = self.check(directive_id_or_name, version=int(version), actor=actor)
        if fresh["status"] != "pass":
            raise ValidationError("directive activation check is blocked")
        if (
            fresh["context_digest"] != approval["context_digest"]
            or fresh["policy_digest"] != approval["policy_digest"]
        ):
            raise ValidationError(
                "directive context changed after approval; re-approve the fresh check"
            )
        agent_rows = self.store.query_all(
            "SELECT id, resources FROM agents WHERE deleted_at IS NULL AND status != ? ORDER BY id",
            ("offline",),
        )
        cohort = [
            str(row["id"])
            for row in agent_rows
            if not bool(
                ensure_json_object(json_loads(row["resources"], {})).get("operator_persona")
            )
        ]
        epoch_row = self.store.query_one(
            "SELECT COALESCE(MAX(epoch), 0) AS epoch FROM fleet_directive_activations"
        )
        epoch = int(epoch_row["epoch"] if epoch_row else 0) + 1
        activation_id = new_id("directive_activation")
        now = utcnow()
        state = "distributing"
        with self.store.transaction() as conn:
            concurrent = conn.execute(
                "SELECT id FROM fleet_directive_activations WHERE directive_id = ? "
                "AND directive_version = ? AND state IN ('distributing','active')",
                (directive["id"], int(version)),
            ).fetchone()
            if concurrent is not None:
                return self._activation_dict(
                    self.store.query_one(
                        "SELECT * FROM fleet_directive_activations WHERE id = ?",
                        (concurrent["id"],),
                    )
                )
            conn.execute(
                "INSERT INTO fleet_directive_activations (id, directive_id, directive_version, "
                "directive_digest, check_id, approval_id, epoch, state, cohort, expected_acks, "
                "created_by, created_at, activated_at, deactivated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    activation_id,
                    directive["id"],
                    int(version),
                    directive_digest,
                    fresh["id"],
                    approval["id"],
                    epoch,
                    state,
                    json_dumps(cohort),
                    len(cohort),
                    self._required_text(actor, "directive activation actor"),
                    now,
                    None,
                ),
            )
            conn.execute(
                "UPDATE fleet_directives SET state = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                (state, actor, now, directive["id"]),
            )
        activation = self._activation_dict(
            self.store.query_one(
                "SELECT * FROM fleet_directive_activations WHERE id = ?", (activation_id,)
            )
        )
        notice = {
            "schema": DIRECTIVE_ACTIVATION_SCHEMA,
            "activation_id": activation_id,
            "directive_id": directive["id"],
            "version": int(version),
            "epoch": epoch,
            "digest": directive_digest,
        }
        if self.activation_notifier:
            for agent_id in cohort:
                try:
                    self.activation_notifier(agent_id, notice)
                except Exception:
                    # Delivery is an accelerator.  Workers also discover the
                    # pending epoch while polling effective policy.
                    pass
        if not cohort:
            self._finalize_activation(activation_id)
            activation = self._activation_dict(
                self.store.query_one(
                    "SELECT * FROM fleet_directive_activations WHERE id = ?", (activation_id,)
                )
            )
        return activation

    def acknowledge(self, activation_id: str, *, agent_id: str, digest: str) -> JsonDict:
        self._require_enabled()
        activation_row = self.store.query_one(
            "SELECT * FROM fleet_directive_activations WHERE id = ?", (activation_id,)
        )
        if activation_row is None:
            raise NotFoundError("directive activation not found: %s" % activation_id)
        if activation_row["state"] not in _ACTIVE_STATES:
            raise ValidationError("directive activation is not acknowledgeable")
        if str(activation_row["directive_digest"]) != str(digest):
            raise ValidationError("directive acknowledgement digest mismatch")
        agent = self.store.query_one("SELECT id, deleted_at FROM agents WHERE id = ?", (agent_id,))
        if agent is None or agent["deleted_at"]:
            raise ValidationError("directive acknowledgement requires a live agent")
        existing = self.store.query_one(
            "SELECT * FROM fleet_directive_acks WHERE activation_id = ? AND agent_id = ?",
            (activation_id, agent_id),
        )
        if existing is None:
            self.store.execute(
                "INSERT INTO fleet_directive_acks (id, activation_id, agent_id, directive_digest, acknowledged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_id("directive_ack"), activation_id, agent_id, digest, utcnow()),
            )
        finalized = False
        if activation_row["state"] == "distributing":
            cohort = set(json_loads(activation_row["cohort"], []))
            ack_rows = self.store.query_all(
                "SELECT agent_id FROM fleet_directive_acks WHERE activation_id = ?",
                (activation_id,),
            )
            acknowledged = {str(row["agent_id"]) for row in ack_rows}
            if cohort <= acknowledged:
                finalized = self._finalize_activation(activation_id)
        activation = self._activation_dict(
            self.store.query_one(
                "SELECT * FROM fleet_directive_activations WHERE id = ?", (activation_id,)
            )
        )
        activation["acknowledged_by"] = agent_id
        activation["finalized"] = finalized
        return activation

    def deactivate(self, directive_id_or_name: str, *, actor: str, reason: str) -> JsonDict:
        self._require_enabled()
        directive = self._directive_row(directive_id_or_name)
        if int(directive["reserved"]):
            raise ValidationError("reserved system constraints cannot be deactivated")
        activation = self.store.query_one(
            "SELECT * FROM fleet_directive_activations WHERE directive_id = ? "
            "AND state IN ('distributing','active') ORDER BY epoch DESC LIMIT 1",
            (directive["id"],),
        )
        if activation is None:
            raise ValidationError("directive has no live activation")
        actor_value = self._required_text(actor, "directive deactivation actor")
        reason_value = self._required_text(reason, "directive deactivation reason")
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE fleet_directive_activations SET state = 'deactivated', deactivated_at = ?, "
                "deactivated_by = ?, deactivation_reason = ? "
                "WHERE id = ? AND state IN ('distributing','active')",
                (now, actor_value, reason_value, activation["id"]),
            )
            conn.execute(
                "UPDATE fleet_directives SET state = 'deactivated', updated_by = ?, updated_at = ? WHERE id = ?",
                (actor_value, now, directive["id"]),
            )
        result = self._activation_dict(
            self.store.query_one(
                "SELECT * FROM fleet_directive_activations WHERE id = ?", (activation["id"],)
            )
        )
        return result

    # Effective snapshots and dispatch gate ----------------------------

    def effective_snapshot(
        self,
        *,
        repository_id: Optional[str] = None,
        project: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> JsonDict:
        if not self.enabled:
            return {
                "schema": DIRECTIVE_SNAPSHOT_SCHEMA,
                "enabled": False,
                "epoch": 0,
                "digest": canonical_digest({"enabled": False}),
                "set": {},
                "directives": [],
                "pending_activations": [],
            }
        repository = self._resolve_repository(repository_id=repository_id, project=project)
        facts = (
            self._facts_for_repository(repository)
            if repository is not None
            else self._fleet_facts(agent_id)
        )
        bindings = (
            self._binding_layers(repository)
            if repository is not None
            else self._fleet_binding_layers()
        )
        policy: JsonDict = {}
        applied: List[JsonDict] = []
        max_epoch = 0
        rows = self.store.query_all(
            "SELECT a.*, v.document FROM fleet_directive_activations a "
            "JOIN fleet_directive_versions v ON v.directive_id = a.directive_id "
            "AND v.version = a.directive_version WHERE a.state = 'active' ORDER BY a.epoch"
        )
        system_row = self.store.query_one(
            "SELECT d.id AS directive_id, d.current_version AS directive_version, "
            "v.digest AS directive_digest, v.document FROM fleet_directives d "
            "JOIN fleet_directive_versions v ON v.directive_id = d.id AND v.version = d.current_version "
            "WHERE d.id = ? AND d.state = 'active'",
            (SYSTEM_DIRECTIVE_ID,),
        )
        sources: List[Mapping[str, Any]] = ([system_row] if system_row is not None else []) + list(
            rows
        )
        for row in sources:
            document = parse_directive_document(json_loads(row["document"], {}))
            evaluation = evaluate_directive(document, facts=facts, bindings=bindings)
            if evaluation.blocked:
                raise ValidationError(evaluation.reason or "effective directive evaluation blocked")
            if not evaluation.matched:
                continue
            if repository is not None and self._matching_waiver(
                str(row["directive_id"]), int(row["directive_version"]), repository
            ):
                continue
            for key, value in evaluation.policy.items():
                if key in policy and policy[key] != value:
                    raise ValidationError("active directives conflict on policy key %s" % key)
                policy[key] = value
            max_epoch = max(
                max_epoch,
                int(
                    row.get("epoch", 0)
                    if isinstance(row, dict)
                    else (row["epoch"] if "epoch" in row.keys() else 0)
                ),
            )
            applied.append(
                {
                    "directive_id": row["directive_id"],
                    "version": int(row["directive_version"]),
                    "digest": row["directive_digest"],
                    "variables": evaluation.variables,
                }
            )
        pending = self.pending_activations(agent_id) if agent_id else []
        payload = {
            "enabled": True,
            "epoch": max_epoch,
            "repository_id": repository["id"] if repository is not None else None,
            "project": repository["project"] if repository is not None else project,
            "agent_id": agent_id,
            "set": policy,
            "directives": applied,
        }
        return {
            "schema": DIRECTIVE_SNAPSHOT_SCHEMA,
            **payload,
            "digest": canonical_digest(payload),
            "pending_activations": pending,
        }

    def pending_activations(self, agent_id: str) -> List[JsonDict]:
        if not self.enabled:
            return []
        rows = self.store.query_all(
            "SELECT a.*, v.document FROM fleet_directive_activations a "
            "JOIN fleet_directive_versions v ON v.directive_id = a.directive_id "
            "AND v.version = a.directive_version "
            "LEFT JOIN fleet_directive_acks k ON k.activation_id = a.id AND k.agent_id = ? "
            "WHERE a.state IN ('distributing','active') AND k.id IS NULL ORDER BY a.epoch",
            (agent_id,),
        )
        result = []
        for row in rows:
            cohort = set(json_loads(row["cohort"], []))
            if row["state"] == "distributing" and agent_id not in cohort:
                continue
            result.append(
                {
                    "activation_id": row["id"],
                    "directive_id": row["directive_id"],
                    "version": int(row["directive_version"]),
                    "epoch": int(row["epoch"]),
                    "digest": row["directive_digest"],
                    "state": row["state"],
                    "document": json_loads(row["document"], {}),
                }
            )
        return result

    def agent_policy_ready(self, agent_id: str) -> bool:
        return not self.pending_activations(agent_id)

    def impact(self, directive_id_or_name: str) -> JsonDict:
        directive = self._directive_row(directive_id_or_name)
        check = self._latest_check(str(directive["id"]), int(directive["current_version"]))
        return {
            "schema": "mac.directive.impact.v1",
            "directive": self._directive_dict(directive),
            "latest_check": check,
            "activation": self._latest_activation(str(directive["id"])),
            "macro_instances": [
                self._macro_instance_dict(row)
                for row in self.store.query_all(
                    "SELECT m.* FROM fleet_directive_macro_instances m "
                    "JOIN fleet_directive_activations a ON a.id = m.activation_id "
                    "WHERE a.directive_id = ? ORDER BY m.created_at DESC",
                    (directive["id"],),
                )
            ],
        }

    # Internal lifecycle -----------------------------------------------

    def _finalize_activation(self, activation_id: str) -> bool:
        row = self.store.query_one(
            "SELECT * FROM fleet_directive_activations WHERE id = ?", (activation_id,)
        )
        if row is None or row["state"] != "distributing":
            return False
        if not self._expand_activation_macros(activation_id):
            now = utcnow()
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE fleet_directive_activations SET state = 'blocked', deactivated_at = ?, "
                    "deactivated_by = 'directive-control-plane', "
                    "deactivation_reason = 'macro_expansion_failed' "
                    "WHERE id = ? AND state = 'distributing'",
                    (now, activation_id),
                )
                conn.execute(
                    "UPDATE fleet_directives SET state = 'approved', updated_at = ? "
                    "WHERE id = ? AND current_version = ?",
                    (now, row["directive_id"], int(row["directive_version"])),
                )
            return False
        now = utcnow()
        with self.store.transaction() as conn:
            updated = conn.execute(
                "UPDATE fleet_directive_activations SET state = 'active', activated_at = ? "
                "WHERE id = ? AND state = 'distributing'",
                (now, activation_id),
            )
            if updated.rowcount != 1:
                return False
            conn.execute(
                "UPDATE fleet_directive_activations SET state = 'deactivated', deactivated_at = ?, "
                "deactivated_by = 'directive-control-plane', "
                "deactivation_reason = 'superseded_by_new_activation' "
                "WHERE directive_id = ? AND id != ? AND state = 'active'",
                (now, row["directive_id"], activation_id),
            )
            conn.execute(
                "UPDATE fleet_directives SET state = 'active', updated_at = ? "
                "WHERE id = ? AND current_version = ?",
                (now, row["directive_id"], int(row["directive_version"])),
            )
        return True

    def _expand_activation_macros(self, activation_id: str) -> bool:
        activation = self.store.query_one(
            "SELECT a.*, v.document FROM fleet_directive_activations a "
            "JOIN fleet_directive_versions v ON v.directive_id = a.directive_id "
            "AND v.version = a.directive_version WHERE a.id = ?",
            (activation_id,),
        )
        if activation is None:
            return False
        document = parse_directive_document(json_loads(activation["document"], {}))
        if document.macro is None:
            return True
        succeeded = True
        repositories = [
            dict(row)
            for row in self.store.query_all(
                "SELECT * FROM project_repositories WHERE enabled = 1 ORDER BY id"
            )
        ]
        for repository in repositories:
            if self._matching_waiver(
                str(activation["directive_id"]), int(activation["directive_version"]), repository
            ):
                continue
            evaluation = evaluate_directive(
                document,
                facts=self._facts_for_repository(repository),
                bindings=self._binding_layers(repository),
            )
            if not evaluation.matched or evaluation.blocked or evaluation.macro is None:
                continue
            existing = self.store.query_one(
                "SELECT * FROM fleet_directive_macro_instances WHERE activation_id = ? AND repository_id = ?",
                (activation_id, repository["id"]),
            )
            if existing is not None:
                if existing["state"] != "held":
                    succeeded = False
                continue
            instance_id = new_id("directive_macro")
            now = utcnow()
            self.store.execute(
                "INSERT INTO fleet_directive_macro_instances (id, activation_id, repository_id, "
                "work_package_id, state, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, 'expanding', ?, ?, ?)",
                (
                    instance_id,
                    activation_id,
                    repository["id"],
                    json_dumps({"macro": evaluation.macro}),
                    now,
                    now,
                ),
            )
            try:
                if self.macro_expander is None:
                    # Macro expansion compiled a directive into a held managed
                    # work-package DAG.  That pipeline was removed (it never
                    # ran), and no replacement expander is wired, so a macro
                    # directive is recorded as blocked rather than silently
                    # appearing to have been applied.
                    raise ValidationError(
                        "directive macro expansion is unavailable: it required "
                        "the removed work-package pipeline"
                    )
                result = dict(
                    self.macro_expander(
                        self._activation_dict(activation),
                        dict(repository),
                        evaluation.macro,
                        {
                            "variables": evaluation.variables,
                            "directive": document.to_dict(),
                        },
                    )
                )
                if not result.get("held"):
                    raise ValidationError("directive macros must produce held work")
                self.store.execute(
                    "UPDATE fleet_directive_macro_instances SET work_package_id = ?, state = 'held', "
                    "detail = ?, updated_at = ? WHERE id = ?",
                    (
                        result.get("work_package_id") or result.get("package_id"),
                        json_dumps(result),
                        utcnow(),
                        instance_id,
                    ),
                )
            except Exception as exc:
                succeeded = False
                self.store.execute(
                    "UPDATE fleet_directive_macro_instances SET state = 'blocked', detail = ?, updated_at = ? WHERE id = ?",
                    (
                        json_dumps({"error": str(exc), "macro": evaluation.macro}),
                        utcnow(),
                        instance_id,
                    ),
                )
        return succeeded

    def _check_macro_workflow(
        self,
        macro: Mapping[str, Any],
        repository: Mapping[str, Any],
        blockers: List[JsonDict],
    ) -> None:
        if self.workflow_resolver is None:
            blockers.append(
                {
                    "code": "workflow_resolver_unavailable",
                    "repository_id": repository["id"],
                }
            )
            return
        try:
            workflow = self.workflow_resolver(str(macro["workflow"]), int(macro["version"]))
            if not bool(
                getattr(
                    workflow,
                    "enabled",
                    workflow.get("enabled", False) if isinstance(workflow, Mapping) else False,
                )
            ):
                raise ValidationError("workflow is disabled")
        except Exception as exc:
            blockers.append(
                {
                    "code": "workflow_unavailable",
                    "repository_id": repository["id"],
                    "workflow": macro.get("workflow"),
                    "version": macro.get("version"),
                    "detail": str(exc),
                }
            )

    def _check_repository_macro_conflicts(
        self,
        macro: Mapping[str, Any],
        repository: Mapping[str, Any],
        active_documents: Sequence[Tuple[JsonDict, DirectiveDocument]],
        facts: Mapping[str, Any],
        bindings: Sequence[Mapping[str, Any]],
        blockers: List[JsonDict],
    ) -> None:
        left = self._effects(macro.get("effects") or {})
        for active, active_document in active_documents:
            evaluation = evaluate_directive(active_document, facts=facts, bindings=bindings)
            if not evaluation.matched or evaluation.blocked or evaluation.macro is None:
                continue
            reasons = effect_conflicts(left, self._effects(evaluation.macro.get("effects") or {}))
            if reasons:
                blockers.append(
                    {
                        "code": "macro_effect_conflict",
                        "repository_id": repository["id"],
                        "other_directive_id": active["directive_id"],
                        "reasons": reasons,
                    }
                )

    # Context helpers --------------------------------------------------

    def _facts_for_repository(self, repository: Mapping[str, Any]) -> JsonDict:
        metadata = ensure_json_object(
            json_loads(repository["metadata"], {})
            if isinstance(repository.get("metadata"), str)
            else repository.get("metadata")
        )
        project_row = self.store.query_one(
            "SELECT * FROM projects WHERE name = ? OR id = ? ORDER BY name = ? DESC LIMIT 1",
            (repository["project"], repository["project"], repository["project"]),
        )
        project_metadata = ensure_json_object(
            json_loads(project_row["metadata"], {}) if project_row is not None else {}
        )
        return {
            "fleet": {"name": "mac"},
            "project": {
                "id": str(project_row["id"])
                if project_row is not None
                else str(repository["project"]),
                "name": str(repository["project"]),
                "metadata": project_metadata,
            },
            "repository": {
                "id": str(repository["id"]),
                "name": str(repository["name"]),
                "path": str(repository["path"]),
                "source": str(repository["source"]),
                "project": str(repository["project"]),
                "metadata": metadata,
            },
            "agent": {},
        }

    def _fleet_facts(self, agent_id: Optional[str]) -> JsonDict:
        agent: JsonDict = {}
        if agent_id:
            row = self.store.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
            if row is not None:
                agent = {
                    "id": row["id"],
                    "name": row["name"],
                    "status": row["status"],
                    "capabilities": json_loads(row["capabilities"], []),
                }
        return {"fleet": {"name": "mac"}, "project": {}, "repository": {}, "agent": agent}

    def _binding_layers(self, repository: Mapping[str, Any]) -> List[JsonDict]:
        project_row = self.store.query_one(
            "SELECT id FROM projects WHERE name = ? OR id = ? ORDER BY name = ? DESC LIMIT 1",
            (repository["project"], repository["project"], repository["project"]),
        )
        layers = [self._bindings_for("repository", str(repository["id"]))]
        if project_row is not None:
            layers.append(self._bindings_for("project", str(project_row["id"])))
        layers.append(self._bindings_for("project", str(repository["project"])))
        layers.append(self._bindings_for("fleet", "fleet"))
        return layers

    def _fleet_binding_layers(self) -> List[JsonDict]:
        return [self._bindings_for("fleet", "fleet")]

    def _bindings_for(self, target_type: str, target_id: str) -> JsonDict:
        rows = self.store.query_all(
            "SELECT binding_key, binding_value FROM fleet_directive_bindings "
            "WHERE target_type = ? AND target_id = ? AND active = 1 ORDER BY binding_key",
            (target_type, target_id),
        )
        result: JsonDict = {}
        for row in rows:
            current: JsonDict = result
            segments = str(row["binding_key"]).split(".")
            for segment in segments[:-1]:
                next_value = current.setdefault(segment, {})
                if not isinstance(next_value, dict):
                    raise ValidationError("directive bindings have a path collision")
                current = next_value
            current[segments[-1]] = json_loads(row["binding_value"], None)
        return result

    def _matching_waiver(
        self,
        directive_id: str,
        version: int,
        repository: Mapping[str, Any],
    ) -> Optional[Mapping[str, Any]]:
        rows = self.store.query_all(
            "SELECT * FROM fleet_directive_waivers WHERE directive_id = ? AND directive_version = ? "
            "AND revoked_at IS NULL ORDER BY created_at DESC",
            (directive_id, int(version)),
        )
        now = datetime.now(timezone.utc)
        project_ids = {str(repository["project"])}
        project_row = self.store.query_one(
            "SELECT id FROM projects WHERE name = ? OR id = ? LIMIT 1",
            (repository["project"], repository["project"]),
        )
        if project_row is not None:
            project_ids.add(str(project_row["id"]))
        for row in rows:
            if row["expires_at"] and self._parse_timestamp(str(row["expires_at"])) <= now:
                continue
            if row["target_type"] == "repository" and row["target_id"] == repository["id"]:
                return row
            if row["target_type"] == "project" and row["target_id"] in project_ids:
                return row
        return None

    def _current_context_payload(self) -> JsonDict:
        return {
            "repositories": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "project": row["project"],
                    "source": row["source"],
                    "metadata": json_loads(row["metadata"], {}),
                    "updated_at": row["updated_at"],
                }
                for row in self.store.query_all(
                    "SELECT * FROM project_repositories WHERE enabled = 1 ORDER BY id"
                )
            ],
            "bindings": self.list_bindings(),
            "waivers": self.list_waivers(),
            "active_directives": [
                {
                    "directive_id": row["directive_id"],
                    "version": int(row["directive_version"]),
                    "digest": row["directive_digest"],
                    "epoch": int(row["epoch"]),
                }
                for row in self.store.query_all(
                    "SELECT * FROM fleet_directive_activations WHERE state = 'active' ORDER BY epoch"
                )
            ],
        }

    def _active_documents(
        self, *, exclude_directive_id: Optional[str] = None
    ) -> List[Tuple[JsonDict, DirectiveDocument]]:
        rows = self.store.query_all(
            "SELECT a.directive_id, a.directive_version AS version, a.directive_digest AS digest, "
            "a.epoch, v.document FROM fleet_directive_activations a "
            "JOIN fleet_directive_versions v ON v.directive_id = a.directive_id "
            "AND v.version = a.directive_version WHERE a.state IN ('active','distributing') ORDER BY a.epoch"
        )
        result: List[Tuple[JsonDict, DirectiveDocument]] = []
        system = self.store.query_one(
            "SELECT d.id AS directive_id, d.current_version AS version, v.digest, 0 AS epoch, v.document "
            "FROM fleet_directives d JOIN fleet_directive_versions v ON v.directive_id = d.id "
            "AND v.version = d.current_version WHERE d.id = ? AND d.state = 'active'",
            (SYSTEM_DIRECTIVE_ID,),
        )
        all_rows = ([system] if system is not None else []) + list(rows)
        for row in all_rows:
            if exclude_directive_id and row["directive_id"] == exclude_directive_id:
                continue
            metadata = {
                "directive_id": row["directive_id"],
                "version": int(row["version"]),
                "digest": row["digest"],
                "epoch": int(row["epoch"]),
            }
            result.append((metadata, parse_directive_document(json_loads(row["document"], {}))))
        return result

    def _resolve_repository(
        self, *, repository_id: Optional[str], project: Optional[str]
    ) -> Optional[Mapping[str, Any]]:
        if repository_id:
            row = self.store.query_one(
                "SELECT * FROM project_repositories WHERE id = ? AND enabled = 1",
                (repository_id,),
            )
            if row is None:
                raise NotFoundError("project repository not found: %s" % repository_id)
            return dict(row)
        if project:
            rows = self.store.query_all(
                "SELECT * FROM project_repositories WHERE project = ? AND enabled = 1 ORDER BY id",
                (project,),
            )
            if len(rows) > 1:
                raise ValidationError(
                    "project has multiple repositories; repository_id is required"
                )
            return dict(rows[0]) if rows else None
        return None

    # Row helpers ------------------------------------------------------

    def _directive_row(self, directive_id_or_name: str) -> Mapping[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM fleet_directives WHERE id = ? OR name = ? ORDER BY id = ? DESC LIMIT 1",
            (directive_id_or_name, directive_id_or_name, directive_id_or_name),
        )
        if row is None:
            raise NotFoundError("directive not found: %s" % directive_id_or_name)
        return row

    def _version_row(self, directive_id: str, version: int) -> Mapping[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM fleet_directive_versions WHERE directive_id = ? AND version = ?",
            (directive_id, int(version)),
        )
        if row is None:
            raise NotFoundError("directive version not found: %s@%s" % (directive_id, version))
        return row

    def _insert_version(
        self,
        conn: Any,
        directive_id: str,
        version: int,
        document: DirectiveDocument,
        actor: str,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO fleet_directive_versions (id, directive_id, version, document, digest, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("directive_version"),
                directive_id,
                int(version),
                json_dumps(document.to_dict()),
                document.digest,
                actor,
                now,
            ),
        )

    def _ensure_system_directive(self) -> None:
        document = parse_directive_document(
            {
                "schema": DIRECTIVE_SCHEMA,
                "name": SYSTEM_DIRECTIVE_NAME,
                "description": "Immutable executor safety constraints enforced by the MAC control plane.",
                "scope": "fleet",
                "set": {
                    "executor.host_package_install_allowed": False,
                    "verification.tests_required": True,
                    "review.required": True,
                    "secrets.exposure_allowed": False,
                    "publication.owner": "hub",
                },
            }
        )
        existing = self.store.query_one(
            "SELECT * FROM fleet_directives WHERE id = ?", (SYSTEM_DIRECTIVE_ID,)
        )
        if existing is not None:
            current = self._version_row(SYSTEM_DIRECTIVE_ID, int(existing["current_version"]))
            if str(current["digest"]) == document.digest:
                return
            version = int(existing["current_version"]) + 1
            now = utcnow()
            try:
                with self.store.transaction() as conn:
                    self._insert_version(
                        conn, SYSTEM_DIRECTIVE_ID, version, document, "system", now
                    )
                    updated = conn.execute(
                        "UPDATE fleet_directives SET name = ?, description = ?, scope = 'fleet', "
                        "current_version = ?, state = 'active', reserved = 1, updated_by = 'system', "
                        "updated_at = ? WHERE id = ? AND current_version = ?",
                        (
                            document.name,
                            document.description,
                            version,
                            now,
                            SYSTEM_DIRECTIVE_ID,
                            int(existing["current_version"]),
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ValidationError(
                            "system directive changed while updating its baseline"
                        )
            except Exception:
                refreshed = self.store.query_one(
                    "SELECT current_version FROM fleet_directives WHERE id = ?",
                    (SYSTEM_DIRECTIVE_ID,),
                )
                if refreshed is not None:
                    current = self._version_row(
                        SYSTEM_DIRECTIVE_ID, int(refreshed["current_version"])
                    )
                    if str(current["digest"]) == document.digest:
                        return
                raise
            return

        now = utcnow()
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO fleet_directives (id, name, description, scope, current_version, state, "
                    "reserved, created_by, updated_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'fleet', 1, 'active', 1, 'system', 'system', ?, ?)",
                    (SYSTEM_DIRECTIVE_ID, document.name, document.description, now, now),
                )
                self._insert_version(conn, SYSTEM_DIRECTIVE_ID, 1, document, "system", now)
        except Exception:
            if not self.store.query_one(
                "SELECT id FROM fleet_directives WHERE id = ?", (SYSTEM_DIRECTIVE_ID,)
            ):
                raise

    def _directive_dict(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "scope": row["scope"],
            "current_version": int(row["current_version"]),
            "state": row["state"],
            "reserved": bool(row["reserved"]),
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _version_dict(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "id": row["id"],
            "directive_id": row["directive_id"],
            "version": int(row["version"]),
            "document": json_loads(row["document"], {}),
            "digest": row["digest"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _binding_dict(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "id": row["id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "key": row["binding_key"],
            "value": json_loads(row["binding_value"], None),
            "version": int(row["version"]),
            "active": bool(row["active"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "superseded_at": row["superseded_at"],
        }

    def _waiver_dict(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "id": row["id"],
            "directive_id": row["directive_id"],
            "directive_version": int(row["directive_version"]),
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_by": row["revoked_by"],
            "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
        }

    def _approval_dict(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "id": row["id"],
            "directive_id": row["directive_id"],
            "directive_version": int(row["directive_version"]),
            "directive_digest": row["directive_digest"],
            "check_id": row["check_id"],
            "context_digest": row["context_digest"],
            "policy_digest": row["policy_digest"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
        }

    def _activation_dict(self, row: Mapping[str, Any]) -> JsonDict:
        ack = self.store.query_one(
            "SELECT COUNT(*) AS count FROM fleet_directive_acks WHERE activation_id = ?",
            (row["id"],),
        )
        return {
            "schema": DIRECTIVE_ACTIVATION_SCHEMA,
            "id": row["id"],
            "directive_id": row["directive_id"],
            "directive_version": int(row["directive_version"]),
            "directive_digest": row["directive_digest"],
            "check_id": row["check_id"],
            "approval_id": row["approval_id"],
            "epoch": int(row["epoch"]),
            "state": row["state"],
            "cohort": json_loads(row["cohort"], []),
            "expected_acks": int(row["expected_acks"]),
            "ack_count": int(ack["count"] if ack else 0),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "deactivated_at": row["deactivated_at"],
            "deactivated_by": row["deactivated_by"],
            "deactivation_reason": row["deactivation_reason"],
        }

    def _macro_instance_dict(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "id": row["id"],
            "activation_id": row["activation_id"],
            "repository_id": row["repository_id"],
            "work_package_id": row["work_package_id"],
            "state": row["state"],
            "detail": json_loads(row["detail"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _latest_check(self, directive_id: str, version: int) -> Optional[JsonDict]:
        row = self.store.query_one(
            "SELECT * FROM fleet_directive_checks WHERE directive_id = ? AND directive_version = ? "
            "ORDER BY checked_at DESC LIMIT 1",
            (directive_id, int(version)),
        )
        if row is None:
            return None
        result = json_loads(row["report"], {})
        result.update({"id": row["id"], "checked_at": row["checked_at"]})
        return result

    def _latest_activation(self, directive_id: str) -> Optional[JsonDict]:
        row = self.store.query_one(
            "SELECT * FROM fleet_directive_activations WHERE directive_id = ? ORDER BY epoch DESC LIMIT 1",
            (directive_id,),
        )
        return self._activation_dict(row) if row is not None else None

    def _validate_target(self, target_type: str, target_id: str) -> Tuple[str, str]:
        target_type_value = str(target_type or "").strip().lower()
        if target_type_value not in _TARGET_TYPES:
            raise ValidationError("directive target_type must be fleet, project, or repository")
        target_id_value = self._required_text(target_id, "directive target_id")
        if target_type_value == "fleet":
            if target_id_value not in {"fleet", "mac"}:
                raise ValidationError("fleet binding target_id must be fleet")
            return "fleet", "fleet"
        table = "projects" if target_type_value == "project" else "project_repositories"
        if target_type_value == "project":
            row = self.store.query_one(
                "SELECT id FROM projects WHERE id = ? OR name = ? LIMIT 1",
                (target_id_value, target_id_value),
            )
        else:
            row = self.store.query_one(
                "SELECT id FROM project_repositories WHERE id = ? OR name = ? LIMIT 1",
                (target_id_value, target_id_value),
            )
        if row is None:
            raise NotFoundError(
                "directive %s target not found: %s" % (target_type_value, target_id_value)
            )
        return target_type_value, str(row["id"])

    @staticmethod
    def _effects(raw: Mapping[str, Any]) -> DeclaredEffects:
        return DeclaredEffects(
            reads=tuple(str(item) for item in raw.get("reads", [])),
            writes=tuple(str(item) for item in raw.get("writes", [])),
            exclusive=tuple(str(item) for item in raw.get("exclusive", [])),
            external=tuple(str(item) for item in raw.get("external", [])),
        )

    @staticmethod
    def _symbolic_effects(raw: Mapping[str, Any]) -> DeclaredEffects:
        def values(kind: str) -> Tuple[str, ...]:
            return tuple(
                item if isinstance(item, str) else json_dumps(item) for item in raw.get(kind, [])
            )

        return DeclaredEffects(
            reads=values("reads"),
            writes=values("writes"),
            exclusive=values("exclusive"),
            external=values("external"),
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ValidationError("fleet directives are disabled (set MAC_DIRECTIVES_ENABLED=1)")

    @staticmethod
    def _required_text(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("%s is required" % label)
        if len(text) > 4000:
            raise ValidationError("%s is too long" % label)
        return text

    @staticmethod
    def _required_path(value: Any, label: str) -> str:
        text = DirectiveService._required_text(value, label)
        if any(
            not segment or not segment.replace("_", "a").replace("-", "a").isalnum()
            for segment in text.split(".")
        ):
            raise ValidationError("%s is invalid" % label)
        return text

    @staticmethod
    def _value_type(value: Any) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "list"
        if isinstance(value, Mapping):
            return "object"
        raise ValidationError("directive binding value must be JSON data")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValidationError("directive timestamps require a timezone")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _optional_timestamp(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        cls._parse_timestamp(str(value).strip())
        return str(value).strip()
