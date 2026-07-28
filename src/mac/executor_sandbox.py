"""Autonomous task executor (extracted from the deploy heredoc — loop-01).

This is the process the MacWorker spawns per claimed task. It builds a prompt,
runs an authenticated coding-agent CLI inside a mandatory OpenShell sandbox in
the task's git worktree, then derives **honest,
deterministic** evidence from real git state (or, for non-repo work, records the
agent's output as an *unverified* operator_result — never a fabricated pass).

Previously this lived as ~500 lines of Python inside a bash heredoc in
``deploy/deploy-mac-fleet.sh`` — untestable and prone to drift. It now lives
here as an importable, unit-tested module; the deploy writes only a 2-line shim
that calls :func:`main`.

Three capabilities beyond the original:

* **Telemetry path** — every run emits executor-scoped observations
  (``layer="executor"``, ``executor.*``) to the hub so the autonomous loop is
  visible distinctly from the per-command audit trail.
* **Memory feed (deployment gets smarter over time)** — before running, the
  executor *recalls* prior "deployment lessons" for the project and injects
  them into the agent prompt; after running, it *records* a structured
  ``deployment_learning`` memory from the outcome. The nap consolidator
  (mem-08) later promotes those records into the vector tier, so recall
  improves with every task the fleet completes.
* **Automatic task sizing** — before running the agent, the executor inspects
  the task title and description for "plan" signals (conjunctions of verbs,
  numbered steps, multi-phase language, excessive scope).  When signals are
  found the agent receives an explicit instruction to call ``add_child_tasks``
  via the MAC API and write evidence_type=plan_decomposed, which causes the
  parent to block on its children.  A post-run hook (``maybe_auto_decompose``)
  also reads the agent's output for a ``plan_steps`` JSON block and auto-posts
  child tasks when the agent explicitly declares them.

All hub I/O is best-effort and gated on hub env (URL + token): absent those,
the executor still runs and writes evidence — it just doesn't emit telemetry,
recall, or record. The HTTP seam (:func:`_hub_post` / :func:`_hub_get`) and the
agent runner are injectable so the logic is testable without a live hub.

Optional OpenShell sandboxing (sandbox-01): the agent already runs ``--yolo``
(Hermes' own approval prompts bypassed). When ``MAC_OPENSHELL_SANDBOX`` is set,
:func:`_maybe_wrap_openshell` launches that invocation as a confined child of an
OpenShell sandbox, which then enforces *all* guardrails (filesystem, syscall,
and deny-by-default network egress) from a declarative policy. Default OFF —
the wrap is a pure argv transform, so behavior is unchanged unless enabled. See
``docs/openshell-sandbox.md``.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re as _re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac import mac_paths
from mac import relay_observability
from mac.agent_command import PROMPT_SENTINEL
from mac.models import (
    NON_REPOSITORY_OUTCOME_EVIDENCE_TYPES,
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    REPORT_REPOSITORY_READ_ONLY_MODE,
    metadata_declares_read_only_report_repository,
    metadata_declares_report_deliverable,
)
from mac.codegraph_audit import (
    codegraph_audit_check,
    codegraph_audit_manifest_problems,
    codegraph_audit_passed,
    run_codegraph_audit,
)
from mac.fleet_learning import (
    REPOSITORY_ACCESS_RECORD_TYPE,
    parse_repository_access_learning,
    repository_host,
    task_repository_remote,
)
from mac.gitops import (
    CanonicalFreshnessResult,
    check_canonical_freshness,
    guarded_push,
    resolve_canonical_publication_target,
    sync_worktree_with_canonical,
)
from mac.trusted_artifact import (
    nofollow_regular_file_identity,
    nofollow_source_bundle_digest,
)
from mac.openshell_runtime import (
    SANDBOX_BASE_PATH as _SANDBOX_BASE_PATH,
    openshell_required_for_local_agent as _openshell_required_for_local_agent,
    truthy as _truthy,
)
from mac.env_config import (
    env_bool,
    env_str,
    resolve_env_chain,
)
from mac.review_failure_classifier import (
    FinalizerRefusalKind,
    classify_finalizer_refusal,
)
from mac.repository_access_env import (
    REPOSITORY_CREDENTIAL_ENV_NAMES,
    fence_read_only_repository_environment,
    read_only_repository_content_digest,
)
from mac.read_only_report_verifier import (
    INTEGRITY_SCHEMA as _READ_ONLY_VERIFICATION_INTEGRITY_SCHEMA,
    raw_git_control_digest as _authoritative_read_only_git_control_digest,
)

# ---------------------------------------------------------------------------
# Small utilities, hub I/O seam, and plan-detection
# (Extracted to mac.executor_hub_io — re-exported here for backward compat)
# ---------------------------------------------------------------------------
from mac.executor_hub_io import (  # noqa: E402,F401 - compatibility re-exports
    utcnow,
    sha256_text,
    command_audit_id,
    redacted_arg,
    audit_safe_argv,
    safe_path_component,
    local_agent_id,
    _hub_env,
    _hub_post,
    _hub_post_json,
    _hub_get,
    _hub_put,
    _hub_post_child_tasks,
    _PLAN_TITLE_KEYWORDS,
    _NUMBERED_STEP_RE,
    _BULLET_RE,
    detect_plan_signals,
    _plan_detection_section,
)
from mac.executor_memory import (  # noqa: E402,F401 - compatibility re-exports
    DEPLOYMENT_LEARNING_PREFIX,
    _LESSON_CURATION_PROMPT,
    _LESSON_PROMPT_BUDGET,
    _LESSON_STOPWORDS,
    _PLAN_LEARNING_SCHEMA,
    _append_lesson_with_budget,
    _format_learning_content,
    _format_plan_learning_content,
    _lesson_terms,
    _plan_family_terms,
    _string_list,
    _structured_lesson_content,
    _task_project,
    build_learning_record,
    build_plan_learning_record,
    build_telemetry_record,
    curate_lessons_from_outcome,
    emit_telemetry,
    recall_deployment_lessons,
    recall_plan_lessons,
    recall_prior_attempt_lessons,
    record_curated_lessons,
    record_deployment_learning,
    record_plan_outcome,
)
from mac.executor_scope import (  # noqa: E402,F401 - compatibility re-exports
    MAC_TASK_SUMMARY_BEGIN,
    MAC_TASK_SUMMARY_END,
    NEW_FILE_COMMIT_RULE,
    _SCOPE_LARGE_DESC_CHARS,
    _SCOPE_LARGE_DESC_WORDS,
    _SCOPE_LARGE_REPO_CMDS,
    _compute_scope_signals,
    _lessons_section,
    _nested_dict,
    build_planning_prompt,
    compute_scope_estimate,
    is_plan_decomposed_evidence,
    is_planning_phase,
    maybe_auto_decompose,
    maybe_preflight_scope_estimate,
    needs_scope_estimate,
    recall_scope_lessons,
    record_scope_estimate,
)
from mac.executor_prompt import (  # noqa: E402,F401 - compatibility re-exports
    _blind_review_protocol,
    _cooperative_integration_section,
    _error_signature,
    _is_truthy,
    _is_untracked_new_files_refusal,
    _read_json_object,
    _repository_bootstrap_timeout,
    _repository_contract_bootstrap,
    _repository_contract_canonical_branch,
    _repository_contract_canonical_remote,
    _repository_contract_test_command,
    _repository_lease_id,
    _repository_prepared_base,
    _repository_publication_remote,
    _repository_task_branch,
    _review_experiment_assignment,
    _run_repository_bootstrap_if_needed,
    _run_captured,
    build_blind_review_discovery_prompt,
    build_review_prompt,
    build_task_prompt,
    classify_outcome,
    clip_process_text,
    repository_contract_section,
    run_with_stall_watchdog,
    task_evidence_type,
    task_is_repo_coupled,
)
from mac.executor_finalizer import (  # noqa: E402,F401 - compatibility re-exports
    BREAK_GLASS_AUTHORIZATION_SCHEMA,
    PRESERVED_EXECUTOR_EVIDENCE_FILENAME,
    PRESERVED_EXECUTOR_WORKTREE_FILENAME,
    REPOSITORY_WIP_BUNDLE_SCHEMA,
    REPOSITORY_WIP_MANIFEST_FILENAME,
    PreservationMissing,
    PreservedExecutorState,
    _FinalizerPhaseContext,
    _cooperative_integration_check,
    _finalizer_phase_timeout,
    _git,
    _load_harness_recovery_log,
    _new_file_finalize_message,
    _preserve_executor_state_before_refusal,
    _read_executor_evidence_payload,
    _record_recovery_learnings,
    _sign_verdict,
    _split_porcelain_status,
    _untracked_finalize_message,
    _write_git_finalizer_refusal_manifest,
    _write_partial_finalizer_evidence,
    load_preserved_executor_state,
    preserve_repository_wip_bundle,
    recover_from_new_file_refusal,
    run_deterministic_git_finalizer,
    run_deterministic_review_verdict,
    write_fallback_evidence_manifest,
)

def post_command_audit(agent_id: str, payload: Dict[str, Any]) -> None:
    """Post a command audit record for the given agent to the hub."""
    if not agent_id:
        return
    _hub_post("/agents/%s/command-audit" % agent_id, payload)



def run_audited_command(argv: List[str], cwd: Path, task_id, metadata: Dict[str, Any]):
    """Run a command with audit records emitted before and after execution."""
    command_id = command_audit_id()
    agent_id = local_agent_id()
    started_at = utcnow()
    started = time.monotonic()
    argv_hash = sha256_text(json.dumps(argv, separators=(",", ":")))
    base = {
        "command_id": command_id,
        "argv": audit_safe_argv(argv),
        "cwd": str(cwd),
        "task_id": task_id,
        "started_at": started_at,
        "metadata": {"component": "mac-task-executor", "argv_sha256": argv_hash, **metadata},
    }
    post_command_audit(agent_id, {**base, "phase": "started"})
    timeout = metadata.pop("timeout", None) if isinstance(metadata, dict) else None
    try:
        result = _run_captured(argv, cwd, timeout)
    except subprocess.TimeoutExpired as exc:
        # loop-01 resilience: a wedged TokenHub turn can hang the agent
        # indefinitely. Bound it. The agent may already have written a valid
        # mac-evidence.json before the trailing turn stalled; main() salvages
        # that so verified work isn't discarded just because the run was capped.
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        post_command_audit(
            agent_id,
            {
                **base,
                "phase": "timeout",
                "completed_at": utcnow(),
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "metadata": {**base["metadata"], "timeout_seconds": timeout},
            },
        )
        return subprocess.CompletedProcess(argv, 124, out, err + "\n[executor] agent run timed out after %ss" % timeout)
    except OSError as exc:
        post_command_audit(
            agent_id,
            {
                **base,
                "phase": "error",
                "completed_at": utcnow(),
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "metadata": {**base["metadata"], "error": str(exc)},
            },
        )
        raise
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    post_command_audit(
        agent_id,
        {
            **base,
            "phase": "completed" if result.returncode == 0 else "failed",
            "completed_at": utcnow(),
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "returncode": result.returncode,
            "stdout_sha256": sha256_text(stdout),
            "stderr_sha256": sha256_text(stderr),
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AgentCommandBundle:
    workspace: Path
    prompt_file: Path
    command_file: Path
    policy_file: Path
    interpreter: str

    def argv(self, *, sandbox_workspace: Optional[str] = None) -> List[str]:
        if sandbox_workspace:
            command_file = "%s/%s" % (sandbox_workspace.rstrip("/"), self.command_file.name)
            prompt_file = "%s/%s" % (sandbox_workspace.rstrip("/"), self.prompt_file.name)
            interpreter = "/opt/mac-venv/bin/python"
        else:
            command_file = str(self.command_file)
            prompt_file = str(self.prompt_file)
            interpreter = self.interpreter
        return [
            interpreter,
            "-m",
            "mac.agent_command",
            "--command-file",
            command_file,
            "--prompt-file",
            prompt_file,
        ]

    def cleanup(self) -> None:
        self.command_file.unlink(missing_ok=True)
        self.prompt_file.unlink(missing_ok=True)
        self.policy_file.unlink(missing_ok=True)


def _write_agent_command_bundle(
    workspace: Path, prompt: str, agent_argv: List[str]
) -> _AgentCommandBundle:
    import uuid

    if agent_argv.count(PROMPT_SENTINEL) != 1:
        raise ValueError("agent argv must contain exactly one private-prompt sentinel")
    workspace.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    prompt_file = workspace / (".mac-agent-prompt-%s" % nonce)
    command_file = workspace / (".mac-agent-command-%s.json" % nonce)
    policy_file = workspace / ".mac-executor-policy.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    command_file.write_text(
        json.dumps({"schema": "mac.agent_command.v1", "argv": agent_argv}),
        encoding="utf-8",
    )
    prompt_file.chmod(0o600)
    command_file.chmod(0o600)
    policy_file.write_text(
        (Path(__file__).resolve().parent / "executor-policy.txt").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    policy_file.chmod(0o600)
    return _AgentCommandBundle(
        workspace=workspace,
        prompt_file=prompt_file,
        command_file=command_file,
        policy_file=policy_file,
        interpreter=sys.executable,
    )


# ---------------------------------------------------------------------------
# OpenShell sandbox wrapping (sandbox-01)
#
# The agent already runs ``--yolo`` (Hermes' own permission/approval prompts are
# bypassed). On its own that is unguarded. When OpenShell sandboxing is enabled
# the (still ``--yolo``) Hermes invocation is launched as a *confined child* of
# an OpenShell sandbox, which then becomes the SOLE guardrail authority:
#   * Landlock  — filesystem confinement to declared paths
#   * seccomp   — syscall filtering + privilege drop (never runs as root)
#   * egress    — deny-by-default network proxy driven by a declarative policy
# The policy YAML (MAC_OPENSHELL_POLICY) *is* the guardrail specification.
#
# Default OFF: with MAC_OPENSHELL_SANDBOX unset/false ``_maybe_wrap_openshell``
# returns the argv unchanged, so the executor behaves exactly as before. The
# wrap is a pure argv transform — it does not itself require OpenShell to be
# installed; that is the deployer's responsibility (see
# docs/openshell-sandbox.md).
#
# Knobs (read at wrap time — nothing is frozen at import):
#   MAC_OPENSHELL_SANDBOX          truthy -> enable wrapping
#   MAC_OPENSHELL_BIN             openshell binary (default "openshell")
#   MAC_OPENSHELL_POLICY          explicit policy YAML path (the guardrail spec).
#                                 When unset, the wrap resolves a policy in this
#                                 order and ALWAYS passes one (never the OpenShell
#                                 image default): explicit -> ~/.mac/openshell-
#                                 policy.yaml -> bundled fail-closed default
#                                 (src/mac/openshell/default-policy.yaml).
#   MAC_OPENSHELL_SANDBOX_NAME    fixed sandbox name (debug; default: ephemeral)
#   MAC_OPENSHELL_KEEP            truthy -> --keep (debug; default one-shot teardown)
#   MAC_OPENSHELL_GC              truthy -> delete old orphaned MAC sandboxes
#                                 before creating a new task sandbox
#   MAC_OPENSHELL_STALE_AFTER_SECONDS minimum age for automatic GC (default 86400)
#   MAC_OPENSHELL_REAP_ORPHANS   default-on; fail-closed reap of MAC-owned task
#                                 sandboxes with mac.keep=false + a dead recorded
#                                 mac.pid (no age wait). Set 0 to disable.
#   MAC_OPENSHELL_CREATE_ARGS     extra `sandbox create` args (shell-split), e.g.
#                                 "--from my-image" or "--upload /src:/src" used to
#                                 make the Hermes runtime + workspace available
#                                 inside the sandbox
#   MAC_OPENSHELL_ENV_PASSTHROUGH comma list of env names copied through a
#                                 private mode-0600 workspace file
#   MAC_ALLOW_UNSANDBOXED_YOLO    truthy (default "1") -> allow --yolo with no
#                                 sandbox (current fleet, logs a warning). Set
#                                 "0" to fail closed: refuse unguarded YOLO so
#                                 --yolo is only ever used inside the sandbox.
# ---------------------------------------------------------------------------

# Forward the env the agent needs to reach the hub + model gateway from inside
# the sandbox. (Network reachability is still gated by the OpenShell policy;
# this only makes the values visible to the process.)
_DEFAULT_OPENSHELL_ENV_PASSTHROUGH = (
    "MAC_HUB_URL,MAC_URL,MAC_WORKER_TOKEN,MAC_TOKEN,MAC_API_TOKEN,"
    "MAC_WORKER_AGENT_ID,MAC_WORKER_AGENT_NAME,MAC_AGENT_ID,MAC_TASK_ID,MAC_LEASE_ID,"
    "HERMES_GATEWAY_BASE_URL,HERMES_GATEWAY_MODEL,HERMES_SESSION_KEY,HERMES_YOLO_MODE,"
    # Model-gateway base_url + api_key live in the agent's ~/.hermes/.env, which
    # is NOT in the sandbox image; the gateway requires auth. Forward them so the
    # sandboxed hermes can authenticate (the *_BASE_URL values have their host
    # loopback rewritten to the sandbox host alias in the private env file).
    "MAC_HERMES_GATEWAY_BASE_URL,MAC_HERMES_GATEWAY_API_KEY,MAC_HERMES_GATEWAY_PROVIDER,"
    "OPENAI_BASE_URL,OPENAI_API_KEY,CODEX_API_KEY,"
    "MAC_CODEX_BASE_URL,MAC_CODEX_TOKEN,MAC_CODEX_PROVIDER,MAC_CODEX_WIRE_API,MAC_CODEX_MODEL,"
    # Coding-agent CLI credentials (see mac.coding_agent). A sandboxed coding
    # agent authenticates safely via these env keys. File-based Codex auth is not
    # forwarded by default because OpenShell uploads are copies: a throwaway
    # sandbox can consume and rotate the refresh token without persisting the
    # replacement back to the host.
    "ANTHROPIC_API_KEY,ANTHROPIC_AUTH_TOKEN,ANTHROPIC_BASE_URL,ANTHROPIC_MODEL,"
    "CLAUDE_CODE_OAUTH_TOKEN,CLAUDE_CODE_USE_BEDROCK,CLAUDE_CODE_USE_VERTEX,CLAUDE_CODE_USE_FOUNDRY,"
    "CURSOR_API_KEY,MAC_CURSOR_ENDPOINT,CURSOR_AGENT_ENDPOINT,MAC_CURSOR_MODEL,"
    # Repository credentials are separate from model-route credentials.  They
    # use the same private mode-0600 upload as the other sandbox secrets so git
    # and gh work inside the confined executor without copying host SSH keys.
    "GH_TOKEN,GITHUB_TOKEN,GITEA_TOKEN,GITEA_USER"
)

# PATH is an image/runtime invariant, not configuration to import from the
# worker host.  The OpenShell image owns this baseline; repository-contract
# tools are prepended by ``mac_sandbox_toolchain_setup`` below.  Keeping the
# shared runtime value as well as the Containerfile makes custom env
# passthrough fail closed instead of allowing a host virtualenv or
# package-manager shim to leak into sandbox command resolution.
_FORBIDDEN_OPENSHELL_ENV_PASSTHROUGH = frozenset({"PATH"})
_HOST_ONLY_HUB_CREDENTIALS = frozenset(
    {
        "MAC_WORKER_TOKEN",
        "MAC_TOKEN",
        "MAC_API_TOKEN",
        "MAC_ATTESTATION_KEY",
        "MAC_HUB_TOKEN",
    }
)
_DEFAULT_OPENSHELL_ENV_NAMES = frozenset(
    item.strip()
    for item in _DEFAULT_OPENSHELL_ENV_PASSTHROUGH.split(",")
    if item.strip()
)
_READ_ONLY_REPORT_ENV_ALLOWLIST = (
    _DEFAULT_OPENSHELL_ENV_NAMES
    - _HOST_ONLY_HUB_CREDENTIALS
    - REPOSITORY_CREDENTIAL_ENV_NAMES
    - _FORBIDDEN_OPENSHELL_ENV_PASSTHROUGH
)


def _read_only_report_environment_passthrough_valid() -> bool:
    """Whether the requested report environment is the positive allowlist.

    A custom passthrough is not an extension point for repository reports. It
    may only select names from the reviewed default model/runtime surface.
    Controller, worker, attestation, repository, Python-injection, and unknown
    variables never qualify for the pre-claim attestation.
    """

    custom = env_str("MAC_OPENSHELL_ENV_PASSTHROUGH")
    if not custom:
        # The reviewed default is reduced below before use: host authority is
        # stripped globally and repository authority is fenced for this lane.
        return True
    names = custom
    requested = {
        item.strip() for item in names.split(",") if item.strip()
    }
    return requested <= _READ_ONLY_REPORT_ENV_ALLOWLIST


def _openshell_enabled() -> bool:
    return _truthy(env_str("MAC_OPENSHELL_SANDBOX"))


_OPENSHELL_HOST_ALIAS_DEFAULT = "host.openshell.internal"
_HOST_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")


def _openshell_host_alias() -> str:
    """The in-sandbox alias for the host (OpenShell injects this hosts entry).
    A forwarded ``http://127.0.0.1:8789`` is unreachable from inside the sandbox
    (that loopback is the sandbox's own); rewrite it to this alias."""
    return env_str("MAC_OPENSHELL_HOST_ALIAS") or _OPENSHELL_HOST_ALIAS_DEFAULT


def _rewrite_host_local_url(value: str, alias: str) -> str:
    """Rewrite a URL whose host is the machine's loopback to the sandbox host
    alias, so forwarded service URLs (MAC_HUB_URL, gateway base) resolve from
    inside the sandbox. Only touches values that look like URLs (contain '://')
    and only the authority's loopback host — tokens/other values pass through."""
    if not value or "://" not in value:
        return value
    out = value
    for h in _HOST_LOCAL_HOSTS:
        out = out.replace("://%s" % h, "://%s" % alias).replace("@%s" % h, "@%s" % alias)
    return out


def _openshell_environment() -> Dict[str, str]:
    """Environment copied through a private workspace file, never process argv."""
    names = env_str("MAC_OPENSHELL_ENV_PASSTHROUGH") or _DEFAULT_OPENSHELL_ENV_PASSTHROUGH
    alias = _openshell_host_alias()
    values: Dict[str, str] = {}
    seen = set()
    read_only_repository = (
        env_str("MAC_TASK_REPO_ACCESS_MODE") == REPORT_REPOSITORY_READ_ONLY_MODE
        and env_str("MAC_TASK_REPO_ACCESS_SCHEMA") == REPORT_REPOSITORY_ACCESS_SCHEMA
    )
    if read_only_repository and not _read_only_report_environment_passthrough_valid():
        raise ValueError(
            "read-only repository report environment contains a non-allowlisted variable"
        )
    for raw in names.split(","):
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name in _FORBIDDEN_OPENSHELL_ENV_PASSTHROUGH:
            raise ValueError(
                "%s may not be forwarded from the host into OpenShell; "
                "the sandbox image and repository toolchain own command resolution"
                % name
            )
        val = os.environ.get(name)
        if val is None:
            continue
        values[name] = _rewrite_host_local_url(val, alias)
    # Hub authority stays in the host executor for every task class. The model
    # sandbox does not need to heartbeat, claim work, mutate the ledger, or sign
    # controller evidence; forwarding a fleet worker bearer would let it do all
    # of those things as the host identity.
    for name in _HOST_ONLY_HUB_CREDENTIALS:
        values.pop(name, None)
    if read_only_repository:
        fence_read_only_repository_environment(values)
    return values


_LANDLOCK_CREATE_RULESET_SYSCALL = 444
_LANDLOCK_CREATE_RULESET_VERSION = 1


def _landlock_abi_version() -> int:
    """Return the kernel Landlock ABI version, or zero when unavailable.

    Querying ``/sys/kernel/security/lsm`` is not reliable inside containers:
    Kubernetes commonly leaves securityfs unmounted even though the shared
    host kernel implements and permits Landlock.  The version-query form of
    ``landlock_create_ruleset(2)`` is the kernel's authoritative feature probe.

    Linux assigned syscall number 444 to ``landlock_create_ruleset`` for the
    architectures MAC supports (including x86_64 and arm64).  An older kernel,
    a blocked syscall, or any execution error returns zero so callers retain
    fail-closed behavior.
    """
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        result = syscall(
            ctypes.c_long(_LANDLOCK_CREATE_RULESET_SYSCALL),
            ctypes.c_void_p(),
            ctypes.c_size_t(0),
            ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return int(result) if result > 0 else 0


def _kernel_has_landlock() -> bool:
    """True if the running kernel exposes a usable Landlock ABI.

    The operator policy uses ``landlock: best_effort`` because OpenShell's egress
    proxy is incompatible with ``hard_requirement`` on current kernels (it adds a
    directory ReadDir right on its own non-directory proxy path, which Landlock
    ABI >= 3 rejects). best_effort still fully enforces on a Landlock-capable
    kernel, but would silently run UNCONFINED on a kernel without Landlock — so
    the executor performs this precheck to recover the fail-closed guarantee.
    """
    return _landlock_abi_version() > 0


def _bundled_default_policy() -> Path:
    """Path to the fail-closed OpenShell policy bundled in this package."""
    return Path(__file__).resolve().parent / "openshell" / "default-policy.yaml"


def _resolve_openshell_policy() -> str:
    """Resolve the policy passed to ``openshell sandbox create``.

    Resolution order (first hit wins):
      1. ``MAC_OPENSHELL_POLICY`` (explicit) — must exist, else raise.
      2. ``~/.mac/openshell-policy.yaml`` — the operator-filled fleet policy.
      3. the package's bundled fail-closed default (``openshell/default-policy.yaml``).

    A policy is *always* returned (or we raise) — the wrap never omits
    ``--policy``, so enabling sandboxing can never silently fall back to
    OpenShell's own image-default profile. The bundled default denies all
    network egress, so an unconfigured deployment fails closed (tasks can't
    reach the hub/gateway) rather than running under an unknown profile.
    """
    explicit = env_str("MAC_OPENSHELL_POLICY")
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError("MAC_OPENSHELL_POLICY=%r but no such file" % explicit)
        return explicit
    deployed = mac_paths.mac_home() / "openshell-policy.yaml"
    if deployed.is_file():
        return str(deployed)
    bundled = _bundled_default_policy()
    if bundled.is_file():
        return str(bundled)
    raise FileNotFoundError(
        "OpenShell sandboxing is enabled but no policy could be resolved "
        "(set MAC_OPENSHELL_POLICY, install %s, or ship %s). Refusing to run "
        "without an explicit policy." % (deployed, bundled)
    )


# OpenShell sandboxes are container copies with NO bind-mount, so the task git
# worktree must be UPLOADED in and the agent's results DOWNLOADED back out — a
# plain ``create -- argv`` would run the agent against an empty /sandbox and lose
# its edits + evidence on teardown. The run is therefore a lifecycle:
#   create (--upload workspace, run agent in it, KEEP) -> download -> delete.
# ``include_workdir`` in the policy only grants Landlock access to the path; it
# does not copy files. /sandbox is OpenShell's writable workspace root (uploads
# and downloads must live under it).
_SANDBOX_WORKDIR = "/sandbox"
_SANDBOX_HOME = "/tmp"
_SANDBOX_VERIFICATION_FILE = "mac-sandbox-verification.json"
_TRUSTED_READ_ONLY_VERIFICATION_FILE = (
    ".mac-trusted-read-only-sandbox-verification.json"
)
_MAX_SANDBOX_VERIFICATION_BYTES = 2 * 1024 * 1024


def _openshell_bin() -> str:
    return env_str("MAC_OPENSHELL_BIN") or "openshell"


def _sandbox_name() -> str:
    """A unique name for the kept sandbox so the download + delete steps can
    target it. Overridable via MAC_OPENSHELL_SANDBOX_NAME (debug a single run)."""
    explicit = env_str("MAC_OPENSHELL_SANDBOX_NAME")
    if explicit:
        return explicit
    import uuid

    return "mac-task-" + uuid.uuid4().hex[:12]


def _sandbox_identity_labels() -> List[str]:
    """Durable lease-authority identity labels for the current executor.

    The recorded creator ``mac.pid`` only proves liveness on the *creating*
    host, so a sandbox left Ready by an executor that exited (or that ran on a
    now-unreachable host) cannot be reconciled from PID liveness alone. Stamping
    the durable ``MAC_TASK_ID`` / ``MAC_LEASE_ID`` onto the sandbox lets the
    reaper consult the authoritative lease store instead: a sandbox whose task
    is terminal, unleased, lease-expired, or whose lease has been superseded is
    provably orphaned regardless of where — or whether — its creator still runs.
    Labels are emitted only when the corresponding value is present so
    non-task sandboxes and older callers are unaffected.
    """

    labels: List[str] = []
    task_id = (env_str("MAC_TASK_ID") or "").strip()
    if task_id:
        labels += ["--label", "mac.task.id=%s" % task_id]
    lease_id = (env_str("MAC_LEASE_ID") or "").strip()
    if lease_id:
        labels += ["--label", "mac.lease.id=%s" % lease_id]
    return labels


def _sandbox_label_argv(kind: str, *, keep: bool = False) -> List[str]:
    # Repository sandboxes carry the only copy of sandbox-local clean commits
    # until harvest creates a durable host bundle. Mark them protected from the
    # stale/dead-PID/lease reapers even when the operator did not request debug
    # retention. The ordinary lifecycle still deletes them directly after
    # preservation succeeds. Read-only report sandboxes cannot contain WIP and
    # retain their mandatory-teardown contract.
    repository_wip_guard = (
        kind == "task"
        and bool((env_str("MAC_TASK_REPO_WORKTREE") or "").strip())
        and (env_str("MAC_TASK_REPO_ACCESS_MODE") or "").strip().lower()
        != REPORT_REPOSITORY_READ_ONLY_MODE
    )
    return [
        "--label",
        "mac.owner=mac",
        "--label",
        "mac.kind=%s" % kind,
        "--label",
        "mac.pid=%d" % os.getpid(),
        "--label",
        "mac.keep=%s" % ("true" if keep or repository_wip_guard else "false"),
    ] + _sandbox_identity_labels()


def _sandbox_gc_best_effort() -> None:
    if not env_bool("MAC_OPENSHELL_GC"):
        return
    try:
        stale_after = float(
            env_str("MAC_OPENSHELL_STALE_AFTER_SECONDS") or "86400"
        )
    except ValueError:
        stale_after = 86400.0
    try:
        from .openshell_sandbox_gc import reconcile_stale_sandboxes

        report = reconcile_stale_sandboxes(
            openshell_bin=_openshell_bin(),
            stale_after_seconds=max(0.0, stale_after),
            include_legacy=True,
            apply=True,
        )
        if report["deleted"]:
            sys.stderr.write(
                "[executor] removed %d stale OpenShell sandbox(es)\n"
                % len(report["deleted"])
            )
        if report["failures"]:
            sys.stderr.write(
                "[executor] WARNING: failed to remove %d stale OpenShell sandbox(es)\n"
                % len(report["failures"])
            )
    except Exception as exc:  # noqa: BLE001 - cleanup must not block guarded execution
        sys.stderr.write("[executor] WARNING: OpenShell sandbox GC failed: %s\n" % exc)


def _reap_orphaned_task_sandboxes_best_effort(audit_id: Any = None) -> None:
    """Fail-closed reap of orphaned MAC-owned task sandboxes with a dead PID.

    Every executor sandbox lifecycle begins here, so completion, timeout,
    cancellation, reviewer handoff, agent restart, and abrupt executor-exit all
    converge on this sweep the next time *any* executor runs: a sandbox whose
    owning executor exited (dead recorded ``mac.pid``) and which is not marked
    ``mac.keep=true`` is reaped immediately, with no age wait. Unlike the
    age-gated stale GC this runs by default because it is fail-closed — it only
    ever deletes exact MAC-owned sandboxes it can prove are orphaned. Set
    ``MAC_OPENSHELL_REAP_ORPHANS=0`` to disable.

    Best-effort: classification and deletion failures are logged and never block
    the guarded run that follows.
    """

    if not env_bool("MAC_OPENSHELL_REAP_ORPHANS", True):
        return
    try:
        from .openshell_sandbox_gc import reap_orphaned_task_sandboxes

        report = reap_orphaned_task_sandboxes(
            openshell_bin=_openshell_bin(),
            apply=True,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not block guarded execution
        sys.stderr.write(
            "[executor] WARNING: orphaned task sandbox reap failed: %s\n" % exc
        )
        return

    if report["candidates"]:
        emit_telemetry(
            "sandbox_orphan_reaped",
            task_id=str(audit_id) if audit_id else None,
            level="info" if not report["failures"] else "warning",
            scanned=report["scanned"],
            protected=report["protected"],
            reaped=len(report["deleted"]),
            failed=len(report["failures"]),
            names=[str(row["name"]) for row in report["candidates"]],
        )
    if report["deleted"]:
        sys.stderr.write(
            "[executor] reaped %d orphaned OpenShell task sandbox(es) with a dead PID\n"
            % len(report["deleted"])
        )
    if report["failures"]:
        sys.stderr.write(
            "[executor] WARNING: failed to reap %d orphaned OpenShell task sandbox(es)\n"
            % len(report["failures"])
        )


def _reconcile_task_sandboxes_from_lease_authority_best_effort(
    audit_id: Any = None,
) -> None:
    """Fail-closed reconcile of task sandboxes against durable lease authority.

    The dead-PID reaper only proves orphanhood on the *creating* host. This
    sweep additionally consults the authoritative lease store (the same source
    of truth the k8s controller uses for stuck Jobs): a Ready task sandbox whose
    ``mac.task.id`` maps to a terminal, unleased, lease-expired, or
    lease-superseded task is reaped even when its recorded creator PID cannot be
    proven dead. Sandboxes without identity labels, with an unresolvable task,
    with ``mac.keep=true``, or with a live matching lease are always preserved.

    Best-effort: requires hub access (``MAC_HUB_URL``/``MAC_URL`` + token) to
    resolve tasks; a missing hub, lookup failures, and delete failures are logged
    and never block the guarded run that follows. Set
    ``MAC_OPENSHELL_RECONCILE_LEASES=0`` to disable.
    """

    if not env_bool("MAC_OPENSHELL_RECONCILE_LEASES", True):
        return

    from mac.executor_hub_io import _hub_env, _hub_get

    base_url, token = _hub_env()
    if not base_url or not token:
        return

    def _lookup_task(task_id: str) -> Optional[Mapping[str, Any]]:
        if not task_id:
            return None
        result = _hub_get("/tasks/%s" % task_id)
        if isinstance(result, Mapping):
            inner = result.get("task")
            if isinstance(inner, Mapping):
                return inner
            return result
        return None

    try:
        from .openshell_sandbox_gc import (
            reconcile_task_sandboxes_from_lease_authority,
        )

        report = reconcile_task_sandboxes_from_lease_authority(
            _lookup_task,
            openshell_bin=_openshell_bin(),
            apply=True,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not block guarded execution
        sys.stderr.write(
            "[executor] WARNING: lease-authority sandbox reconcile failed: %s\n" % exc
        )
        return

    if report["candidates"]:
        emit_telemetry(
            "sandbox_lease_reconciled",
            task_id=str(audit_id) if audit_id else None,
            level="info" if not report["failures"] else "warning",
            scanned=report["scanned"],
            protected=report["protected"],
            reaped=len(report["deleted"]),
            failed=len(report["failures"]),
            names=[str(row["name"]) for row in report["candidates"]],
        )
    if report["deleted"]:
        sys.stderr.write(
            "[executor] reconciled %d orphaned OpenShell task sandbox(es) from lease authority\n"
            % len(report["deleted"])
        )
    if report["failures"]:
        sys.stderr.write(
            "[executor] WARNING: failed to reconcile %d orphaned OpenShell task sandbox(es)\n"
            % len(report["failures"])
        )


def _workspace_basename(workspace: Path) -> str:
    """OpenShell's ``upload <dir> /sandbox`` nests the dir under its basename
    (-> /sandbox/<basename>); that is where the agent runs and what we download."""
    return os.path.basename(str(workspace).rstrip("/")) or "workspace"


def _sandbox_path_for_workspace_child(workspace: Path, sandbox_workspace: str, value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        rel = Path(raw).expanduser().resolve().relative_to(workspace.expanduser().resolve())
    except (OSError, ValueError):
        return None
    return "%s/%s" % (sandbox_workspace.rstrip("/"), str(rel).replace(os.sep, "/"))


def _sandbox_repository_environment(workspace: Path, sandbox_workspace: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    mapped_worktree = _sandbox_path_for_workspace_child(
        workspace,
        sandbox_workspace,
        env_str("MAC_TASK_REPO_WORKTREE"),
    )
    if mapped_worktree:
        values["MAC_TASK_REPO_WORKTREE"] = mapped_worktree
    for name in (
        "MAC_TASK_REPO_BRANCH",
        "MAC_TASK_REPO_LEASE_ID",
        "MAC_TASK_REPO_BASE_SHA",
        "MAC_TASK_REPO_BASE_TREE",
        "MAC_TASK_REPO_REFS_DIGEST",
        "MAC_TASK_REPO_CONTENT_DIGEST",
        "MAC_TASK_REPO_REMOTE",
        "MAC_TASK_CANONICAL_REMOTE",
        "MAC_TASK_REPO_DEFAULT_BRANCH",
        "MAC_TASK_REPO_ACCESS_MODE",
        "MAC_TASK_REPO_ACCESS_SCHEMA",
    ):
        value = os.environ.get(name)
        if value:
            values[name] = value
    return values


def _ensure_landlock_or_fail() -> None:
    """Fail closed if the kernel can't enforce Landlock: the operator policy is
    best_effort (forced by OpenShell's proxy/hard_requirement incompatibility),
    which would otherwise run UNCONFINED on a Landlock-less kernel. Override only
    for a deliberate, audited exception.

    On macOS the host kernel is *never* the enforcement point — OpenShell
    sandboxes run as Linux containers inside the Docker (Desktop) Linux VM, whose
    LinuxKit kernel does not surface ``/sys/kernel/security/lsm`` to containers.
    seccomp + namespaces + the deny-by-default egress proxy still enforce there;
    Landlock path-confinement is the only piece waived. macOS Docker-based fleet
    nodes therefore set ``MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1`` as the documented
    posture (see ADR 0008 amendment / docs/openshell-sandbox.md)."""
    if _kernel_has_landlock() or env_bool("MAC_OPENSHELL_ALLOW_NO_LANDLOCK"):
        return
    if sys.platform == "darwin":
        raise RuntimeError(
            "OpenShell sandboxing is enabled on macOS, where the host kernel "
            "cannot expose Landlock (sandboxes run in the Docker Desktop Linux "
            "VM; its LinuxKit kernel does not surface /sys/kernel/security/lsm). "
            "seccomp + namespaces + the egress proxy still enforce. Set "
            "MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1 to accept this posture (the "
            "documented default for macOS Docker fleet nodes)."
        )
    raise RuntimeError(
        "OpenShell sandboxing is enabled but the Landlock ABI syscall is "
        "unavailable or blocked; the policy's "
        "filesystem confinement (best_effort) would not be enforced. Refusing "
        "to run (fail closed). Use a Landlock-capable kernel (>=5.13, ABI>=3 "
        "recommended), or set MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1 to override."
    )


def _sandbox_toolchain_setup_shell() -> str:
    """Shell function injected into the task sandbox before agent/test work."""
    return r'''
mac_sandbox_toolchain_setup() {
  set +e
  MAC_SANDBOX_PYTHON="${MAC_SANDBOX_PYTHON:-/opt/mac-venv/bin/python}"
  [ -x "$MAC_SANDBOX_PYTHON" ] || MAC_SANDBOX_PYTHON="$(command -v python3 || command -v python || true)"
  [ -n "$MAC_SANDBOX_PYTHON" ] || return 0
  export MAC_TOOLCHAIN_ROOT="${MAC_TOOLCHAIN_ROOT:-${MAC_TASK_WORKSPACE:-$PWD}/.mac-toolchain}"
  export MAC_TOOLCHAIN_BIN="$MAC_TOOLCHAIN_ROOT/bin"
  mkdir -p "$MAC_TOOLCHAIN_BIN"
  export MAC_SANDBOX_BASE_PATH="${MAC_SANDBOX_BASE_PATH:-/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin}"
  mac_refresh_sandbox_path() {
    MAC_SANDBOX_PATH_PREFIX="$MAC_TOOLCHAIN_BIN:$MAC_TOOLCHAIN_ROOT/node_modules/.bin"
    [ -n "${JAVA_HOME:-}" ] && MAC_SANDBOX_PATH_PREFIX="$MAC_SANDBOX_PATH_PREFIX:$JAVA_HOME/bin"
    export MAC_SANDBOX_PATH_PREFIX
    export PATH="$MAC_SANDBOX_PATH_PREFIX:$MAC_SANDBOX_BASE_PATH"
    hash -r 2>/dev/null || true
  }
  mac_refresh_sandbox_path
  eval "$("$MAC_SANDBOX_PYTHON" - "$MAC_TASK_FILE" <<'PY'
import json, shlex, sys
try:
    loaded = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    loaded = {}
task = loaded.get("task", loaded) if isinstance(loaded, dict) else {}
metadata = task.get("metadata") if isinstance(task, dict) else {}
if not isinstance(metadata, dict):
    metadata = {}
access = metadata.get("report_repository_access")
read_only_report = (
    str(metadata.get("deliverable") or "").strip().lower()
    in {"report", "answer", "analysis", "investigation", "question", "triage"}
    and isinstance(access, dict)
    and str(access.get("schema") or "").strip() == "mac.report_repository_access.v1"
    and str(access.get("mode") or "").strip().lower() == "read_only"
)
contracts = []
contract_paths = (
    (("execution_contract", "repository_contract"),)
    if read_only_report
    else (
        ("execution_contract", "repository_contract"),
        ("origin", "repository_contract"),
        ("repository_contract",),
    )
)
for path in contract_paths:
    node = metadata
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, dict):
        contracts.append(node)
required, seen = [], set()
creates = []
bootstrap = ""
test = ""
for contract in contracts:
    toolchain = contract.get("toolchain") if isinstance(contract.get("toolchain"), dict) else {}
    for command in toolchain.get("required_commands") or []:
        command = str(command).strip()
        if command and command not in seen:
            seen.add(command)
            required.append(command)
    if not bootstrap:
        boot = contract.get("bootstrap") if isinstance(contract.get("bootstrap"), dict) else {}
        bootstrap = str(boot.get("command") or "").strip()
        creates = [str(item).strip() for item in (boot.get("creates") or []) if str(item).strip()]
    if not test:
        test_block = contract.get("test") if isinstance(contract.get("test"), dict) else {}
        test = str(test_block.get("command") or "").strip()
print("export MAC_REPO_REQUIRED_COMMANDS=%s" % shlex.quote(" ".join(required)))
print("export MAC_REPO_BOOTSTRAP_COMMAND=%s" % shlex.quote(bootstrap))
print("export MAC_REPO_BOOTSTRAP_CREATES=%s" % shlex.quote("\n".join(creates)))
print("export MAC_REPO_TEST_COMMAND=%s" % shlex.quote(test))
PY
)"
  mac_log="$MAC_TOOLCHAIN_ROOT/provisioning.log"
  mac_note() { printf '%s\n' "$*" >> "$mac_log"; }
  mac_install_java_local() {
    command -v curl >/dev/null 2>&1 || return 1
    command -v tar >/dev/null 2>&1 || return 1
    arch="$(uname -m 2>/dev/null || echo x64)"
    case "$arch" in
      x86_64|amd64) arch="x64" ;;
      aarch64|arm64) arch="aarch64" ;;
      *) return 1 ;;
    esac
    mkdir -p "$MAC_TOOLCHAIN_ROOT/java"
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/17/ga/linux/${arch}/jre/hotspot/normal/eclipse?project=jdk" -o "$MAC_TOOLCHAIN_ROOT/jre.tar.gz" || return 1
    tar -xzf "$MAC_TOOLCHAIN_ROOT/jre.tar.gz" -C "$MAC_TOOLCHAIN_ROOT/java" --strip-components=1 || return 1
    export JAVA_HOME="$MAC_TOOLCHAIN_ROOT/java"
    mac_refresh_sandbox_path
  }
  mac_install_gh_local() {
    command -v curl >/dev/null 2>&1 || return 1
    command -v tar >/dev/null 2>&1 || return 1
    arch="$(uname -m 2>/dev/null || echo x64)"
    case "$arch" in
      x86_64|amd64) asset_arch="linux_amd64" ;;
      aarch64|arm64) asset_arch="linux_arm64" ;;
      armv6l|armv7l) asset_arch="linux_armv6" ;;
      *) return 1 ;;
    esac
    release_json="$MAC_TOOLCHAIN_ROOT/gh-release.json"
    curl -fsSL https://api.github.com/repos/cli/cli/releases/latest -o "$release_json" || return 1
    url="$("$MAC_SANDBOX_PYTHON" - "$release_json" "$asset_arch" <<'PYGH'
import json, sys
path, asset_arch = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
for asset in data.get("assets") or []:
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if name.endswith("%s.tar.gz" % asset_arch) and url:
        print(url)
        break
PYGH
)"
    [ -n "$url" ] || return 1
    mkdir -p "$MAC_TOOLCHAIN_ROOT/gh"
    curl -fsSL "$url" -o "$MAC_TOOLCHAIN_ROOT/gh.tar.gz" || return 1
    tar -xzf "$MAC_TOOLCHAIN_ROOT/gh.tar.gz" -C "$MAC_TOOLCHAIN_ROOT/gh" --strip-components=1 || return 1
    [ -x "$MAC_TOOLCHAIN_ROOT/gh/bin/gh" ] || return 1
    ln -sf "$MAC_TOOLCHAIN_ROOT/gh/bin/gh" "$MAC_TOOLCHAIN_BIN/gh"
    mac_refresh_sandbox_path
  }
  mac_install_node_local() {
    command -v curl >/dev/null 2>&1 || return 1
    command -v tar >/dev/null 2>&1 || return 1
    narch="$(uname -m 2>/dev/null || echo x64)"
    case "$narch" in
      x86_64|amd64) narch="x64" ;;
      aarch64|arm64) narch="arm64" ;;
      *) return 1 ;;
    esac
    nver="${MAC_SANDBOX_NODE_VERSION:-v22.12.0}"
    mkdir -p "$MAC_TOOLCHAIN_ROOT/node"
    curl -fsSL "https://nodejs.org/dist/${nver}/node-${nver}-linux-${narch}.tar.xz" -o "$MAC_TOOLCHAIN_ROOT/node.tar.xz" >> "$mac_log" 2>&1 || return 1
    tar -xJf "$MAC_TOOLCHAIN_ROOT/node.tar.xz" -C "$MAC_TOOLCHAIN_ROOT/node" --strip-components=1 >> "$mac_log" 2>&1 || return 1
    for b in node npm npx corepack; do
      [ -x "$MAC_TOOLCHAIN_ROOT/node/bin/$b" ] && ln -sf "$MAC_TOOLCHAIN_ROOT/node/bin/$b" "$MAC_TOOLCHAIN_BIN/$b"
    done
    mac_refresh_sandbox_path
    [ -x "$MAC_TOOLCHAIN_BIN/node" ] || return 1
  }
  mac_ensure_modern_node() {
    # The base sandbox ships Node 18, but modern pnpm (v10, the corepack/repo
    # default) requires Node >=22 and aborts otherwise, failing every Node test
    # target. If node is missing or older than v20, install a pinned Node 22 into
    # the task toolchain and shadow the stale one on PATH (MAC_TOOLCHAIN_BIN is
    # already PATH-first). Override the version with MAC_SANDBOX_NODE_VERSION.
    nmajor="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    case "$nmajor" in ''|*[!0-9]*) nmajor=0 ;; esac
    if [ "$nmajor" -ge 20 ] 2>/dev/null; then
      return 0
    fi
    mac_note "node major=$nmajor (<20); installing pinned modern node"
    if mac_install_node_local; then
      return 0
    fi
    # Modern Node couldn't be fetched (e.g. nodejs.org is not on the sandbox
    # egress allowlist -> curl 403). Fall back to a Node-18-compatible pnpm
    # (pnpm@9, installed from the allowlisted npm registry via corepack) placed
    # in MAC_TOOLCHAIN_BIN so it SHADOWS any system pnpm@10 that would reject
    # Node 18 ("requires Node >=22"). Lets pnpm install / Node tests run on 18.
    mac_note "modern node unavailable; pinning Node-18-compatible pnpm instead"
    mac_install_command pnpm || mac_note "could not pin Node-18-compatible pnpm"
  }
  mac_install_command() {
    cmd="$1"
    case "$cmd" in
      gh)
        if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && command -v apt-get >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
          mkdir -p -m 755 /etc/apt/keyrings >> "$mac_log" 2>&1 || true
          curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg >> "$mac_log" 2>&1 || true
          chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg >> "$mac_log" 2>&1 || true
          echo "deb [arch=$(dpkg --print-architecture 2>/dev/null || echo amd64) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list 2>> "$mac_log" || true
          DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gh >> "$mac_log" 2>&1 && return 0
        fi
        mac_install_gh_local
        ;;
      pnpm)
        # Pin pnpm to a version compatible with the sandbox's Node. pnpm@latest
        # (v10) demands Node >=22.13, but the base sandbox image ships Node 18, so
        # `pnpm` aborts ("requires at least Node.js v22.13") and every Node test
        # target fails. pnpm@9 supports Node >=18.12, so it runs on the sandbox's
        # Node 18 and on newer Node alike. Override with MAC_SANDBOX_PNPM_VERSION.
        # Install a REAL pnpm@<ver> binary into the toolchain bin (PATH-first) so
        # it SHADOWS any system/corepack pnpm. A `corepack prepare ... --activate`
        # only leaves a shim that RE-RESOLVES to the newer version at run time
        # (which is why pnpm install kept hitting "requires Node v22" even after
        # we "pinned" 9), so prefer a concrete npm-installed binary.
        pnpm_ver="${MAC_SANDBOX_PNPM_VERSION:-9}"
        if command -v npm >/dev/null 2>&1; then
          npm install --no-fund --no-audit --prefix "$MAC_TOOLCHAIN_ROOT" "pnpm@${pnpm_ver}" >> "$mac_log" 2>&1
          if [ -x "$MAC_TOOLCHAIN_ROOT/node_modules/.bin/pnpm" ]; then
            ln -sf "$MAC_TOOLCHAIN_ROOT/node_modules/.bin/pnpm" "$MAC_TOOLCHAIN_BIN/pnpm"
            mac_refresh_sandbox_path
            command -v pnpm >/dev/null 2>&1 && return 0
          fi
        fi
        if command -v corepack >/dev/null 2>&1; then
          corepack enable --install-directory "$MAC_TOOLCHAIN_BIN" >> "$mac_log" 2>&1 || true
          corepack prepare "pnpm@${pnpm_ver}" --activate >> "$mac_log" 2>&1 || true
        fi
        command -v pnpm >/dev/null 2>&1 && return 0
        return 1
        ;;
      lein)
        command -v curl >/dev/null 2>&1 || return 1
        curl -fsSL https://raw.githubusercontent.com/technomancy/leiningen/stable/bin/lein -o "$MAC_TOOLCHAIN_BIN/lein" >> "$mac_log" 2>&1 || return 1
        chmod +x "$MAC_TOOLCHAIN_BIN/lein"
        ;;
      java)
        if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && command -v apt-get >/dev/null 2>&1; then
          DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openjdk-17-jre-headless >> "$mac_log" 2>&1 && return 0
        fi
        mac_install_java_local
        ;;
      node|npm)
        [ "$(id -u 2>/dev/null || echo 1)" = "0" ] || return 1
        command -v apt-get >/dev/null 2>&1 || return 1
        DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs npm >> "$mac_log" 2>&1
        ;;
      make)
        [ "$(id -u 2>/dev/null || echo 1)" = "0" ] || return 1
        command -v apt-get >/dev/null 2>&1 || return 1
        DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends make >> "$mac_log" 2>&1
        ;;
      cargo|rustc|rustup)
        # Cargo lives at ~/.cargo/bin, which is not on MAC_SANDBOX_BASE_PATH.
        # Avoid a false-negative by first promoting any existing installation
        # into the toolchain bin, then falling back to a rustup-based install.
        mac_cargo_home="${CARGO_HOME:-$HOME/.cargo}"
        if [ -x "$mac_cargo_home/bin/cargo" ]; then
          for mac_rust_bin in cargo rustc rustup rust-analyzer; do
            [ -x "$mac_cargo_home/bin/$mac_rust_bin" ] && \
              ln -sf "$mac_cargo_home/bin/$mac_rust_bin" "$MAC_TOOLCHAIN_BIN/$mac_rust_bin"
          done
          mac_refresh_sandbox_path
          command -v cargo >/dev/null 2>&1 && return 0
        fi
        command -v curl >/dev/null 2>&1 || return 1
        curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path >> "$mac_log" 2>&1 || return 1
        mac_cargo_home="${CARGO_HOME:-$HOME/.cargo}"
        for mac_rust_bin in cargo rustc rustup; do
          [ -x "$mac_cargo_home/bin/$mac_rust_bin" ] && \
            ln -sf "$mac_cargo_home/bin/$mac_rust_bin" "$MAC_TOOLCHAIN_BIN/$mac_rust_bin"
        done
        mac_refresh_sandbox_path
        command -v cargo >/dev/null 2>&1 && return 0
        return 1
        ;;
      *)
        return 1
        ;;
    esac
  }
  # Force a modern Node BEFORE the per-command loop: node may already be present
  # (so the loop would skip it) yet be too old for the repo's pnpm. Only for repos
  # whose toolchain actually uses Node, to avoid an unnecessary download.
  case " $MAC_REPO_REQUIRED_COMMANDS " in
    *" node "*|*" npm "*|*" pnpm "*) mac_ensure_modern_node ;;
  esac
  # Pin a pnpm that READS the repo's declared config. The base image ships pnpm
  # 11, which DROPPED reading pnpm settings from package.json (onlyBuiltDependencies
  # etc.) and from .npmrc — so repos that declare config there get a broken/
  # incomplete install: native build scripts are ignored ("ERR_PNPM_IGNORED_BUILDS")
  # and devDeps like jest/vitest end up half-linked ("Cannot find module .../jest").
  # pnpm 9 reads package.json + .npmrc config and installs completely on Node 18-22,
  # and (unlike pnpm 11) does not run the high-concurrency release-age metadata pass
  # that the egress proxy can't sustain. When pnpm is required and the system pnpm
  # is >=10, install a task-local pnpm@<ver> PATH-first so the repo's config is
  # honored. Override/opt out with MAC_SANDBOX_PNPM_VERSION.
  case " $MAC_REPO_REQUIRED_COMMANDS " in
    *" pnpm "*)
      mac_pnpm_major="$(pnpm --version 2>/dev/null | cut -d. -f1)"
      case "$mac_pnpm_major" in ''|*[!0-9]*) mac_pnpm_major=0 ;; esac
      mac_pnpm_want="${MAC_SANDBOX_PNPM_VERSION:-9}"
      if [ "$mac_pnpm_want" != "system" ] && [ "$mac_pnpm_major" -ge 10 ] 2>/dev/null \
         && [ ! -x "$MAC_TOOLCHAIN_BIN/pnpm" ]; then
        mac_install_command pnpm && mac_note "pinned task-local pnpm@${mac_pnpm_want} (image pnpm ${mac_pnpm_major} ignores package.json/.npmrc config)" \
          || mac_note "could not pin compatible pnpm; using system pnpm ${mac_pnpm_major}"
      fi
      ;;
  esac
  for cmd in $MAC_REPO_REQUIRED_COMMANDS; do
    command -v "$cmd" >/dev/null 2>&1 && continue
    mac_note "missing command before provisioning: $cmd"
    mac_install_command "$cmd" || mac_note "could not provision command: $cmd"
  done
  missing_after=""
  for cmd in $MAC_REPO_REQUIRED_COMMANDS; do
    command -v "$cmd" >/dev/null 2>&1 || missing_after="$missing_after $cmd"
  done
  worktree="${MAC_TASK_REPO_WORKTREE:-$PWD}"
  needs_bootstrap=0
  bootstrap_ran=0
  bootstrap_returncode=0
  bootstrap_status="skipped"
  if [ -n "$MAC_REPO_BOOTSTRAP_COMMAND" ] && [ "${MAC_READ_ONLY_AUTHORITATIVE_VERIFIER:-0}" != "1" ]; then
    if [ -z "$MAC_REPO_BOOTSTRAP_CREATES" ]; then
      needs_bootstrap=1
    else
      while IFS= read -r create_path; do
        [ -z "$create_path" ] && continue
        [ -e "$worktree/$create_path" ] || needs_bootstrap=1
      done <<EOF
$MAC_REPO_BOOTSTRAP_CREATES
EOF
    fi
  fi
  if [ "$needs_bootstrap" = "1" ] && [ -d "$worktree" ]; then
    bootstrap_ran=1
    mac_note "running bootstrap.command: $MAC_REPO_BOOTSTRAP_COMMAND"
    # pnpm >=10.16 reads install tuning ONLY from pnpm-workspace.yaml (camelCase) —
    # NOT .npmrc, env vars, or `pnpm config set --global` (all verified ignored).
    # The deny-by-default L7 egress proxy resets high-concurrency registry fetches
    # (UND_ERR_SOCKET / ERR_PNPM_META_FETCH_FAIL) and pnpm's release-age supply-
    # chain pass amplifies it by fetching metadata for every lockfile entry. Cap
    # network concurrency + disable the release-age pass DURING install by
    # appending to pnpm-workspace.yaml, then RESTORE the file so the worktree stays
    # clean for the contract dirty-check (installed node_modules persist). Gated on
    # the file existing, so non-pnpm repos are untouched. See ADR 0009.
    mac_ws_yaml="$worktree/pnpm-workspace.yaml"
    mac_ws_tuned=0
    # Only relevant for pnpm >=10 (which reads these from pnpm-workspace.yaml and
    # runs the release-age pass). Under the pinned pnpm 9 the file isn't consulted
    # for these keys, so skip the edit to avoid an unknown-setting warning.
    mac_eff_pnpm="$(pnpm --version 2>/dev/null | cut -d. -f1)"
    case "$mac_eff_pnpm" in ''|*[!0-9]*) mac_eff_pnpm=0 ;; esac
    if [ "$mac_eff_pnpm" -ge 10 ] 2>/dev/null && [ -f "$mac_ws_yaml" ] && ! grep -q "networkConcurrency:" "$mac_ws_yaml" 2>/dev/null; then
      if cp "$mac_ws_yaml" "$MAC_TOOLCHAIN_ROOT/pnpm-workspace.yaml.macbak" 2>/dev/null; then
        mac_ws_tuned=1
        {
          printf '\n# mac: temporary install tuning for the constrained sandbox egress proxy\n'
          printf 'networkConcurrency: %s\n' "${MAC_SANDBOX_NETWORK_CONCURRENCY:-2}"
          printf 'minimumReleaseAge: 0\n'
        } >> "$mac_ws_yaml"
        mac_note "tuned pnpm-workspace.yaml for install (networkConcurrency=${MAC_SANDBOX_NETWORK_CONCURRENCY:-2}, minimumReleaseAge=0)"
      fi
    fi
    # `bash -lc` runs the login profile, which RESETS PATH to the system default
    # and discards the toolchain bin we prepended above. Re-assert the toolchain
    # PATH (and clear bash's command hash) INSIDE the login shell, after the
    # profile runs, so the pinned tools win.
    ( cd "$worktree" && /bin/bash -lc 'export PATH="$MAC_SANDBOX_PATH_PREFIX:$MAC_SANDBOX_BASE_PATH"; hash -r 2>/dev/null || true; '"$MAC_REPO_BOOTSTRAP_COMMAND" ) >> "$mac_log" 2>&1
    bootstrap_returncode=$?
    # Restore the original pnpm-workspace.yaml so the worktree is not left dirty.
    if [ "$mac_ws_tuned" = "1" ]; then
      mv -f "$MAC_TOOLCHAIN_ROOT/pnpm-workspace.yaml.macbak" "$mac_ws_yaml" 2>/dev/null || true
    fi
    if [ "$bootstrap_returncode" = "0" ]; then
      bootstrap_status="pass"
    else
      bootstrap_status="fail"
      mac_note "bootstrap.command failed"
    fi
  fi
  export MAC_REPO_BOOTSTRAP_SETUP_RAN="$bootstrap_ran"
  export MAC_REPO_BOOTSTRAP_SETUP_RETURNCODE="$bootstrap_returncode"
  export MAC_REPO_BOOTSTRAP_SETUP_STATUS="$bootstrap_status"
  "$MAC_SANDBOX_PYTHON" - <<'PY' >/dev/null 2>&1 || true
import json, os, shutil
root = os.environ.get("MAC_TOOLCHAIN_ROOT") or ""
if not root:
    raise SystemExit(0)
required = [item for item in os.environ.get("MAC_REPO_REQUIRED_COMMANDS", "").split() if item]
delta = {
    "schema": "mac.sandbox_environment_delta.v1",
    "package_manager": "sandbox-toolchain",
    "commands": required,
    "missing_after": [item for item in required if shutil.which(item) is None],
    "toolchain_root": root,
    "reason": "repository_contract.toolchain.required_commands",
}
os.makedirs(root, exist_ok=True)
with open(os.path.join(root, "environment-delta.json"), "w", encoding="utf-8") as handle:
    json.dump(delta, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  return 0
}
'''


def _sandbox_repository_verification_shell(
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    # Verification runs through a fresh ``openshell sandbox exec`` process, so
    # it does not inherit the private environment sourced by the agent process.
    # Re-export only non-secret workspace/repository paths needed to re-read the
    # task's repository contract and run its test gate.
    exports = [
        "export %s=%s" % (name, shlex.quote(value))
        for name, value in sorted((environment or {}).items())
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ]
    return "\n".join(
        [
            *exports,
            _sandbox_toolchain_setup_shell(),
            'cd "$MAC_TASK_WORKSPACE"',
            "mac_sandbox_toolchain_setup || true",
            r'''$MAC_SANDBOX_PYTHON - <<'PY'
import json, os, signal, subprocess, tempfile, time
workspace = os.environ.get("MAC_TASK_WORKSPACE") or os.getcwd()
worktree = os.environ.get("MAC_TASK_REPO_WORKTREE") or workspace
command = os.environ.get("MAC_REPO_TEST_COMMAND", "").strip()
# Impact-scoped gate (durable per-task gate-speed fix): when the configured
# command is the full contract gate AND the repo ships the fail-closed sanity
# contract, run only the tests the task's diff touches (base = the pre-task SHA).
# run-sanity-tests.sh itself falls back to the whole-repo gate on any resolver
# error or when an infrastructure path (test-policy.toml global_full_paths)
# changed, and enforces diff-coverage on the selected subset, so verification is
# never weakened — only narrowed to the changed surface. Mirrors the opencode
# executor's gate_detect_test_command and the hub-review verifier, which already
# prefer the sanity contract; the report/worker sandbox path had been left on the
# whole-repo gate, so every code task paid the full ~34-60min suite.
_repo_base_sha = os.environ.get("MAC_TASK_REPO_BASE_SHA", "").strip()
if command in ("scripts/run-contract-tests.sh", "./scripts/run-contract-tests.sh") and _repo_base_sha:
    _sanity = os.path.join(worktree, "scripts", "run-sanity-tests.sh")
    if os.path.isfile(_sanity) and os.access(_sanity, os.X_OK):
        command = "scripts/run-sanity-tests.sh --base " + _repo_base_sha
bootstrap_command = os.environ.get("MAC_REPO_BOOTSTRAP_COMMAND", "").strip()
# `bash -lc` re-runs the login profile, which resets PATH to the system default
# and drops the toolchain bin we prepended during setup — so repo bootstrap/test
# commands would resolve a stale system tool (e.g. pnpm@10 that demands Node 22)
# instead of the pinned toolchain one (pnpm@9). Re-assert the toolchain PATH (and
# clear bash's command hash) INSIDE the login shell so the pinned tools win.
_TC_PATH_PREFIX = (
    'export PATH="$MAC_SANDBOX_PATH_PREFIX:$MAC_SANDBOX_BASE_PATH"; '
    'hash -r 2>/dev/null || true; '
)
bootstrap_creates = [
    item.strip()
    for item in os.environ.get("MAC_REPO_BOOTSTRAP_CREATES", "").splitlines()
    if item.strip()
]
result_path = os.path.join(workspace, "mac-sandbox-verification.json")
delta_path = os.path.join(os.environ.get("MAC_TOOLCHAIN_ROOT", ""), "environment-delta.json")
delta = {}
try:
    with open(delta_path, encoding="utf-8") as handle:
        delta = json.load(handle)
except Exception:
    delta = {}

def missing_bootstrap_outputs():
    return [
        path
        for path in bootstrap_creates
        if not os.path.exists(os.path.join(worktree, path))
    ]

def clip(value, limit=4000):
    # Keep head AND tail — pytest/pip print the diagnosis LAST; a head-only
    # cut hid every long failure from evidence (observed live, repeatedly).
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    marker = "\n… [%d chars omitted] …\n" % (len(text) - head - tail)
    return text[:head] + marker + text[-tail:]

def run_bounded_bash(command, timeout):
    """Run a verifier command and terminate its whole process group.

    Output goes to files rather than pipes. A background descendant can inherit
    a pipe after the login shell exits, causing ``communicate()`` to wait until
    the full repository timeout even though the declared command already
    completed. Files let ``wait()`` observe the command process directly; any
    descendants left in its process group are then killed as verifier debris.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", _TC_PATH_PREFIX + command],
                cwd=worktree,
                env=os.environ.copy(),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                proc.wait()
            else:
                # The command process exited. Do not allow background children
                # from the verifier to leak into later sandbox steps.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    pass
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            return (
                124 if timed_out else int(proc.returncode),
                stdout or "",
                stderr or "",
                timed_out,
            )

bootstrap = None
if bootstrap_command:
    setup_ran = os.environ.get("MAC_REPO_BOOTSTRAP_SETUP_RAN") == "1"
    try:
        setup_returncode = int(os.environ.get("MAC_REPO_BOOTSTRAP_SETUP_RETURNCODE") or "0")
    except ValueError:
        setup_returncode = 1
    setup_status = os.environ.get("MAC_REPO_BOOTSTRAP_SETUP_STATUS") or (
        "pass" if setup_returncode == 0 else "fail"
    )
    missing_before = missing_bootstrap_outputs()
    if not bootstrap_creates:
        bootstrap = {
            "command": bootstrap_command,
            "creates": bootstrap_creates,
            "returncode": setup_returncode,
            "status": setup_status,
            "reason": "bootstrap.creates omitted; setup phase ran bootstrap before verification"
            if setup_ran
            else "bootstrap.creates omitted; setup phase did not run bootstrap",
        }
    elif not missing_before:
        bootstrap = {
            "command": bootstrap_command,
            "creates": bootstrap_creates,
            "returncode": 0,
            "status": "skipped",
            "reason": "declared bootstrap outputs already exist",
        }
    else:
        started = time.time()
        try:
            timeout = float(
                os.environ.get("MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT")
                or os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT")
                or "1800"
            )
        except ValueError:
            timeout = 1800.0
        returncode, stdout, stderr, timed_out = run_bounded_bash(bootstrap_command, timeout)
        bootstrap = {
            "command": bootstrap_command,
            "creates": bootstrap_creates,
            "missing_before": missing_before,
            "returncode": returncode,
            "status": "pass" if returncode == 0 else "fail",
            "stdout": clip(stdout),
            "stderr": clip(stderr),
            "duration_ms": int((time.time() - started) * 1000),
        }
        if timed_out:
            bootstrap["error"] = "bootstrap command timed out after %ss" % timeout
    if bootstrap.get("returncode") == 0 and bootstrap_creates:
        missing_after = missing_bootstrap_outputs()
        if missing_after:
            bootstrap = dict(bootstrap)
            bootstrap["returncode"] = 1
            bootstrap["status"] = "fail"
            bootstrap["missing_after"] = missing_after
            bootstrap["error"] = "bootstrap command did not create declared outputs"

if not command:
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "fail",
        "command": "",
        "returncode": 1,
        "stderr": "repository contract test.command is missing",
        "environment_delta": delta,
    }
elif bootstrap is not None and bootstrap.get("returncode") != 0:
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "fail",
        "command": command,
        "returncode": int(bootstrap.get("returncode") or 1),
        "stderr": "repository bootstrap failed before sandbox verification tests",
        "worktree": worktree,
        "environment_delta": delta,
        "bootstrap": bootstrap,
    }
else:
    started = time.time()
    try:
        timeout = float(os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "1800") or "1800")
    except ValueError:
        timeout = 1800.0
    returncode, stdout, stderr, timed_out = run_bounded_bash(command, timeout)
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "pass" if returncode == 0 else "fail",
        "command": command,
        "returncode": returncode,
        "stdout": clip(stdout),
        "stderr": clip(stderr),
        "duration_ms": int((time.time() - started) * 1000),
        "worktree": worktree,
        "environment_delta": delta,
    }
    if timed_out:
        payload["error"] = "repository test command timed out after %ss" % timeout
    if bootstrap is not None:
        payload["bootstrap"] = bootstrap
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
raise SystemExit(0 if payload.get("returncode") == 0 else int(payload.get("returncode") or 1))
PY''',
        ]
    )


def _sandbox_read_only_repository_verification_shell(
    environment: Mapping[str, str],
) -> str:
    """Run the image-owned report verifier after toolchain-only setup.

    Bootstrap and test execution belong to the trusted module because it
    installs mutation watches before either command.  The setup helper may
    provision immutable runtime tools, but the verifier mode prevents it from
    executing ``bootstrap.command`` early.
    """

    exports = [
        "export %s=%s" % (name, shlex.quote(value))
        for name, value in sorted(environment.items())
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ]
    return "\n".join(
        [
            *exports,
            'export MAC_READ_ONLY_AUTHORITATIVE_VERIFIER="1"',
            _sandbox_toolchain_setup_shell(),
            'cd "$MAC_TASK_WORKSPACE"',
            "mac_sandbox_toolchain_setup || exit 70",
            'exec "$MAC_SANDBOX_PYTHON" -I -m mac.read_only_report_verifier',
        ]
    )


def _write_private_shell_env(path: Path, values: Mapping[str, str]) -> Path:
    env_lines = [
        "export %s=%s" % (name, shlex.quote(value))
        for name, value in sorted(values.items())
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ]
    path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _write_sandbox_runtime_files(
    workspace: Path, sandbox_workspace: str
) -> tuple[Path, Path]:
    env_values: Dict[str, str] = {
        **_openshell_environment(),
        **_sandbox_repository_environment(workspace, sandbox_workspace),
        "MAC_TASK_WORKSPACE": sandbox_workspace,
        "MAC_TASK_FILE": "%s/task.json" % sandbox_workspace.rstrip("/"),
        # OpenShell runs the image as its unprivileged sandbox user. Hermes'
        # uploaded config is deliberately rooted under /tmp, so HOME belongs in
        # the private environment file rather than the process-visible
        # MAC_OPENSHELL_CREATE_ARGS argv.
        "HOME": _SANDBOX_HOME,
        # Never inherit the worker host's executable search path.  The image
        # runtime is the stable baseline and task-local contract tools are
        # prepended when the toolchain setup file is sourced.
        "MAC_SANDBOX_BASE_PATH": _SANDBOX_BASE_PATH,
        "PATH": _SANDBOX_BASE_PATH,
    }
    env_file = _write_private_shell_env(
        workspace / ".mac-openshell-env.sh", env_values
    )

    toolchain_file = workspace / ".mac-sandbox-toolchain.sh"
    toolchain_file.write_text(_sandbox_toolchain_setup_shell(), encoding="utf-8")
    toolchain_file.chmod(0o700)
    return env_file, toolchain_file


def _task_requires_gpu(task: Any) -> bool:
    """Return whether this task explicitly requires a GPU-backed sandbox.

    Host GPU presence is not enough: CPU-only coding probes and ordinary tasks
    must remain runnable when a nested container runtime cannot expose the
    accelerator.  The dispatch contract's required capabilities are the
    authoritative task-level request.
    """
    if not isinstance(task, dict):
        return False
    raw = task.get("required_capabilities")
    if not isinstance(raw, (list, tuple, set)):
        return False
    capabilities = {str(item).strip().lower() for item in raw if str(item).strip()}
    return bool(capabilities & {"gpu", "cuda", "rocm"})


def _openshell_extra_create_argv(*, require_gpu: bool = False) -> List[str]:
    """Parse executor-owned OpenShell args and apply task-specific GPU access.

    ``bootstrap-openshell.sh`` only writes the Codex OAuth upload when the
    operator opts in, but the rendered ``MAC_OPENSHELL_CREATE_ARGS`` can outlive
    that opt-in.  Never keep copying the rotating host auth file merely because
    an old recipe still contains it.  File auth is retained only when both
    explicit risk flags remain enabled *and* no environment API key is present;
    environment auth wins the coding-agent selection and makes the file both
    unnecessary and unsafe to copy into a throwaway sandbox. A legacy global
    ``--gpu`` is always removed: only an explicit GPU task may add it back, and
    only after bootstrap proved the nested OpenShell GPU path.
    """
    extra = env_str("MAC_OPENSHELL_CREATE_ARGS")
    if not extra:
        return []
    argv = shlex.split(extra)
    if "--env" in argv or "--" in argv:
        raise ValueError(
            "MAC_OPENSHELL_CREATE_ARGS may not contain --env or --; "
            "use MAC_OPENSHELL_ENV_PASSTHROUGH for private environment transfer"
        )
    permit_codex_file_auth = (
        not (os.environ.get("OPENAI_API_KEY") or "").strip()
        and env_bool("MAC_OPENSHELL_UPLOAD_CODEX_AUTH")
        and env_bool("MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH")
    )
    filtered: List[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--gpu" or token.startswith("--gpu="):
            index += 1
            if token == "--gpu" and index < len(argv) and argv[index].isdigit():
                index += 1
            continue
        if token == "--upload" and index + 1 < len(argv):
            upload = argv[index + 1]
            _source, separator, destination = upload.rpartition(":")
            if (
                separator
                and destination == "/tmp/.codex/auth.json"
                and not permit_codex_file_auth
            ):
                index += 2
                continue
        filtered.append(token)
        index += 1
    if require_gpu:
        if not env_bool("MAC_OPENSHELL_GPU_AVAILABLE"):
            raise RuntimeError(
                "task requires GPU but bootstrap did not verify OpenShell GPU access"
            )
        filtered.append("--gpu")
    return filtered


_MANAGED_OPENSHELL_RUNTIME_REF_RE = _re.compile(
    r"ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}"
)


def _managed_openshell_runtime_image_ref() -> str:
    """Return the deployment-pinned OpenShell image, or fail closed."""

    mac_home = mac_paths.mac_home()
    path = Path(
        env_str("MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE")
        or mac_home / "openshell" / "runtime-image-ref"
    ).expanduser()
    try:
        image_ref = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            "read-only repository reports require a readable immutable "
            "OpenShell runtime image reference at %s" % path
        ) from exc
    if not _MANAGED_OPENSHELL_RUNTIME_REF_RE.fullmatch(image_ref):
        raise RuntimeError(
            "read-only repository reports require the managed immutable "
            "mac-openshell-runtime@sha256 image reference"
        )
    return image_ref


def _assert_approved_read_only_report_runtime(
    *, runtime_image_ref: str
) -> None:
    """Revalidate the hub-approved tuple immediately before sandbox create."""

    expected_runtime = env_str("MAC_REPORT_EXECUTOR_APPROVED_RUNTIME_IMAGE_REF")
    expected_policy = env_str("MAC_REPORT_EXECUTOR_APPROVED_POLICY_SHA256")
    expected_bin_path = env_str("MAC_REPORT_EXECUTOR_APPROVED_OPENSHELL_BIN_PATH")
    expected_bin_digest = env_str(
        "MAC_REPORT_EXECUTOR_APPROVED_OPENSHELL_BIN_SHA256"
    )
    expected_platform = env_str("MAC_REPORT_EXECUTOR_APPROVED_PLATFORM")
    expected_posture = env_str("MAC_REPORT_EXECUTOR_APPROVED_ISOLATION_POSTURE")
    expected_python_path = env_str("MAC_REPORT_EXECUTOR_APPROVED_PYTHON_PATH")
    expected_python_digest = env_str("MAC_REPORT_EXECUTOR_APPROVED_PYTHON_SHA256")
    expected_script_path = env_str(
        "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_PATH"
    )
    expected_script_digest = env_str(
        "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_SHA256"
    )
    expected_source_root = env_str("MAC_REPORT_EXECUTOR_APPROVED_SOURCE_ROOT")
    expected_source_digest = env_str(
        "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_BUNDLE_SHA256"
    )
    if not all(
        (
            expected_runtime,
            expected_policy,
            expected_bin_path,
            expected_bin_digest,
            expected_platform,
            expected_posture,
            expected_python_path,
            expected_python_digest,
            expected_script_path,
            expected_script_digest,
            expected_source_root,
            expected_source_digest,
        )
    ):
        raise RuntimeError(
            "read-only repository report lacks the hub-approved runtime tuple"
        )
    if runtime_image_ref != expected_runtime:
        raise RuntimeError(
            "read-only repository report runtime image differs from hub approval"
        )
    _policy_path, policy_digest = nofollow_regular_file_identity(
        _resolve_openshell_policy()
    )
    if policy_digest != expected_policy:
        raise RuntimeError(
            "read-only repository report policy differs from hub approval"
        )
    resolved_bin = shutil.which(_openshell_bin())
    if resolved_bin is None:
        raise RuntimeError("approved OpenShell binary is unavailable")
    bin_path, bin_digest = nofollow_regular_file_identity(resolved_bin)
    if bin_path != expected_bin_path or bin_digest != expected_bin_digest:
        raise RuntimeError(
            "read-only repository report OpenShell binary differs from hub approval"
        )
    python_candidate = env_str("MAC_TASK_EXECUTOR_PYTHON") or sys.executable
    python_path, python_digest = nofollow_regular_file_identity(
        Path(python_candidate).expanduser().resolve(strict=True)
    )
    if python_path != expected_python_path or python_digest != expected_python_digest:
        raise RuntimeError(
            "read-only repository report Python differs from hub approval"
        )
    script_candidate = env_str("MAC_TASK_EXECUTOR_SCRIPT")
    if not script_candidate:
        raise RuntimeError(
            "read-only repository report executor script is not configured"
        )
    script_path, script_digest = nofollow_regular_file_identity(script_candidate)
    if script_path != expected_script_path or script_digest != expected_script_digest:
        raise RuntimeError(
            "read-only repository report executor script differs from hub approval"
        )
    source_candidate = env_str("MAC_SELF_UPDATE_REPO")
    if not source_candidate:
        raise RuntimeError(
            "read-only repository report MAC source root is not configured"
        )
    source_root, source_digest = nofollow_source_bundle_digest(source_candidate)
    if source_root != expected_source_root or source_digest != expected_source_digest:
        raise RuntimeError(
            "read-only repository report MAC source differs from hub approval"
        )
    if sys.platform.startswith("linux"):
        if (
            expected_platform != "linux"
            or expected_posture != "landlock_enforced"
            or not _kernel_has_landlock()
        ):
            raise RuntimeError(
                "read-only repository reports on Linux require approved, enforced Landlock"
            )
    elif sys.platform == "darwin":
        if (
            expected_platform != "darwin"
            or expected_posture != "macos_docker_vm_seccomp_egress"
            or not env_bool("MAC_OPENSHELL_ALLOW_NO_LANDLOCK")
        ):
            raise RuntimeError(
                "read-only repository report lacks the approved macOS Docker isolation posture"
            )
    else:
        raise RuntimeError(
            "read-only repository reports are unsupported on this platform"
        )


def _read_only_report_extra_create_argv(
    *, require_approval: bool = True, require_gpu: bool = False
) -> List[str]:
    """Return the complete allowlisted extra argv for a report sandbox.

    The ordinary coding lane permits operator conveniences. Repository reports
    do not: duplicate policy/name flags can override the controller's boundary,
    uploads can import host data, and a fixed sandbox name defeats per-task
    isolation. The only accepted extras are bounded CPU/memory/GPU requests;
    the image is always replaced with the deployment-pinned immutable digest.
    Unknown and boundary-changing arguments are errors, never silently dropped.
    """

    if env_str("MAC_OPENSHELL_SANDBOX_NAME"):
        raise RuntimeError(
            "read-only repository reports forbid MAC_OPENSHELL_SANDBOX_NAME; "
            "a fresh per-task sandbox identity is mandatory"
        )
    source = _openshell_extra_create_argv(require_gpu=require_gpu)
    runtime_image_ref = _managed_openshell_runtime_image_ref()
    if require_approval:
        _assert_approved_read_only_report_runtime(
            runtime_image_ref=runtime_image_ref
        )
    filtered: List[str] = ["--from", runtime_image_ref]
    saw_from = False
    index = 0
    while index < len(source):
        token = source[index]
        if token == "--from" or token.startswith("--from="):
            if saw_from:
                raise ValueError(
                    "read-only repository reports forbid duplicate --from arguments"
                )
            saw_from = True
            if token == "--from":
                if index + 1 >= len(source) or source[index + 1].startswith("-"):
                    raise ValueError("MAC_OPENSHELL_CREATE_ARGS --from requires a value")
                index += 2
            else:
                if not token.partition("=")[2]:
                    raise ValueError("MAC_OPENSHELL_CREATE_ARGS --from requires a value")
                index += 1
            continue
        if token == "--cpu" or token.startswith("--cpu="):
            value = (
                source[index + 1]
                if token == "--cpu" and index + 1 < len(source)
                else token.partition("=")[2]
            )
            if not value.isdigit() or not 1 <= int(value) <= 256:
                raise ValueError("read-only repository report --cpu must be 1..256")
            filtered.extend(("--cpu", value))
            index += 2 if token == "--cpu" else 1
            continue
        if token == "--memory" or token.startswith("--memory="):
            value = (
                source[index + 1]
                if token == "--memory" and index + 1 < len(source)
                else token.partition("=")[2]
            )
            match = _re.fullmatch(
                r"([1-9][0-9]{0,5})([KMGTP]i?B?|[kmgpt])?", value
            )
            if match is None or int(match.group(1)) > 65536:
                raise ValueError(
                    "read-only repository report --memory is missing or unbounded"
                )
            filtered.extend(("--memory", value))
            index += 2 if token == "--memory" else 1
            continue
        if token == "--gpu" or token.startswith("--gpu="):
            filtered.append("--gpu")
            if token.startswith("--gpu="):
                value = token.partition("=")[2]
                if not value.isdigit() or not 0 <= int(value) <= 64:
                    raise ValueError("read-only repository report --gpu must be 0..64")
                filtered.append(value)
            elif index + 1 < len(source) and source[index + 1].isdigit():
                value = source[index + 1]
                if not 0 <= int(value) <= 64:
                    raise ValueError("read-only repository report --gpu must be 0..64")
                filtered.append(value)
                index += 1
            index += 1
            continue
        raise ValueError(
            "MAC_OPENSHELL_CREATE_ARGS argument %r is forbidden for read-only "
            "repository reports" % token
        )
    return filtered


def _build_sandbox_create_argv(
    name: str,
    workspace: Path,
    basename: str,
    agent_argv: List[str],
    *,
    extra_create_argv: Optional[List[str]] = None,
) -> List[str]:
    """``openshell sandbox create`` argv that uploads the task workspace, runs the
    agent inside it, and KEEPS the sandbox so results can be downloaded.

    A policy is ALWAYS passed (explicit -> deployed -> bundled fail-closed
    default) so OpenShell can never silently apply its own image-default profile.
    The host workspace is uploaded to /sandbox (landing at /sandbox/<basename>);
    the agent runs there with $MAC_TASK_WORKSPACE/$MAC_TASK_FILE repointed at the
    in-sandbox paths (the host paths don't exist inside the sandbox), so its
    evidence manifest is written where ``download`` later fetches it. The agent
    ``agent_argv`` is the private-file wrapper, not the underlying prompt-bearing
    command. Secrets and the toolchain body are sourced from uploaded mode-0600
    files, keeping the host's process list small and credential-free.
    """
    if "mac.agent_command" not in agent_argv:
        raise ValueError("sandbox agent argv must use the private-file command wrapper")
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    argv: List[str] = [_openshell_bin(), "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_openshell_policy(), "--name", name]
    argv += _sandbox_label_argv(
        "task", keep=env_bool("MAC_OPENSHELL_KEEP")
    )
    argv += (
        _openshell_extra_create_argv()
        if extra_create_argv is None
        else list(extra_create_argv)
    )
    argv += ["--upload", "%s:%s" % (str(workspace), _SANDBOX_WORKDIR)]
    inner = "\n".join(
        [
            "cd %s" % shlex.quote(sub),
            "set -a",
            ". ./.mac-openshell-env.sh",
            "set +a",
            "rm -f ./.mac-openshell-env.sh",
            'if [ -n "${MAC_TASK_REPO_WORKTREE:-}" ] && [ -d "$MAC_TASK_REPO_WORKTREE" ] && [ ! -e /sandbox/mac-clone ]; then ln -s "$MAC_TASK_REPO_WORKTREE" /sandbox/mac-clone || true; fi',
            ". ./.mac-sandbox-toolchain.sh",
            "rm -f ./.mac-sandbox-toolchain.sh",
            "mac_sandbox_toolchain_setup || true",
            # The workspace is tar-uploaded, so its files can be owned by a
            # different uid than the sandbox user; without a safe.directory
            # whitelist every git command against uploaded paths dies with
            # "dubious ownership" (the sandbox is single-purpose and isolated,
            # so trusting all paths inside it is safe). Env form, not --global,
            # so it reaches every git subprocess regardless of HOME.
            "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'",
            # A host git worktree stores `.git` as a pointer into a host-only
            # common directory.  That pointer is invalid after OpenShell uploads
            # the workspace, and host credentials/remotes must not be copied
            # into the sandbox merely to make Git usable.  Replace it with a
            # credential-free snapshot repository so the agent can inspect its
            # own diff and run tools that expect Git.  The download merger
            # deliberately excludes this sandbox-only `.git` directory; the
            # deterministic host finalizer commits and publishes the harvested
            # file changes using the real task worktree.
            'if [ -n "${MAC_TASK_REPO_WORKTREE:-}" ] && [ -d "$MAC_TASK_REPO_WORKTREE" ] && command -v git >/dev/null 2>&1; then',
            '  if [ "${MAC_TASK_REPO_ACCESS_SCHEMA:-}" = "mac.report_repository_access.v1" ] && [ "${MAC_TASK_REPO_ACCESS_MODE:-}" = "read_only" ]; then',
            '    test -d "$MAC_TASK_REPO_WORKTREE/.git"',
            "  else",
            '    rm -rf "$MAC_TASK_REPO_WORKTREE/.git"',
            '    git -C "$MAC_TASK_REPO_WORKTREE" init -q',
            '    mkdir -p "$MAC_TASK_REPO_WORKTREE/.git/info"',
            '    printf ".codegraph/\\n" > "$MAC_TASK_REPO_WORKTREE/.git/info/exclude"',
            '    git -C "$MAC_TASK_REPO_WORKTREE" config user.email mac-sandbox@invalid',
            '    git -C "$MAC_TASK_REPO_WORKTREE" config user.name "MAC OpenShell sandbox"',
            '    git -C "$MAC_TASK_REPO_WORKTREE" add -A',
            '    git -C "$MAC_TASK_REPO_WORKTREE" commit -q --allow-empty -m "MAC OpenShell sandbox baseline"',
            "  fi",
            "fi",
            "exec %s" % shlex.join(agent_argv),
        ]
    )
    argv += ["--", "/bin/bash", "-c", inner]
    return argv


def _sandbox_step(args: List[str], *, timeout: float) -> "tuple[bool, str]":
    """Run an openshell lifecycle step (download/delete) out-of-band of the
    audited agent run. Best-effort: returns (ok, message), never raises."""
    try:
        proc = _run_captured(
            [_openshell_bin(), "sandbox", *args],
            Path.cwd(),
            timeout,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - teardown must never mask the run
        return False, str(exc)


_SANDBOX_DOWNLOAD_RUNTIME_ROOT_NAMES = {
    ".codegraph",
    ".venv",
    "venv",
    "node_modules",
}


def _relative_path_or_none(path: Path, root: Path) -> Optional[Path]:
    try:
        return path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return None


def _sandbox_repository_roots(workspace: Path, download_root: Path) -> set[Path]:
    roots: set[Path] = set()

    env_worktree = env_str("MAC_TASK_REPO_WORKTREE")
    if env_worktree:
        rel = _relative_path_or_none(Path(env_worktree), workspace)
        if rel is not None:
            roots.add(rel)

    # Only the host-authored context can define protected repository roots.
    # The downloaded copy is agent-controlled and must never repoint merge
    # exclusions away from the real task checkout.
    for context_file in (workspace / "repository-worktree.json",):
        try:
            context = json.loads(context_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(context, dict):
            continue
        worktree = str(context.get("repository_worktree") or "").strip()
        if not worktree:
            continue
        rel = _relative_path_or_none(Path(worktree), workspace)
        if rel is not None:
            roots.add(rel)

    return roots


def _path_is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _sandbox_download_path_is_git_backup(rel_path: Path) -> bool:
    return any(part.startswith(".git.bak") for part in rel_path.parts)


def _sandbox_download_path_is_host_control(rel_path: Path) -> bool:
    if len(rel_path.parts) != 1:
        return False
    name = rel_path.name
    if name in {
        "task.json",
        "repository-worktree.json",
        REPOSITORY_WIP_MANIFEST_FILENAME,
        "executor-task.json",
        "executor-evidence.json",
        ".mac-executor-policy.txt",
        ".mac-openshell-env.sh",
        ".mac-sandbox-toolchain.sh",
        ".mac-sandbox-repository-verify.sh",
        _TRUSTED_READ_ONLY_VERIFICATION_FILE,
        "worker-result.json",
        "review-result.json",
        "stdout.txt",
        "stderr.txt",
    }:
        return True
    if (
        name.startswith("repository-wip-")
        and name.endswith(".bundle")
    ):
        return True
    return name.startswith((".mac-agent-command-", ".mac-agent-prompt-"))


_SANDBOX_DOWNLOAD_REGULAR_OUTPUT_NAMES = {
    "mac-evidence.json",
    _SANDBOX_VERIFICATION_FILE,
    "review-independent-findings.json",
    "review-protocol.json",
}


def _validate_sandbox_download_symlinks(
    download_root: Path, workspace: Path, repository_roots: set[Path]
) -> None:
    """Validate every symlink before the merge mutates host workspace state."""

    download_root_resolved = download_root.resolve()
    workspace_resolved = workspace.resolve()
    for root, dirs, files in os.walk(download_root, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(download_root)
        for name in [*dirs, *files]:
            src = root_path / name
            if not src.is_symlink():
                continue
            rel = rel_root / name
            if _sandbox_download_path_is_host_control(rel) or (
                len(rel.parts) == 1
                and rel.name in _SANDBOX_DOWNLOAD_REGULAR_OUTPUT_NAMES
            ):
                raise ValueError(
                    "sandbox download attempted to replace host/evidence control %s with a symlink"
                    % rel
                )
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            target = os.readlink(src)
            if os.path.isabs(target):
                raise ValueError(
                    "sandbox download symlink has an absolute target: %s" % rel
                )
            try:
                src.resolve(strict=False).relative_to(download_root_resolved)
                (workspace / rel).parent.joinpath(target).resolve(
                    strict=False
                ).relative_to(workspace_resolved)
            except (OSError, RuntimeError, ValueError):
                raise ValueError(
                    "sandbox download symlink escapes the task workspace: %s" % rel
                ) from None


def _sandbox_download_path_excluded(rel_path: Path, repository_roots: set[Path]) -> bool:
    # Git metadata is never a legitimate file payload. Copying a sandbox .git
    # directory over a host git-worktree .git file caused the live P0 failure.
    # OpenShell transfers can also materialize a sibling .git.bak* when a
    # container checkout and host git-worktree metadata differ; treat that as
    # transfer metadata too, while preserving real repo files like .gitignore.
    if (
        ".git" in rel_path.parts
        or _sandbox_download_path_is_git_backup(rel_path)
        or _sandbox_download_path_is_host_control(rel_path)
    ):
        return True
    for root in repository_roots:
        for name in _SANDBOX_DOWNLOAD_RUNTIME_ROOT_NAMES:
            runtime_root = root / name
            if _path_is_under(rel_path, runtime_root):
                return True
    return False


def _ensure_sandbox_destination_directory(workspace: Path, rel_path: Path) -> None:
    """Create ``rel_path`` without ever following a destination symlink.

    A safe symlink harvested by one attempt may occupy a path which a later
    attempt supplies as a real directory. Path-based ``mkdir``/``copy2`` would
    follow that stale link and could redirect a nested output onto a top-level
    host control. Walk with directory descriptors and ``O_NOFOLLOW`` instead,
    replacing every non-directory component before descending.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(workspace, os.O_RDONLY | directory | nofollow)
    try:
        for part in rel_path.parts:
            if part in {"", ".", ".."}:
                if part in {"", "."}:
                    continue
                raise ValueError("sandbox destination path is not relative")
            try:
                observed = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            else:
                if not stat.S_ISDIR(observed.st_mode):
                    os.unlink(part, dir_fd=descriptor)
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
            child = os.open(
                part,
                os.O_RDONLY | directory | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def _merge_sandbox_download_tree(download_root: Path, workspace: Path) -> None:
    """Merge a downloaded sandbox workspace into the host workspace.

    OpenShell downloads a tar archive. Extracting directly over a git worktree is
    unsafe for repo tasks because task worktrees may use a host ``.git`` file
    while the sandbox checkout may contain a ``.git`` directory. Keep host git
    metadata and container-local dependency caches out of the merge; the
    deterministic finalizer rebuilds/tests from the host worktree.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    repository_roots = _sandbox_repository_roots(workspace, download_root)
    _validate_sandbox_download_symlinks(
        download_root, workspace, repository_roots
    )
    source_files: set[Path] = set()
    source_dirs: set[Path] = {Path(".")}
    source_links: set[Path] = set()

    for root, dirs, files in os.walk(download_root, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(download_root)
        if rel_root != Path("."):
            source_dirs.add(rel_root)
        kept_dirs: List[str] = []
        for name in dirs:
            rel = rel_root / name
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            src = root_path / name
            if src.is_symlink():
                source_links.add(rel)
            else:
                source_dirs.add(rel)
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            rel = rel_root / name
            if not _sandbox_download_path_excluded(rel, repository_roots):
                source_files.add(rel)

    for root, dirs, files in os.walk(workspace, topdown=False, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(workspace)
        for name in files:
            rel = rel_root / name
            if _sandbox_download_path_is_git_backup(rel):
                (root_path / name).unlink(missing_ok=True)
                continue
            if _sandbox_download_path_excluded(rel, repository_roots) or rel in source_files:
                continue
            (root_path / name).unlink(missing_ok=True)
        for name in dirs:
            rel = rel_root / name
            target = root_path / name
            if _sandbox_download_path_is_git_backup(rel):
                shutil.rmtree(target, ignore_errors=True)
                continue
            if (
                _sandbox_download_path_excluded(rel, repository_roots)
                or rel in source_dirs
                or rel in source_links
            ):
                continue
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            else:
                shutil.rmtree(target, ignore_errors=True)

    for rel in sorted(source_dirs, key=lambda item: (len(item.parts), str(item))):
        _ensure_sandbox_destination_directory(workspace, rel)

    for root, dirs, files in os.walk(download_root, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(download_root)
        kept_dirs = []
        for name in dirs:
            rel = rel_root / name
            src = root_path / name
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            if src.is_symlink():
                _ensure_sandbox_destination_directory(workspace, rel.parent)
                dst = workspace / rel
                if dst.exists() or dst.is_symlink():
                    if dst.is_dir() and not dst.is_symlink():
                        shutil.rmtree(dst)
                    else:
                        dst.unlink()
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(os.readlink(src))
            else:
                _ensure_sandbox_destination_directory(workspace, rel)
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            rel = rel_root / name
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            src = root_path / name
            _ensure_sandbox_destination_directory(workspace, rel.parent)
            dst = workspace / rel
            if dst.exists() or dst.is_symlink():
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                dst.symlink_to(os.readlink(src))
            else:
                shutil.copy2(src, dst)


def _read_only_verifier_extra_create_argv() -> List[str]:
    """Return executor-owned OpenShell arguments safe for a verifier sandbox.

    The ordinary task sandbox may receive provider attachments, credential
    uploads, and debugger conveniences for the coding agent.  The independent
    verifier runs repository-owned commands only; none of those surfaces are
    needed, and carrying them into the second sandbox would reintroduce the
    credentials that read-only reports deliberately fence.  Preserve runtime,
    resource, and gateway selection while stripping every attachment/identity
    override and every option that could prevent deterministic teardown.
    """

    # Use the same fail-closed boundary as the report agent sandbox. In
    # particular, never silently filter an unknown option: that would let the
    # agent and verifier run under different isolation contracts.
    return _read_only_report_extra_create_argv()


_READ_ONLY_GIT_CONTROL_PATHS = (
    "HEAD",
    "config",
    "config.worktree",
    "commondir",
    "gitdir",
    "packed-refs",
    "shallow",
    "refs",
    "info",
    "objects/info",
    "worktrees",
    "modules",
)


def _read_only_git_control_digest(worktree: Path) -> str:
    """Hash security-sensitive Git controls without following any symlink.

    The index and object store are intentionally excluded: ordinary read-only
    Git inspection may refresh index stat data, while object identity is proved
    separately by HEAD/tree checks.  Configuration, ref routing, alternates,
    excludes, linked-worktree controls, and submodule controls are immutable for
    a read-only report and are hashed as raw directory entries and bytes.
    """

    candidate = Path(worktree).expanduser()
    if candidate.is_symlink():
        raise ValueError("read-only repository worktree is a symlink")
    root = candidate.resolve(strict=True)
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    digest = hashlib.sha256()

    def _record(parent_fd: int, name: str, relative: str) -> None:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            digest.update(b"M\0" + relative.encode("utf-8", "surrogateescape") + b"\0")
            return
        relative_bytes = relative.encode("utf-8", "surrogateescape")
        if stat.S_ISLNK(info.st_mode):
            payload = os.readlink(name, dir_fd=parent_fd).encode(
                "utf-8", "surrogateescape"
            )
            digest.update(b"L\0" + relative_bytes + b"\0")
            digest.update(hashlib.sha256(payload).digest())
            return
        if stat.S_ISREG(info.st_mode):
            descriptor = os.open(
                name,
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=parent_fd,
            )
            try:
                observed = os.fstat(descriptor)
                if not stat.S_ISREG(observed.st_mode):
                    raise OSError("Git control changed type while being read")
                payload = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    payload.update(chunk)
            finally:
                os.close(descriptor)
            digest.update(b"F\0" + relative_bytes + b"\0")
            digest.update(payload.digest())
            return
        if stat.S_ISDIR(info.st_mode):
            descriptor = os.open(
                name,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=parent_fd,
            )
            try:
                digest.update(b"D\0" + relative_bytes + b"\0")
                for child in sorted(os.listdir(descriptor)):
                    _record(descriptor, child, "%s/%s" % (relative, child))
            finally:
                os.close(descriptor)
            return
        digest.update(b"O\0" + relative_bytes + b"\0")

    root_fd = os.open(root, os.O_RDONLY | directory | nofollow | cloexec)
    try:
        git_info = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(git_info.st_mode):
            raise ValueError("read-only repository .git control is not a directory")
        git_fd = os.open(
            ".git",
            os.O_RDONLY | directory | nofollow | cloexec,
            dir_fd=root_fd,
        )
        try:
            digest.update(b"D\0.git\0")
            for relative in _READ_ONLY_GIT_CONTROL_PATHS:
                parts = relative.split("/")
                parent_fd = os.dup(git_fd)
                try:
                    traversed: List[str] = []
                    for part in parts[:-1]:
                        traversed.append(part)
                        try:
                            child_fd = os.open(
                                part,
                                os.O_RDONLY | directory | nofollow | cloexec,
                                dir_fd=parent_fd,
                            )
                        except OSError:
                            _record(parent_fd, part, "/".join(traversed))
                            digest.update(
                                b"M\0"
                                + relative.encode("utf-8", "surrogateescape")
                                + b"\0"
                            )
                            break
                        os.close(parent_fd)
                        parent_fd = child_fd
                    else:
                        _record(parent_fd, parts[-1], relative)
                finally:
                    os.close(parent_fd)
        finally:
            os.close(git_fd)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def _read_only_git_control_digest_program() -> str:
    """Standalone equivalent used before Git in the OpenShell postcheck."""

    paths = repr(_READ_ONLY_GIT_CONTROL_PATHS)
    return r'''import hashlib, os, stat, sys
paths = %s
root = os.path.realpath(sys.argv[1])
directory = getattr(os, "O_DIRECTORY", 0)
nofollow = getattr(os, "O_NOFOLLOW", 0)
cloexec = getattr(os, "O_CLOEXEC", 0)
digest = hashlib.sha256()

def record(parent_fd, name, relative):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        digest.update(b"M\0" + relative.encode("utf-8", "surrogateescape") + b"\0")
        return
    relative_bytes = relative.encode("utf-8", "surrogateescape")
    if stat.S_ISLNK(info.st_mode):
        payload = os.readlink(name, dir_fd=parent_fd).encode("utf-8", "surrogateescape")
        digest.update(b"L\0" + relative_bytes + b"\0")
        digest.update(hashlib.sha256(payload).digest())
    elif stat.S_ISREG(info.st_mode):
        descriptor = os.open(name, os.O_RDONLY | nofollow | cloexec, dir_fd=parent_fd)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode):
                raise OSError("Git control changed type while being read")
            payload = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                payload.update(chunk)
        finally:
            os.close(descriptor)
        digest.update(b"F\0" + relative_bytes + b"\0")
        digest.update(payload.digest())
    elif stat.S_ISDIR(info.st_mode):
        descriptor = os.open(name, os.O_RDONLY | directory | nofollow | cloexec, dir_fd=parent_fd)
        try:
            digest.update(b"D\0" + relative_bytes + b"\0")
            for child in sorted(os.listdir(descriptor)):
                record(descriptor, child, relative + "/" + child)
        finally:
            os.close(descriptor)
    else:
        digest.update(b"O\0" + relative_bytes + b"\0")

root_fd = os.open(root, os.O_RDONLY | directory | nofollow | cloexec)
try:
    git_info = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(git_info.st_mode):
        raise ValueError("read-only repository .git control is not a directory")
    git_fd = os.open(".git", os.O_RDONLY | directory | nofollow | cloexec, dir_fd=root_fd)
    try:
        digest.update(b"D\0.git\0")
        for relative in paths:
            parts = relative.split("/")
            parent_fd = os.dup(git_fd)
            try:
                traversed = []
                for part in parts[:-1]:
                    traversed.append(part)
                    try:
                        child_fd = os.open(part, os.O_RDONLY | directory | nofollow | cloexec, dir_fd=parent_fd)
                    except OSError:
                        record(parent_fd, part, "/".join(traversed))
                        digest.update(b"M\0" + relative.encode("utf-8", "surrogateescape") + b"\0")
                        break
                    os.close(parent_fd)
                    parent_fd = child_fd
                else:
                    record(parent_fd, parts[-1], relative)
            finally:
                os.close(parent_fd)
    finally:
        os.close(git_fd)
finally:
    os.close(root_fd)
print(digest.hexdigest())
''' % paths


def _git_for_read_only_verifier(
    worktree: Path, args: List[str]
) -> subprocess.CompletedProcess[str]:
    """Run an absolute, environment- and worktree-fenced Git postcheck."""

    resolved = Path(worktree).expanduser().resolve(strict=True)
    git_control = resolved / ".git"
    try:
        git_info = git_control.lstat()
    except OSError as exc:
        return subprocess.CompletedProcess(args, 128, "", str(exc))
    if not stat.S_ISDIR(git_info.st_mode):
        return subprocess.CompletedProcess(
            args, 128, "", "read-only repository .git control is not a directory"
        )
    git = shutil.which("git")
    if not git:
        return subprocess.CompletedProcess(args, 127, "", "git executable not found")
    git = str(Path(git).resolve(strict=True))
    # Start from an empty environment.  In particular this excludes less common
    # Git routing variables (GIT_DIR, GIT_WORK_TREE, GIT_OBJECT_DIRECTORY,
    # GIT_INDEX_FILE) instead of trying to maintain a fragile denylist.
    environment: Dict[str, str] = {}
    fence_read_only_repository_environment(environment)
    environment.update(
        {
            "HOME": "/tmp/mac-read-only-postcheck",
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return subprocess.run(
        [
            git,
            "--no-optional-locks",
            "--git-dir=%s" % git_control,
            "--work-tree=%s" % resolved,
            "-c",
            "safe.directory=%s" % resolved,
            "-c",
            "core.worktree=%s" % resolved,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.file.allow=never",
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def _prepare_read_only_verifier_workspace(
    workspace: Path,
    verifier_workspace: Path,
    task: Mapping[str, Any],
) -> tuple[Path, Path]:
    """Copy and prove one pristine exact-base checkout for independent tests."""

    metadata = task.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    runtime = metadata.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    worktree_raw = str(runtime.get("repository_worktree") or "").strip()
    expected_head = str(runtime.get("repository_base_sha") or "").strip()
    expected_tree = str(runtime.get("repository_base_tree") or "").strip()
    expected_refs = str(runtime.get("repository_refs_digest") or "").strip()
    expected_content = str(runtime.get("repository_content_digest") or "").strip()
    if not all(
        (worktree_raw, expected_head, expected_tree, expected_refs, expected_content)
    ):
        raise ValueError("read-only verifier exact-base context is incomplete")

    workspace_resolved = workspace.resolve()
    source = Path(worktree_raw).resolve()
    try:
        relative = source.relative_to(workspace_resolved)
    except ValueError as exc:
        raise ValueError("read-only verifier checkout is outside its task workspace") from exc
    if not source.is_dir() or not (source / ".git").is_dir():
        raise ValueError("read-only verifier source checkout is unavailable")

    verifier_workspace.mkdir(parents=True, mode=0o700)
    target = verifier_workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        symlinks=True,
        ignore=shutil.ignore_patterns(".codegraph"),
    )
    for name in ("task.json", "repository-worktree.json"):
        control = workspace / name
        if control.is_file() and not control.is_symlink():
            shutil.copy2(control, verifier_workspace / name)

    clean = _git_for_read_only_verifier(target, ["clean", "-fdx"])
    status = _git_for_read_only_verifier(target, ["status", "--porcelain"])
    head = _git_for_read_only_verifier(target, ["rev-parse", "HEAD"])
    tree = _git_for_read_only_verifier(target, ["rev-parse", "HEAD^{tree}"])
    refs = _git_for_read_only_verifier(
        target, ["for-each-ref", "--format=%(refname) %(objectname)"]
    )
    remotes = _git_for_read_only_verifier(target, ["remote"])
    observed_refs = (
        hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest()
        if refs.returncode == 0
        else ""
    )
    try:
        observed_content = read_only_repository_content_digest(target)
    except OSError:
        observed_content = ""
    if not (
        clean.returncode == 0
        and status.returncode == 0
        and not status.stdout.strip()
        and head.returncode == 0
        and head.stdout.strip() == expected_head
        and tree.returncode == 0
        and tree.stdout.strip() == expected_tree
        and refs.returncode == 0
        and observed_refs == expected_refs
        and remotes.returncode == 0
        and not remotes.stdout.strip()
        and observed_content == expected_content
    ):
        raise ValueError("read-only verifier copy failed its exact-base proof")
    return relative, target


def _store_trusted_read_only_verification(
    source: Path,
    workspace: Path,
    task: Mapping[str, Any],
) -> bool:
    """Validate and stage the independent verifier's bounded JSON result."""

    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > _MAX_SANDBOX_VERIFICATION_BYTES
        ):
            return False
        chunks: List[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                return False
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    expected_command = _repository_contract_test_command(dict(task))
    integrity = payload.get("integrity") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "mac.sandbox_verification.v1"
        or payload.get("command") != expected_command
        or payload.get("status") not in {"pass", "fail"}
        or isinstance(payload.get("returncode"), bool)
        or not isinstance(payload.get("returncode"), int)
        or not isinstance(integrity, dict)
        or integrity.get("schema") != _READ_ONLY_VERIFICATION_INTEGRITY_SCHEMA
        or integrity.get("fresh_control_process") is not True
        or integrity.get("raw_git_control_first") is not True
        or integrity.get("cgroup_quiescent") is not True
        or not isinstance(integrity.get("problems"), list)
    ):
        return False
    passed = payload["status"] == "pass" and payload["returncode"] == 0
    if passed and (
        integrity.get("immutable_inputs") is not True
        or integrity.get("exact_base_revalidated") is not True
        or integrity.get("problems")
    ):
        return False
    trusted = workspace / _TRUSTED_READ_ONLY_VERIFICATION_FILE
    temp: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=workspace,
            prefix=trusted.name + ".host-",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, trusted)
    except OSError:
        if temp is not None:
            temp.unlink(missing_ok=True)
        return False
    return passed


def _sandbox_run_read_only_repository_verification(
    name: str, workspace: Path, task: Mapping[str, Any]
) -> bool:
    """Run contract tests in a second, fresh exact-base OpenShell sandbox.

    The coding-agent sandbox is adversary-controlled after the agent returns:
    ignored build products, task-local executables, HOME state, and background
    processes can all survive a second ``exec``.  Reusing it cannot produce a
    host-authoritative test result.  This path copies the still-pristine host
    checkout, proves its exact identity, uploads only secret-free controls to a
    separately named sandbox, and downloads exactly one bounded result file.
    """

    import uuid

    verifier_name = "%s-verify-%s" % (name[:38], uuid.uuid4().hex[:8])
    trusted = workspace / _TRUSTED_READ_ONLY_VERIFICATION_FILE
    trusted.unlink(missing_ok=True)
    try:
        timeout = float(env_str("MAC_WORKER_REPOSITORY_TEST_TIMEOUT") or "1800")
    except ValueError:
        timeout = 1800.0
    timeout = max(1.0, timeout)
    created = False
    downloaded = False
    stored_pass = False
    deletion_succeeded = [False]

    def _delete_verifier() -> None:
        deletion_succeeded[0] = _sandbox_delete(verifier_name)

    with contextlib.ExitStack() as cleanup, tempfile.TemporaryDirectory(
        prefix=".%s-read-only-verifier-" % workspace.name,
        dir=str(workspace.parent) if workspace.parent.is_dir() else None,
    ) as temp:
        # Register before any preparation or OpenShell call so even early
        # returns and unexpected exceptions cannot strand the verifier.
        cleanup.callback(_delete_verifier)
        root = Path(temp)
        verifier_workspace = root / "workspace"
        try:
            relative, target = _prepare_read_only_verifier_workspace(
                workspace, verifier_workspace, task
            )
            expected_git_control = _authoritative_read_only_git_control_digest(
                target
            )
        except (OSError, ValueError) as exc:
            sys.stderr.write(
                "[executor] WARNING: could not prepare independent read-only "
                "verifier: %s\n" % exc
            )
            return False

        basename = _workspace_basename(verifier_workspace)
        sandbox_workspace = "%s/%s" % (_SANDBOX_WORKDIR, basename)
        sandbox_worktree = "%s/%s" % (
            sandbox_workspace.rstrip("/"),
            str(relative).replace(os.sep, "/"),
        )
        script_path = verifier_workspace / ".mac-sandbox-repository-verify.sh"
        verification_environment = {
            "HOME": "/tmp/mac-read-only-verifier-home",
            "PATH": _SANDBOX_BASE_PATH,
            "MAC_SANDBOX_BASE_PATH": _SANDBOX_BASE_PATH,
            "MAC_TASK_FILE": "%s/task.json" % sandbox_workspace,
            "MAC_TASK_WORKSPACE": sandbox_workspace,
            "MAC_TASK_REPO_WORKTREE": sandbox_worktree,
            "MAC_TASK_REPO_ACCESS_SCHEMA": REPORT_REPOSITORY_ACCESS_SCHEMA,
            "MAC_TASK_REPO_ACCESS_MODE": REPORT_REPOSITORY_READ_ONLY_MODE,
            "MAC_TASK_REPO_GIT_CONTROL_DIGEST": expected_git_control,
        }
        metadata = task.get("metadata")
        runtime = metadata.get("runtime") if isinstance(metadata, Mapping) else None
        runtime = runtime if isinstance(runtime, Mapping) else {}
        for environment_name, runtime_name in (
            ("MAC_TASK_REPO_BASE_SHA", "repository_base_sha"),
            ("MAC_TASK_REPO_BASE_TREE", "repository_base_tree"),
            ("MAC_TASK_REPO_REFS_DIGEST", "repository_refs_digest"),
            ("MAC_TASK_REPO_CONTENT_DIGEST", "repository_content_digest"),
        ):
            value = str(runtime.get(runtime_name) or "").strip()
            if value:
                verification_environment[environment_name] = value
        script_path.write_text(
            _sandbox_read_only_repository_verification_shell(
                verification_environment
            )
            + "\n",
            encoding="utf-8",
        )
        script_path.chmod(0o700)
        sandbox_script = "%s/%s" % (sandbox_workspace, script_path.name)
        create_args: List[str] = [
            "create",
            "--no-auto-providers",
            "--policy",
            _resolve_openshell_policy(),
            "--name",
            verifier_name,
            *_sandbox_label_argv("read-only-verifier"),
            *_read_only_verifier_extra_create_argv(),
            "--no-git-ignore",
            "--no-tty",
            "--upload",
            "%s:%s" % (verifier_workspace, _SANDBOX_WORKDIR),
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            sandbox_script,
        ]
        created, create_message = _sandbox_step(
            create_args, timeout=timeout + 90.0
        )
        if not created and create_message:
            sys.stderr.write(
                "[executor] independent read-only verifier returned non-zero: %s\n"
                % create_message[:500]
            )
        destination = root / _SANDBOX_VERIFICATION_FILE
        downloaded, download_message = _sandbox_step(
            [
                "download",
                verifier_name,
                "%s/%s" % (sandbox_workspace, _SANDBOX_VERIFICATION_FILE),
                str(destination),
            ],
            timeout=120.0,
        )
        if downloaded:
            stored_pass = _store_trusted_read_only_verification(
                destination, workspace, task
            )
        elif download_message:
            sys.stderr.write(
                "[executor] WARNING: independent read-only verifier result "
                "download failed: %s\n" % download_message[:500]
            )
    return bool(created and downloaded and stored_pass and deletion_succeeded[0])


def _promote_trusted_read_only_verification(workspace: Path) -> bool:
    """Replace any agent-authored verification file with the trusted result."""

    trusted = workspace / _TRUSTED_READ_ONLY_VERIFICATION_FILE
    destination = workspace / _SANDBOX_VERIFICATION_FILE
    try:
        info = trusted.lstat()
        if not stat.S_ISREG(info.st_mode):
            trusted.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            return False
        os.replace(trusted, destination)
        destination.chmod(0o600)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            trusted.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        return False


def _sandbox_run_repository_verification(
    name: str, basename: str, workspace: Path, task: Any
) -> Optional[bool]:
    if not isinstance(task, dict):
        return None
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not task_is_repo_coupled(task) and not metadata_declares_read_only_report_repository(
        metadata
    ):
        return None
    if metadata_declares_read_only_report_repository(metadata):
        # A read-only report is trusted only after the CURRENT repository
        # contract has supplied and passed its test command.  Returning None
        # here used to mean "verification not applicable", allowing a stale or
        # absent contract to preserve agent-authored success evidence.
        if not _repository_contract_test_command(task):
            return False
        return _sandbox_run_read_only_repository_verification(name, workspace, task)
    if not _repository_contract_test_command(task):
        return None
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    script_path = workspace / ".mac-sandbox-repository-verify.sh"
    verification_environment = {
        **_sandbox_repository_environment(workspace, sub),
        "HOME": _SANDBOX_HOME,
        "MAC_TASK_FILE": "%s/task.json" % sub,
        "MAC_TASK_WORKSPACE": sub,
    }
    script_path.write_text(
        _sandbox_repository_verification_shell(verification_environment) + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o700)
    sandbox_script = "%s/%s" % (sub, script_path.name)
    ok, msg = _sandbox_step(
        ["upload", name, str(script_path), sandbox_script],
        timeout=120.0,
    )
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox repository verification upload failed: %s\n" % msg)
        return False
    try:
        timeout = float(env_str("MAC_WORKER_REPOSITORY_TEST_TIMEOUT") or "1800")
    except ValueError:
        timeout = 1800.0
    ok, msg = _sandbox_step(
        [
            "exec",
            "--name",
            name,
            "--workdir",
            sub,
            "--timeout",
            str(max(1, int(timeout))),
            "--no-tty",
            "--",
            "/bin/bash",
            sandbox_script,
        ],
        timeout=timeout + 90.0,
    )
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox repository verification failed: %s\n" % msg)
    return ok


def _sandbox_read_only_repository_violation(
    name: str,
    basename: str,
    workspace: Path,
    task: Any,
    expected_git_control_digest: str = "",
) -> str:
    """Verify sandbox-only Git state before throwaway metadata is discarded."""

    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not metadata_declares_read_only_report_repository(metadata):
        return ""
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else None
    runtime = runtime if isinstance(runtime, dict) else {}
    expected_head = str(runtime.get("repository_base_sha") or "").strip()
    expected_tree = str(runtime.get("repository_base_tree") or "").strip()
    host_worktree = str(runtime.get("repository_worktree") or "").strip()
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    repo = _sandbox_path_for_workspace_child(workspace, sub, host_worktree) or ""
    if not all((repo, expected_head, expected_tree, expected_git_control_digest)):
        return "read-only repository report sandbox proof is incomplete"
    digest_program = _read_only_git_control_digest_program()
    script = "\n".join(
        [
            "set -eu",
            "repo=%s" % shlex.quote(repo),
            "expected_head=%s" % shlex.quote(expected_head),
            "expected_tree=%s" % shlex.quote(expected_tree),
            "expected_git_control=%s"
            % shlex.quote(expected_git_control_digest),
            'fail() { printf "%s\\n" "$1" >&2; exit 66; }',
            'test ! -L "$repo" || fail "read-only repository worktree became a symlink"',
            'test -d "$repo/.git" || fail "read-only repository Git metadata is missing"',
            # This raw, O_NOFOLLOW digest is deliberately the FIRST
            # post-agent repository interpretation.  In particular, do not run
            # `git -C`: a poisoned core.worktree would make Git inspect an
            # attacker-selected clean tree instead of the uploaded checkout.
            'python_bin=/opt/mac-venv/bin/python',
            '[ -x "$python_bin" ] || python_bin="$(PATH=%s command -v python3 || PATH=%s command -v python || true)"'
            % (shlex.quote(_SANDBOX_BASE_PATH), shlex.quote(_SANDBOX_BASE_PATH)),
            '[ -n "$python_bin" ] || fail "trusted Python is unavailable for Git control validation"',
            'observed_git_control="$("$python_bin" - "$repo" <<\'PY\'',
            digest_program,
            "PY",
            ')" || fail "could not digest read-only repository Git controls"',
            'test "$observed_git_control" = "$expected_git_control" || fail "read-only repository Git control metadata changed"',
            'git_bin="$(PATH=%s command -v git || true)"'
            % shlex.quote(_SANDBOX_BASE_PATH),
            'case "$git_bin" in /*) ;; *) fail "trusted absolute Git executable is unavailable" ;; esac',
            'trusted_git() { env -i HOME=/tmp/mac-read-only-postcheck PATH=%s GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0 "$git_bin" --no-optional-locks --git-dir="$repo/.git" --work-tree="$repo" -c "safe.directory=$repo" -c "core.worktree=$repo" -c core.fsmonitor=false -c core.hooksPath=/dev/null -c credential.helper= -c protocol.file.allow=never "$@"; }'
            % shlex.quote(_SANDBOX_BASE_PATH),
            'status="$(trusted_git status --porcelain)" || fail "could not inspect read-only repository status"',
            'test -z "$status" || fail "read-only repository files were mutated"',
            'head="$(trusted_git rev-parse HEAD)" || fail "could not inspect read-only repository HEAD"',
            'test "$head" = "$expected_head" || fail "read-only repository HEAD changed"',
            'tree="$(trusted_git rev-parse HEAD^{tree})" || fail "could not inspect read-only repository tree"',
            'test "$tree" = "$expected_tree" || fail "read-only repository tree changed"',
            'refs="$(trusted_git for-each-ref --format="%(refname) %(objectname)")" || fail "could not inspect read-only repository refs"',
            'test -z "$refs" || fail "read-only repository refs changed"',
            'remotes="$(trusted_git remote)" || fail "could not inspect read-only repository remotes"',
            'test -z "$remotes" || fail "read-only repository retained a publication remote"',
        ]
    )
    ok, message = _sandbox_step(
        [
            "exec",
            "--name",
            name,
            "--workdir",
            sub,
            "--timeout",
            "120",
            "--no-tty",
            "--",
            "/bin/bash",
            "-c",
            script,
        ],
        timeout=150.0,
    )
    return "" if ok else (message or "read-only repository sandbox validation failed")


def _sandbox_download(name: str, basename: str, workspace: Path) -> bool:
    """Sync the agent's edits (+ the evidence manifest) from the kept sandbox
    back into the host workspace. Best-effort: a failure is logged, not fatal —
    completeness is still judged by the evidence manifest on the host."""
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    temp_parent = str(workspace.parent) if workspace.parent.is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix=".%s-openshell-download-" % workspace.name,
        dir=temp_parent,
    ) as tmp:
        download_root = Path(tmp)
        ok, msg = _sandbox_step(["download", name, sub, str(download_root)], timeout=300.0)
        if ok:
            try:
                _merge_sandbox_download_tree(download_root, workspace)
            except Exception as exc:  # noqa: BLE001 - download sync is best-effort
                ok = False
                msg = "sandbox download merge failed: %s" % exc
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox download failed: %s\n" % msg)
    return ok


def _sandbox_delete(name: str) -> bool:
    ok, msg = _sandbox_step(["delete", name], timeout=120.0)
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox delete failed (possible leak): %s\n" % msg)
    return ok


def _sandbox_progress_interval() -> float:
    raw = (env_str("MAC_OPENSHELL_PROGRESS_INTERVAL") or "5")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _sandbox_progress_snapshot(
    name: str, basename: str, workspace: Path
) -> Optional[Dict[str, str]]:
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    mapped_repo = _sandbox_path_for_workspace_child(
        workspace, sub, env_str("MAC_TASK_REPO_WORKTREE")
    )
    repo = mapped_repo or ""
    base = env_str("MAC_TASK_REPO_BASE_SHA")
    script = "\n".join(
        [
            "set -eu",
            "repo=%s" % shlex.quote(repo),
            "base=%s" % shlex.quote(base),
            "head=",
            "changed_count=0",
            "changed_digest=",
            'if [ -n "$repo" ] && [ -d "$repo" ]; then',
            '  head="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"',
            '  changed="$( { git -C "$repo" status --porcelain 2>/dev/null; [ -n "$base" ] && git -C "$repo" diff --name-only "$base..HEAD" 2>/dev/null || true; } | sort -u )"',
            '  changed_count="$(printf %s "$changed" | sed "/^$/d" | wc -l | tr -d " ")"',
            '  changed_digest="$(printf %s "$changed" | sha256sum 2>/dev/null | cut -d" " -f1 || true)"',
            "fi",
            'printf "ready=1\\nhead=%%s\\nchanged_count=%%s\\nchanged_digest=%%s\\nmanifest=%%s\\n" "$head" "$changed_count" "$changed_digest" "$( [ -f %s/mac-evidence.json ] && echo 1 || echo 0 )"'
            % shlex.quote(sub),
        ]
    )
    ok, output = _sandbox_step(
        [
            "exec",
            "--name",
            name,
            "--workdir",
            sub,
            "--timeout",
            "15",
            "--no-tty",
            "--",
            "/bin/bash",
            "-c",
            script,
        ],
        timeout=30.0,
    )
    if not ok:
        return None
    snapshot: Dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "ready",
            "head",
            "changed_count",
            "changed_digest",
            "manifest",
        }:
            snapshot[key] = value.strip()
    return snapshot if snapshot.get("ready") == "1" else None


class _SandboxProgressMonitor:
    """Transition-based observer of the real sandbox workspace."""

    def __init__(self, name: str, basename: str, workspace: Path, task_id: Any) -> None:
        self.name = name
        self.basename = basename
        self.workspace = workspace
        self.task_id = str(task_id) if task_id else None
        self.interval = _sandbox_progress_interval()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.ready = False
        self.mutated = False
        self.manifest_seen = False
        self.last_head = ""
        self.changed_file_count = 0
        self.changed_file_digest = ""
        self.stopped = False

    def start(self) -> None:
        if self.interval <= 0:
            return
        self.thread = threading.Thread(
            target=self._loop,
            name="mac-sandbox-progress-%s" % self.name,
            daemon=True,
        )
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.observe()

    def observe(self) -> None:
        snapshot = _sandbox_progress_snapshot(
            self.name, self.basename, self.workspace
        )
        if snapshot is None:
            return
        if not self.ready:
            self.ready = True
            emit_telemetry(
                "sandbox_ready",
                task_id=self.task_id,
                sandbox=self.name,
                state="ready",
            )
        head = snapshot.get("head", "")
        try:
            changed_count = int(snapshot.get("changed_count") or 0)
        except ValueError:
            changed_count = 0
        changed = changed_count > 0 or (
            bool(head)
            and bool(env_str("MAC_TASK_REPO_BASE_SHA"))
            and head != env_str("MAC_TASK_REPO_BASE_SHA")
        )
        self.changed_file_count = changed_count
        self.changed_file_digest = snapshot.get("changed_digest", "")
        if changed and not self.mutated:
            self.mutated = True
            emit_telemetry(
                "sandbox_first_mutation",
                task_id=self.task_id,
                sandbox=self.name,
                state="sandbox_dirty",
                changed_file_count=changed_count,
                changed_file_digest=snapshot.get("changed_digest", ""),
                head_sha=head,
            )
        if head and head != self.last_head:
            self.last_head = head
            emit_telemetry(
                "sandbox_head_observed",
                task_id=self.task_id,
                sandbox=self.name,
                head_sha=head,
            )
        if snapshot.get("manifest") == "1" and not self.manifest_seen:
            self.manifest_seen = True
            emit_telemetry(
                "sandbox_manifest_observed",
                task_id=self.task_id,
                sandbox=self.name,
            )

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.interval <= 0:
            return
        self.observe()
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=min(2.0, self.interval + 0.5))
        if not self.ready:
            emit_telemetry(
                "sandbox_observation_unavailable",
                task_id=self.task_id,
                level="warning",
                sandbox=self.name,
                state="unknown",
            )
        elif not self.mutated:
            emit_telemetry(
                "sandbox_no_effect",
                task_id=self.task_id,
                level="warning",
                sandbox=self.name,
                state="sandbox_clean",
            )

    def evidence(self) -> Dict[str, object]:
        return {
            "ready_observed": self.ready,
            "mutation_observed": self.mutated,
            "manifest_observed": self.manifest_seen,
            "head_sha": self.last_head,
            "changed_file_count": self.changed_file_count,
            "changed_file_digest": self.changed_file_digest,
        }


def _run_sandboxed(
    runner: Callable[..., Any], agent_argv: List[str], workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent through the OpenShell sandbox lifecycle: create (upload the
    workspace + run the agent, keep) -> download results -> delete. The agent
    runs confined. Harvest is attempted before teardown on every exit path,
    including runner exceptions and cancellation. Repository failures are
    deleted only after WIP is durably bundled; preservation failure retains the
    GC-protected sandbox instead of destroying the only remaining copy."""
    _force_child_yolo_env()  # truly silent agent; OpenShell is the guardrail
    _ensure_landlock_or_fail()
    _sandbox_gc_best_effort()
    _reap_orphaned_task_sandboxes_best_effort(audit_id)
    _reconcile_task_sandboxes_from_lease_authority_best_effort(audit_id)
    task = opts.get("task")
    task_metadata = task.get("metadata") if isinstance(task, dict) else None
    read_only_report = metadata_declares_read_only_report_repository(task_metadata)
    require_gpu = _task_requires_gpu(task)
    expected_git_control_digest = ""
    task_extra_create_argv = _openshell_extra_create_argv(require_gpu=require_gpu)
    report_extra_create_argv: Optional[List[str]] = None
    if read_only_report:
        if env_bool("MAC_OPENSHELL_KEEP"):
            raise RuntimeError(
                "read-only repository reports forbid MAC_OPENSHELL_KEEP; "
                "successful sandbox deletion is mandatory"
            )
        runtime = task_metadata.get("runtime") if isinstance(task_metadata, dict) else None
        worktree_raw = (
            str(runtime.get("repository_worktree") or "").strip()
            if isinstance(runtime, dict)
            else ""
        )
        if not worktree_raw:
            raise RuntimeError(
                "read-only repository report has no task-owned worktree for Git control proof"
            )
        try:
            worktree = Path(worktree_raw).expanduser().resolve(strict=True)
            worktree.relative_to(workspace.expanduser().resolve(strict=True))
            expected_git_control_digest = _read_only_git_control_digest(worktree)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "could not capture pre-agent read-only Git controls: %s" % exc
            ) from exc
        report_extra_create_argv = (
            _read_only_report_extra_create_argv(require_gpu=True)
            if require_gpu
            else _read_only_report_extra_create_argv()
        )
    name = _sandbox_name()
    basename = _workspace_basename(workspace)
    sandbox_workspace = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    runtime_files = _write_sandbox_runtime_files(workspace, sandbox_workspace)
    try:
        create_argv = _build_sandbox_create_argv(
            name,
            workspace,
            basename,
            agent_argv,
            extra_create_argv=(
                report_extra_create_argv
                if report_extra_create_argv is not None
                else task_extra_create_argv
            ),
        )
    except Exception:
        for path in runtime_files:
            path.unlink(missing_ok=True)
        raise
    runner_completed = False
    result: Any = None
    harvested = False
    kept = env_bool("MAC_OPENSHELL_KEEP")
    emit_telemetry(
        "sandbox_started",
        task_id=str(audit_id) if audit_id else None,
        sandbox=name,
        workspace=basename,
    )
    progress = _SandboxProgressMonitor(name, basename, workspace, audit_id)
    if read_only_report:
        # The progress observer uses Git for ordinary coding tasks.  A read-only
        # report must perform its raw control comparison before ANY post-agent
        # Git process, so disable that advisory observer for this task class.
        progress.interval = 0.0
    progress.start()
    try:
        result = runner(create_argv, workspace, audit_id, opts)
        runner_completed = True
        if read_only_report:
            setattr(
                result,
                "mac_read_only_git_control_digest",
                expected_git_control_digest,
            )
        progress.stop()
        emit_telemetry(
            "sandbox_agent_completed",
            task_id=str(audit_id) if audit_id else None,
            level="info" if int(getattr(result, "returncode", 1)) == 0 else "warning",
            sandbox=name,
            returncode=int(getattr(result, "returncode", 1)),
        )
        # A failed agent that demonstrably left the repository untouched cannot
        # benefit from bootstrap/tests/CodeGraph/publication finalization. The
        # old path spent minutes in those phases, renewed the lease, and made a
        # clean authentication failure look like a hung task. Preserve harvest
        # and teardown in ``finally``, but return the original failure promptly.
        progress_evidence = progress.evidence()
        read_only_violation = _sandbox_read_only_repository_violation(
            name,
            basename,
            workspace,
            task,
            expected_git_control_digest,
        )
        if read_only_violation:
            result = subprocess.CompletedProcess(
                getattr(result, "args", ["read_only_repository_report"]),
                66,
                getattr(result, "stdout", "") or "",
                "\n".join(
                    part
                    for part in (
                        (getattr(result, "stderr", "") or "").strip(),
                        read_only_violation,
                    )
                    if part
                ),
            )
            setattr(
                result,
                "mac_read_only_repository_violation",
                read_only_violation,
            )
            setattr(
                result,
                "mac_read_only_git_control_digest",
                expected_git_control_digest,
            )
            return result
        if (
            int(getattr(result, "returncode", 1)) != 0
            and progress_evidence.get("ready_observed") is True
            and progress_evidence.get("mutation_observed") is False
            and progress_evidence.get("manifest_observed") is False
        ):
            # Carry the proof across _invoke_agent's return boundary.  Without
            # this marker _run_executor would still enter deterministic git or
            # review finalization after the sandbox verifier correctly stopped.
            setattr(result, "mac_clean_agent_failure", True)
            emit_telemetry(
                "sandbox_verification_skipped",
                task_id=str(audit_id) if audit_id else None,
                level="warning",
                sandbox=name,
                reason="clean_agent_failure",
                returncode=int(getattr(result, "returncode", 1)),
            )
            return result
        verification_expected = read_only_report or (
            isinstance(task, dict)
            and task_is_repo_coupled(task)
            and bool(_repository_contract_test_command(task))
        )
        if verification_expected:
            emit_telemetry(
                "sandbox_verification_started",
                task_id=str(audit_id) if audit_id else None,
                sandbox=name,
            )
        verification = _sandbox_run_repository_verification(
            name, basename, workspace, task
        )
        if verification is not None:
            emit_telemetry(
                "sandbox_verification_completed",
                task_id=str(audit_id) if audit_id else None,
                level="info" if verification else "warning",
                sandbox=name,
                passed=verification,
            )
            metadata = (
                task.get("metadata")
                if isinstance(task, dict)
                else None
            )
            if (
                verification is False
                and metadata_declares_read_only_report_repository(metadata)
            ):
                result = subprocess.CompletedProcess(
                    getattr(result, "args", ["read_only_repository_report"]),
                    67,
                    getattr(result, "stdout", "") or "",
                    "\n".join(
                        part
                        for part in (
                            (getattr(result, "stderr", "") or "").strip(),
                            "read-only repository contract verification failed",
                        )
                        if part
                    ),
                )
                setattr(result, "mac_read_only_verification_failure", True)
        return result
    finally:
        active_error = sys.exc_info()[1]
        progress.stop()
        progress_evidence = progress.evidence()
        try:
            harvested = _sandbox_download(name, basename, workspace)
        except Exception as exc:  # noqa: BLE001 - teardown must continue to delete
            harvested = False
            sys.stderr.write(
                "[executor] WARNING: sandbox download raised unexpectedly: %s\n"
                % exc
            )
        promoted: Optional[bool] = None
        if read_only_report:
            # The agent sandbox is allowed to contain a same-named file, but it
            # is never authoritative for a read-only report.  Install the
            # separately-sandboxed verifier result only after the agent harvest
            # has finished, or remove the untrusted copy when no trusted result
            # was produced.
            try:
                promoted = _promote_trusted_read_only_verification(workspace)
            except Exception as exc:  # noqa: BLE001 - teardown must still delete
                promoted = False
                sys.stderr.write(
                    "[executor] WARNING: trusted verification promotion raised: %s\n"
                    % exc
                )
        preservation_required = (
            not read_only_report
            and isinstance(task, dict)
            and task_is_repo_coupled(task)
            and (
                active_error is not None
                or not runner_completed
                or result is None
                or int(getattr(result, "returncode", 1)) != 0
            )
        )
        wip_preservation: Dict[str, Any] = {
            "schema": REPOSITORY_WIP_BUNDLE_SCHEMA,
            "status": "not_required",
        }
        if preservation_required:
            try:
                if not harvested:
                    raise PreservationMissing(
                        "sandbox harvest failed before repository WIP preservation"
                    )
                wip_preservation = preserve_repository_wip_bundle(workspace, task)
                if (
                    wip_preservation.get("status") == "no_changes"
                    and progress_evidence.get("mutation_observed") is True
                ):
                    raise PreservationMissing(
                        "sandbox reported repository mutation but harvested host worktree is unchanged"
                    )
            except Exception as exc:  # noqa: BLE001 - retain sandbox fail-closed
                kept = True
                wip_preservation = {
                    "schema": REPOSITORY_WIP_BUNDLE_SCHEMA,
                    "status": "failed",
                    "error": str(exc),
                }
                sys.stderr.write(
                    "[executor] WARNING: repository WIP preservation failed; "
                    "retaining sandbox %s: %s\n" % (name, exc)
                )
                emit_telemetry(
                    "sandbox_wip_preservation_failed",
                    task_id=str(audit_id) if audit_id else None,
                    level="warning",
                    sandbox=name,
                    error=str(exc),
                )
            else:
                emit_telemetry(
                    "sandbox_wip_preserved",
                    task_id=str(audit_id) if audit_id else None,
                    sandbox=name,
                    status=str(wip_preservation.get("status") or ""),
                    bundle_sha256=str(
                        wip_preservation.get("bundle_sha256") or ""
                    ),
                    salvage_head_sha=str(
                        wip_preservation.get("salvage_head_sha") or ""
                    ),
                )
        salvage = {
            "schema": "mac.openshell_salvage.v1",
            "sandbox": name,
            "runner_completed": runner_completed,
            "harvest_attempted": True,
            "harvested": harvested,
            "kept": kept,
            "error": str(active_error) if active_error is not None else "",
            "progress": progress_evidence,
            "wip_preservation": wip_preservation,
            "at": utcnow(),
        }
        try:
            (workspace / "openshell-salvage.json").write_text(
                json.dumps(salvage, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            sys.stderr.write("[executor] WARNING: could not write sandbox salvage record: %s\n" % exc)
        emit_telemetry(
            "sandbox_harvested",
            task_id=str(audit_id) if audit_id else None,
            level="info" if harvested else "warning",
            sandbox=name,
            runner_completed=runner_completed,
            harvested=harvested,
        )
        deleted = False
        if not kept:
            deleted = _sandbox_delete(name)
            emit_telemetry(
                "sandbox_deleted",
                task_id=str(audit_id) if audit_id else None,
                level="info" if deleted else "warning",
                sandbox=name,
                deleted=deleted,
            )
        if read_only_report and runner_completed and result is not None:
            lifecycle_problems: List[str] = []
            if not harvested:
                lifecycle_problems.append("sandbox result harvest failed")
            if promoted is not True:
                lifecycle_problems.append(
                    "trusted repository verification result was not promoted"
                )
            if kept:
                lifecycle_problems.append(
                    "sandbox deletion was skipped by MAC_OPENSHELL_KEEP"
                )
            elif not deleted:
                lifecycle_problems.append("sandbox deletion failed")
            if lifecycle_problems:
                detail = "read-only report lifecycle incomplete: %s" % "; ".join(
                    lifecycle_problems
                )
                # A return expression inside the try has already retained this
                # object when finally runs, so mutate it rather than rebinding
                # the local.  This makes teardown failures authoritative at the
                # caller and causes _run_executor to replace agent evidence.
                setattr(result, "returncode", 68)
                prior_stderr = str(getattr(result, "stderr", "") or "").strip()
                setattr(
                    result,
                    "stderr",
                    "\n".join(part for part in (prior_stderr, detail) if part),
                )
                setattr(result, "mac_read_only_lifecycle_failure", detail)
                setattr(result, "mac_read_only_repository_violation", detail)
        for path in runtime_files:
            path.unlink(missing_ok=True)
        (workspace / ".mac-sandbox-repository-verify.sh").unlink(missing_ok=True)


def _force_child_yolo_env() -> None:
    """Make the agent subprocess inherit HERMES_YOLO_MODE=1.

    Hermes freezes its YOLO/approval bypass from HERMES_YOLO_MODE at *import*
    time (tools/approval.py: ``_YOLO_MODE_FROZEN``). The ``--yolo`` CLI flag sets
    that env only AFTER Hermes has already imported approval.py, so the freeze
    can capture False and ``--yolo`` silently FAILS to bypass approval — the
    agent still prompts. Setting the env here, in the executor, before the child
    is spawned (the child inherits ``os.environ``) guarantees it is present at
    the child's process start, before any import, so the freeze captures True
    and approval is genuinely bypassed. This is the executor-side fix for the
    import-order freeze; ``approvals.mode=off`` in the deployed config.yaml is
    the config-side lever that covers the gateway too.
    """
    os.environ["HERMES_YOLO_MODE"] = "1"


def _validated_host_break_glass_authorization(task: Any) -> Optional[Dict[str, Any]]:
    """Validate the lease-bound control-plane projection for host execution.

    The task description and durable task metadata are untrusted.  The worker
    receives this projection only in a claimed assignment and strips any
    caller-supplied lookalikes.  We still validate every binding here so a
    malformed or replayed task file fails closed before bypassing OpenShell.
    """

    if not isinstance(task, dict):
        return None
    metadata = task.get("metadata")
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else None
    raw = (
        runtime.get("break_glass_authorization")
        if isinstance(runtime, dict)
        else None
    )
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuntimeError("invalid break-glass authorization projection")
    checks = {
        "schema": raw.get("metadata", {}).get("schema")
        if isinstance(raw.get("metadata"), dict)
        else None,
        "status": raw.get("status"),
        "execution_boundary": raw.get("execution_boundary"),
        "task_id": raw.get("task_id"),
        "agent_id": raw.get("agent_id"),
        "lease_id": raw.get("lease_id"),
    }
    expected = {
        "schema": BREAK_GLASS_AUTHORIZATION_SCHEMA,
        "status": "claimed",
        "execution_boundary": "host",
        "task_id": str(task.get("id") or ""),
        "agent_id": env_str("MAC_AGENT_ID"),
        "lease_id": env_str("MAC_LEASE_ID"),
    }
    mismatches = [
        key
        for key, value in expected.items()
        if not value or str(checks.get(key) or "") != value
    ]
    if mismatches:
        raise RuntimeError(
            "break-glass authorization binding mismatch: %s"
            % ", ".join(sorted(mismatches))
        )
    if not str(raw.get("id") or "").startswith("breakglass_"):
        raise RuntimeError("break-glass authorization id is invalid")
    return dict(raw)


def _break_glass_prompt(authorization: Mapping[str, Any]) -> str:
    return """

HOST BREAK-GLASS RECOVERY BOUNDARY (EXPLICITLY AUTHORIZED)

This exact task and lease are running directly on the trusted worker host because
the work may need to repair the sandbox, worker, router, deployment, or other
execution infrastructure that a sandbox cannot modify. This is not general
permission to broaden scope. Make only the host changes necessary for the task,
preserve secrets, record every material host mutation in evidence, keep rollback
possible, and leave the host in a verified state. Authorization: %s. Reason: %s.
""" % (
        str(authorization.get("id") or "unknown"),
        str(authorization.get("reason") or "operator-authorized recovery"),
    )


def _prepare_host_break_glass_environment(
    authorization: Mapping[str, Any],
) -> None:
    """Replace sandbox-only process settings with trusted host equivalents.

    launchd workers have a deliberately narrow PATH.  Once an exact lease is
    authorized to run on the host, expand it to the trusted host tool locations
    the recovery task may need.  This is process-local (the task executor is
    one-shot) and does not mutate host config.
    """

    configured = env_str("MAC_BREAK_GLASS_HOST_PATH")
    candidates = [
        *(configured.split(os.pathsep) if configured else []),
        str(mac_paths.mac_home() / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/opt/local/bin",
        "/usr/local/bin",
        *str(os.environ.get("PATH") or "").split(os.pathsep),
    ]
    host_path: List[str] = []
    for raw in candidates:
        path = str(Path(raw).expanduser()) if raw else ""
        if path and path not in host_path and Path(path).is_dir():
            host_path.append(path)
    if host_path:
        os.environ["PATH"] = os.pathsep.join(host_path)

    emit_telemetry(
        "break_glass_host_environment_prepared",
        level="warning",
        authorization_id=authorization.get("id"),
        path_entries=len(host_path),
    )


def _unsandboxed_agent_argv(
    agent_argv: List[str],
    *,
    break_glass_authorization: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """Gate an already-built agent argv for an UNSANDBOXED run.

    The agent runs with its own approval bypass (Hermes ``--yolo`` or a coding
    agent's ``--dangerously-*``); running that unsandboxed is unguarded,
    permitted ONLY via ``MAC_ALLOW_UNSANDBOXED_YOLO`` (default "1" to preserve
    the current live fleet; "0" fails closed). Raises when fail-closed. The
    sandboxed path does not go through here — see :func:`_invoke_agent`.
    """
    if break_glass_authorization is not None:
        _force_child_yolo_env()
        authorization_id = str(break_glass_authorization.get("id") or "unknown")
        sys.stderr.write(
            "[executor] BREAK-GLASS: launching exact lease directly on the host "
            "under authorization %s; OpenShell bypass is task-scoped.\n"
            % authorization_id
        )
        emit_telemetry(
            "break_glass_host_execution",
            level="warning",
            authorization_id=authorization_id,
            execution_boundary="host",
            authorized_by=break_glass_authorization.get("authorized_by"),
        )
        return agent_argv
    default_unsandboxed = "0" if _openshell_required_for_local_agent() else "1"
    if _truthy(env_str("MAC_ALLOW_UNSANDBOXED_YOLO") or default_unsandboxed):
        _force_child_yolo_env()
        sys.stderr.write(
            "[executor] WARNING: launching an approval-bypassed agent WITHOUT an "
            "OpenShell sandbox (MAC_OPENSHELL_SANDBOX unset) — the agent's own "
            "approval gate is disabled and there is no sandbox confinement. Enable "
            "MAC_OPENSHELL_SANDBOX=1 (with a policy), or set "
            "MAC_ALLOW_UNSANDBOXED_YOLO=0 to fail closed.\n"
        )
        return agent_argv
    raise RuntimeError(
        "refusing to launch an approval-bypassed agent without an OpenShell sandbox: "
        "MAC_OPENSHELL_SANDBOX is unset and MAC_ALLOW_UNSANDBOXED_YOLO is disabled. "
        "Set MAC_OPENSHELL_SANDBOX=1 with a policy to enforce silently, or "
        "MAC_ALLOW_UNSANDBOXED_YOLO=1 to explicitly allow unsandboxed YOLO."
    )


def _record_runner_choice(
    target: str,
    rationale: List[str],
    *,
    task_id: str = "",
    route: Optional[Mapping[str, Any]] = None,
) -> None:
    """Make the coding-agent-vs-gateway routing decision legible (best-effort).

    Mirrors :func:`mac.agent_provider.record_provider_decision`: a secret-free
    line so an operator (or the agent) can answer "why did this task run on
    Claude / Codex / Cursor / the gateway?" rather than facing a silent choice.
    """
    sys.stderr.write(
        "[executor] coding-agent routing: %s (%s)\n" % (target, "; ".join(rationale) or "no rationale")
    )
    try:
        detail: Dict[str, Any] = {
            "task_id": task_id or None,
            "level": "info",
            "schema": "mac.coding_agent.routing.v1",
            "runner": target,
            "rationale": list(rationale),
        }
        if route:
            detail.update(
                {
                    "coding_agent": route.get("agent"),
                    "provider": route.get("provider"),
                    "protocol": route.get("protocol"),
                    "endpoint": route.get("endpoint"),
                    "requested_model": route.get("model"),
                    "route_fingerprint": route.get("route_fingerprint"),
                }
            )
        emit_telemetry("runner_selected", **detail)
    except Exception:  # noqa: BLE001 - telemetry must never break execution
        pass


def _coding_agent_required_failure_argv(reason: str) -> List[str]:
    msg = (
        "task execution requires an available coding agent and, when confined, "
        "a verified in-sandbox route; %s"
        % (reason or "no coding agent was verified")
    )
    code = "import sys; sys.stderr.write(%r + '\\n'); raise SystemExit(42)" % msg
    # Every command is serialized through ``_write_agent_command_bundle``, which
    # requires exactly one private-prompt sentinel so no task prompt can leak
    # into argv.  The fail-closed command does not consume the prompt, but it
    # still needs the sentinel as an inert ``sys.argv[1]`` for the common bundle
    # contract.  Omitting it made the error path itself raise ValueError before
    # the intended exit-42 diagnostic could run, exhausting task retry budgets.
    return ["python3", "-c", code, PROMPT_SENTINEL]


def _coding_agent_auth_is_safe_for_openshell(choice: Any) -> bool:
    """Whether the selected coding-agent auth can be copied into OpenShell safely.

    Codex OAuth state in ``~/.codex/auth.json`` is a rotating credential. Because
    OpenShell currently supports upload-copy semantics rather than a persistent
    writable mount for this path, a preflight or task sandbox can consume the
    refresh token and leave the host copy stale. Treat that auth source as
    unavailable under OpenShell unless the operator explicitly opts into the
    risk for a one-off debug run.
    """
    if (
        getattr(choice, "agent", "") == "codex"
        and getattr(choice, "auth_source", "") == "~/.codex/auth.json"
        and not env_bool("MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH")
    ):
        sys.stderr.write(
            "[executor] coding-agent sandbox preflight (codex): skipped "
            "(~/.codex/auth.json is rotating file auth; route unavailable)\n"
        )
        return False
    return True


# Per-process cache keyed by the full secret-free route fingerprint. A binary-only
# key incorrectly reused success after an endpoint, protocol, auth source, or model
# changed. Entries expire so revoked credentials and dead routes stop dispatch.
_SANDBOX_PREFLIGHT_CACHE: Dict[str, Dict[str, object]] = {}
_SANDBOX_PREFLIGHT_CACHE_LOCK = threading.Lock()


def _coding_agent_preflight_timeout() -> float:
    raw = env_str("MAC_CODING_AGENT_PREFLIGHT_TIMEOUT")
    try:
        val = float(raw)
        return val if val > 0 else 180.0
    except ValueError:
        return 180.0


def _coding_agent_preflight_ttl(verified: bool) -> float:
    name = (
        "MAC_CODING_AGENT_PREFLIGHT_TTL_SECONDS"
        if verified
        else "MAC_CODING_AGENT_PREFLIGHT_FAILURE_TTL_SECONDS"
    )
    # Worker heartbeats probe successful routes every ten minutes. Keep this
    # cache shorter than that interval so a scheduled refresh can never reuse
    # the old proof and then wait another full interval.
    default = 300.0 if verified else 60.0
    try:
        return max(1.0, float(os.environ.get(name) or default))
    except ValueError:
        return default


def _classify_coding_agent_preflight_failure(returncode: int, output: str) -> str:
    """Map a failed coding-agent preflight probe onto an actionable class.

    The classes are ordered from most specific to most generic so a caller can
    react without re-parsing the raw probe output. ``probe_failed`` is the
    catch-all of last resort; every marker added here strictly narrows what
    would otherwise collapse into it, which is what makes a failed run
    diagnosable (see the ``rc=1, class=probe_failed`` fleet failures that
    carried no recovery signal).
    """
    text = (output or "").lower()
    if returncode in {124, 137} or "timed out" in text or "timeout" in text:
        return "timeout"
    if (
        "nvidia-persistenced" in text
        or (
            ("oci runtime create failed" in text or "containerstartfailed" in text)
            and ("nvidia" in text or "gpu" in text or "cdi" in text)
        )
    ):
        return "sandbox_gpu_unavailable"
    # The OpenShell sandbox itself could not be created/uploaded, so the probe
    # never reached the coding agent. Check this before generic filesystem and
    # provider markers: OCI mount failures commonly contain both "no such file"
    # and a gateway status code.
    if (
        "sandbox create" in text
        or "failed to create sandbox" in text
        or "oci runtime create failed" in text
        or "containerstartfailed" in text
        or "error mounting" in text
        or "sandbox entered error phase" in text
    ):
        return "sandbox_unavailable"
    # Provider throttling. A 429 (or an explicit rate-limit message) is
    # transient: retry with backoff rather than treating the route as broken.
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    # Provider-side server faults (5xx / gateway errors). Like throttling these
    # are transient and route-independent: the endpoint, credentials, and model
    # are all correct, the upstream just failed this call. Steer an automated
    # retry with backoff instead of collapsing into the opaque ``probe_failed``.
    # Checked before the generic ``404``/``not found`` protocol test below so a
    # "502 bad gateway" is not mis-reported as an endpoint/protocol mismatch.
    if (
        "500" in text
        or "502" in text
        or "503" in text
        or "504" in text
        or "internal server error" in text
        or "bad gateway" in text
        or "service unavailable" in text
        or "gateway timeout" in text
    ):
        return "provider_server_error"
    if "connection refused" in text or "failed to connect" in text:
        return "endpoint_unreachable"
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "authentication_failed"
    # The coding-agent CLI (or the shell wrapper) is absent from the sandbox
    # image. This must be checked before the ``not found`` protocol test below,
    # otherwise a missing binary is mis-reported as an endpoint mismatch and the
    # operator repairs the wrong layer.
    if (
        "command not found" in text
        or "no such file or directory" in text
        or "executable file not found" in text
        or ": not found" in text
    ):
        return "agent_binary_missing"
    if "404" in text or "not found" in text or "unsupported" in text:
        return "endpoint_protocol_mismatch"
    if returncode == 0:
        return "sentinel_missing"
    return "probe_failed"


def _coding_agent_binary_status(verified: bool, failure_class: str) -> str:
    """Whether the in-sandbox probe proved the selected executable exists."""
    if verified:
        return "present"
    if failure_class == "agent_binary_missing":
        return "missing"
    if failure_class in {
        "authentication_failed",
        "endpoint_protocol_mismatch",
        "endpoint_unreachable",
        "provider_server_error",
        "rate_limited",
        "sentinel_missing",
    }:
        # These failures are emitted only after the CLI launched far enough to
        # contact or parse a response from its provider.
        return "present"
    return "unverified"


_SANDBOX_CODING_AGENT_BINARIES = frozenset(
    {"claude", "codex", "cursor", "cursor-agent"}
)


def coding_agent_sandbox_which(name: str) -> Optional[str]:
    """Resolve binaries declared by the OpenShell task-image contract.

    This is deliberately a declared inventory, not proof.  Every route selected
    through it still has to pass the disposable in-sandbox preflight, which
    reports a missing executable explicitly if an image drifts from the
    contract.
    """
    return name if name in _SANDBOX_CODING_AGENT_BINARIES else None


def _build_sandbox_probe_argv(
    name: str, agent_argv: List[str], private_dir: Path
) -> List[str]:
    """Build a credential-free process argv for the coding-agent probe."""
    if "mac.agent_command" not in agent_argv:
        raise ValueError("sandbox probe must use the private-file command wrapper")
    argv: List[str] = [_openshell_bin(), "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_openshell_policy(), "--name", name]
    argv += _sandbox_label_argv("codingcap")
    argv += _openshell_extra_create_argv()
    sandbox_dir = "/sandbox/%s" % private_dir.name
    argv += ["--upload", "%s:/sandbox" % private_dir]
    inner = "\n".join(
        [
            "cd %s" % shlex.quote(sandbox_dir),
            "set -a",
            ". ./.mac-openshell-env.sh",
            "set +a",
            "rm -f ./.mac-openshell-env.sh",
            "exec %s" % shlex.join(agent_argv),
        ]
    )
    argv += ["--", "/bin/bash", "-lc", inner]
    return argv


def _coding_agent_choice_for_sandbox(choice: Any) -> Any:
    """Return a choice whose endpoint and executable resolve inside OpenShell.

    Coding-agent detection intentionally runs on the host, where ``which`` returns
    an absolute host path (for example ``/opt/homebrew/bin/codex``).  Passing that
    path into a Linux sandbox bypasses the sandbox's PATH contract and fails even
    when the image contains the CLI at ``/usr/local/bin/codex``.  Execute the
    detected basename through the image-owned PATH instead.  The preflight still
    proves that the corresponding binary is actually present before work routes
    to it.
    """
    endpoint = str(getattr(choice, "endpoint", "") or "")
    binary = str(getattr(choice, "binary", "") or "")
    rewritten_endpoint = (
        _rewrite_host_local_url(endpoint, _openshell_host_alias())
        if endpoint
        else endpoint
    )
    sandbox_binary = Path(binary).name if binary else binary
    if rewritten_endpoint == endpoint and sandbox_binary == binary:
        return choice
    return replace(choice, endpoint=rewritten_endpoint, binary=sandbox_binary)


def _coding_agent_env_for_sandbox(choice: Any) -> Dict[str, str]:
    """Normalize an explicit coding-agent command onto the sandbox PATH.

    Command overrides remain useful for CLI flag drift, but their executable may
    not be a host-only absolute path.  Other explicit arguments are preserved and
    are validated by the same live sandbox preflight.
    """
    from . import coding_agent as _ca

    import shlex

    env = dict(os.environ)
    key = _ca.COMMAND_ENV.get(str(getattr(choice, "agent", "") or ""))
    raw = str(env.get(key) or "").strip() if key else ""
    if not raw:
        return env
    argv = shlex.split(raw)
    if argv:
        argv[0] = Path(argv[0]).name
        env[key] = shlex.join(argv)
    return env


def _openshell_probe(create_argv: List[str], *, timeout: float) -> "tuple[int, str]":
    """Run a one-shot ``sandbox create`` probe; return (returncode, combined output).
    Best-effort: any failure returns a non-zero code (never raises)."""
    try:
        proc = subprocess.run(create_argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - a probe failure must mean "not ready", not a crash
        return 1, str(exc)


def _run_coding_agent_preflight_result(choice: Any) -> Dict[str, object]:
    """Verify, inside a throwaway OpenShell sandbox, that the coding agent runs
    end-to-end: it must execute, authenticate, reach the provider, and echo the
    sentinel back. Proves the agent will actually work for a real sandboxed task
    (binary present + creds resolvable + egress allowed) — host-side availability
    is NOT sufficient. The probe sandbox is always deleted."""
    from . import coding_agent as _ca

    import uuid

    name = "mac-codingcap-%s-%s" % (choice.agent, uuid.uuid4().hex[:12])
    with tempfile.TemporaryDirectory(prefix="mac-coding-agent-probe-") as tmp:
        private_dir = Path(tmp)
        sandbox_choice = _coding_agent_choice_for_sandbox(choice)
        probe_argv = _ca.coding_agent_argv(
            sandbox_choice,
            PROMPT_SENTINEL,
            env=_coding_agent_env_for_sandbox(sandbox_choice),
        )
        bundle = _write_agent_command_bundle(
            private_dir, _ca.PREFLIGHT_PROMPT, probe_argv
        )
        _write_private_shell_env(
            private_dir / ".mac-openshell-env.sh",
            {**_openshell_environment(), "HOME": _SANDBOX_HOME},
        )
        sandbox_dir = "/sandbox/%s" % private_dir.name
        try:
            rc, out = _openshell_probe(
                _build_sandbox_probe_argv(
                    name,
                    bundle.argv(sandbox_workspace=sandbox_dir),
                    private_dir,
                ),
                timeout=_coding_agent_preflight_timeout(),
            )
        finally:
            bundle.cleanup()
            _sandbox_step(["delete", name], timeout=60.0)
    ok = rc == 0 and _ca.PREFLIGHT_SENTINEL in out
    failure_class = (
        "" if ok else _classify_coding_agent_preflight_failure(rc, out)
    )
    result: Dict[str, object] = {
        "schema": "mac.coding_agent.verification.v1",
        "agent": choice.agent,
        "binary": getattr(choice, "binary", ""),
        "execution_binary": getattr(sandbox_choice, "binary", ""),
        "binary_status": _coding_agent_binary_status(ok, failure_class),
        "provider": getattr(choice, "provider", ""),
        "protocol": getattr(choice, "protocol", ""),
        "auth_kind": getattr(choice, "auth_kind", ""),
        "auth_source": getattr(choice, "auth_source", ""),
        "endpoint": getattr(choice, "endpoint", ""),
        "model": getattr(choice, "model", ""),
        "route_fingerprint": choice.route_fingerprint(),
        "verified": ok,
        "checked_at": utcnow(),
        "returncode": rc,
        "failure_class": failure_class,
    }
    sys.stderr.write(
        "[executor] coding-agent sandbox preflight (%s): %s\n"
        % (
            choice.agent,
            "OK"
            if ok
            else "FAILED (rc=%s, class=%s) — falling back to gateway"
            % (rc, result["failure_class"]),
        )
    )
    return result


def _run_coding_agent_preflight(choice: Any) -> bool:
    """Compatibility wrapper returning only the verified verdict."""
    return bool(_run_coding_agent_preflight_result(choice).get("verified"))


def coding_agent_sandbox_verification(choice: Any) -> Dict[str, object]:
    """Return the cached/live full route verification used by worker heartbeats."""
    if not getattr(choice, "available", False) or not getattr(choice, "agent", ""):
        return {
            "schema": "mac.coding_agent.verification.v1",
            "agent": getattr(choice, "agent", ""),
            "binary": getattr(choice, "binary", ""),
            "binary_status": "unverified",
            "verified": False,
            "checked_at": utcnow(),
            "failure_class": "not_configured",
        }
    if not _coding_agent_auth_is_safe_for_openshell(choice):
        return {
            **choice.observable(),
            "schema": "mac.coding_agent.verification.v1",
            "binary_status": "unverified",
            "verified": False,
            "checked_at": utcnow(),
            "failure_class": "unsafe_rotating_file_auth",
        }
    key = choice.route_fingerprint()
    now = time.monotonic()
    with _SANDBOX_PREFLIGHT_CACHE_LOCK:
        cached = _SANDBOX_PREFLIGHT_CACHE.get(key)
    if cached is not None:
        verified = bool(cached.get("verified"))
        age = now - float(cached.get("cached_monotonic") or 0.0)
        if age < _coding_agent_preflight_ttl(verified):
            return {k: v for k, v in cached.items() if k != "cached_monotonic"}
    result = _run_coding_agent_preflight_result(choice)
    with _SANDBOX_PREFLIGHT_CACHE_LOCK:
        _SANDBOX_PREFLIGHT_CACHE[key] = {
            **result,
            "cached_monotonic": time.monotonic(),
        }
    return result


def _coding_agent_sandbox_ok(choice: Any) -> bool:
    """Whether a coding agent may be used on the SANDBOXED path.

    ``MAC_CODING_AGENT_SANDBOX`` modes:
      * ``verify`` (default) — gate on a cached in-sandbox preflight that actually
        runs the agent; only enable when it works there.
      * ``trust`` / ``1`` — assume the sandbox image is provisioned; skip the probe.
      * ``off`` / ``0`` — never use a coding agent when sandboxed (fail closed).
    """
    mode = (env_str("MAC_CODING_AGENT_SANDBOX") or "verify").lower()
    if mode in {"off", "0", "false", "no"}:
        return False
    if mode in {"trust", "1", "true", "yes", "skip"}:
        return True
    if not _coding_agent_auth_is_safe_for_openshell(choice):
        return False
    return bool(coding_agent_sandbox_verification(choice).get("verified"))


def _agent_argv(prompt: str, workspace: Path, *, confined: bool, task: Any = None) -> List[str]:
    """Pick the agent runner: a coding-agent CLI when one is available + authed
    (and — when OpenShell-confined — verified to actually work inside the sandbox),
    otherwise return a deterministic fail-closed command.

    Coding-agent CLIs (Claude Code, Codex, Cursor) authenticate against a
    subscription/seat rather than a metered API token, so they are preferred for
    cost (see :mod:`mac.coding_agent`). Full mac-runtime parity on this path: the
    CLI runs in the prepared checkout (the ``mac`` CLI + runtime context + hub
    env give it the same hub tool surface Hermes has), receives the same
    structured task/evidence prompt, and — where the CLI supports per-invocation
    MCP, on the unconfined path — the messaging MCP server.

    When OpenShell confinement is in effect (``confined`` — per-task wrap or the
    production supervisor) enablement is gated on :func:`_coding_agent_sandbox_ok`
    (a real in-sandbox preflight by default), because a host-side ``which``/cred
    check does NOT prove the agent works inside the confined sandbox.

    The retired Hermes chat fallback is deliberately not configurable.  A
    missing or unverified route always selects ``coding-agent-required`` so a
    worker cannot silently execute through the runtime being removed.
    """
    from . import coding_agent as _ca

    task_id = str(task.get("id") or "").strip() if isinstance(task, dict) else ""
    verified_fingerprints = set()

    def _accept_sandbox_route(candidate: Any) -> bool:
        accepted = _coding_agent_sandbox_ok(candidate)
        if accepted:
            verified_fingerprints.add(candidate.route_fingerprint())
        return accepted

    choice = _ca.resolve_coding_agent(
        which=coding_agent_sandbox_which if confined else None,
        accept=_accept_sandbox_route if confined else None,
    )
    rationale = list(choice.rationale)
    if not choice.available:
        reason = (
            "no task-sandbox coding agent is configured and verified"
            if confined
            else "no host coding agent is available/authenticated"
        )
        rationale.append(reason)
        _record_runner_choice("coding-agent-required", rationale, task_id=task_id)
        return _coding_agent_required_failure_argv(reason)
    if (
        confined
        and choice.route_fingerprint() not in verified_fingerprints
        and not _coding_agent_sandbox_ok(choice)
    ):
        reason = "%s not verified inside the OpenShell sandbox" % choice.agent
        rationale.append(reason)
        _record_runner_choice("coding-agent-required", rationale, task_id=task_id)
        return _coding_agent_required_failure_argv(reason)

    # MCP wiring is unconfined-only: the host config path + host MCP-server
    # interpreter do not reliably resolve inside the sandbox (messaging-MCP parity
    # there is provisioned image-side). Hub parity (mac CLI + runtime context)
    # still applies regardless.
    # Human-facing delivery is owned exclusively by the OpenClaw gateway.  Do
    # not inject the retired vendored-Hermes messaging MCP into coding agents;
    # task-to-human messages flow through MAC's durable delivery outbox instead.
    mcp_path = None
    if confined:
        rationale.append("verified inside the OpenShell sandbox")
    _record_runner_choice(
        choice.agent,
        rationale,
        task_id=task_id,
        route=choice.observable(),
    )
    argv_choice = _coding_agent_choice_for_sandbox(choice) if confined else choice
    argv_env = _coding_agent_env_for_sandbox(argv_choice) if confined else None
    return _ca.coding_agent_argv(
        argv_choice,
        prompt,
        env=argv_env,
        mcp_config_path=mcp_path,
    )


def _executor_backend() -> str:
    """Which agent runtime drives a task: ``hermes`` (default) or ``acp``.

    ACP (ADR 0006) is opt-in via ``MAC_EXECUTOR_BACKEND=acp`` so Hermes stays the
    default until parity; the external agent command is ``MAC_ACP_AGENT_CMD``."""
    return (env_str("MAC_EXECUTOR_BACKEND") or "hermes").lower()


def _acp_agent_argv() -> List[str]:
    """The external ACP agent command (shell-split). Required for backend=acp."""
    import shlex

    return shlex.split(env_str("MAC_ACP_AGENT_CMD"))


def _acp_update_action_event(audit_id: Any, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Map one ACP ``session/update`` notification to a mac action-event record."""
    inner = params.get("update") or {}
    return {
        "task_id": audit_id,
        "session_id": session_id or params.get("sessionId"),
        "actor": "mac-acp",
        "action_type": "acp.session_update",
        "action_name": str(inner.get("sessionUpdate") or "update"),
        "outcome": "unknown",
        "severity": "info",
        "attributes": {"acp_update": inner},
    }


def _acp_permission_handler(audit_id: Any) -> Callable[[Any], Any]:
    """ACP ``session/request_permission`` handler (Phase 3).

    Evaluates each request through :func:`mac.acp.permission.evaluate_permission`
    instead of blanket auto-approving. The OpenShell *kernel sandbox* remains the
    real gate — when sandboxed the decision short-circuits to allow ("sandbox-
    enforced") and the ACP prompt is advisory. Unsandboxed, the decision consults
    the parsed OpenShell *policy* (network lockdown denies egress; an empty
    read_write set denies writes); with no policy it defaults to allow (Phase-1
    parity), flippable to deny via ``MAC_ACP_PERMISSION_MODE=deny``.

    On *allow* it selects the first ``allow``-kind option the agent offered (else
    the first option); on *deny* it selects a ``reject``-kind option if one is
    offered, else returns a CANCELLED outcome. Every decision + its reason is
    recorded to ``/action-events`` (``attributes.permission_reason``)."""
    from mac.acp.permission import evaluate_permission, load_openshell_policy
    from mac.acp.protocol import PermissionOutcome, RequestPermissionResult

    sandboxed = _openshell_enabled()
    # Load the policy only when it can actually change the decision (unsandboxed
    # under policy mode). Best-effort: a missing/unreadable policy -> None.
    policy = None if sandboxed else load_openshell_policy()

    def _handler(params: Any) -> Any:
        options = list(getattr(params, "options", None) or [])
        tool_call = getattr(params, "tool_call", {}) or {}
        decision = evaluate_permission(tool_call, policy=policy, sandboxed=sandboxed)

        if decision.allow:
            chosen = next((o for o in options if str(o.kind or "").startswith("allow")), None)
            chosen = chosen or (options[0] if options else None)
        else:
            # Prefer an explicit reject option when the agent offered one.
            chosen = next((o for o in options if str(o.kind or "").startswith("reject")), None)

        _hub_post(
            "/action-events",
            {
                "task_id": audit_id,
                "session_id": getattr(params, "session_id", None),
                "actor": "mac-acp",
                "action_type": "acp.permission",
                "action_name": str(tool_call.get("title") or tool_call.get("toolCallId") or "tool_call"),
                "outcome": "allowed" if decision.allow else "denied",
                "severity": "info",
                "attributes": {
                    "tool_call": tool_call,
                    "permission_reason": decision.reason,
                    "allowed": decision.allow,
                },
            },
        )
        if chosen is not None:
            return RequestPermissionResult(outcome=PermissionOutcome.SELECTED, option_id=chosen.option_id)
        return RequestPermissionResult(outcome=PermissionOutcome.CANCELLED)

    return _handler


#: AgentBus content_type for a mirrored ACP session/update chunk.
_ACP_AGENTBUS_CONTENT_TYPE = "application/vnd.mac.acp.update+json"
#: AgentBus topic for the mirrored ACP update stream.
_ACP_AGENTBUS_TOPIC = "acp.session_update"


class _AcpAgentBusMirror:
    """Best-effort mirror of an ACP ``session/update`` stream onto AgentBus.

    Lifecycle (all via :func:`_hub_post`, so failures never raise):

      * :meth:`open`   -> ``POST /agentbus/streams`` once at run start. mac is
        both sender and recipient (a self-stream: the worker agent is the only
        party, and AgentBus requires a concrete recipient), so the worker's own
        ``local_agent_id`` is used for both ends.
      * :meth:`append` -> ``POST /agentbus/streams/{id}/chunks`` per update.
      * :meth:`close`  -> ``POST /agentbus/streams/{id}/close`` at run end.

    If the open fails (no hub env, AgentBus error, unknown agent) the mirror is
    inert: :attr:`stream_id` stays ``None`` and append/close are no-ops, so the
    /action-events path is unaffected."""

    def __init__(self, audit_id: Any) -> None:
        self._agent_id = local_agent_id()
        self._audit_id = audit_id
        self.stream_id: Optional[str] = None

    def open(self) -> None:
        payload: Dict[str, Any] = {
            "sender_agent_id": self._agent_id,
            "recipient_agent_id": self._agent_id,
            "topic": _ACP_AGENTBUS_TOPIC,
            "content_type": _ACP_AGENTBUS_CONTENT_TYPE,
            "headers": {"schema": "mac.acp.session_update.v1", "task_id": self._audit_id},
        }
        if self._audit_id:
            payload["task_id"] = str(self._audit_id)
        resp = _hub_post_json("/agentbus/streams", payload)
        if isinstance(resp, dict):
            sid = resp.get("id")
            if isinstance(sid, str) and sid:
                self.stream_id = sid

    def append(self, session_id: str, params: Dict[str, Any]) -> None:
        if not self.stream_id:
            return
        _hub_post(
            "/agentbus/streams/%s/chunks" % self.stream_id,
            {
                "sender_agent_id": self._agent_id,
                "content_type": _ACP_AGENTBUS_CONTENT_TYPE,
                "payload": {"sessionId": session_id, "update": params.get("update") or {}},
            },
        )

    def close(self, status: str = "closed") -> None:
        if not self.stream_id:
            return
        # The close handler takes sender_agent_id + status as query params.
        _hub_post(
            "/agentbus/streams/%s/close?sender_agent_id=%s&status=%s"
            % (self.stream_id, urllib.parse.quote(self._agent_id), urllib.parse.quote(status)),
            {},
        )


def _invoke_acp_agent(
    prompt: str, workspace: Path, audit_id: Any, opts: dict, *, executor: Any = None
) -> "subprocess.CompletedProcess":
    """Drive an external ACP agent (ADR 0006) for one task turn.

    Streams every ``session/update`` to the hub's ``/action-events`` ledger and
    bridges ``session/request_permission`` through :func:`_acp_permission_handler`.
    Returns a :class:`subprocess.CompletedProcess` so the downstream
    finalizer/evidence flow is unchanged — the deterministic git finalizer
    remains the real proof of work regardless of which agent produced it."""
    from mac.acp import ACPExecutor
    from mac.acp.protocol import ContentBlockType, SessionUpdateKind, StopReason

    if executor is None:
        argv = _acp_agent_argv()
        if not argv:
            return subprocess.CompletedProcess(
                ["acp"], 1, "", "MAC_EXECUTOR_BACKEND=acp but MAC_ACP_AGENT_CMD is unset"
            )
        executor = ACPExecutor(argv, cwd=str(workspace))

    text_chunks: List[str] = []
    # AgentBus mirror (ADR 0006 Phase 3): mirror the session/update stream onto an
    # AgentBus chunk stream alongside the /action-events ledger. Best-effort and
    # failure-tolerant — if the open fails we simply skip the chunk posts. Only
    # runs under backend=acp, so the Hermes path takes on no extra latency.
    mirror = _AcpAgentBusMirror(audit_id)
    mirror.open()

    def _on_update(params: Dict[str, Any]) -> None:
        inner = params.get("update") or {}
        if inner.get("sessionUpdate") in (
            SessionUpdateKind.AGENT_MESSAGE_CHUNK,
            SessionUpdateKind.AGENT_THOUGHT_CHUNK,
        ):
            content = inner.get("content") or {}
            if isinstance(content, dict) and content.get("type") == ContentBlockType.TEXT:
                text_chunks.append(str(content.get("text") or ""))
        _hub_post("/action-events", _acp_update_action_event(audit_id, str(params.get("sessionId") or ""), params))
        mirror.append(str(params.get("sessionId") or ""), params)

    argv_label = list(getattr(executor, "_argv", ["acp"]))
    try:
        run = executor.run(
            prompt,
            on_update=_on_update,
            on_permission=_acp_permission_handler(audit_id),
            timeout=opts.get("timeout"),
        )
    except Exception as exc:  # noqa: BLE001 - a backend failure must finalize, not crash the loop
        mirror.close(status="errored")
        return subprocess.CompletedProcess(argv_label, 1, "".join(text_chunks), "ACP agent run failed: %s" % exc)
    mirror.close()
    rc = 0 if run.stop_reason == StopReason.END_TURN else 1
    stderr = "" if rc == 0 else "ACP agent stopped with reason: %s" % run.stop_reason
    return subprocess.CompletedProcess(argv_label, rc, "".join(text_chunks), stderr)


def _invoke_agent(
    runner: Callable[..., Any], prompt: str, workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent for one task, atomically coupling --yolo to enforcement.

    Invariant: an approval-bypassed coding agent (``--dangerously-*``) is only
    used when the run is confined by OpenShell, so we
    never launch an *unguarded* bypass agent.
      * backend=acp      -> drive an external ACP agent (ADR 0006); confinement
        is the OpenShell sandbox + the permission bridge.
      * sandbox enabled  -> full OpenShell lifecycle (upload workspace, run the
        agent confined, download results, delete). Fails closed if no policy
        resolves or the kernel can't enforce Landlock.
      * sandbox disabled -> direct run, gated by MAC_ALLOW_UNSANDBOXED_YOLO.
    The agent argv is a detected coding-agent CLI when one is available + authed;
    otherwise execution fails closed (see :func:`_agent_argv`).
    Returns the runner's result (carries .returncode)."""
    metadata = (
        opts.get("task", {}).get("metadata")
        if isinstance(opts.get("task"), dict)
        else None
    )
    read_only_repository = metadata_declares_read_only_report_repository(metadata)
    if _executor_backend() == "acp":
        if read_only_repository:
            raise RuntimeError(
                "read-only repository reports require per-task OpenShell confinement; "
                "the ACP backend is not supported"
            )
        return _invoke_acp_agent(prompt, workspace, audit_id, opts)
    # `wrap` is the per-task OpenShell wrap launch model; `confined` is whether
    # OpenShell confinement is in effect by EITHER model — the per-task wrap or
    # the production supervisor (which runs this whole process inside a sandbox,
    # with MAC_OPENSHELL_SANDBOX off but the agent required). Coding-agent
    # enablement is gated on `confined`, not `wrap`.
    break_glass_authorization = _validated_host_break_glass_authorization(
        opts.get("task")
    )
    if break_glass_authorization is not None:
        _prepare_host_break_glass_environment(break_glass_authorization)
    wrap = _openshell_enabled() and break_glass_authorization is None
    if read_only_repository and (break_glass_authorization is not None or not wrap):
        raise RuntimeError(
            "read-only repository reports require per-task OpenShell confinement; "
            "direct, supervisor-only, and host break-glass execution are forbidden"
        )
    confined = (
        wrap or _openshell_required_for_local_agent()
    ) and break_glass_authorization is None
    agent_argv = _agent_argv(
        PROMPT_SENTINEL, workspace, confined=confined, task=opts.get("task")
    )
    bundle = _write_agent_command_bundle(workspace, prompt, agent_argv)
    try:
        if wrap:
            sandbox_workspace = "%s/%s" % (
                _SANDBOX_WORKDIR,
                _workspace_basename(workspace),
            )
            return _run_sandboxed(
                runner,
                bundle.argv(sandbox_workspace=sandbox_workspace),
                workspace,
                audit_id,
                opts,
            )
        return runner(
            _unsandboxed_agent_argv(
                bundle.argv(),
                break_glass_authorization=break_glass_authorization,
            ),
            workspace,
            audit_id,
            {
                **opts,
                "execution_boundary": (
                    "host" if break_glass_authorization is not None else "unsandboxed"
                ),
                "break_glass_authorization_id": (
                    break_glass_authorization.get("id")
                    if break_glass_authorization is not None
                    else None
                ),
            },
        )
    finally:
        bundle.cleanup()


def _agent_timeout() -> Optional[float]:
    """Bound a single agent run so a wedged TokenHub turn can't hang the loop
    forever. Default 900s; set MAC_EXECUTOR_AGENT_TIMEOUT=0 to disable."""
    raw = env_str("MAC_EXECUTOR_AGENT_TIMEOUT")
    if not raw:
        return 900.0
    try:
        val = float(raw)
    except ValueError:
        return 900.0
    return val if val > 0 else None


def _manifest_is_complete(task_workspace: Path) -> bool:
    """True when the agent (or a deterministic finalizer) already wrote a
    complete, typed evidence manifest — i.e. real verified work exists."""
    path = task_workspace / "mac-evidence.json"
    if not path.exists():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not (
        isinstance(manifest, dict)
        and str(manifest.get("status") or "").lower() == "complete"
        and bool(manifest.get("evidence_type"))
    ):
        return False
    if str(manifest.get("evidence_type") or "").strip().lower() != "review_verdict":
        return True

    # A deterministic review finalizer always writes a complete, signed
    # manifest, including when the model never produced a semantic verdict.
    # Do not let the generic timeout-salvage path turn that fail-closed record
    # into a successful review execution.
    if str(manifest.get("semantic_verdict") or "").strip().lower() not in {
        "approved",
        "rejected",
    }:
        return False
    experiment = manifest.get("review_experiment")
    if isinstance(experiment, dict) and experiment.get("blind"):
        protocol = experiment.get("protocol")
        if not isinstance(protocol, dict) or protocol.get("protocol_compliant") is not True:
            return False
    return True


def finalize_with_new_file_recovery(task_workspace, task, task_id) -> None:
    """Run the git finalizer, recovering a new-file-only refusal in place.

    The finalizer refuses to auto-commit NEW files the agent created,
    preserving the worktree + original evidence instead of publishing.
    task_e2ce62d9 implemented the recovery (stage/commit/push the preserved
    new files) but never WIRED it, so every "Implement X" task creating new
    files burned all its attempts: the work was done, then discarded as
    verification_contract_failed (observed live 2026-07-14 — an agent wrote
    fleet_node_install.py three times and lost it three times). This completes
    the loop: attempt the recovery — it fail-closes with
    RepositoryRecoveryError unless the refusal really was new-file-only — then
    re-run the finalizer so the recovered commit produces clean, publishable
    evidence. The adversarial review gate still reviews the full diff (new
    files included) before anything lands.
    """
    from mac.repository_recovery import RepositoryRecoveryError

    run_deterministic_git_finalizer(task_workspace, task)
    try:
        recovery = recover_from_new_file_refusal(task_workspace, task)
    except RepositoryRecoveryError:
        return  # not a new-file-only refusal (or nothing preserved)
    except Exception as exc:  # noqa: BLE001 - recovery must not mask the run
        sys.stderr.write("new-file recovery failed: %s\n" % exc)
        return
    emit_telemetry(
        "new_file_refusal_recovered",
        task_id=task_id,
        level="warning",
        recovered_files=len((recovery or {}).get("recovered_files") or []),
    )
    run_deterministic_git_finalizer(task_workspace, task)


def _write_startup_failclose_evidence(task_workspace: Path, task_id: Any, detail: str) -> None:
    """Best-effort fail-closed evidence when startup dies before the run begins.

    Never clobbers an existing manifest and never fabricates a passing test or
    repo_change — records only the observed startup failure.
    """
    path = task_workspace / "mac-evidence.json"
    if path.exists():
        return
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "task_id": task_id,
        "summary": "Executor startup failed before the agent run began: %s" % detail,
    }
    task_workspace.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(*, runner: Callable[..., Any] = run_audited_command) -> int:
    """Run the task executor entry point and return its exit code."""
    try:
        task_file = Path(os.environ["MAC_TASK_FILE"])
        task_workspace = Path(os.environ["MAC_TASK_WORKSPACE"])
        task_payload = json.loads(task_file.read_text(encoding="utf-8"))
        task = task_payload.get("task", task_payload)
        metadata = task.get("metadata") if isinstance(task, dict) else {}
        review_context = metadata.get("review_context") if isinstance(metadata, dict) else None
        is_review = isinstance(review_context, dict)
        task_id = task.get("id") if isinstance(task, dict) else None
    except Exception as exc:  # noqa: BLE001 - startup must fail closed, not open
        detail = "%s: %s" % (type(exc).__name__, exc)
        resolved_task_id: Optional[str] = None
        try:
            raw = os.environ.get("MAC_TASK_FILE")
            if raw:
                payload = json.loads(Path(raw).read_text(encoding="utf-8"))
                inner = payload.get("task", payload) if isinstance(payload, dict) else None
                if isinstance(inner, dict):
                    resolved_task_id = inner.get("id")
        except Exception:  # noqa: BLE001 - best-effort task_id resolution only
            resolved_task_id = None
        try:
            emit_telemetry(
                "executor_startup_failed",
                task_id=resolved_task_id,
                level="warning",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry must never mask the error
            pass
        workspace_raw = os.environ.get("MAC_TASK_WORKSPACE")
        if workspace_raw:
            try:
                _write_startup_failclose_evidence(
                    Path(workspace_raw), resolved_task_id, detail
                )
            except Exception:  # noqa: BLE001 - evidence write must never re-raise
                pass
        sys.stderr.write("[executor] startup failed: %s\n" % detail)
        return 1

    # NeMo Relay: open an Agent scope for this executor run (no-op when
    # relay is absent or MAC_RELAY_OBSERVABILITY != '1').
    session_id = str(task_id or "unknown")
    with relay_observability.create_agent_scope(session_id):
        try:
            rc = _run_executor(
                runner=runner,
                task=task,
                task_file=task_file,
                task_workspace=task_workspace,
                task_id=task_id,
                review_context=review_context,
                is_review=is_review,
            )
        finally:
            relay_observability.flush()
    return rc


def _read_only_report_repository_violation(
    task: Any, expected_git_control_digest: str = ""
) -> str:
    """Return a fail-closed reason when an inspection checkout was mutated."""

    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not metadata_declares_read_only_report_repository(metadata):
        return ""
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else None
    worktree_raw = env_str("MAC_TASK_REPO_WORKTREE") or (
        str(runtime.get("repository_worktree") or "")
        if isinstance(runtime, dict)
        else ""
    )
    if not worktree_raw:
        return "read-only repository report has no task-owned worktree"
    worktree = Path(worktree_raw).expanduser()
    if not worktree.is_dir():
        return "read-only repository report worktree is missing"
    if not expected_git_control_digest:
        return "read-only repository report has no pre-agent Git control proof"
    try:
        observed_git_control_digest = _read_only_git_control_digest(worktree)
    except (OSError, ValueError):
        return "could not inspect read-only repository report Git controls"
    if observed_git_control_digest != expected_git_control_digest:
        return "read-only repository report Git control metadata changed"
    base_sha = env_str("MAC_TASK_REPO_BASE_SHA") or (
        str(runtime.get("repository_base_sha") or "")
        if isinstance(runtime, dict)
        else ""
    )
    status = _git_for_read_only_verifier(worktree, ["status", "--porcelain"])
    if status.returncode != 0:
        return "could not inspect read-only repository report worktree status"
    if status.stdout.strip():
        return "read-only repository report mutated repository files"
    head = _git_for_read_only_verifier(worktree, ["rev-parse", "HEAD"])
    if head.returncode != 0 or not base_sha or head.stdout.strip() != base_sha:
        return "read-only repository report changed repository HEAD"
    base_tree = env_str("MAC_TASK_REPO_BASE_TREE") or (
        str(runtime.get("repository_base_tree") or "")
        if isinstance(runtime, dict)
        else ""
    )
    tree = _git_for_read_only_verifier(worktree, ["rev-parse", "HEAD^{tree}"])
    if tree.returncode != 0 or not base_tree or tree.stdout.strip() != base_tree:
        return "read-only repository report changed repository tree"
    expected_refs_digest = env_str("MAC_TASK_REPO_REFS_DIGEST") or (
        str(runtime.get("repository_refs_digest") or "")
        if isinstance(runtime, dict)
        else ""
    )
    refs = _git_for_read_only_verifier(
        worktree, ["for-each-ref", "--format=%(refname) %(objectname)"]
    )
    observed_refs_digest = (
        hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest()
        if refs.returncode == 0
        else ""
    )
    if (
        refs.returncode != 0
        or not expected_refs_digest
        or observed_refs_digest != expected_refs_digest
    ):
        return "read-only repository report changed repository refs"
    remotes = _git_for_read_only_verifier(worktree, ["remote"])
    if remotes.returncode != 0 or remotes.stdout.strip():
        return "read-only repository report checkout retained a publication remote"
    # A repository-owned validation command may leave ignored build artifacts.
    # The clean status above proves there are no tracked or untracked edits, so
    # it is safe to remove only the remaining disposable/ignored output while
    # preserving the generated CodeGraph analysis cache.
    cleaned = _git_for_read_only_verifier(
        worktree, ["clean", "-fdx", "-e", ".codegraph/"]
    )
    if cleaned.returncode != 0:
        return "could not clean read-only repository report disposable outputs"
    expected_content_digest = env_str("MAC_TASK_REPO_CONTENT_DIGEST") or (
        str(runtime.get("repository_content_digest") or "")
        if isinstance(runtime, dict)
        else ""
    )
    try:
        observed_content_digest = read_only_repository_content_digest(worktree)
    except OSError:
        observed_content_digest = ""
    if (
        not expected_content_digest
        or observed_content_digest != expected_content_digest
    ):
        return "read-only repository report changed repository content"
    return ""


def _write_read_only_report_violation_manifest(
    task_workspace: Path,
    task: Any,
    detail: str,
    *,
    verification_failure: bool = False,
) -> None:
    summary = (
        "Read-only repository contract verification failed."
        if verification_failure
        else "Read-only repository analysis violated its access contract."
    )
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "invalid",
        "evidence_type": "operator_result",
        "summary": summary,
        "result": detail,
        "problems": [detail],
        "task": {
            "id": task.get("id") if isinstance(task, dict) else None,
            "title": task.get("title") if isinstance(task, dict) else None,
            "project": task.get("project") if isinstance(task, dict) else None,
        },
        "repository_access": {
            "schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
            "mode": REPORT_REPOSITORY_READ_ONLY_MODE,
        },
    }
    (task_workspace / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_executor(
    *,
    runner: Callable[..., Any],
    task: Any,
    task_file: Path,
    task_workspace: Path,
    task_id: Any,
    review_context: Any,
    is_review: bool,
) -> int:
    """Inner executor body extracted so the relay scope wraps the whole run."""
    started = time.monotonic()
    break_glass_authorization = _validated_host_break_glass_authorization(task)
    # Planning-phase flag — determined after the scope estimate below.
    _is_planning = False
    if is_review:
        # Memory feed (in): recall prior deployment lessons (and this task's own
        # prior-attempt outcomes) so the reviewer works with the fleet's
        # hindsight, mirroring the task-execution path. Best-effort — never
        # blocks the run.
        prior_attempt = recall_prior_attempt_lessons(task)
        project_lessons = recall_deployment_lessons(task)
        lessons: List[str] = prior_attempt + [
            lesson for lesson in project_lessons if lesson not in prior_attempt
        ]
        prompt = build_review_prompt(task, task_workspace, review_context, lessons)
    else:
        # Memory feed (in): recall prior deployment lessons so the agent works
        # with the fleet's hindsight. Best-effort — never blocks the run. On a
        # retry, lead with THIS task's own prior-attempt outcome (exact match,
        # highest-value hindsight) before the project-wide lessons.
        prior_attempt = recall_prior_attempt_lessons(task)
        project_lessons = recall_deployment_lessons(task)
        lessons = prior_attempt + [
            lesson for lesson in project_lessons if lesson not in prior_attempt
        ]
        # Prompt is built after planning-phase decision below.
        prompt = ""
    emit_telemetry(
        "started",
        task_id=task_id,
        kind="review" if is_review else "task",
        recalled_lessons=len(lessons),
        sandboxed=_openshell_enabled() and break_glass_authorization is None,
        execution_boundary=(
            "host" if break_glass_authorization is not None else "sandbox"
        ),
        break_glass_authorization_id=(
            break_glass_authorization.get("id")
            if break_glass_authorization is not None
            else None
        ),
    )

    # Scope-estimate preflight (scope-01): on the FIRST attempt of a non-review
    # task, compute a deterministic scope estimate and record it as
    # metadata.scope_estimate on the hub.  Best-effort — never blocks the run.
    if not is_review:
        try:
            estimate = maybe_preflight_scope_estimate(task)
            if estimate is not None:
                emit_telemetry(
                    "scope_estimated",
                    task_id=task_id,
                    size=estimate.get("size"),
                    estimated_units=estimate.get("estimated_units"),
                    signals=estimate.get("signals", []),
                )
                # Merge the just-computed estimate into the local task dict so
                # is_planning_phase() can read it without another hub round-trip.
                metadata_local = task.get("metadata")
                if not isinstance(metadata_local, dict):
                    metadata_local = {}
                    task["metadata"] = metadata_local  # type: ignore[index]
                metadata_local.setdefault("scope_estimate", estimate)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("scope estimate preflight failed: %s\n" % exc)

    # Planning-phase execution (plan-01): when scope_estimate=large or
    # metadata.plan_first=true, the first run PLANS instead of executing.
    if not is_review:
        try:
            _is_planning = is_planning_phase(task)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("planning phase check failed: %s\n" % exc)
            _is_planning = False

    if not is_review:
        if _is_planning:
            # plan-learn-01: enrich the planning prompt with prior decomposition
            # shapes for similar tasks so the second big migration starts from
            # the first one's shape.  Best-effort — never blocks the run.
            try:
                plan_lessons = recall_plan_lessons(task)
            except Exception:  # noqa: BLE001
                plan_lessons = []
            combined_lessons = (lessons or []) + (plan_lessons or [])
            prompt = build_planning_prompt(task, task_file, combined_lessons)
            emit_telemetry("planning_phase_started", task_id=task_id, level="info")
        else:
            prompt = build_task_prompt(task, task_file, lessons)

    if break_glass_authorization is not None:
        if is_review:
            raise RuntimeError("review tasks cannot execute through host break-glass")
        prompt += _break_glass_prompt(break_glass_authorization)

    audit_task_id = review_context.get("task_id") if is_review else task_id
    assignment = _review_experiment_assignment(task) if is_review else {}
    blind_protocol_failed = False
    if assignment.get("blind"):
        executor_evidence = task_workspace / "executor-evidence.json"
        legacy_withheld_evidence = (
            task_workspace / ".mac-withheld-executor-evidence.json"
        )
        independent_findings = task_workspace / "review-independent-findings.json"
        evidence_hidden = False
        evidence_payload: Optional[bytes] = None
        discovery_started = time.monotonic()
        if legacy_withheld_evidence.exists():
            if not executor_evidence.exists():
                legacy_withheld_evidence.replace(executor_evidence)
            else:
                legacy_withheld_evidence.unlink()
        if independent_findings.exists():
            independent_findings.replace(
                task_workspace / "review-independent-findings.previous.json"
            )
        if executor_evidence.exists():
            # Hold the bounded evidence payload in the host process rather than
            # renaming it inside the workspace. A dotfile in the workspace is
            # still visible to both direct and OpenShell agent invocations.
            evidence_payload = executor_evidence.read_bytes()
            executor_evidence.unlink()
            evidence_hidden = True
        try:
            discovery_result = _invoke_agent(
                runner,
                build_blind_review_discovery_prompt(task, task_workspace, assignment),
                task_workspace,
                str(audit_task_id) if audit_task_id else None,
                {
                    "execution_kind": "review_discovery",
                    "timeout": _agent_timeout(),
                    "task": task,
                },
            )
        finally:
            if evidence_payload is not None:
                executor_evidence.write_bytes(evidence_payload)
        discovery_duration_ms = (time.monotonic() - discovery_started) * 1000.0
        discovery_manifest = task_workspace / "mac-evidence.json"
        if discovery_manifest.exists():
            discovery_manifest.replace(
                task_workspace / "review-independent-draft-evidence.json"
            )
        protocol = _blind_review_protocol(
            task_workspace,
            assignment,
            discovery_result,
            duration_ms=discovery_duration_ms,
            evidence_hidden=evidence_hidden,
        )
        (task_workspace / "review-protocol.json").write_text(
            json.dumps(protocol, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        emit_telemetry(
            "review_discovery_completed",
            task_id=task_id,
            returncode=discovery_result.returncode,
            protocol_compliant=protocol["protocol_compliant"],
            findings=protocol["independent_findings_count"],
            duration_ms=discovery_duration_ms,
        )
        blind_protocol_failed = not bool(protocol["protocol_compliant"])

    if blind_protocol_failed:
        # The blind treatment is already invalid. Running adjudication would
        # spend a second model budget on a sample that can no longer be used,
        # and historically allowed a missing discovery artifact to masquerade
        # as a semantic code rejection. Preserve the discovery output and make
        # the review attempt fail distinctly so reviewer selection can retry or
        # choose another eligible reviewer without re-executing the patch.
        result = subprocess.CompletedProcess(
            getattr(discovery_result, "args", ["review_discovery"]),
            65,
            getattr(discovery_result, "stdout", "") or "",
            "\n".join(
                part
                for part in (
                    (getattr(discovery_result, "stderr", "") or "").strip(),
                    "blind review discovery protocol was not completed",
                )
                if part
            ),
        )
        emit_telemetry(
            "review_protocol_failed",
            task_id=task_id,
            level="warning",
            phase="discovery",
            protocol_compliant=False,
        )
    else:
        result = _invoke_agent(
            runner,
            prompt,
            task_workspace,
            str(audit_task_id) if audit_task_id else None,
            {"execution_kind": "review" if is_review else "task", "timeout": _agent_timeout(), "task": task},
        )
    emit_telemetry(
        "agent_completed",
        task_id=task_id,
        returncode=result.returncode,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )
    read_only_violation = str(
        getattr(result, "mac_read_only_repository_violation", "") or ""
    ).strip() or _read_only_report_repository_violation(
        task,
        str(getattr(result, "mac_read_only_git_control_digest", "") or ""),
    )
    read_only_verification_failure = bool(
        getattr(result, "mac_read_only_verification_failure", False)
    )
    authoritative_read_only_failure = bool(
        read_only_violation or read_only_verification_failure
    )
    if read_only_violation:
        result = subprocess.CompletedProcess(
            getattr(result, "args", ["read_only_repository_report"]),
            66,
            getattr(result, "stdout", "") or "",
            "\n".join(
                part
                for part in (
                    (getattr(result, "stderr", "") or "").strip(),
                    read_only_violation,
                )
                if part
            ),
        )
        _write_read_only_report_violation_manifest(
            task_workspace,
            task,
            read_only_violation,
        )
        emit_telemetry(
            "read_only_repository_violation",
            task_id=task_id,
            level="warning",
            detail=read_only_violation,
        )
    elif read_only_verification_failure:
        detail = "read-only repository contract verification failed"
        result = subprocess.CompletedProcess(
            getattr(result, "args", ["read_only_repository_report"]),
            67,
            getattr(result, "stdout", "") or "",
            "\n".join(
                part
                for part in (
                    (getattr(result, "stderr", "") or "").strip(),
                    detail,
                )
                if part
            ),
        )
        # Once the repository-owned contract gate fails, any model-written
        # complete manifest is untrusted. Replace it authoritatively before the
        # generic evidence-salvage path can observe it.
        _write_read_only_report_violation_manifest(
            task_workspace,
            task,
            detail,
            verification_failure=True,
        )
        emit_telemetry(
            "read_only_repository_verification_failed",
            task_id=task_id,
            level="warning",
            detail=detail,
        )
    clean_agent_failure = bool(
        getattr(result, "mac_clean_agent_failure", False)
    )

    if clean_agent_failure:
        emit_telemetry(
            "executor_finalization_skipped",
            task_id=task_id,
            level="warning",
            reason="clean_agent_failure",
            returncode=result.returncode,
        )
    elif not is_review and (
        metadata_declares_report_deliverable(
            task.get("metadata") if isinstance(task, dict) else None
        )
        or task_evidence_type(task) in NON_REPOSITORY_OUTCOME_EVIDENCE_TYPES
    ):
        emit_telemetry(
            "executor_finalization_skipped",
            task_id=task_id,
            level="info",
            reason=(
                "report_deliverable"
                if metadata_declares_report_deliverable(
                    task.get("metadata") if isinstance(task, dict) else None
                )
                else task_evidence_type(task)
            ),
            returncode=result.returncode,
        )
    elif not is_review:
        try:
            # Planning-phase runs produce evidence_type=plan_decomposed, not a
            # repo change.  Skip the git finalizer so a clean worktree is not
            # treated as a failure.  The agent is responsible for posting the
            # children and writing the plan manifest.
            if _is_planning and is_plan_decomposed_evidence(task_workspace):
                emit_telemetry("planning_phase_completed", task_id=task_id, level="info")
                # plan-learn-01: record this plan outcome so future planning
                # runs on similar tasks can recall the decomposition shape.
                try:
                    record_plan_outcome(
                        task,
                        task_workspace,
                        wall_clock_ms=(time.monotonic() - started) * 1000.0,
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                finalize_with_new_file_recovery(task_workspace, task, task_id)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("git finalizer failed: %s\n" % exc)
    elif not blind_protocol_failed:
        try:
            run_deterministic_review_verdict(task_workspace, task, review_context)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("review verdict finalizer failed: %s\n" % exc)

    # Task-sizing: if the agent wrote plan_steps in its evidence, auto-post them
    # as child tasks so the parent blocks on the children.  Best-effort.
    if (
        not is_review
        and not clean_agent_failure
        and not authoritative_read_only_failure
    ):
        try:
            decomposed = maybe_auto_decompose(task_workspace, task)
            if decomposed:
                emit_telemetry("plan_decomposed", task_id=task_id, level="info")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("auto-decompose failed: %s\n" % exc)

    write_fallback_evidence_manifest(task_workspace, task, result, review_context)

    # loop-01 resilience: if the run was bounded/failed (e.g. a wedged TokenHub
    # trailing turn) but the agent or a deterministic finalizer already wrote a
    # complete, typed manifest, don't discard that verified work — finalize as
    # success. The downstream verification gate still validates the content.
    rc = result.returncode
    if (
        rc != 0
        and not authoritative_read_only_failure
        and _manifest_is_complete(task_workspace)
    ):
        emit_telemetry("evidence_salvaged", task_id=task_id, level="warning", original_returncode=rc)
        rc = 0

    # Memory feed (out): distill this run's outcome into a deployment lesson the
    # nap consolidator will promote into the vector tier — so the fleet's recall
    # gets richer with every task. Reviews don't feed deployment lessons.
    if not is_review:
        outcome = classify_outcome(task_workspace, task, rc)
        emit_telemetry(
            "finalized",
            task_id=task_id,
            level="info" if outcome["outcome"] == "success" else "warning",
            evidence_type=outcome["evidence_type"],
            outcome=outcome["outcome"],
            signals=outcome["signals"],
        )
        with _FinalizerPhaseContext(
            task_workspace,
            task_id,
            "lesson_curation",
        ):
            record_deployment_learning(task, outcome)
            record_curated_lessons(task, outcome)
            # recovery-learn-01: if mid-flight recoveries occurred, feed each
            # choice+outcome into the deployment-learning loop so selection
            # quality improves future recovery choices.
            try:
                _record_recovery_learnings(task_workspace, task, outcome)
            except Exception:  # noqa: BLE001
                pass

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
