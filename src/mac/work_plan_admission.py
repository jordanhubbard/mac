"""Controller bridge from an editable planner DAG to a held work package.

The language model is deliberately confined to proposing work nodes and their
contracts.  Repository identity, the canonical planning ref, its exact object
id, package identity, and materialization are controller decisions.  A preview
is mutation-free; operator acceptance recompiles the edited proposal, verifies
that the canonical base has not moved, and delegates one atomic held admission
to :class:`mac.work_package_service.WorkPackageService`.

Activation is intentionally outside this bridge.  The existing control-plane
activation command performs worker-credential readiness checks and a
plan-version/epoch CAS, so an accepted plan cannot accidentally become
dispatchable merely because it was previewed or persisted.

Both preview and acceptance run one deterministic, redaction-safe secret scan
(:func:`_reject_secret_material`) over the entire nested plan.  It rejects
secret-like fields, credential-bearing URLs, raw bearer/API tokens, private-key
and PEM blocks, and authenticated Git/config fragments.  Rejections raise
:class:`~mac.models.ValidationError` with a fixed, category-only message; a
matched value is never logged, echoed, or embedded in an error.  Plans must
reference credentials by control-plane secret name instead of embedding them;
see ``docs/secrets-management-guide.md`` for the accepted reference mechanism.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import parse_qsl, urlsplit

from mac.models import JsonDict, ValidationError, new_id
from mac.repository_contract import resolve_repository_canonical_remote
from mac.repository_namespace import attest_git_tree_resource_namespace
from mac.store import Store
from mac.work_package_models import (
    WORK_PACKAGE_PLAN_SCHEMA,
    CompiledWorkPackagePlan,
    compile_work_package_plan,
    validate_executable_work_package_effects,
)
from mac.work_package_service import WorkPackageAdmissionResult, WorkPackageService


MANAGED_WORK_PLAN_MODE = "managed"
MANAGED_WORK_PLAN_PREVIEW_SCHEMA = "mac.dashboard.managed_work_plan.v1"
MANAGED_WORK_PLAN_ACCEPT_SCHEMA = "mac.dashboard.managed_work_plan_accept.v1"
MANAGED_WORK_PLAN_PROVENANCE_SCHEMA = "mac.managed_work_plan.provenance.v1"

_FULL_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "password",
        "secret",
        "signature",
        "token",
    }
)

# High-confidence secret detectors that operate on scalar strings.
#
# Each entry pairs a compiled pattern with a fully-formed, redaction-safe
# message.  The message is the only thing ever surfaced in a
# :class:`ValidationError`; the matched span is never logged, echoed, or
# embedded in an error.  Patterns are deliberately anchored on vendor-issued
# prefixes, structural markers, or explicit credential syntax so that ordinary
# planner prose (task titles, descriptions, file paths, license headers) does
# not trip them, keeping the false-positive surface measurable and bounded.
#
# Ordering: authenticated URLs and headers are matched before generic raw
# tokens so that a token embedded inside an authenticated URL is reported with
# the more specific, still redaction-safe, contract.
_SECRET_STRING_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    # --- Authenticated Git / config fragments (credential outside a URL) ---
    # HTTP Authorization headers carrying a live credential.
    (
        re.compile(r"(?im)^\s*authorization\s*:\s*(?:bearer|basic|token)\s+\S+"),
        "managed work plan may not contain authorization-header credentials",
    ),
    # git-credential-store / helper lines and userinfo URLs: scheme://user:pass@host.
    (
        re.compile(r"(?i)\b(?:https?|ssh|git)://[^\s/@:]+:[^\s/@]+@[^\s/]+"),
        "managed work plan may not contain authenticated git URLs",
    ),
    # GitHub Actions x-access-token style credential embedding.
    (
        re.compile(r"(?i)\bx-access-token:[^\s@/]+@"),
        "managed work plan may not contain authenticated git URLs",
    ),
    # .netrc credential fragments: a machine line that also carries a password.
    (
        re.compile(
            r"(?im)^\s*machine\s+\S+\s+(?:login|account)\s+\S+\s+password\s+\S+"
        ),
        "managed work plan may not contain netrc credentials",
    ),
    (
        re.compile(r"(?im)^\s*machine\s+\S+\s+password\s+\S+"),
        "managed work plan may not contain netrc credentials",
    ),
    # --- PEM / private-key blocks (RSA, EC, OpenSSH, PKCS#8, PGP) ---
    (
        re.compile(r"-----BEGIN[ A-Z0-9]*PRIVATE KEY-----"),
        "managed work plan may not contain a private-key block",
    ),
    (
        re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
        "managed work plan may not contain a private-key block",
    ),
    (
        re.compile(r"PuTTY-User-Key-File-\d"),
        "managed work plan may not contain a private-key block",
    ),
    # --- Vendor-issued raw bearer / API tokens ---
    # GitHub personal-access / app / OAuth / refresh tokens.
    (
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
        "managed work plan may not contain a raw GitHub token",
    ),
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
        "managed work plan may not contain a raw GitHub token",
    ),
    # GitLab personal-access tokens.
    (
        re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
        "managed work plan may not contain a raw GitLab token",
    ),
    # AWS access-key IDs.
    (
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b"),
        "managed work plan may not contain a raw AWS access key",
    ),
    # Google API keys.
    (
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "managed work plan may not contain a raw Google API key",
    ),
    # Slack tokens (bot, user, app, legacy, refresh, config).
    (
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
        "managed work plan may not contain a raw Slack token",
    ),
    # Stripe live/test secret keys.
    (
        re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
        "managed work plan may not contain a raw Stripe secret key",
    ),
    # OpenAI / Anthropic style project keys.
    (
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b"),
        "managed work plan may not contain a raw provider secret key",
    ),
    # JSON Web Tokens (three base64url segments; second segment carries claims).
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"
        ),
        "managed work plan may not contain a raw JSON Web Token",
    ),
)
_MODEL_PROPOSAL_FIELDS = frozenset(
    {
        "goal",
        "integration",
        "max_in_flight",
        "max_mutation_wip",
        "metadata",
        "mutation_wip",
        "nodes",
        "steps",
        "tasks",
    }
)
_CONTROLLER_PLAN_FIELDS = frozenset(
    {
        "package_id",
        "plan_generation",
        "planning_base_ref",
        "planning_base_sha",
        "project",
        "repository_id",
        "resource_namespace",
        "schema",
    }
)


@dataclass(frozen=True)
class CanonicalRepositoryBase:
    """Secret-free observation of one exact advertised canonical ref."""

    repository_id: str
    planning_base_ref: str
    planning_base_sha: str
    resource_namespace: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "repository_id": self.repository_id,
            "planning_base_ref": self.planning_base_ref,
            "planning_base_sha": self.planning_base_sha,
            "resource_namespace": copy.deepcopy(self.resource_namespace),
        }


class CanonicalBaseResolver(Protocol):
    def resolve(
        self,
        repository: Mapping[str, Any],
        *,
        requested_ref: Optional[str] = None,
    ) -> CanonicalRepositoryBase: ...


class GitCanonicalBaseResolver:
    """Resolve a remote ref without prompts and without exposing Git output."""

    def __init__(self, *, timeout_seconds: int = 30) -> None:
        timeout = int(timeout_seconds)
        if timeout < 1 or timeout > 300:
            raise ValidationError("canonical base timeout must be between 1 and 300 seconds")
        self.timeout_seconds = timeout

    def resolve(
        self,
        repository: Mapping[str, Any],
        *,
        requested_ref: Optional[str] = None,
    ) -> CanonicalRepositoryBase:
        try:
            canonical = resolve_repository_canonical_remote(repository)
        except ValueError as exc:
            raise ValidationError(
                "managed work planning requires a valid canonical repository"
            ) from exc
        repository_id = canonical.repository_id
        source = canonical.url

        if requested_ref is not None:
            planning_ref = _safe_ref(requested_ref)
            output = self._run(
                ["git", "ls-remote", "--exit-code", "--", source, planning_ref]
            )
            shas = _exact_ref_shas(output, planning_ref)
        else:
            output = self._run(["git", "ls-remote", "--symref", "--", source, "HEAD"])
            symrefs = []
            head_shas = []
            for line in output.splitlines():
                fields = line.strip().split()
                if len(fields) == 3 and fields[0] == "ref:" and fields[2] == "HEAD":
                    symrefs.append(fields[1])
                elif len(fields) == 2 and fields[1] == "HEAD":
                    head_shas.append(fields[0].lower())
            symrefs = sorted(set(symrefs))
            head_shas = sorted(set(head_shas))
            if len(symrefs) != 1 or len(head_shas) != 1:
                raise ValidationError(
                    "canonical repository HEAD did not resolve to exactly one branch and object id"
                )
            planning_ref = _safe_ref(symrefs[0])
            shas = head_shas

        if len(shas) != 1 or not _FULL_OBJECT_ID_RE.fullmatch(shas[0]):
            raise ValidationError(
                "canonical repository ref did not resolve to exactly one full object id"
            )
        resource_namespace = attest_git_tree_resource_namespace(
            repository,
            planning_base_sha=shas[0],
            timeout_seconds=self.timeout_seconds,
        )
        return CanonicalRepositoryBase(
            repository_id=repository_id,
            planning_base_ref=planning_ref,
            planning_base_sha=shas[0],
            resource_namespace=(
                resource_namespace
                if resource_namespace.get("status") == "resolved"
                else {}
            ),
        )

    def _run(self, command: Sequence[str]) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "GCM_INTERACTIVE": "Never",
                "GIT_ASKPASS": "",
                "GIT_TERMINAL_PROMPT": "0",
                "SSH_ASKPASS": "",
            }
        )
        try:
            completed = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("canonical repository resolution failed") from exc
        if completed.returncode != 0:
            # Git diagnostics may include authenticated URLs or credential-helper
            # details.  The failure class is useful; the raw output is not.
            raise ValidationError("canonical repository resolution failed")
        return completed.stdout


@dataclass(frozen=True)
class ManagedWorkPlanPreview:
    plan: JsonDict
    plan_digest: str
    topological_order: Tuple[str, ...]
    repository: JsonDict
    source: str

    def to_dict(self) -> JsonDict:
        result: JsonDict = {
            "schema": MANAGED_WORK_PLAN_PREVIEW_SCHEMA,
            "mode": MANAGED_WORK_PLAN_MODE,
            "source": self.source,
            "package_id": self.plan["package_id"],
            "goal": self.plan["goal"],
            "project": self.plan["project"],
            "repository_id": self.plan["repository_id"],
            "planning_base_ref": self.plan["planning_base_ref"],
            "planning_base_sha": self.plan["planning_base_sha"],
            "plan_generation": self.plan["plan_generation"],
            "plan_digest": self.plan_digest,
            "topological_order": list(self.topological_order),
            "repository": copy.deepcopy(self.repository),
            "nodes": copy.deepcopy(self.plan["nodes"]),
            "plan": copy.deepcopy(self.plan),
            "activation": {
                "required": True,
                "automatic": False,
                "expected_plan_version": 1,
                "expected_epoch": 1,
                "endpoint": "/work-packages/%s/activate" % self.plan["package_id"],
            },
        }
        for field_name in (
            "integration",
            "max_in_flight",
            "max_mutation_wip",
            "metadata",
            "mutation_wip",
            "resource_namespace",
        ):
            if field_name in self.plan:
                result[field_name] = copy.deepcopy(self.plan[field_name])
        return result


@dataclass(frozen=True)
class ManagedWorkPlanAcceptance:
    admission: WorkPackageAdmissionResult

    def to_dict(self) -> JsonDict:
        result = self.admission.to_dict()
        package = result["package"]
        return {
            "schema": MANAGED_WORK_PLAN_ACCEPT_SCHEMA,
            "mode": MANAGED_WORK_PLAN_MODE,
            "admission": result,
            "package": package,
            "task_ids": list(result["task_ids"]),
            "held": bool(result["held"]),
            "activation": {
                "required": package["state"] == "admitted",
                "automatic": False,
                "expected_plan_version": result["plan_version"],
                "expected_epoch": result["epoch"],
                "endpoint": "/work-packages/%s/activate" % package["id"],
            },
        }


class ManagedWorkPlanBridge:
    """Compile planner proposals and admit only operator-approved held DAGs."""

    def __init__(
        self,
        store: Store,
        work_packages: WorkPackageService,
        *,
        base_resolver: Optional[CanonicalBaseResolver] = None,
    ) -> None:
        self.store = store
        self.work_packages = work_packages
        self.base_resolver = base_resolver or GitCanonicalBaseResolver()

    def preview(
        self,
        proposed: Mapping[str, Any] | str,
        *,
        request: Mapping[str, Any],
        source: str = "model",
    ) -> ManagedWorkPlanPreview:
        """Return a mutation-free, editable, fully compiled managed plan."""

        proposal = _proposal_object(proposed)
        repository = self._registered_repository(
            repository_id=request.get("repository_id"),
            project=request.get("project"),
        )
        requested_ref = _optional_text(request.get("planning_base_ref"))
        base = self.base_resolver.resolve(
            repository,
            requested_ref=requested_ref,
        )
        package_id = _optional_text(request.get("package_id")) or new_id("wp")
        plan = _planner_proposal_to_plan(
            proposal,
            request=request,
            repository=repository,
            base=base,
            package_id=package_id,
            source=source,
        )
        compiled = self._compile_managed(plan)
        return ManagedWorkPlanPreview(
            plan=copy.deepcopy(plan),
            plan_digest=compiled.plan_digest,
            topological_order=compiled.topological_order,
            repository={
                "id": str(repository["id"]),
                "name": str(repository["name"]),
                "project": str(repository["project"]),
            },
            source=_required_text(source, "managed work plan source", maximum=80),
        )

    def accept(
        self,
        plan: Mapping[str, Any],
        *,
        actor: str,
        reason: str,
        tenant_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
    ) -> ManagedWorkPlanAcceptance:
        """Recompile and atomically admit an edited plan, still held.

        The canonical ref is resolved a second time.  A branch movement between
        preview and acceptance therefore fails before any package/task row is
        created.  ``WorkPackageService.admit`` independently attests the same
        ref while materializing, closing the remaining resolution/admission
        race rather than trusting this preflight observation.
        """

        if not isinstance(plan, Mapping):
            raise ValidationError("managed work plan acceptance requires a plan object")
        candidate = copy.deepcopy(dict(plan))
        _reject_secret_material(candidate)
        compiled = self._compile_managed(candidate)
        definition = compiled.definition
        repository = self._registered_repository(
            repository_id=definition["repository_id"],
            project=definition.get("project"),
        )
        current = self.base_resolver.resolve(
            repository,
            requested_ref=str(definition["planning_base_ref"]),
        )
        if current.planning_base_sha != str(definition["planning_base_sha"]).lower():
            raise ValidationError(
                "managed work plan is stale: canonical planning ref moved after preview"
            )
        admission = self.work_packages.admit(
            candidate,
            actor=actor,
            reason=reason,
            tenant_id=tenant_id,
            root_task_id=root_task_id,
        )
        if admission.package.state not in {"admitted", "active"}:
            raise ValidationError("managed work plan admission returned an invalid package state")
        return ManagedWorkPlanAcceptance(admission=admission)

    def _compile_managed(self, plan: Mapping[str, Any]) -> CompiledWorkPackagePlan:
        _require_explicit_node_contracts(plan)
        compiled = compile_work_package_plan(plan)
        validate_executable_work_package_effects(compiled)
        _validate_managed_station_topology(compiled)
        return compiled

    def _registered_repository(
        self,
        *,
        repository_id: Any,
        project: Any,
    ) -> JsonDict:
        repository_value = _optional_text(repository_id)
        project_value = _optional_text(project)
        if repository_value:
            rows = self.store.query_all(
                "SELECT * FROM project_repositories WHERE id = ? AND enabled = ?",
                (repository_value, 1),
            )
        else:
            if not project_value:
                raise ValidationError(
                    "managed work planning requires repository_id or project"
                )
            rows = self.store.query_all(
                "SELECT * FROM project_repositories "
                "WHERE project = ? AND enabled = ? ORDER BY id",
                (project_value, 1),
            )
        if len(rows) != 1:
            if not rows:
                raise ValidationError(
                    "managed work planning requires exactly one enabled registered repository"
                )
            raise ValidationError(
                "managed work planning repository selection is ambiguous"
            )
        repository = dict(rows[0])
        if project_value and str(repository.get("project") or "") != project_value:
            raise ValidationError(
                "managed work plan project does not own the registered repository"
            )
        return repository


def managed_plan_from_dashboard_accept(request: Mapping[str, Any]) -> JsonDict:
    """Project the rich dashboard form onto the compiler's closed plan schema."""

    if str(request.get("mode") or "").strip().lower() != MANAGED_WORK_PLAN_MODE:
        raise ValidationError("dashboard acceptance is not a managed work plan")
    plan: JsonDict = {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "goal": request.get("goal"),
        "package_id": request.get("package_id"),
        "project": request.get("project"),
        "repository_id": request.get("repository_id"),
        "planning_base_ref": request.get("planning_base_ref"),
        "planning_base_sha": request.get("planning_base_sha"),
        "plan_generation": request.get("plan_generation", 1),
        "metadata": copy.deepcopy(dict(request.get("metadata") or {})),
        "nodes": copy.deepcopy(request.get("nodes")),
    }
    for field_name in (
        "integration",
        "max_in_flight",
        "max_mutation_wip",
        "mutation_wip",
        "resource_namespace",
    ):
        if field_name in request and request.get(field_name) is not None:
            plan[field_name] = copy.deepcopy(request[field_name])
    return plan


def _planner_proposal_to_plan(
    proposal: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    repository: Mapping[str, Any],
    base: CanonicalRepositoryBase,
    package_id: str,
    source: str,
) -> JsonDict:
    unknown = sorted(set(proposal) - _MODEL_PROPOSAL_FIELDS - _CONTROLLER_PLAN_FIELDS)
    if unknown:
        raise ValidationError(
            "managed planner proposal contains unknown fields: %s" % ", ".join(unknown)
        )
    locked = sorted(set(proposal) & _CONTROLLER_PLAN_FIELDS)
    if locked:
        raise ValidationError(
            "managed planner proposal may not set controller-owned fields: %s"
            % ", ".join(locked)
        )
    aliases = [name for name in ("nodes", "tasks", "steps") if name in proposal]
    if len(aliases) != 1:
        raise ValidationError(
            "managed planner proposal must set exactly one of nodes, tasks, or steps"
        )
    raw_nodes = proposal[aliases[0]]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValidationError("managed planner proposal must include non-empty nodes")
    _reject_secret_material(proposal)

    request_goal = _optional_text(request.get("goal"))
    proposed_goal = _optional_text(proposal.get("goal"))
    if request_goal and proposed_goal and request_goal != proposed_goal:
        raise ValidationError("managed planner may not replace the operator goal")
    goal = request_goal or proposed_goal
    if not goal:
        raise ValidationError("managed work plan goal is required")

    metadata = copy.deepcopy(dict(proposal.get("metadata") or {}))
    if "managed_work_plan" in metadata:
        raise ValidationError("managed planner metadata uses a controller-reserved key")
    metadata["managed_work_plan"] = {
        "schema": MANAGED_WORK_PLAN_PROVENANCE_SCHEMA,
        "mode": MANAGED_WORK_PLAN_MODE,
        "proposal_source": _required_text(
            source, "managed work plan source", maximum=80
        ),
    }
    plan: JsonDict = {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "goal": goal,
        "package_id": _required_text(
            package_id, "managed work plan package_id", maximum=240
        ),
        "project": str(repository["project"]),
        "repository_id": str(repository["id"]),
        "planning_base_ref": base.planning_base_ref,
        "planning_base_sha": base.planning_base_sha,
        "plan_generation": 1,
        "metadata": metadata,
        "nodes": copy.deepcopy(raw_nodes),
    }
    if base.resource_namespace.get("status") == "resolved":
        plan["resource_namespace"] = {
            field_name: copy.deepcopy(base.resource_namespace[field_name])
            for field_name in (
                "case_sensitive",
                "unicode_normalization",
                "symlink_resolution",
            )
        }
    for field_name in (
        "integration",
        "max_in_flight",
        "max_mutation_wip",
        "mutation_wip",
    ):
        if field_name in proposal:
            plan[field_name] = copy.deepcopy(proposal[field_name])
    return plan


def _require_explicit_node_contracts(plan: Mapping[str, Any]) -> None:
    raw_nodes = plan.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValidationError("managed work plan must contain non-empty nodes")
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, Mapping):
            raise ValidationError(
                "managed work plan.nodes[%d] must be an object" % index
            )
        missing = [
            field_name
            for field_name in ("effects", "expected_outputs", "verification")
            if field_name not in node
        ]
        if missing:
            raise ValidationError(
                "managed work plan.nodes[%d] must explicitly declare %s"
                % (index, ", ".join(missing))
            )
        if not isinstance(node.get("effects"), Mapping):
            raise ValidationError(
                "managed work plan.nodes[%d].effects must be an object" % index
            )
        if not isinstance(node.get("expected_outputs"), list) or not node.get(
            "expected_outputs"
        ):
            raise ValidationError(
                "managed work plan.nodes[%d].expected_outputs must be non-empty"
                % index
            )
        if not isinstance(node.get("verification"), Mapping) or not node.get(
            "verification"
        ):
            raise ValidationError(
                "managed work plan.nodes[%d].verification must be non-empty" % index
            )


def _validate_managed_station_topology(compiled: CompiledWorkPackagePlan) -> None:
    by_key = {node.node_key: node for node in compiled.task_specs}
    mutations = [node for node in compiled.task_specs if node.node_type == "mutation"]
    integrations = [
        node for node in compiled.task_specs if node.node_type == "integration"
    ]
    certifications = [
        node for node in compiled.task_specs if node.node_type == "certification"
    ]
    if not mutations:
        raise ValidationError("managed work plan requires at least one mutation node")
    if len(integrations) != 1 or len(certifications) != 1:
        raise ValidationError(
            "managed work plan requires exactly one integration station and one certification station"
        )
    integration = integrations[0]
    certification = certifications[0]
    if tuple(certification.depends_on) != (integration.node_key,):
        raise ValidationError(
            "managed certification station must depend directly and only on the integration station"
        )

    dependents: Dict[str, set[str]] = {key: set() for key in by_key}
    ancestors: Dict[str, set[str]] = {}
    for key in compiled.topological_order:
        values: set[str] = set()
        for dependency in by_key[key].depends_on:
            dependents[dependency].add(key)
            values.add(dependency)
            values.update(ancestors[dependency])
        ancestors[key] = values
    mutation_leaves = {
        node.node_key
        for node in mutations
        if not any(
            node.node_key in ancestors[other.node_key]
            for other in mutations
            if other.node_key != node.node_key
        )
    }
    if set(integration.depends_on) != mutation_leaves:
        raise ValidationError(
            "managed integration station must depend directly on every mutation leaf and no other node"
        )
    mutation_keys = {node.node_key for node in mutations}
    if not mutation_keys <= ancestors[integration.node_key]:
        raise ValidationError(
            "managed integration station must follow every mutation node"
        )
    if dependents[integration.node_key] != {certification.node_key}:
        raise ValidationError(
            "managed integration station may release only its certification station"
        )
    if dependents[certification.node_key]:
        raise ValidationError("managed certification station must be the terminal node")
    if set(by_key) - {certification.node_key} != ancestors[certification.node_key]:
        raise ValidationError(
            "managed work plan must be one connected DAG terminating at certification"
        )
    for station in (integration, certification):
        if station.effects.writes or station.effects.exclusive or station.effects.external:
            raise ValidationError(
                "managed controller stations may declare reads but not write, exclusive, or external effects"
            )


def _proposal_object(value: Mapping[str, Any] | str) -> JsonDict:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError("managed planner response JSON is invalid") from exc
        value = parsed
    if not isinstance(value, Mapping):
        raise ValidationError("managed planner returned a non-object response")
    return copy.deepcopy(dict(value))


def _reject_secret_material(value: Any, path: str = "plan") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = "%s.%s" % (path, key)
            if _secretish_key(key):
                raise ValidationError(
                    "managed work plan may not contain secret-like field: %s" % where
                )
            _reject_secret_material(item, where)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_material(item, "%s[%d]" % (path, index))
        return
    if not isinstance(value, str):
        return
    _reject_secret_string(value)


def _reject_secret_string(value: str) -> None:
    """Reject a scalar string that carries high-confidence secret material.

    Only redaction-safe labels reach the raised :class:`ValidationError`; the
    matched span itself is never included, logged, or echoed.  Detection is
    deterministic and identical at preview and acceptance because both paths
    funnel every scalar through this helper.
    """

    if _BEARER_RE.search(value):
        raise ValidationError("managed work plan may not contain bearer credentials")
    for pattern, message in _SECRET_STRING_PATTERNS:
        if pattern.search(value):
            raise ValidationError(message)
    for candidate in re.findall(r"(?:https?|ssh|git)://[^\s<>]+", value):
        parsed = urlsplit(candidate.rstrip(".,);]"))
        if parsed.username is not None or parsed.password is not None:
            raise ValidationError(
                "managed work plan may not contain credential-bearing URLs"
            )
        query_keys = {
            key.lower()
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        if query_keys & _SECRET_QUERY_KEYS:
            raise ValidationError(
                "managed work plan may not contain credential-bearing URLs"
            )


def _secretish_key(value: Any) -> bool:
    raw = str(value or "")
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw).lower()
    words = {word for word in re.split(r"[^a-z0-9]+", snake) if word}
    if words & {
        "authorization",
        "credential",
        "password",
        "passwd",
        "secret",
        "token",
    }:
        return True
    compact = re.sub(r"[^a-z0-9]", "", snake)
    return any(marker in compact for marker in ("apikey", "privatekey"))


def _exact_ref_shas(output: str, ref: str) -> list[str]:
    return sorted(
        {
            fields[0].lower()
            for line in output.splitlines()
            if len(fields := line.strip().split()) == 2 and fields[1] == ref
        }
    )


def _reject_credential_source(source: str) -> None:
    if "://" not in source:
        return
    parsed = urlsplit(source)
    if parsed.password is not None or (
        parsed.username is not None
        and not (parsed.scheme.lower() == "ssh" and parsed.username == "git")
    ):
        raise ValidationError(
            "registered canonical repository source may not contain inline credentials"
        )
    query_keys = {
        key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    if query_keys & _SECRET_QUERY_KEYS:
        raise ValidationError(
            "registered canonical repository source may not contain inline credentials"
        )


def _safe_ref(value: Any) -> str:
    ref = _required_text(value, "canonical planning ref", maximum=500)
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in ref)
        or ref.startswith(("-", ".", "/"))
        or ref.endswith((".", "/", ".lock"))
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or any(char in ref for char in " ~^:?*[\\")
    ):
        raise ValidationError("canonical planning ref is unsafe")
    return ref


def _required_text(value: Any, field_name: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError("%s is required" % field_name)
    if len(text) > maximum or any(char in text for char in "\x00\r\n"):
        raise ValidationError("%s is invalid" % field_name)
    return text


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
