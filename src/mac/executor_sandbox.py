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
  ``deployment_learning`` memory from the outcome, so recall improves with
  every task the fleet completes.
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

import atexit
import base64
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
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from mac import mac_paths
from mac import relay_observability
from mac.agent_command import PROMPT_SENTINEL
from mac.sandbox_egress import classify_egress_hosts, expand_policy_text
from mac.models import (
    NON_REPOSITORY_OUTCOME_EVIDENCE_TYPES,
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    REPORT_REPOSITORY_HOST_INSTALL_PLATFORMS,
    REPORT_REPOSITORY_LINUX_POSTURE,
    REPORT_REPOSITORY_MACOS_HOST_POSTURE,
    REPORT_REPOSITORY_READ_ONLY_MODE,
    metadata_declares_read_only_report_repository,
    metadata_declares_report_deliverable,
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
    assert_exec_argv_single_line,
    openshell_create_keepalive_args,
    openshell_required_for_local_agent as _openshell_required_for_local_agent,
    single_line_shell_script,
    split_sandbox_create_command,
    truthy as _truthy,
    verifier_resource_profile,
    verifier_profile_create_args,
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
from mac.prompt_master import compile_prompt
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
    hub_write_capability,
    _plan_detection_section,
)
from mac.executor_memory import (  # noqa: E402,F401 - compatibility re-exports
    DEPLOYMENT_LEARNING_PREFIX,
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
    emit_telemetry,
    recall_deployment_lessons,
    recall_plan_lessons,
    recall_prior_attempt_lessons,
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
    planning_phase_skip_notice,
    recall_scope_lessons,
    record_scope_estimate,
    reject_empty_plan_decomposed_evidence,
    should_enter_planning_phase,
)
from mac.executor_prompt import (  # noqa: E402,F401 - compatibility re-exports
    _cooperative_integration_section,
    _error_signature,
    _is_truthy,
    _is_untracked_new_files_refusal,
    _repository_bootstrap_timeout,
    _repository_contract_bootstrap,
    _repository_contract_canonical_branch,
    _repository_contract_canonical_remote,
    _repository_contract_test_command,
    _repository_lease_id,
    _repository_prepared_base,
    _repository_publication_remote,
    _repository_task_branch,
    _run_repository_bootstrap_if_needed,
    _run_captured,
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
    _new_file_finalize_message,
    _preserve_executor_state_before_refusal,
    _read_executor_evidence_payload,
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


def post_task_transcript(task_id, payload: Dict[str, Any]) -> None:
    """Send one coding-CLI turn to the hub, best effort.

    Best effort ON PURPOSE: the transcript is a record of work, not the work.
    A hub that rejects or drops it must not fail a task whose code changes are
    already correct -- that would trade a complete audit trail for lost output,
    which is the wrong way round.
    """
    if not task_id:
        return
    try:
        _hub_post("/tasks/%s/transcript" % task_id, payload)
    except Exception as exc:  # noqa: BLE001 - never fail the run over bookkeeping
        sys.stderr.write("[executor] WARNING: transcript not recorded: %s\n" % exc)


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
        return subprocess.CompletedProcess(
            argv, 124, out, err + "\n[executor] agent run timed out after %ss" % timeout
        )
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
    # Keep the exchange itself, not just its fingerprint. The audit record below
    # stores sha256(stdout) and a byte count, which proves an output existed and
    # supports nothing else -- no summary, no knowledge base, no answering "why
    # did the agent do that". The prompt is the LAST argv element of
    # `opencode run`, and it never reaches the audit record because
    # audit_safe_argv truncates anything over 512 chars.
    post_task_transcript(
        task_id,
        {
            "prompt": argv[-1] if argv else "",
            "response": stdout,
            "stderr": stderr,
            "agent_id": agent_id,
            "command_id": command_id,
            "coding_agent": str((metadata or {}).get("coding_agent") or "") or None,
            "model": str((metadata or {}).get("model") or "") or None,
            "returncode": result.returncode,
            "started_at": started_at,
            "completed_at": utcnow(),
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "metadata": {"argv_sha256": argv_hash},
        },
    )
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
        (Path(__file__).resolve().parent / "executor-policy.txt").read_text(encoding="utf-8"),
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
    "OPENAI_BASE_URL,OPENAI_API_KEY,"
    # The coding CLI (opencode) authenticates with the per-task
    # MAC_INFERENCE_TOKEN written into the private environment file, not with
    # a provider key forwarded from the host.
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
    item.strip() for item in _DEFAULT_OPENSHELL_ENV_PASSTHROUGH.split(",") if item.strip()
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
    requested = {item.strip() for item in names.split(",") if item.strip()}
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
                "the sandbox image and repository toolchain own command resolution" % name
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
    # What the sandbox gets instead: this task's inference-only token, which
    # reaches the hub's model router and nothing else (mac.inference_tokens).
    inference_token = os.environ.get(_INFERENCE_TOKEN_ENV)
    if inference_token:
        values[_INFERENCE_TOKEN_ENV] = inference_token
    if read_only_repository:
        fence_read_only_repository_environment(values)
    return values


# ---------------------------------------------------------------------------
# Per-task inference token and the opencode router config
# ---------------------------------------------------------------------------
_INFERENCE_TOKEN_ENV = "MAC_INFERENCE_TOKEN"
_OPENCODE_CONFIG_FILENAME = ".mac-opencode.json"
#: A preflight probe is one short completion; its token lives minutes, not hours.
_PREFLIGHT_INFERENCE_TOKEN_TTL_SECONDS = 15 * 60
#: This executor process's task token: {"id": ..., "token": ...}. One executor
#: process runs one task, so this is the task's token.
_TASK_INFERENCE_TOKEN: Dict[str, str] = {}


def _uses_router_opencode(choice: Any) -> bool:
    """Whether the route authenticates to the hub router with a task token.

    Both coding CLIs do: opencode through /v1/chat/completions and Claude
    Code through /v1/messages. (The name predates Claude Code.)
    """
    return getattr(choice, "agent", "") in ("opencode", "claude") and (
        getattr(choice, "provider", "") == "mac-router"
    )


#: The image's Python, which the sandbox policy lets reach the hub. Claude
#: Code's hooks run under it.
_SANDBOX_AGENT_PYTHON = "/opt/mac-venv/bin/python"


def _write_claude_agent_files(
    directory: Path, config_directory: str, env_values: Mapping[str, str], *, python: str
) -> Dict[str, str]:
    """Write Claude Code's settings, hooks and board command; return its env.

    Everything goes under ``.mac-agent/`` in the task workspace, which sits
    outside the repository, so none of it can be committed. The hook script is
    a copy of :mod:`mac.claude_hooks` (standard library only), so the hooks are
    this MAC version's even inside an older sandbox image. Claude Code's own
    state (``CLAUDE_CONFIG_DIR``, including the session transcript) lives
    there too, so it comes back with the workspace and a later run can resume
    the session. Nothing is written without an inference token and a hub URL.
    """
    from . import coding_agent as _ca

    token = str(env_values.get(_INFERENCE_TOKEN_ENV) or "")
    hub = _ca.router_hub_url(env_values)
    if not token or not hub:
        return {}
    agent_dir = directory / _ca.CLAUDE_AGENT_DIR
    (agent_dir / "state").mkdir(parents=True, exist_ok=True)
    (agent_dir / "claude").mkdir(parents=True, exist_ok=True)
    hooks_source = Path(__file__).resolve().parent / "claude_hooks.py"
    (agent_dir / "claude_hooks.py").write_text(hooks_source.read_text(encoding="utf-8"), encoding="utf-8")
    board = agent_dir / "board"
    board.write_text(
        "#!/bin/sh\n"
        'exec "${MAC_AGENT_PYTHON:-python3}" "$(dirname "$0")/claude_hooks.py" board "$@"\n',
        encoding="utf-8",
    )
    board.chmod(0o755)

    def _hook(event: str) -> Dict[str, Any]:
        return {
            "type": "command",
            "command": '"$MAC_AGENT_PYTHON" "$MAC_AGENT_DIR/claude_hooks.py" %s' % event,
            "timeout": 30,
        }

    settings = {
        "hooks": {
            "SessionStart": [{"hooks": [_hook("session-start")]}],
            "PostToolUse": [{"matcher": "*", "hooks": [_hook("post-tool")]}],
            "Stop": [{"hooks": [_hook("stop")]}],
        }
    }
    settings_path = directory / _ca.CLAUDE_SETTINGS_FILE
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    settings_path.chmod(0o600)
    sandbox_agent_dir = "%s/%s" % (config_directory.rstrip("/"), _ca.CLAUDE_AGENT_DIR)
    overlay = {
        # Claude Code appends /v1/messages itself.
        "ANTHROPIC_BASE_URL": hub,
        "ANTHROPIC_AUTH_TOKEN": token,
        "CLAUDE_CONFIG_DIR": sandbox_agent_dir + "/claude",
        "MAC_AGENT_DIR": sandbox_agent_dir,
        "MAC_AGENT_STATE_DIR": sandbox_agent_dir + "/state",
        "MAC_AGENT_PYTHON": python,
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    task_id = str(env_values.get("MAC_TASK_ID") or "").strip()
    if task_id:
        overlay["ANTHROPIC_CUSTOM_HEADERS"] = "X-MAC-Task-ID: %s" % task_id
    return overlay


def _write_coding_agent_config(
    directory: Path, config_directory: str, env_values: Mapping[str, str], *, python: str
) -> Dict[str, str]:
    """Write the selected coding CLI's router config; return its env overlay."""
    from . import coding_agent as _ca

    if _ca.selected_agent() == _ca.CLAUDE_AGENT:
        return _write_claude_agent_files(directory, config_directory, env_values, python=python)
    return _write_opencode_router_config(directory, config_directory, env_values)


def _mint_inference_token(*, task_id: str, ttl_seconds: int) -> Dict[str, Any]:
    """Ask the hub, as this worker, for an inference-only token bound to it."""
    from mac.inference_tokens import request_inference_token

    base_url, worker_token = _hub_env()
    if not base_url or not worker_token:
        raise RuntimeError("no hub URL or worker token to mint an inference token with")
    return request_inference_token(
        base_url,
        worker_token,
        local_agent_id(),
        task_id=task_id,
        ttl_seconds=ttl_seconds,
    )


def _revoke_inference_token(token_id: str) -> None:
    """Best effort: expiry still ends the token if the hub cannot be reached."""
    from mac.inference_tokens import revoke_inference_token

    base_url, worker_token = _hub_env()
    if not token_id or not base_url or not worker_token:
        return
    try:
        revoke_inference_token(base_url, worker_token, local_agent_id(), token_id)
    except Exception as exc:  # noqa: BLE001 - revocation must never fail a task
        sys.stderr.write(
            "[executor] inference token %s not revoked (%s); it expires on its own\n"
            % (token_id, exc.__class__.__name__)
        )


def _ensure_task_inference_token(task_id: str) -> None:
    """Mint this task's inference token once and expose it to the sandbox env."""
    from mac.inference_tokens import DEFAULT_TTL_SECONDS

    if _TASK_INFERENCE_TOKEN.get("token"):
        return
    issued = _mint_inference_token(task_id=task_id, ttl_seconds=DEFAULT_TTL_SECONDS)
    _TASK_INFERENCE_TOKEN.update(id=str(issued.get("id") or ""), token=str(issued["token"]))
    os.environ[_INFERENCE_TOKEN_ENV] = _TASK_INFERENCE_TOKEN["token"]


def revoke_task_inference_token() -> None:
    """Revoke the task's inference token once the task has finished."""
    token_id = _TASK_INFERENCE_TOKEN.get("id") or ""
    _TASK_INFERENCE_TOKEN.clear()
    os.environ.pop(_INFERENCE_TOKEN_ENV, None)
    _revoke_inference_token(token_id)


def host_opencode_router_env(
    directory: Path, *, task_id: str, ttl_seconds: int
) -> Tuple[Dict[str, str], str]:
    """Mint a token and write the router config for opencode run on the HOST.

    Returns the environment overlay (``MAC_INFERENCE_TOKEN`` and
    ``OPENCODE_CONFIG``) and the token id for revocation. Used by the
    host-install route probe, which has no sandbox to hand the token to.
    """
    issued = _mint_inference_token(task_id=task_id, ttl_seconds=ttl_seconds)
    overlay = {_INFERENCE_TOKEN_ENV: str(issued["token"])}
    overlay.update(
        _write_opencode_router_config(directory, str(directory), {**os.environ, **overlay})
    )
    return overlay, str(issued.get("id") or "")


def _write_opencode_router_config(
    directory: Path, config_directory: str, env_values: Mapping[str, str]
) -> Dict[str, str]:
    """Write the ``machub`` opencode config and return ``OPENCODE_CONFIG`` for it.

    ``directory`` is where the file is written; ``config_directory`` is the
    same directory as the CLI will see it (the sandbox path when uploaded).
    Nothing is written without an inference token and a hub URL. The file
    holds no secret: the API key is an ``{env:MAC_INFERENCE_TOKEN}`` reference.
    """
    from . import coding_agent as _ca

    if not env_values.get(_INFERENCE_TOKEN_ENV) or not _ca.router_hub_url(env_values):
        return {}
    path = directory / _OPENCODE_CONFIG_FILENAME
    path.write_text(
        json.dumps(_ca.opencode_router_config(env_values), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return {"OPENCODE_CONFIG": "%s/%s" % (config_directory.rstrip("/"), _OPENCODE_CONFIG_FILENAME)}


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
    """Resolve the BASE policy passed to ``openshell sandbox create``.

    Resolution order (first hit wins):
      1. ``MAC_OPENSHELL_POLICY`` (explicit) — must exist, else raise.
      2. ``~/.mac/openshell-policy.yaml`` — the operator-filled fleet policy.
      3. the package's bundled fail-closed default (``openshell/default-policy.yaml``).

    A policy is *always* returned (or we raise) — the wrap never omits
    ``--policy``, so enabling sandboxing can never silently fall back to
    OpenShell's own image-default profile. The bundled default denies all
    network egress, so an unconfigured deployment fails closed (tasks can't
    reach the hub/gateway) rather than running under an unknown profile.

    This is the host-wide floor. Per-task widening (ADR 0009 §2a) is layered on
    top by :func:`_resolve_task_openshell_policy`, which only ever appends.
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


# --- Per-repo egress expansion (ADR 0009 §2a) -------------------------------
# Default OFF. With MAC_OPENSHELL_TASK_EGRESS unset the base policy is passed
# through byte for byte and behaviour is exactly as before, so enabling the
# expansion is a deliberate operator act rather than something a repo can
# trigger by adding a lockfile.
_EXPANDED_POLICY_FILES: List[Path] = []
#: Rendered policies to keep on disk. The file must outlive
#: ``_build_sandbox_create_argv`` (OpenShell reads it during ``sandbox create``)
#: so it cannot be deleted inline, and the worker is a long-lived run loop — so
#: without a bound this would leak one file per task for the process lifetime.
#: A small window keeps the previous runs around for post-mortem diffing.
_EXPANDED_POLICY_RETAIN = 4


def _cleanup_expanded_policies(*, retain: int = 0) -> None:
    """Remove rendered per-task policies, keeping the newest ``retain``.

    The files are 0600 and secret-free, but they name the fleet's hub/gateway
    hosts; leaving one per task run in the OS temp dir is needless residue.
    """
    while len(_EXPANDED_POLICY_FILES) > retain:
        try:
            _EXPANDED_POLICY_FILES.pop(0).unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup_expanded_policies)


def _task_egress_proposals(task: Any) -> Tuple[List[Any], List[Any]]:
    """Return ``(derived, declared)`` egress host proposals for one task.

    ``derived`` comes from the environment contract the worker computed by
    statically analysing the repository worktree — repo content, therefore
    untrusted (see :mod:`mac.sandbox_egress`). It lives under
    ``metadata.runtime`` because that whole subtree is worker-written.

    ``declared`` is read from ``metadata.egress_contract``, a TOP-LEVEL task
    metadata key. The distinction is the point: the worker writes
    ``metadata.runtime`` locally, so anything under it carries only repo trust,
    whereas top-level task metadata was set through an authenticated hub
    credential at task creation. Never move the declared list under ``runtime``.
    """
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return [], []

    derived: List[Any] = []
    runtime = metadata.get("runtime")
    if isinstance(runtime, dict):
        contract = runtime.get("environment_contract")
        if isinstance(contract, dict):
            egress = contract.get("egress")
            if isinstance(egress, dict):
                hosts = egress.get("hosts")
                if isinstance(hosts, list):
                    derived = list(hosts)

    declared: List[Any] = []
    contract_block = metadata.get("egress_contract")
    if isinstance(contract_block, dict):
        hosts = contract_block.get("hosts")
        if isinstance(hosts, list):
            declared = list(hosts)

    return derived, declared


def _egress_policy_binaries() -> List[str]:
    """Binary paths permitted to open the per-repo egress sockets.

    Package fetches are performed by the language runtimes, matching the
    ``node_packages``/``python_packages`` blocks in the operator template. The
    coding-agent CLIs are deliberately EXCLUDED: the agent's own model egress is
    already scoped by its provider block, and adding it here would let a --yolo
    agent reach repo-declared hosts directly rather than only via the build.
    """
    return [
        "/usr/bin/node",
        "/usr/local/bin/node",
        "/usr/bin/npm",
        "/usr/local/bin/npm",
        "/usr/bin/npx",
        "/usr/local/bin/npx",
        "/usr/bin/corepack",
        "/usr/local/bin/corepack",
        "/usr/bin/python3",
        "/usr/local/bin/python3",
        "/usr/local/bin/python",
        "/usr/bin/git",
        "/usr/bin/curl",
    ]


def _resolve_task_openshell_policy(task: Any) -> str:
    """Resolve the policy for one task, widening egress per ADR 0009 §2a.

    Returns the base policy path unchanged unless ALL of the following hold:
    expansion is enabled, the task is not a read-only repository report, and at
    least one proposed host survives classification. Any failure to render falls
    back to the unexpanded base policy — a task that cannot widen its egress
    fails the way it did before this feature existed (a denied fetch), which is
    strictly safer than failing open or aborting the run.
    """
    base = _resolve_openshell_policy()
    if not env_bool("MAC_OPENSHELL_TASK_EGRESS"):
        return base

    task_metadata = task.get("metadata") if isinstance(task, dict) else None
    if metadata_declares_read_only_report_repository(task_metadata):
        # A read-only report attests policy_sha256 of the exact policy it ran
        # under, and the hub only projects the dispatch marker when that
        # attestation matches the admin-approved tuple. Rendering a per-task
        # policy would change the digest and invalidate the attestation, so this
        # task class keeps the host policy verbatim.
        return base

    derived, declared = _task_egress_proposals(task)
    if not derived and not declared:
        return base

    decision = classify_egress_hosts(derived=derived, declared=declared)
    task_id = str(task.get("id") or "") if isinstance(task, dict) else ""
    try:
        emit_telemetry(
            "sandbox_egress_decision",
            task_id=task_id or None,
            level="info",
            **decision.to_dict(),
        )
    except Exception:  # noqa: BLE001 - telemetry must never break execution
        pass
    if decision.rejected:
        # Loud on stderr as well as in telemetry: a denied fetch is otherwise
        # diagnosed as a flaky network several runs later.
        sys.stderr.write(
            "[executor] per-repo egress: %d host(s) granted, %d refused (%s)\n"
            % (
                len(decision.granted),
                len(decision.rejected),
                "; ".join("%s: %s" % (item.host, item.reason) for item in decision.rejected[:5]),
            )
        )
    if decision.is_empty:
        return base

    try:
        base_text = Path(base).read_text(encoding="utf-8")
        expanded = expand_policy_text(base_text, decision, binaries=_egress_policy_binaries())
        handle, raw_path = tempfile.mkstemp(prefix="mac-task-policy-", suffix=".yaml")
        os.close(handle)
        path = Path(raw_path)
        path.write_text(expanded, encoding="utf-8")
        path.chmod(0o600)
        _EXPANDED_POLICY_FILES.append(path)
        _cleanup_expanded_policies(retain=_EXPANDED_POLICY_RETAIN)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "[executor] per-repo egress expansion failed (%s); using base policy\n" % exc
        )
        return base
    sys.stderr.write("[executor] per-repo egress: granted %s\n" % ", ".join(decision.granted_hosts))
    return str(path)


# OpenShell sandboxes are container copies with NO bind-mount, so the task git
# worktree must be UPLOADED in and the agent's results DOWNLOADED back out — a
# plain ``create -- argv`` would run the agent against an empty /sandbox and lose
# its edits + evidence on teardown. The run is therefore a lifecycle:
#   create (--upload workspace, kept alive) -> exec agent -> download -> delete.
# Create and agent are separate steps: OpenShell 0.1 rejects --upload with a
# command, and a create command would be the main process whose exit ends Ready.
# ``include_workdir`` in the policy only grants Landlock access to the path; it
# does not copy files. /sandbox is OpenShell's writable workspace root (uploads
# and downloads must live under it).
_SANDBOX_WORKDIR = "/sandbox"
_SANDBOX_HOME = "/tmp"
_SANDBOX_VERIFICATION_FILE = "mac-sandbox-verification.json"
_SANDBOX_VERIFICATION_STARTED_FILE = ".mac-sandbox-verification.started"
_TRUSTED_READ_ONLY_VERIFICATION_FILE = ".mac-trusted-read-only-sandbox-verification.json"
_MAX_SANDBOX_VERIFICATION_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class _SandboxRepositoryVerificationResult:
    """Authoritative outcome of the ordinary repository verifier.

    ``failure_class`` keeps an OpenShell lifecycle failure distinct from a
    repository-owned test failure.  The distinction must survive the sandbox
    boundary: infrastructure failures are retryable by the dispatcher, while a
    real test failure belongs to the task's change.
    """

    passed: bool
    failure_class: str = ""
    detail: str = ""
    retryable: bool = False
    attempt_count: int = 1

    def __iter__(self):
        # Preserve the small internal (ok, message) seam used by focused tests
        # while carrying the structured cause to _run_sandboxed.
        yield self.passed
        yield self.detail

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": "mac.openshell_repository_verification.v1",
            "passed": self.passed,
            "failure_class": self.failure_class,
            "detail": self.detail,
            "retryable": self.retryable,
            "attempt_count": self.attempt_count,
        }


def _openshell_bin() -> str:
    return env_str("MAC_OPENSHELL_BIN") or "openshell"


def _sandbox_name() -> str:
    """A unique name for the kept sandbox so the download + delete steps can
    target it. Overridable via MAC_OPENSHELL_SANDBOX_NAME (debug a single run)."""
    assigned = env_str("MAC_TASK_OPENSHELL_SANDBOX_NAME")
    if assigned:
        if not _re.fullmatch(r"mac-task-[0-9a-f]{8}", assigned):
            raise RuntimeError("invalid controller-owned task sandbox identity")
        return assigned
    explicit = env_str("MAC_OPENSHELL_SANDBOX_NAME")
    if explicit:
        return explicit
    import uuid

    # openshell rejects sandbox names over 19 characters ("name exceeds
    # maximum length (21 > 19)" was observed live with a 12-hex-char
    # suffix, i.e. "mac-task-" (9) + 12 = 21). 8 hex chars keeps the total
    # at 17, under the limit with margin, while still giving 32 bits of
    # randomness -- ample for a per-task ephemeral sandbox name.
    return "mac-task-" + uuid.uuid4().hex[:8]


_OPENSHELL_SANDBOX_NAME_MAX_LENGTH = 19


def _coding_agent_probe_sandbox_name() -> str:
    """A unique name for the throwaway coding-agent preflight probe sandbox.

    "mac-codingcap-<agent>-<12 hex>" was 29-35 chars depending on agent --
    every coding-agent preflight failed to even create its probe sandbox
    with "name exceeds maximum length", surfacing as an opaque
    "probe_failed"/"route verification failed" for every configured CLI.
    The agent/route identity already lives in the sandbox's
    mac.kind=codingcap label, so it isn't needed in the name too.
    """
    import uuid

    return "mac-cc-%s" % uuid.uuid4().hex[:10]


def _read_only_verifier_sandbox_name() -> str:
    """A unique name for the read-only-report second verification sandbox.

    "<task-sandbox-name>-verify-<8 hex>" derived from an already-shortened
    17-char task sandbox name was still 33+ chars. This is a distinct
    sandbox, so it gets its own compact name rather than deriving from the
    parent task sandbox's.
    """
    import uuid

    return "mac-vf-%s" % uuid.uuid4().hex[:10]


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


def _sandbox_label_argv(
    kind: str,
    *,
    keep: bool = False,
    process_identity: Optional[Callable[[int], Tuple[str, str]]] = None,
) -> List[str]:
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
    from .openshell_sandbox_gc import _process_identity

    pid = os.getpid()
    state, identity = (process_identity or _process_identity)(pid)
    labels = [
        "--label",
        "mac.owner=mac",
        "--label",
        "mac.kind=%s" % kind,
        "--label",
        "mac.pid=%d" % pid,
        "--label",
        "mac.pid.identity=%s" % ("verified" if state == "present" else state),
        "--label",
        "mac.keep=%s" % ("true" if keep or repository_wip_guard else "false"),
    ]
    # Process identity strengthens PID reuse detection, but its temporary
    # unavailability must not prevent unrelated sandbox creation. Omitting the
    # pair makes the reaper preserve any live/reused PID, which is fail-closed.
    if state == "present" and ":" in identity:
        boot_id, pid_start = identity.split(":", 1)
        labels += [
            "--label",
            "mac.pid.start=%s" % pid_start,
            "--label",
            "mac.boot.id=%s" % boot_id,
        ]
    return labels + _sandbox_identity_labels()


def _sandbox_gc_best_effort() -> None:
    if not env_bool("MAC_OPENSHELL_GC"):
        return
    try:
        stale_after = float(env_str("MAC_OPENSHELL_STALE_AFTER_SECONDS") or "86400")
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
                "[executor] removed %d stale OpenShell sandbox(es)\n" % len(report["deleted"])
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
        sys.stderr.write("[executor] WARNING: orphaned task sandbox reap failed: %s\n" % exc)
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
    sweep additionally consults the authoritative lease store: a Ready task sandbox whose
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
        sys.stderr.write("[executor] WARNING: lease-authority sandbox reconcile failed: %s\n" % exc)
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


def _sandbox_path_for_workspace_child(
    workspace: Path, sandbox_workspace: str, value: str
) -> Optional[str]:
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

    The managed OpenShell runtime is Linux-only (ADR 0015). macOS nodes run
    the agent as a plain host application and never enable this sandbox, so
    ``MAC_OPENSHELL_SANDBOX`` being set on darwin is a misconfiguration rather
    than a posture to waive."""
    if _kernel_has_landlock() or env_bool("MAC_OPENSHELL_ALLOW_NO_LANDLOCK"):
        return
    if sys.platform == "darwin":
        raise RuntimeError(
            "OpenShell sandboxing is enabled on macOS, but the managed "
            "OpenShell runtime is Linux-only and the macOS host kernel cannot "
            "enforce Landlock. macOS fleet nodes run host installs (isolation "
            "posture macos_host); unset MAC_OPENSHELL_SANDBOX on this node "
            "(see ADR 0015)."
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
    return r"""
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
"""


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
            verifier_resource_profile()[2],
            'if [ -n "${VERIFICATION_START_MARKER:-}" ]; then : > "$VERIFICATION_START_MARKER"; fi',
            _sandbox_toolchain_setup_shell(),
            'cd "$MAC_TASK_WORKSPACE"',
            "mac_sandbox_toolchain_setup || true",
            r'''$MAC_SANDBOX_PYTHON - <<'PY'
import json, os, signal, subprocess, sys, tempfile, time
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
#
# There is deliberately NO "clean tree => pass" shortcut. A clean `git status`
# only means the agent committed its work; reporting that as a pass recorded
# 265 test passes in 90 days where nothing ran. Only a HEAD that never moved
# off the baseline skips the gate, and that is recorded as skipped, not pass.
# --- baseline-resolver (extracted verbatim by tests/test_sandbox_baseline.py) ---
def _resolve_baseline_sha(subprocess, worktree, env_base):
    """The commit the agent started from, as this repository can name it.

    The host's base SHA is preferred, but it is usually absent here: the
    sandbox runs `git init` and makes its own "MAC OpenShell sandbox
    baseline" commit, whose SHA is newly generated and cannot equal the
    host's. Clearing the base on that miss is what silently escalated every
    task to the whole-repo gate.

    The sandbox's own baseline commit IS the pre-task state -- it is exactly
    what was uploaded -- so name it by the message we wrote, which no other
    commit carries. A preserved .git has no such commit and yields "", which
    is the old behaviour and the safe one.
    """

    def _git(*args):
        try:
            return subprocess.run(
                ["git", "-C", worktree, *args],
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            return None

    if env_base:
        probe = _git("cat-file", "-e", env_base + "^{commit}")
        if probe is not None and probe.returncode == 0:
            return env_base
    found = _git(
        "log", "--format=%H", "--fixed-strings",
        "--grep=MAC OpenShell sandbox baseline", "-1",
    )
    if found is not None and found.returncode == 0:
        return (found.stdout or "").strip().splitlines()[0].strip() if (
            found.stdout or ""
        ).strip() else ""
    return ""
# --- end baseline-resolver ---

_repo_base_sha = _resolve_baseline_sha(
    subprocess, worktree, os.environ.get("MAC_TASK_REPO_BASE_SHA", "").strip()
)

def _worktree_is_unchanged_baseline(base):
    """True only when HEAD IS the pre-task baseline and nothing is uncommitted.

    Not "the tree is clean": an agent that committed its work leaves a clean
    tree on top of new commits. Only an unmoved HEAD proves the task changed
    nothing, and even then the record says skipped -- never pass.
    """
    if not base:
        return False
    try:
        head = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        status = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain", "-uall"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return (
        head.returncode == 0
        and head.stdout.strip() == base
        and status.returncode == 0
        and not status.stdout.strip()
    )

_unchanged_baseline = _worktree_is_unchanged_baseline(_repo_base_sha)
if command in ("scripts/run-contract-tests.sh", "./scripts/run-contract-tests.sh") and _repo_base_sha:
    _sanity = os.path.join(worktree, "scripts", "run-sanity-tests.sh")
    if os.path.isfile(_sanity) and os.access(_sanity, os.X_OK):
        command = "scripts/run-sanity-tests.sh --base " + _repo_base_sha
bootstrap_command = os.environ.get("MAC_REPO_BOOTSTRAP_COMMAND", "").strip()

def _gate_log(message):
    """One channel for the gate's own narration.

    Goes to stderr so it survives even when the test command's stdout is
    truncated for evidence, and is prefixed so it can be grepped out of a
    pytest log that is otherwise thousands of lines long.
    """
    sys.stderr.write("[gate] %s\n" % message)
    sys.stderr.flush()

def _effective_timeout(names, fallback=7200.0):
    """Resolve a timeout AND say where it came from.

    Reporting the source is the point. MAC_WORKER_REPOSITORY_TEST_TIMEOUT was
    set to 5400 on the host for three consecutive canary attempts while this
    process enforced 1800, because the variable was never forwarded into the
    sandbox. Every attempt failed with "timed out after 1800.0s" against a
    configuration file that plainly said 5400, and the investigation stopped at
    the configuration each time. A knob that silently does nothing is worse
    than no knob.
    """
    for name in names:
        raw = os.environ.get(name)
        if raw:
            try:
                return float(raw), name
            except ValueError:
                return fallback, "%s=%r unparseable, using default" % (name, raw)
    return fallback, "default (%s unset)" % ", ".join(names)

_test_timeout, _test_timeout_source = _effective_timeout(
    ["MAC_WORKER_REPOSITORY_TEST_TIMEOUT"]
)
_bootstrap_timeout, _bootstrap_timeout_source = _effective_timeout(
    ["MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT", "MAC_WORKER_REPOSITORY_TEST_TIMEOUT"]
)

# The resolved values, at the point that enforces them -- not the point that
# configures them. Everything here has been wrong at least once this month
# while the host-side configuration looked correct.
_gate_log("effective configuration:")
_gate_log("  test command:      %s" % (command or "<missing>"))
_gate_log("  bootstrap command: %s" % (bootstrap_command or "<none>"))
_gate_log("  test timeout:      %.1fs (%s)" % (_test_timeout, _test_timeout_source))
_gate_log("  bootstrap timeout: %.1fs (%s)" % (_bootstrap_timeout, _bootstrap_timeout_source))
_gate_log(
    "  baseline sha:      %s"
    % (_repo_base_sha or "<unresolved -- selection cannot be scoped, expect a full run>")
)
_gate_log("  worktree:          %s" % worktree)
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
            # Same hazard as the verifier launch: a repository test command that
            # reads stdin would block on the supervisor's pipe forever instead of
            # seeing EOF and carrying on.
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", _TC_PATH_PREFIX + command],
                cwd=worktree,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
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
        timeout = _bootstrap_timeout
        _gate_log("phase bootstrap: start (timeout %.1fs)" % timeout)
        returncode, stdout, stderr, timed_out = run_bounded_bash(bootstrap_command, timeout)
        _gate_log(
            "phase bootstrap: %.1fs rc=%s%s"
            % (time.time() - started, returncode, " TIMED OUT" if timed_out else "")
        )
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
elif _unchanged_baseline:
    # The task changed nothing, so there is no change of the task's to judge.
    # Recorded as SKIPPED, not pass: this is not a test result and must never
    # be read as one (a clean-tree "pass" here once stood in for 265 test runs
    # that never happened). Repository changes are verified on the exact
    # commit they publish, by the pre-push verifier, regardless.
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "skipped",
        "command": command,
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "skipped": True,
        "skipped_reason": "HEAD is the uploaded baseline and the worktree is clean",
        "duration_ms": 0,
        "worktree": worktree,
        "environment_delta": delta,
    }
else:
    started = time.time()
    timeout = _test_timeout
    _gate_log("phase tests: start (timeout %.1fs) %s" % (timeout, command))
    returncode, stdout, stderr, timed_out = run_bounded_bash(command, timeout)
    _gate_log(
        "phase tests: %.1fs rc=%s%s"
        % (
            time.time() - started,
            returncode,
            (" TIMED OUT at %.1fs (%s)" % (timeout, _test_timeout_source))
            if timed_out
            else "",
        )
    )
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
            verifier_resource_profile()[2],
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


def _write_sandbox_runtime_files(workspace: Path, sandbox_workspace: str) -> tuple[Path, Path]:
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
    env_values.update(
        _write_coding_agent_config(
            workspace, sandbox_workspace, env_values, python=_SANDBOX_AGENT_PYTHON
        )
    )
    env_file = _write_private_shell_env(workspace / ".mac-openshell-env.sh", env_values)

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

    A legacy global ``--gpu`` is always removed: only an explicit GPU task may
    add it back, and only after bootstrap proved the nested OpenShell GPU path.
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
    filtered: List[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--gpu" or token.startswith("--gpu="):
            index += 1
            if token == "--gpu" and index < len(argv) and argv[index].isdigit():
                index += 1
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


def _runtime_executor_config_sha256(
    *, runtime_image_ref: str, source_bundle_sha256: str, host_install: bool = False
) -> str:
    """Digest the effective process-local sandbox contract.

    The service wrapper rotates ``MAC_WORKER_PROCESS_REVISION`` on every start.
    Including it prevents a startup report cached by the hub from surviving a
    worker restart even when the image and source happen to be unchanged.
    """

    create_argv = [] if host_install else _openshell_extra_create_argv()
    payload = {
        "create_argv": create_argv,
        "process_revision": os.environ.get("MAC_WORKER_PROCESS_REVISION") or "unversioned",
        "runtime_image_ref": runtime_image_ref,
        "source_bundle_sha256": source_bundle_sha256,
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _managed_openshell_runtime_image_ref() -> str:
    """Return the immutable image the worker will actually pass to OpenShell.

    ``MAC_OPENSHELL_CREATE_ARGS`` is the execution authority for ordinary task
    sandboxes.  The older implementation attested only the sidecar
    ``runtime-image-ref`` file, so changing ``--from`` could make tasks run one
    image while the worker continued advertising another.  Prefer the effective
    create argument and retain the file only as a backwards-compatible fallback
    for deployments which do not spell out ``--from``.
    """

    create_argv = _openshell_extra_create_argv()
    configured_refs: List[str] = []
    index = 0
    while index < len(create_argv):
        token = create_argv[index]
        if token == "--from":
            if index + 1 >= len(create_argv):
                raise RuntimeError("MAC_OPENSHELL_CREATE_ARGS --from requires a value")
            configured_refs.append(create_argv[index + 1])
            index += 2
            continue
        if token.startswith("--from="):
            configured_refs.append(token.partition("=")[2])
        index += 1
    if len(configured_refs) > 1:
        raise ValueError("MAC_OPENSHELL_CREATE_ARGS contains duplicate --from arguments")
    if configured_refs:
        image_ref = configured_refs[0]
        if not _MANAGED_OPENSHELL_RUNTIME_REF_RE.fullmatch(image_ref):
            raise RuntimeError(
                "read-only repository reports require MAC_OPENSHELL_CREATE_ARGS "
                "to select the immutable mac-openshell-runtime@sha256 image"
            )
        return image_ref

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


def _assert_approved_read_only_report_runtime(*, runtime_image_ref: str) -> None:
    """Revalidate the hub-approved tuple immediately before sandbox create."""

    expected_runtime = env_str("MAC_REPORT_EXECUTOR_APPROVED_RUNTIME_IMAGE_REF")
    expected_policy = env_str("MAC_REPORT_EXECUTOR_APPROVED_POLICY_SHA256")
    expected_bin_path = env_str("MAC_REPORT_EXECUTOR_APPROVED_OPENSHELL_BIN_PATH")
    expected_bin_digest = env_str("MAC_REPORT_EXECUTOR_APPROVED_OPENSHELL_BIN_SHA256")
    expected_platform = env_str("MAC_REPORT_EXECUTOR_APPROVED_PLATFORM")
    expected_posture = env_str("MAC_REPORT_EXECUTOR_APPROVED_ISOLATION_POSTURE")
    expected_python_path = env_str("MAC_REPORT_EXECUTOR_APPROVED_PYTHON_PATH")
    expected_python_digest = env_str("MAC_REPORT_EXECUTOR_APPROVED_PYTHON_SHA256")
    expected_script_path = env_str("MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_PATH")
    expected_script_digest = env_str("MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_SHA256")
    expected_source_root = env_str("MAC_REPORT_EXECUTOR_APPROVED_SOURCE_ROOT")
    expected_source_digest = env_str("MAC_REPORT_EXECUTOR_APPROVED_SOURCE_BUNDLE_SHA256")
    expected_runtime_config_digest = env_str("MAC_REPORT_EXECUTOR_APPROVED_RUNTIME_CONFIG_SHA256")
    # macOS nodes are host installs: no image, no policy, no OpenShell binary
    # exists to be approved, so those four fields are legitimately empty and
    # must not be present. Everything that still exists stays digest-bound.
    host_install = sys.platform in REPORT_REPOSITORY_HOST_INSTALL_PLATFORMS
    required = [
        expected_platform,
        expected_posture,
        expected_python_path,
        expected_python_digest,
        expected_script_path,
        expected_script_digest,
        expected_source_root,
        expected_source_digest,
        expected_runtime_config_digest,
    ]
    container_fields = (
        expected_runtime,
        expected_policy,
        expected_bin_path,
        expected_bin_digest,
    )
    if not host_install:
        required.extend(container_fields)
    if not all(required):
        raise RuntimeError("read-only repository report lacks the hub-approved runtime tuple")
    if host_install:
        if any(container_fields) or runtime_image_ref:
            raise RuntimeError(
                "read-only repository report on a host install must not claim a container runtime"
            )
    else:
        if runtime_image_ref != expected_runtime:
            raise RuntimeError(
                "read-only repository report runtime image differs from hub approval"
            )
        _policy_path, policy_digest = nofollow_regular_file_identity(_resolve_openshell_policy())
        if policy_digest != expected_policy:
            raise RuntimeError("read-only repository report policy differs from hub approval")
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
        raise RuntimeError("read-only repository report Python differs from hub approval")
    script_candidate = env_str("MAC_TASK_EXECUTOR_SCRIPT")
    if not script_candidate:
        raise RuntimeError("read-only repository report executor script is not configured")
    script_path, script_digest = nofollow_regular_file_identity(script_candidate)
    if script_path != expected_script_path or script_digest != expected_script_digest:
        raise RuntimeError("read-only repository report executor script differs from hub approval")
    source_candidate = env_str("MAC_SELF_UPDATE_REPO")
    if not source_candidate:
        raise RuntimeError("read-only repository report MAC source root is not configured")
    source_root, source_digest = nofollow_source_bundle_digest(source_candidate)
    if source_root != expected_source_root or source_digest != expected_source_digest:
        raise RuntimeError("read-only repository report MAC source differs from hub approval")
    runtime_config_digest = _runtime_executor_config_sha256(
        runtime_image_ref=runtime_image_ref,
        source_bundle_sha256=source_digest,
        host_install=host_install,
    )
    if runtime_config_digest != expected_runtime_config_digest:
        raise RuntimeError(
            "read-only repository report process/create configuration differs from hub approval"
        )
    if sys.platform.startswith("linux"):
        if (
            expected_platform != "linux"
            or expected_posture != REPORT_REPOSITORY_LINUX_POSTURE
            or not _kernel_has_landlock()
        ):
            raise RuntimeError(
                "read-only repository reports on Linux require approved, enforced Landlock"
            )
    elif sys.platform == "darwin":
        if (
            expected_platform != "darwin"
            or expected_posture != REPORT_REPOSITORY_MACOS_HOST_POSTURE
        ):
            raise RuntimeError(
                "read-only repository report lacks the approved macOS host-install "
                "isolation posture"
            )
    else:
        raise RuntimeError("read-only repository reports are unsupported on this platform")


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
        _assert_approved_read_only_report_runtime(runtime_image_ref=runtime_image_ref)
    filtered: List[str] = ["--from", runtime_image_ref]
    saw_from = False
    index = 0
    while index < len(source):
        token = source[index]
        if token == "--from" or token.startswith("--from="):
            if saw_from:
                raise ValueError("read-only repository reports forbid duplicate --from arguments")
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
            match = _re.fullmatch(r"([1-9][0-9]{0,5})([KMGTP]i?B?|[kmgpt])?", value)
            if match is None or int(match.group(1)) > 65536:
                raise ValueError("read-only repository report --memory is missing or unbounded")
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
    task: Any = None,
) -> List[str]:
    """The task's logical ``sandbox create --upload ... -- <agent>`` argv.

    It is never executed verbatim: OpenShell 0.1 rejects ``--upload`` combined
    with a command, and a trailing command would become the main process whose
    exit ends Ready. :func:`_sandbox_launch_argvs` splits it into a kept-alive
    create (uploading the workspace) and a ``sandbox exec`` running the agent,
    so the sandbox stays Ready for verification, download and delete.

    A policy is ALWAYS passed (explicit -> deployed -> bundled fail-closed
    default) so OpenShell can never silently apply its own image-default profile.
    When ``task`` is supplied and per-repo egress expansion is enabled, the
    policy is that base widened by the task's own reviewed egress grants
    (ADR 0009 §2a); with expansion off it is the base policy unchanged.
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
    policy = _resolve_openshell_policy() if task is None else _resolve_task_openshell_policy(task)
    argv += ["--policy", policy, "--name", name]
    argv += _sandbox_label_argv("task", keep=env_bool("MAC_OPENSHELL_KEEP"))
    argv += verifier_profile_create_args(
        _openshell_extra_create_argv() if extra_create_argv is None else list(extra_create_argv)
    )
    argv += ["--upload", "%s:%s" % (str(workspace), _SANDBOX_WORKDIR)]
    inner = "\n".join(
        [
            "cd %s" % shlex.quote(sub),
            "set -a",
            ". ./.mac-openshell-env.sh",
            "set +a",
            "rm -f ./.mac-openshell-env.sh",
            verifier_resource_profile()[2],
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
            '    git -C "$MAC_TASK_REPO_WORKTREE" config user.email mac-sandbox@invalid',
            '    git -C "$MAC_TASK_REPO_WORKTREE" config user.name "MAC OpenShell sandbox"',
            '    git -C "$MAC_TASK_REPO_WORKTREE" add -A',
            '    git -C "$MAC_TASK_REPO_WORKTREE" commit -q --allow-empty -m "MAC OpenShell sandbox baseline"',
            "  fi",
            "fi",
            "exec %s" % shlex.join(agent_argv),
        ]
    )
    # One line: OpenShell's exec RPC rejects newline-bearing arguments.
    argv += ["--", "/bin/bash", "-c", single_line_shell_script(inner)]
    return argv


#: Bound for the create phase alone (image pull + workspace upload). The agent
#: itself runs in the following exec under the runner's own timeout.
_SANDBOX_CREATE_TIMEOUT_SECONDS = 900.0


def _sandbox_launch_argvs(create_argv: List[str]) -> "tuple[List[str], List[str]]":
    """Split a logical create+command argv into (kept-alive create, exec)."""
    return split_sandbox_create_command(create_argv)


def _sandbox_create_detached(
    create_argv: List[str], *, timeout: float = _SANDBOX_CREATE_TIMEOUT_SECONDS
) -> "subprocess.CompletedProcess[str]":
    """Create (and upload into) a sandbox that stays Ready; never raises.

    A timeout maps to 124 and a missing/unrunnable CLI to 127, mirroring what
    the audited runner reported when create and the agent were one process.
    """
    try:
        return _run_captured(create_argv, Path.cwd(), timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return subprocess.CompletedProcess(
            create_argv, 124, out, err + "\n[executor] sandbox create timed out after %ss" % timeout
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            create_argv, 127, "", "[executor] sandbox create could not run: %s" % exc
        )


def _sandbox_step(args: List[str], *, timeout: float) -> "tuple[bool, str]":
    """Run an openshell lifecycle step (download/delete) out-of-band of the
    audited agent run. Best-effort: returns (ok, message); it raises only
    :class:`OpenShellExecArgvError` for an exec argv OpenShell would reject,
    which is a programming error rather than a runtime failure."""
    if args and args[0] == "exec":
        assert_exec_argv_single_line(args)
    try:
        proc = _run_captured(
            [_openshell_bin(), "sandbox", *args],
            Path.cwd(),
            timeout,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - teardown must never mask the run
        return False, str(exc)


def _openshell_transfer_timeout() -> float:
    """Budget large repository uploads/downloads without weakening probes."""

    try:
        return max(1.0, float(env_str("MAC_OPENSHELL_TRANSFER_TIMEOUT") or "1800"))
    except ValueError:
        return 1800.0


def _openshell_delete_timeout() -> float:
    """Allow large sandbox filesystems to retire before declaring a leak."""

    try:
        return max(1.0, float(env_str("MAC_OPENSHELL_DELETE_TIMEOUT") or "600"))
    except ValueError:
        return 600.0


def _verifier_output_excerpt(stdout_file, stderr_file, *, limit: int = 600) -> str:
    """A bounded excerpt of what the verifier actually said.

    stderr first: a launcher that refuses says so there. Both streams are
    temporary files the caller already holds open, so this only has to rewind
    and read -- which is precisely what the failure path never did.
    """
    parts = []
    for label, handle in (("stderr", stderr_file), ("stdout", stdout_file)):
        try:
            handle.seek(0)
            text = handle.read().strip()
        except Exception:  # noqa: BLE001 - diagnostics must not raise
            continue
        if not text:
            continue
        if len(text) > limit:
            text = text[:limit] + "... (truncated)"
        parts.append("%s=%s" % (label, text.replace("\n", " | ")))
    return "; ".join(parts)


def _sandbox_verification_report_detail(name: str, sub: str, *, limit: int = 1200) -> str:
    """Recover what the gate said from the report the sandbox wrote.

    The in-sandbox verifier deliberately prints nothing: it captures the gate's
    stdout and stderr into ``mac-sandbox-verification.json`` and exits with the
    gate's status. So on the host both streams are empty and the failure detail
    degrades to "repository verifier exited with status N" -- a bare number, for
    a run that produced a full pytest report. Every gate failure has therefore
    looked identical, naming neither the failing test nor the reason, which is
    why five separate causes were diagnosed one at a time by hand.

    The report is downloaded with the workspace, but only after verification
    returns, so the host cannot wait for it. Read it out of the sandbox, which
    is still alive at this point.
    """

    argv = [
        _openshell_bin(),
        "sandbox",
        "exec",
        "--name",
        name,
        "--workdir",
        sub,
        "--no-tty",
        "--timeout",
        "30",
        "--",
        "/bin/cat",
        _SANDBOX_VERIFICATION_FILE,
    ]
    assert_exec_argv_single_line(argv)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        return ""
    raw = (proc.stdout or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001 - a partial report is not a failure
        return ""
    if not isinstance(payload, Mapping):
        return ""
    parts = []
    error = str(payload.get("error") or "").strip()
    if error:
        parts.append("error=%s" % error)
    # A failed bootstrap is reported as the whole run's status, so the exit code
    # says "the tests failed" when dependency setup never finished and no test
    # ran at all. Those lead opposite ways -- one is a regression to fix, the
    # other an environment to repair -- so name the phase explicitly.
    bootstrap = payload.get("bootstrap")
    if isinstance(bootstrap, Mapping) and bootstrap.get("returncode") not in (0, None):
        detail = str(bootstrap.get("stderr") or bootstrap.get("stdout") or "").strip()
        if len(detail) > limit:
            detail = "... (head omitted) " + detail[-limit:]
        parts.append(
            "bootstrap failed (rc=%s)%s"
            % (
                bootstrap.get("returncode"),
                ": " + detail.replace("\n", " | ") if detail else "",
            )
        )
    # stdout before stderr, unlike the launcher excerpt: pytest names the failing
    # tests and prints its summary line there, while stderr is usually warnings.
    for label in ("stdout", "stderr"):
        text = str(payload.get(label) or "").strip()
        if not text:
            continue
        if len(text) > limit:
            text = "... (head omitted) " + text[-limit:]
        parts.append("%s=%s" % (label, text.replace("\n", " | ")))
    return "; ".join(parts)


def _terminate_sandbox_client(proc: subprocess.Popen[Any]) -> None:
    """Terminate an OpenShell client and every local helper it spawned."""
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=5.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _sandbox_run_repository_verification_exec(
    name: str,
    sub: str,
    sandbox_script: str,
    start_marker: str,
    *,
    timeout: float,
) -> _SandboxRepositoryVerificationResult:
    """Run the verifier with a bounded proof that OpenShell launched it.

    The OpenShell client has been observed waiting indefinitely even though no
    corresponding command exists in the sandbox. Its ordinary ``--timeout`` is
    the repository-test timeout, which is intentionally long for slow projects.
    A host-side start handshake distinguishes that transport failure from a
    legitimately long test without shortening the latter's budget.
    """
    try:
        start_timeout = float(env_str("MAC_OPENSHELL_VERIFICATION_START_TIMEOUT") or "600")
    except ValueError:
        start_timeout = 600.0
    start_timeout = max(0.05, min(start_timeout, 1800.0))
    argv = [
        _openshell_bin(),
        "sandbox",
        "exec",
        "--name",
        name,
        "--workdir",
        sub,
        "--timeout",
        str(max(1, int(timeout))),
        "--no-tty",
        "--",
        "/usr/bin/env",
        "VERIFICATION_START_MARKER=%s" % start_marker,
        "/bin/bash",
        sandbox_script,
    ]
    assert_exec_argv_single_line(argv)
    started_at = time.monotonic()
    start_deadline = started_at + start_timeout
    total_deadline = started_at + timeout + 90.0
    marker_command = "test -f %s" % shlex.quote(start_marker)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            try:
                # stdin is DEVNULL, not inherited. `openshell sandbox exec`
                # READS STDIN, and the agent runs under a supervisor whose
                # stdin is an open pipe that never delivers. The child then
                # blocks before running anything: no output on either stream,
                # no marker written, and the poll loop reports a 120s start
                # timeout for a process that was never going to start.
                #
                # Measured on a worker: with stdin an open pipe the exec hangs
                # until killed (rc=124, zero output); with DEVNULL the same
                # command returns in one second.
                proc = subprocess.Popen(
                    argv,
                    cwd=str(Path.cwd()),
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except Exception as exc:  # noqa: BLE001 - report lifecycle failure
                return _SandboxRepositoryVerificationResult(
                    False,
                    "verifier_infrastructure",
                    "could not launch OpenShell repository verifier: %s: %s"
                    % (type(exc).__name__, exc),
                    retryable=True,
                )

            marker_seen = False
            while proc.poll() is None and time.monotonic() < start_deadline:
                ok, _msg = _sandbox_step(
                    [
                        "exec",
                        "--name",
                        name,
                        "--workdir",
                        sub,
                        "--timeout",
                        "5",
                        "--no-tty",
                        "--",
                        "/bin/sh",
                        "-c",
                        marker_command,
                    ],
                    timeout=10.0,
                )
                if ok:
                    marker_seen = True
                    break
                time.sleep(0.5)

            if not marker_seen:
                # Cover a verifier that started and exited between the final
                # process poll and marker probe.
                marker_seen, _msg = _sandbox_step(
                    [
                        "exec",
                        "--name",
                        name,
                        "--workdir",
                        sub,
                        "--timeout",
                        "5",
                        "--no-tty",
                        "--",
                        "/bin/sh",
                        "-c",
                        marker_command,
                    ],
                    timeout=10.0,
                )
            if not marker_seen:
                # Say what actually happened. Both streams were captured all
                # along and this branch returned without reading either, so
                # every failure looked like the same 120s timeout -- three
                # canaries reported it verbatim and none of them said why.
                #
                # The two cases need telling apart, because they lead opposite
                # ways: a verifier that EXITED immediately did not time out at
                # all, and reporting a timeout for it sends the next person to
                # raise the limit again, which is what already happened once
                # (45s -> 120s changed nothing).
                exit_code = proc.poll()
                _terminate_sandbox_client(proc)
                detail = _verifier_output_excerpt(stdout_file, stderr_file)
                elapsed = time.monotonic() - started_at
                if exit_code is not None:
                    message = (
                        "OpenShell repository verifier exited immediately "
                        "(rc=%s after %.1fs, start budget %.1fs)"
                        % (exit_code, elapsed, start_timeout)
                    )
                else:
                    message = (
                        "OpenShell repository verifier did not start within "
                        "%.1fs (still running; marker never appeared)" % start_timeout
                    )
                if detail:
                    message = "%s: %s" % (message, detail)
                return _SandboxRepositoryVerificationResult(
                    False,
                    "verifier_infrastructure",
                    message,
                    retryable=True,
                )

            try:
                proc.wait(timeout=max(0.05, total_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _terminate_sandbox_client(proc)
                return _SandboxRepositoryVerificationResult(
                    False,
                    "verifier_infrastructure",
                    "OpenShell repository verifier exceeded %.1fs execution timeout"
                    % (timeout + 90.0),
                )

            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            detail = (stderr or stdout or "").strip()
            if proc.returncode == 0:
                return _SandboxRepositoryVerificationResult(True, detail=detail)
            return _SandboxRepositoryVerificationResult(
                False,
                "repository_test_failed",
                detail or "repository verifier exited with status %d" % proc.returncode,
            )


_SANDBOX_DOWNLOAD_RUNTIME_ROOT_NAMES = {
    ".venv",
    "venv",
    "node_modules",
}

_SANDBOX_DOWNLOAD_WORKSPACE_RUNTIME_ROOT_NAMES = {
    ".mac-toolchain",
    _SANDBOX_VERIFICATION_STARTED_FILE,
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
        _OPENCODE_CONFIG_FILENAME,
        ".mac-sandbox-toolchain.sh",
        ".mac-sandbox-repository-verify.sh",
        _TRUSTED_READ_ONLY_VERIFICATION_FILE,
        "worker-result.json",
        "review-result.json",
        _NEEDS_INPUT_MARKER,
        "stdout.txt",
        "stderr.txt",
    }:
        return True
    if name.startswith("repository-wip-") and name.endswith(".bundle"):
        return True
    return name.startswith((".mac-agent-command-", ".mac-agent-prompt-"))


_SANDBOX_DOWNLOAD_REGULAR_OUTPUT_NAMES = {
    "mac-evidence.json",
    _SANDBOX_VERIFICATION_FILE,
    "review-independent-findings.json",
    "review-protocol.json",
}


def _sandbox_download_path_in_repository(rel_path: Path, repository_roots: set[Path]) -> bool:
    """True when ``rel_path`` is inside, or is an ancestor of, a repository root.

    Entries there carry the task deliverable, so a problem with one must fail
    the harvest closed. Everything else in the task workspace is agent scratch
    (virtualenvs, tool downloads, caches) whose loss never loses repo work.
    """

    return any(
        _path_is_under(rel_path, root) or _path_is_under(root, rel_path)
        for root in repository_roots
    )


def _sandbox_download_special_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "special_file"


def _classify_sandbox_download_entries(
    download_root: Path, workspace: Path, repository_roots: set[Path]
) -> Dict[str, List[Dict[str, str]]]:
    """Vet every entry before the merge mutates host workspace state.

    An entry the host must never materialize (a symlink whose target escapes
    the task workspace, a FIFO/socket/device node, an unreadable directory) is
    fatal inside a repository worktree or on a host/evidence control, and is
    skipped and recorded anywhere else. One stray venv symlink must not discard
    the repository changes harvested alongside it (live 2026-10-03,
    task_b3e16b5f).
    """

    download_root_resolved = download_root.resolve()
    workspace_resolved = workspace.resolve()
    skipped_symlinks: List[Dict[str, str]] = []
    skipped_entries: List[Dict[str, str]] = []

    def _walk_error(error: OSError) -> None:
        failed = Path(getattr(error, "filename", "") or "")
        rel = _relative_path_or_none(failed, download_root)
        if rel is None or rel == Path("."):
            raise ValueError("sandbox download could not be read: %s" % error) from None
        if _sandbox_download_path_excluded(rel, repository_roots):
            return
        if _sandbox_download_path_in_repository(rel, repository_roots):
            raise ValueError(
                "sandbox download directory inside the repository worktree is unreadable: %s" % rel
            ) from None
        skipped_entries.append({"path": str(rel), "kind": "directory", "reason": "unreadable"})

    for root, dirs, files in os.walk(
        download_root, topdown=True, followlinks=False, onerror=_walk_error
    ):
        root_path = Path(root)
        rel_root = root_path.relative_to(download_root)
        for name in [*dirs, *files]:
            src = root_path / name
            rel = rel_root / name
            try:
                mode = os.lstat(src).st_mode
            except OSError as exc:
                if _sandbox_download_path_excluded(rel, repository_roots):
                    continue
                if _sandbox_download_path_in_repository(rel, repository_roots):
                    raise ValueError(
                        "sandbox download entry inside the repository worktree is unreadable: "
                        "%s (%s)" % (rel, exc)
                    ) from None
                skipped_entries.append({"path": str(rel), "kind": "unknown", "reason": str(exc)})
                continue
            is_link = stat.S_ISLNK(mode)
            if is_link and (
                _sandbox_download_path_is_host_control(rel)
                or (len(rel.parts) == 1 and rel.name in _SANDBOX_DOWNLOAD_REGULAR_OUTPUT_NAMES)
            ):
                raise ValueError(
                    "sandbox download attempted to replace host/evidence control %s with a symlink"
                    % rel
                )
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            in_repository = _sandbox_download_path_in_repository(rel, repository_roots)
            if is_link:
                target = os.readlink(src)
                problem = ""
                if os.path.isabs(target):
                    problem = "absolute target"
                else:
                    try:
                        src.resolve(strict=False).relative_to(download_root_resolved)
                        (workspace / rel).parent.joinpath(target).resolve(strict=False).relative_to(
                            workspace_resolved
                        )
                    except (OSError, RuntimeError, ValueError):
                        problem = "escapes the task workspace"
                if not problem:
                    continue
                if in_repository:
                    if problem == "absolute target":
                        raise ValueError(
                            "sandbox download symlink has an absolute target: %s -> %s"
                            % (rel, target)
                        )
                    raise ValueError(
                        "sandbox download symlink escapes the task workspace: %s -> %s"
                        % (rel, target)
                    )
                skipped_symlinks.append({"path": str(rel), "target": target, "reason": problem})
                continue
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                continue
            kind = _sandbox_download_special_kind(mode)
            if in_repository:
                raise ValueError(
                    "sandbox download contains a %s inside the repository worktree: %s"
                    % (kind, rel)
                )
            if len(rel.parts) == 1 and rel.name in _SANDBOX_DOWNLOAD_REGULAR_OUTPUT_NAMES:
                raise ValueError(
                    "sandbox download attempted to replace evidence output %s with a %s"
                    % (rel, kind)
                )
            skipped_entries.append({"path": str(rel), "kind": kind, "reason": "special file"})

    return {"skipped_symlinks": skipped_symlinks, "skipped_entries": skipped_entries}


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
    if rel_path.parts and rel_path.parts[0] in _SANDBOX_DOWNLOAD_WORKSPACE_RUNTIME_ROOT_NAMES:
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


def _merge_sandbox_download_tree(
    download_root: Path, workspace: Path
) -> Dict[str, List[Dict[str, str]]]:
    """Merge a downloaded sandbox workspace into the host workspace.

    OpenShell downloads a tar archive. Extracting directly over a git worktree is
    unsafe for repo tasks because task worktrees may use a host ``.git`` file
    while the sandbox checkout may contain a ``.git`` directory. Keep host git
    metadata and container-local dependency caches out of the merge; the
    deterministic finalizer rebuilds/tests from the host worktree.

    Returns the scratch entries that were skipped rather than materialized
    (``skipped_symlinks`` / ``skipped_entries``). A skipped path is absent on
    the host afterwards, and nothing beneath it is ever written.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    repository_roots = _sandbox_repository_roots(workspace, download_root)
    report = _classify_sandbox_download_entries(download_root, workspace, repository_roots)
    skipped_paths = {
        Path(item["path"]) for item in [*report["skipped_symlinks"], *report["skipped_entries"]]
    }

    def _skipped(rel_path: Path) -> bool:
        return any(_path_is_under(rel_path, skipped) for skipped in skipped_paths)

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
            if _sandbox_download_path_excluded(rel, repository_roots) or _skipped(rel):
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
            if not (_sandbox_download_path_excluded(rel, repository_roots) or _skipped(rel)):
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
            if _sandbox_download_path_excluded(rel, repository_roots) or _skipped(rel):
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
            if _sandbox_download_path_excluded(rel, repository_roots) or _skipped(rel):
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
                continue
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                if _sandbox_download_path_in_repository(rel, repository_roots) or (
                    len(rel.parts) == 1 and rel.name in _SANDBOX_DOWNLOAD_REGULAR_OUTPUT_NAMES
                ):
                    raise ValueError(
                        "sandbox download could not copy protected file %s: %s" % (rel, exc)
                    ) from None
                with contextlib.suppress(OSError):
                    dst.unlink()
                report["skipped_entries"].append(
                    {"path": str(rel), "kind": "file", "reason": "copy failed: %s" % exc}
                )

    return report


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
            payload = os.readlink(name, dir_fd=parent_fd).encode("utf-8", "surrogateescape")
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
                                b"M\0" + relative.encode("utf-8", "surrogateescape") + b"\0"
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
    return (
        r"""import hashlib, os, stat, sys
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
"""
        % paths
    )


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
        # git can prompt (credentials, editors) and would then wait on a stdin
        # nobody will ever write to.
        stdin=subprocess.DEVNULL,
        args=[
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
    if not all((worktree_raw, expected_head, expected_tree, expected_refs, expected_content)):
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
        hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest() if refs.returncode == 0 else ""
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

    verifier_name = _read_only_verifier_sandbox_name()
    trusted = workspace / _TRUSTED_READ_ONLY_VERIFICATION_FILE
    trusted.unlink(missing_ok=True)
    try:
        timeout = float(env_str("MAC_WORKER_REPOSITORY_TEST_TIMEOUT") or "7200")
    except ValueError:
        timeout = 7200.0
    timeout = max(1.0, timeout)
    created = False
    downloaded = False
    stored_pass = False
    deletion_succeeded = [False]

    def _delete_verifier() -> None:
        deletion_succeeded[0] = _sandbox_delete(verifier_name)

    with (
        contextlib.ExitStack() as cleanup,
        tempfile.TemporaryDirectory(
            prefix=".%s-read-only-verifier-" % workspace.name,
            dir=str(workspace.parent) if workspace.parent.is_dir() else None,
        ) as temp,
    ):
        # Register before any preparation or OpenShell call so even early
        # returns and unexpected exceptions cannot strand the verifier.
        cleanup.callback(_delete_verifier)
        root = Path(temp)
        verifier_workspace = root / "workspace"
        try:
            relative, target = _prepare_read_only_verifier_workspace(
                workspace, verifier_workspace, task
            )
            expected_git_control = _authoritative_read_only_git_control_digest(target)
        except (OSError, ValueError) as exc:
            sys.stderr.write(
                "[executor] WARNING: could not prepare independent read-only verifier: %s\n" % exc
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
            _sandbox_read_only_repository_verification_shell(verification_environment) + "\n",
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
            *verifier_profile_create_args(_read_only_verifier_extra_create_argv()),
            "--no-git-ignore",
            "--upload",
            "%s:%s" % (verifier_workspace, _SANDBOX_WORKDIR),
            *openshell_create_keepalive_args(_openshell_bin()),
        ]
        # Upload on create, then exec the verifier: OpenShell 0.1 rejects an
        # upload combined with a command. ``created`` still means "the
        # verifier ran and exited zero", as when both were one create.
        created, create_message = _sandbox_step(create_args, timeout=timeout + 90.0)
        if created:
            created, create_message = _sandbox_step(
                [
                    "exec",
                    "--name",
                    verifier_name,
                    "--no-tty",
                    "--",
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    sandbox_script,
                ],
                timeout=timeout + 90.0,
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
            stored_pass = _store_trusted_read_only_verification(destination, workspace, task)
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
) -> Any:
    if not isinstance(task, dict):
        return None
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not task_is_repo_coupled(task) and not metadata_declares_read_only_report_repository(
        metadata
    ):
        return None
    if metadata_declares_report_deliverable(metadata) and not (
        metadata_declares_read_only_report_repository(metadata)
    ):
        # A report deliverable changes nothing in the repository, so there is
        # no repository outcome to verify. Running the repo's test command
        # against it can only produce false negatives -- and did: on
        # 2026-08-10 four probe tasks ran their commands correctly, wrote
        # valid evidence, and were failed by this gate.
        #
        # Deliberately NOT skipped for a read-only report bound to the
        # repository: that one is trusted precisely because the current
        # contract's test command ran and passed, which is the case handled
        # immediately below.
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
    # The gate's timeout is read INSIDE the sandbox, from the sandbox's own
    # environment -- which carried only the three names above, so the in-script
    # default of 1800s applied no matter what the host was configured with.
    #
    # Raising MAC_WORKER_REPOSITORY_TEST_TIMEOUT on a worker therefore did
    # nothing: the host waited the longer time while the script inside kept
    # killing the run at thirty minutes, and the task failed with "repository
    # test command timed out after 1800.0s" while the operator looked at a
    # config that said 5400. A knob that silently does nothing is worse than no
    # knob, because it ends the investigation.
    for timeout_name in (
        "MAC_WORKER_REPOSITORY_TEST_TIMEOUT",
        "MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT",
    ):
        configured = env_str(timeout_name)
        if configured:
            verification_environment[timeout_name] = configured
    script_path.write_text(
        _sandbox_repository_verification_shell(verification_environment) + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o700)
    sandbox_script = "%s/%s" % (sub, script_path.name)
    ok, msg = _sandbox_step(
        ["upload", name, str(script_path), sandbox_script],
        timeout=_openshell_transfer_timeout(),
    )
    if not ok:
        sys.stderr.write(
            "[executor] WARNING: sandbox repository verification upload failed: %s\n" % msg
        )
        return _SandboxRepositoryVerificationResult(
            False,
            "verifier_infrastructure",
            "sandbox repository verification upload failed: %s" % msg,
            retryable=True,
            attempt_count=0,
        )
    try:
        timeout = float(env_str("MAC_WORKER_REPOSITORY_TEST_TIMEOUT") or "7200")
    except ValueError:
        timeout = 7200.0
    verification: Optional[_SandboxRepositoryVerificationResult] = None
    for attempt in range(1, 3):
        import uuid

        # The task workspace is reused across attempts and uploaded wholesale.
        # A fixed marker can therefore be stale.  A unique /tmp marker needs no
        # preflight `openshell sandbox exec rm`, which was itself the most common
        # verifier-launch failure and consumed half of the command budget before
        # tests even began.
        start_marker = "/tmp/mac-repository-verifier-%s.started" % uuid.uuid4().hex
        verification = replace(
            _sandbox_run_repository_verification_exec(
                name,
                sub,
                sandbox_script,
                start_marker,
                timeout=timeout,
            ),
            attempt_count=attempt,
        )
        if verification.passed or not verification.retryable:
            break
        if attempt < 2:
            emit_telemetry(
                "sandbox_verification_retry",
                task_id=str(task.get("id") or "") or None,
                level="warning",
                sandbox=name,
                attempt=attempt,
                failure_class=verification.failure_class,
                detail=verification.detail,
            )
            time.sleep(0.5)
    assert verification is not None
    if not verification.passed:
        # The gate's own output lives in the sandbox's report, not on the
        # streams this failure was built from, so a plain non-zero exit
        # arrives here as a number with nothing attached. Recover the report
        # while the sandbox still exists.
        report_detail = _sandbox_verification_report_detail(name, sub)
        if report_detail:
            verification = replace(
                verification,
                detail="%s: %s" % (verification.detail, report_detail),
            )
        sys.stderr.write(
            "[executor] WARNING: sandbox repository verification failed "
            "(%s, attempt %d): %s\n"
            % (
                verification.failure_class,
                verification.attempt_count,
                verification.detail,
            )
        )
    return verification


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
            "expected_git_control=%s" % shlex.quote(expected_git_control_digest),
            'fail() { printf "%s\\n" "$1" >&2; exit 66; }',
            'test ! -L "$repo" || fail "read-only repository worktree became a symlink"',
            'test -d "$repo/.git" || fail "read-only repository Git metadata is missing"',
            # This raw, O_NOFOLLOW digest is deliberately the FIRST
            # post-agent repository interpretation.  In particular, do not run
            # `git -C`: a poisoned core.worktree would make Git inspect an
            # attacker-selected clean tree instead of the uploaded checkout.
            "python_bin=/opt/mac-venv/bin/python",
            '[ -x "$python_bin" ] || python_bin="$(PATH=%s command -v python3 || PATH=%s command -v python || true)"'
            % (shlex.quote(_SANDBOX_BASE_PATH), shlex.quote(_SANDBOX_BASE_PATH)),
            '[ -n "$python_bin" ] || fail "trusted Python is unavailable for Git control validation"',
            'observed_git_control="$("$python_bin" -I - "$repo" <<\'PY\'',
            digest_program,
            "PY",
            ')" || fail "could not digest read-only repository Git controls"',
            'test "$observed_git_control" = "$expected_git_control" || fail "read-only repository Git control metadata changed"',
            'git_bin="$(PATH=%s command -v git || true)"' % shlex.quote(_SANDBOX_BASE_PATH),
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
    encoded_script = base64.b64encode(script.encode("utf-8")).decode("ascii")
    # OpenShell rejects newline/CR-bearing command arguments. Decode through
    # the immutable image interpreter, never an agent-writable script. Both
    # Python processes must ignore cwd/user imports before the raw Git digest;
    # the child shell must not source an agent-controlled BASH_ENV either.
    decoder = (
        "import base64,subprocess,sys;"
        "sys.exit(subprocess.run(['/bin/bash','--noprofile','--norc'],"
        "input=base64.b64decode(sys.argv[1]),"
        "env={'PATH':%r,'HOME':'/tmp/mac-read-only-postcheck'}).returncode)" % _SANDBOX_BASE_PATH
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
            "/opt/mac-venv/bin/python",
            "-I",
            "-c",
            decoder,
            encoded_script,
        ],
        timeout=150.0,
    )
    return "" if ok else (message or "read-only repository sandbox validation failed")


def _sandbox_download(
    name: str,
    basename: str,
    workspace: Path,
    skipped: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> bool:
    """Sync the agent's edits (+ the evidence manifest) from the kept sandbox
    back into the host workspace. Best-effort: a failure is logged, not fatal —
    completeness is still judged by the evidence manifest on the host.

    Scratch entries the merge refused to materialize are added to ``skipped``
    (``skipped_symlinks`` / ``skipped_entries``) for the salvage record."""
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    repository_roots = _sandbox_repository_roots(workspace, workspace)
    generated_paths = {
        Path(root_name) for root_name in _SANDBOX_DOWNLOAD_WORKSPACE_RUNTIME_ROOT_NAMES
    }
    for repository_root in repository_roots:
        generated_paths.update(
            repository_root / root_name for root_name in _SANDBOX_DOWNLOAD_RUNTIME_ROOT_NAMES
        )
    cleanup_script = shlex.join(
        ["rm", "-rf", "--", *(str(path) for path in sorted(generated_paths))]
    )
    cleanup_ok, cleanup_msg = _sandbox_step(
        [
            "exec",
            "--name",
            name,
            "--workdir",
            sub,
            "--timeout",
            "30",
            "--no-tty",
            "--",
            "/bin/sh",
            "-c",
            cleanup_script,
        ],
        timeout=60.0,
    )
    if not cleanup_ok:
        sys.stderr.write(
            "[executor] WARNING: sandbox generated-state cleanup failed before "
            "download: %s\n" % cleanup_msg
        )
    temp_parent = str(workspace.parent) if workspace.parent.is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix=".%s-openshell-download-" % workspace.name,
        dir=temp_parent,
    ) as tmp:
        download_root = Path(tmp)
        ok, msg = _sandbox_step(
            ["download", name, sub, str(download_root)],
            timeout=_openshell_transfer_timeout(),
        )
        if ok:
            try:
                report = _merge_sandbox_download_tree(download_root, workspace) or {}
            except Exception as exc:  # noqa: BLE001 - download sync is best-effort
                ok = False
                msg = "sandbox download merge failed: %s" % exc
            else:
                skipped_symlinks = list(report.get("skipped_symlinks") or [])
                skipped_entries = list(report.get("skipped_entries") or [])
                if skipped is not None:
                    skipped.setdefault("skipped_symlinks", []).extend(skipped_symlinks)
                    skipped.setdefault("skipped_entries", []).extend(skipped_entries)
                if skipped_symlinks or skipped_entries:
                    shown = [
                        "%s -> %s" % (item["path"], item["target"]) for item in skipped_symlinks
                    ] + ["%s (%s)" % (item["path"], item["kind"]) for item in skipped_entries]
                    sys.stderr.write(
                        "[executor] WARNING: sandbox download skipped %d scratch entr%s "
                        "outside the repository worktree that cannot be materialized "
                        "on the host: %s%s\n"
                        % (
                            len(shown),
                            "y" if len(shown) == 1 else "ies",
                            ", ".join(shown[:5]),
                            ", ..." if len(shown) > 5 else "",
                        )
                    )
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox download failed: %s\n" % msg)
    return ok


def _sandbox_delete(name: str) -> bool:
    ok, msg = _sandbox_step(["delete", name], timeout=_openshell_delete_timeout())
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox delete failed (possible leak): %s\n" % msg)
    return ok


def _sandbox_progress_interval() -> float:
    raw = env_str("MAC_OPENSHELL_PROGRESS_INTERVAL") or "5"
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
            single_line_shell_script(script),
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
        snapshot = _sandbox_progress_snapshot(self.name, self.basename, self.workspace)
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


def _capture_read_only_report_git_control(task: Any, workspace: Path) -> str:
    """Capture raw controls before any agent can change Git command behavior."""

    task_metadata = task.get("metadata") if isinstance(task, dict) else None
    runtime = task_metadata.get("runtime") if isinstance(task_metadata, dict) else None
    worktree_raw = (
        str(runtime.get("repository_worktree") or "").strip() if isinstance(runtime, dict) else ""
    )
    if not worktree_raw:
        raise RuntimeError(
            "read-only repository report has no task-owned worktree for Git control proof"
        )
    try:
        worktree = Path(worktree_raw).expanduser().resolve(strict=True)
        worktree.relative_to(workspace.expanduser().resolve(strict=True))
        return _read_only_git_control_digest(worktree)
    except (OSError, ValueError) as exc:
        raise RuntimeError("could not capture pre-agent read-only Git controls: %s" % exc) from exc


def _run_sandboxed(
    runner: Callable[..., Any], agent_argv: List[str], workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent through the OpenShell sandbox lifecycle: create (upload the
    workspace, kept alive) -> exec the agent -> download results -> delete. The agent
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
        expected_git_control_digest = _capture_read_only_report_git_control(task, workspace)
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
            task=task,
        )
        launch_create_argv, launch_exec_argv = _sandbox_launch_argvs(create_argv)
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
        hub_connectivity=hub_write_capability(),
    )
    progress = _SandboxProgressMonitor(name, basename, workspace, audit_id)
    if read_only_report:
        # The progress observer uses Git for ordinary coding tasks.  A read-only
        # report must perform its raw control comparison before ANY post-agent
        # Git process, so disable that advisory observer for this task class.
        progress.interval = 0.0
    progress.start()
    try:
        # Create (uploading the workspace) and run the agent as two steps: the
        # sandbox must still be Ready afterwards for verification and harvest.
        created = _sandbox_create_detached(launch_create_argv)
        if created.returncode != 0:
            # Same outcome the combined create used to report: the runner's
            # result is the failed create, and teardown still runs below.
            result = created
            runner_completed = True
            progress.stop()
            emit_telemetry(
                "sandbox_create_failed",
                task_id=str(audit_id) if audit_id else None,
                level="warning",
                sandbox=name,
                returncode=int(created.returncode),
                detail=clip_process_text(created.stderr or created.stdout or "", 600),
            )
            return result
        result = runner(launch_exec_argv, workspace, audit_id, opts)
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
        # benefit from bootstrap/tests/publication finalization. The
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
        verification = _sandbox_run_repository_verification(name, basename, workspace, task)
        if verification is not None:
            verification_passed = (
                verification.passed
                if isinstance(verification, _SandboxRepositoryVerificationResult)
                else bool(verification)
            )
            verification_detail = (
                verification.detail
                if isinstance(verification, _SandboxRepositoryVerificationResult)
                else ""
            )
            verification_failure_class = (
                verification.failure_class
                if isinstance(verification, _SandboxRepositoryVerificationResult)
                else ""
            )
            verification_attempt_count = (
                verification.attempt_count
                if isinstance(verification, _SandboxRepositoryVerificationResult)
                else 1
            )
            emit_telemetry(
                "sandbox_verification_completed",
                task_id=str(audit_id) if audit_id else None,
                level="info" if verification_passed else "warning",
                sandbox=name,
                passed=verification_passed,
                failure_class=verification_failure_class,
                detail=verification_detail,
                attempt_count=verification_attempt_count,
            )
            metadata = task.get("metadata") if isinstance(task, dict) else None
            if not verification_passed and metadata_declares_read_only_report_repository(metadata):
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
            elif not verification_passed:
                failure = (
                    verification.as_dict()
                    if isinstance(verification, _SandboxRepositoryVerificationResult)
                    else {
                        "schema": "mac.openshell_repository_verification.v1",
                        "passed": False,
                        "failure_class": "verifier_infrastructure",
                        "detail": (
                            "ordinary repository verification returned false "
                            "without a structured cause"
                        ),
                        "retryable": True,
                        "attempt_count": 1,
                    }
                )
                detail = str(failure.get("detail") or "").strip()
                summary = "OpenShell repository verification failed"
                if failure.get("failure_class"):
                    summary += " (%s)" % failure["failure_class"]
                if detail:
                    summary += ": %s" % detail
                result = subprocess.CompletedProcess(
                    getattr(result, "args", ["repository_task"]),
                    68,
                    getattr(result, "stdout", "") or "",
                    "\n".join(
                        part
                        for part in (
                            (getattr(result, "stderr", "") or "").strip(),
                            summary,
                        )
                        if part
                    ),
                )
                setattr(result, "mac_repository_verification_failure", failure)
        return result
    finally:
        active_error = sys.exc_info()[1]
        progress.stop()
        progress_evidence = progress.evidence()
        harvest_skipped: Dict[str, List[Dict[str, str]]] = {
            "skipped_symlinks": [],
            "skipped_entries": [],
        }
        try:
            harvested = _sandbox_download(name, basename, workspace, harvest_skipped)
        except Exception as exc:  # noqa: BLE001 - teardown must continue to delete
            harvested = False
            sys.stderr.write("[executor] WARNING: sandbox download raised unexpectedly: %s\n" % exc)
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
                    "[executor] WARNING: trusted verification promotion raised: %s\n" % exc
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
                    bundle_sha256=str(wip_preservation.get("bundle_sha256") or ""),
                    salvage_head_sha=str(wip_preservation.get("salvage_head_sha") or ""),
                )
        salvage = {
            "schema": "mac.openshell_salvage.v1",
            "sandbox": name,
            "runner_completed": runner_completed,
            "harvest_attempted": True,
            "harvested": harvested,
            "skipped_symlinks": harvest_skipped["skipped_symlinks"],
            "skipped_entries": harvest_skipped["skipped_entries"],
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
            sys.stderr.write(
                "[executor] WARNING: could not write sandbox salvage record: %s\n" % exc
            )
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
                lifecycle_problems.append("trusted repository verification result was not promoted")
            if kept:
                lifecycle_problems.append("sandbox deletion was skipped by MAC_OPENSHELL_KEEP")
            elif not deleted:
                lifecycle_problems.append("sandbox deletion failed")
            if lifecycle_problems:
                detail = "read-only report lifecycle incomplete: %s" % "; ".join(lifecycle_problems)
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
                if not getattr(result, "mac_read_only_repository_violation", ""):
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
    raw = runtime.get("break_glass_authorization") if isinstance(runtime, dict) else None
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
        key for key, value in expected.items() if not value or str(checks.get(key) or "") != value
    ]
    if mismatches:
        raise RuntimeError(
            "break-glass authorization binding mismatch: %s" % ", ".join(sorted(mismatches))
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
            "under authorization %s; OpenShell bypass is task-scoped.\n" % authorization_id
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
    line so an operator (or the agent) can answer "why did this task run, or
    fail closed?" rather than facing a silent choice.
    """
    sys.stderr.write(
        "[executor] coding-agent routing: %s (%s)\n"
        % (target, "; ".join(rationale) or "no rationale")
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
        "a verified in-sandbox route; %s" % (reason or "no coding agent was verified")
    )
    code = "import sys; sys.stderr.write(%r + '\\n'); raise SystemExit(42)" % msg
    # Every command is serialized through ``_write_agent_command_bundle``, which
    # requires exactly one private-prompt sentinel so no task prompt can leak
    # into argv.  The fail-closed command does not consume the prompt, but it
    # still needs the sentinel as an inert ``sys.argv[1]`` for the common bundle
    # contract.  Omitting it made the error path itself raise ValueError before
    # the intended exit-42 diagnostic could run, exhausting task retry budgets.
    return ["python3", "-c", code, PROMPT_SENTINEL]


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


#: Exit statuses with a fixed meaning: the timeout wrapper's and SIGKILL's, and
#: the shell's "not executable" / "command not found".
_PREFLIGHT_RETURNCODE_CLASSES: Dict[int, str] = {
    124: "timeout",
    137: "timeout",
    126: "agent_binary_missing",
    127: "agent_binary_missing",
}

#: Error codes carried by a structured (JSON) error line: OpenShell's egress
#: proxy (``{"error": "policy_denied", ...}``) and OpenAI-style error bodies
#: from the hub router (``{"error": {"code": ..., "type": ...}}``).
_PREFLIGHT_ERROR_CODE_CLASSES: Dict[str, str] = {
    "policy_denied": "sandbox_policy_denied",
    "invalid_api_key": "authentication_failed",
    "invalid_token": "authentication_failed",
    "unauthorized": "authentication_failed",
    "authentication_error": "authentication_failed",
    "rate_limit_exceeded": "rate_limited",
    "rate_limit_error": "rate_limited",
}


def _structured_error_class(output: str) -> str:
    """Class of the first JSON error object in ``output``, or ``""``.

    Only a line that ENDS in a complete JSON object with an ``error`` member
    counts (OpenShell's proxy prefixes its body with ``HTTP 403``). Words in
    free text never do: matching substrings of a transcript is how a sandbox
    named ``mac-task-429907755059`` was once classed ``rate_limited``.
    """
    for line in (output or "").splitlines():
        line = line.strip()
        start = line.find("{")
        if start < 0 or not line.endswith("}"):
            continue
        try:
            document = json.loads(line[start:])
        except ValueError:
            continue
        if not isinstance(document, dict) or "error" not in document:
            continue
        error = document.get("error")
        codes: List[object] = []
        status: object = document.get("status")
        if isinstance(error, dict):
            codes += [error.get("code"), error.get("type")]
            status = error.get("status", status)
        else:
            codes.append(error)
        for code in codes:
            mapped = _PREFLIGHT_ERROR_CODE_CLASSES.get(str(code or "").strip().lower())
            if mapped:
                return mapped
        try:
            status_code = int(str(status))
        except ValueError:
            status_code = 0
        if status_code in {401, 403}:
            return "authentication_failed"
        if status_code == 429:
            return "rate_limited"
        if 500 <= status_code <= 599:
            return "provider_server_error"
    return ""


def _classify_coding_agent_preflight_failure(returncode: int, output: str) -> str:
    """Map a failed preflight probe onto a class, from exit status and structure.

    Fixed exit statuses (timeout, command not found) come first, then the first
    whole-line JSON error object. Free-text output is never searched: an exit
    0 without the sentinel is ``sentinel_missing``, anything else
    ``probe_failed``.
    """
    by_returncode = _PREFLIGHT_RETURNCODE_CLASSES.get(returncode)
    if by_returncode:
        return by_returncode
    structured = _structured_error_class(output)
    if structured:
        return structured
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
        "provider_server_error",
        "rate_limited",
        "sandbox_policy_denied",
        "sentinel_missing",
    }:
        # These failures are emitted only after the CLI launched far enough to
        # open a socket toward its provider and parse a response — including a
        # policy denial synthesized by the sandbox egress proxy, which proves
        # the executable ran just as surely as a provider reply does.
        return "present"
    return "unverified"


_SANDBOX_CODING_AGENT_BINARIES = frozenset({"opencode", "claude"})


def coding_agent_sandbox_which(name: str) -> Optional[str]:
    """Resolve binaries declared by the OpenShell task-image contract.

    This is deliberately a declared inventory, not proof.  Every route selected
    through it still has to pass the disposable in-sandbox preflight, which
    reports a missing executable explicitly if an image drifts from the
    contract.
    """
    return name if name in _SANDBOX_CODING_AGENT_BINARIES else None


def _build_sandbox_probe_argv(name: str, agent_argv: List[str], private_dir: Path) -> List[str]:
    """Build the coding-agent probe's logical create+command argv.

    :func:`_openshell_probe` runs it as a kept-alive create plus an exec.

    No process-visible secrets: the prompt/command are private uploaded files
    (see agent_argv's mac.agent_command wrapper), and the probe's inference
    token travels in the private mode-0600 environment file.
    """
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
    # One line: OpenShell's exec RPC rejects newline-bearing arguments.
    argv += ["--", "/bin/bash", "-lc", single_line_shell_script(inner)]
    return argv


def _coding_agent_choice_for_sandbox(choice: Any) -> Any:
    """Return a choice whose endpoint and executable resolve inside OpenShell.

    Coding-agent detection intentionally runs on the host, where ``which`` returns
    an absolute host path (for example ``/opt/homebrew/bin/opencode``).  Passing that
    path into a Linux sandbox bypasses the sandbox's PATH contract and fails even
    when the image contains the CLI at ``/usr/local/bin/opencode``.  Execute the
    detected basename through the image-owned PATH instead.  The preflight still
    proves that the corresponding binary is actually present before work routes
    to it.
    """
    endpoint = str(getattr(choice, "endpoint", "") or "")
    binary = str(getattr(choice, "binary", "") or "")
    rewritten_endpoint = (
        _rewrite_host_local_url(endpoint, _openshell_host_alias()) if endpoint else endpoint
    )
    sandbox_binary = Path(binary).name if binary else binary
    if rewritten_endpoint == endpoint and sandbox_binary == binary:
        return choice
    return replace(choice, endpoint=rewritten_endpoint, binary=sandbox_binary)


def _openshell_probe(create_argv: List[str], *, timeout: float) -> "tuple[int, str]":
    """Run a probe given as one logical ``sandbox create ... -- <cmd>`` argv.

    It runs as create (with uploads, kept alive) then ``sandbox exec``, because
    OpenShell 0.1 rejects an upload combined with a command. Returns the exec's
    returncode (or the create's, when create fails) and the combined output of
    both steps; the caller deletes the sandbox. Best-effort: any failure
    returns a non-zero code (never raises)."""
    deadline = time.monotonic() + timeout
    output = ""
    try:
        for step in _sandbox_launch_argvs(create_argv):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 124, output + "probe timed out after %ss" % timeout
            proc = subprocess.run(
                step,
                capture_output=True,
                text=True,
                timeout=remaining,
                stdin=subprocess.DEVNULL,
            )
            output += (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0:
                return proc.returncode, output
        return 0, output
    except subprocess.TimeoutExpired as exc:
        return 124, output + str(exc)
    except Exception as exc:  # noqa: BLE001 - a probe failure must mean "not ready", not a crash
        return 1, output + str(exc)


def _run_coding_agent_preflight_result(choice: Any) -> Dict[str, object]:
    """Verify, inside a throwaway OpenShell sandbox, that the coding agent runs
    end-to-end: it must execute, authenticate, reach the provider, and echo the
    sentinel back. Proves the agent will actually work for a real sandboxed task
    (binary present + creds resolvable + egress allowed) — host-side availability
    is NOT sufficient. The probe sandbox is always deleted."""
    from . import coding_agent as _ca

    name = _coding_agent_probe_sandbox_name()
    with tempfile.TemporaryDirectory(prefix="mac-coding-agent-probe-") as tmp:
        private_dir = Path(tmp)
        sandbox_choice = _coding_agent_choice_for_sandbox(choice)
        probe_argv = _ca.coding_agent_argv(sandbox_choice, PROMPT_SENTINEL)
        bundle = _write_agent_command_bundle(private_dir, _ca.PREFLIGHT_PROMPT, probe_argv)
        sandbox_dir = "/sandbox/%s" % private_dir.name
        env_values = {**_openshell_environment(), "HOME": _SANDBOX_HOME}
        probe_token: Dict[str, Any] = {}
        token_failure = ""
        if _uses_router_opencode(choice):
            # The probe proves the same path a task takes: an inference-only
            # token through the hub router. Mint a short one just for it.
            try:
                probe_token = _mint_inference_token(
                    task_id="", ttl_seconds=_PREFLIGHT_INFERENCE_TOKEN_TTL_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - an unminted token means "not verified"
                token_failure = "inference token unavailable: %s" % exc.__class__.__name__
            else:
                env_values[_INFERENCE_TOKEN_ENV] = str(probe_token["token"])
                env_values.update(
                    _write_coding_agent_config(
                        private_dir, sandbox_dir, env_values, python=_SANDBOX_AGENT_PYTHON
                    )
                )
        _write_private_shell_env(private_dir / ".mac-openshell-env.sh", env_values)
        try:
            if token_failure:
                rc, out = 1, token_failure
            else:
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
            if not token_failure:
                _sandbox_step(["delete", name], timeout=60.0)
            if probe_token:
                _revoke_inference_token(str(probe_token.get("id") or ""))
    ok = rc == 0 and _ca.PREFLIGHT_SENTINEL in out
    failure_class = "" if ok else _classify_coding_agent_preflight_failure(rc, out)
    if token_failure:
        failure_class = "inference_token_unavailable"
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
            "OK" if ok else "FAILED (rc=%s, class=%s)" % (rc, result["failure_class"]),
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
    return bool(coding_agent_sandbox_verification(choice).get("verified"))


def _agent_argv(
    prompt: str,
    workspace: Path,
    *,
    confined: bool,
    task: Any = None,
    chosen: Optional[Dict[str, str]] = None,
    resume_session: str = "",
) -> List[str]:
    """The coding CLI argv for this task, or a deterministic fail-closed command.

    opencode runs on a ``machub`` model through the hub router with this task's
    inference-only token (see :mod:`mac.coding_agent`). When OpenShell
    confinement is in effect (``confined`` -- per-task wrap or the production
    supervisor) the route must also pass :func:`_coding_agent_sandbox_ok` (a
    real in-sandbox preflight by default), because a host-side ``which`` does
    NOT prove the CLI works inside the confined sandbox.

    There is no fallback runtime and no second CLI: a missing or unverified
    route selects ``coding-agent-required``.
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
    if chosen is not None:
        # The caller attributes the transcript to the route that actually ran:
        # `task_agent_transcripts` carries `coding_agent` and `model` columns,
        # and this is the only truthful source for either.
        chosen["agent"] = choice.agent
        chosen["fingerprint"] = choice.route_fingerprint()
        if choice.model:
            chosen["model"] = choice.model
    rationale = list(choice.rationale)
    if not choice.available:
        reason = (
            "opencode is not configured and verified inside the task sandbox"
            if confined
            else "opencode is not available on this host"
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

    if _uses_router_opencode(choice):
        # opencode authenticates to the hub router with this task's own
        # inference-only token; the worker token stays on the host.
        try:
            _ensure_task_inference_token(task_id)
        except Exception as exc:  # noqa: BLE001 - no token means no route
            reason = "opencode: could not mint the task's inference token (%s)" % (
                exc.__class__.__name__
            )
            rationale.append(reason)
            _record_runner_choice("coding-agent-required", rationale, task_id=task_id)
            return _coding_agent_required_failure_argv(reason)
        if not confined:
            os.environ.update(
                _write_coding_agent_config(
                    workspace, str(workspace), dict(os.environ), python=sys.executable
                )
            )
    if confined:
        rationale.append("verified inside the OpenShell sandbox")
    _record_runner_choice(
        choice.agent,
        rationale,
        task_id=task_id,
        route=choice.observable(),
    )
    argv_choice = _coding_agent_choice_for_sandbox(choice) if confined else choice
    if choice.agent == _ca.CLAUDE_AGENT:
        if resume_session:
            return _ca.coding_agent_argv(argv_choice, prompt, resume=resume_session)
        # Name the session so its transcript can be found, and resumed, later.
        session_id = str(uuid.uuid4())
        agent_dir = workspace / _ca.CLAUDE_AGENT_DIR
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "session-id").write_text(session_id + "\n", encoding="utf-8")
        return _ca.coding_agent_argv(argv_choice, prompt, session_id=session_id)
    return _ca.coding_agent_argv(argv_choice, prompt)




def _opts_with_route(opts: dict, route: Dict[str, str]) -> dict:
    """Carry the resolved coding-agent route into the runner's audit metadata.

    `run_audited_command` writes `coding_agent` and `model` onto every
    `task_agent_transcripts` row, reading them from the metadata it is handed.
    Nothing put them there, so both columns were empty on ALL 275 rows on the
    live hub -- every transcript recorded WHAT was said and by nobody in
    particular. That makes the one question a transcript exists to answer --
    which CLI and which model produced this -- unanswerable, and it silently
    defeats any comparison between agents or models.

    The task's own metadata is not the source: 0 of 8,154 live tasks carry
    `coding_agent`, and 11 carry `model`. `route` is populated by `_agent_argv`
    with the route that ACTUALLY ran.

    Existing keys win: an explicit value already in `opts` is not overwritten.
    """
    agent = str(route.get("agent") or "").strip()
    model = str(route.get("model") or "").strip()
    if not agent and not model:
        return opts
    merged = dict(opts)
    if agent and not merged.get("coding_agent"):
        merged["coding_agent"] = agent
    if model and not merged.get("model"):
        merged["model"] = model
    return merged


def _compile_outbound_prompt(
    prompt: str,
    target: str,
    opts: Mapping[str, Any],
    *,
    audit_id: Any,
    model: str = "",
    route_fingerprint: str = "",
) -> str:
    """Apply the mandatory policy after routing and before prompt byte staging."""

    task = opts.get("task") if isinstance(opts.get("task"), dict) else {}
    result = compile_prompt(
        prompt,
        target=target,
        model=model,
        prompt_kind=str(opts.get("execution_kind") or "task"),
        task_id=str(task.get("id") or audit_id or ""),
        attempt=task.get("attempt_count"),
        agent_id=local_agent_id(),
        route_fingerprint=route_fingerprint,
        command_id=str(opts.get("command_id") or ""),
    )
    emit_telemetry("prompt_rewrite", **result.evidence)
    return result.text


def _invoke_agent(
    runner: Callable[..., Any], prompt: str, workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent for one task, atomically coupling --yolo to enforcement.

    Invariant: an approval-bypassed coding agent (``--dangerously-*``) is only
    used when the run is confined by OpenShell, so we
    never launch an *unguarded* bypass agent.
      * sandbox enabled  -> full OpenShell lifecycle (upload workspace, run the
        agent confined, download results, delete). Fails closed if no policy
        resolves or the kernel can't enforce Landlock.
      * sandbox disabled -> direct run, gated by MAC_ALLOW_UNSANDBOXED_YOLO.
    The agent argv is a detected coding-agent CLI when one is available + authed;
    otherwise execution fails closed (see :func:`_agent_argv`).
    Returns the runner's result (carries .returncode)."""
    metadata = opts.get("task", {}).get("metadata") if isinstance(opts.get("task"), dict) else None
    read_only_repository = metadata_declares_read_only_report_repository(metadata)
    # `wrap` is the per-task OpenShell wrap launch model; `confined` is whether
    # OpenShell confinement is in effect by EITHER model — the per-task wrap or
    # the production supervisor (which runs this whole process inside a sandbox,
    # with MAC_OPENSHELL_SANDBOX off but the agent required). Coding-agent
    # enablement is gated on `confined`, not `wrap`.
    break_glass_authorization = _validated_host_break_glass_authorization(opts.get("task"))
    if break_glass_authorization is not None:
        _prepare_host_break_glass_environment(break_glass_authorization)
    wrap = _openshell_enabled() and break_glass_authorization is None
    approved_macos_host = (
        sys.platform == "darwin"
        and os.environ.get("MAC_REPORT_EXECUTOR_APPROVED_PLATFORM") == "darwin"
        and os.environ.get("MAC_REPORT_EXECUTOR_APPROVED_ISOLATION_POSTURE")
        == REPORT_REPOSITORY_MACOS_HOST_POSTURE
        and all(
            os.environ.get(name)
            for name in (
                "MAC_REPORT_EXECUTOR_APPROVED_HOST_EXECUTOR_PATH",
                "MAC_REPORT_EXECUTOR_APPROVED_HOST_EXECUTOR_SHA256",
                "MAC_REPORT_EXECUTOR_APPROVED_PYTHON_PATH",
                "MAC_REPORT_EXECUTOR_APPROVED_PYTHON_SHA256",
                "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_PATH",
                "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_SHA256",
                "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_ROOT",
                "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_BUNDLE_SHA256",
                "MAC_REPORT_EXECUTOR_APPROVED_RUNTIME_CONFIG_SHA256",
            )
        )
    )
    if read_only_repository and (
        break_glass_authorization is not None or (not wrap and not approved_macos_host)
    ):
        raise RuntimeError(
            "read-only repository reports require per-task OpenShell confinement; "
            "direct, supervisor-only, and host break-glass execution are forbidden"
        )
    expected_git_control_digest = ""
    if read_only_repository and approved_macos_host and not wrap:
        _assert_approved_read_only_report_runtime(runtime_image_ref="")
        expected_git_control_digest = _capture_read_only_report_git_control(
            opts.get("task"), workspace
        )
    confined = (wrap or _openshell_required_for_local_agent()) and break_glass_authorization is None
    route: Dict[str, str] = {}
    resume = str(opts.get("resume_session") or "")
    agent_argv = _agent_argv(
        PROMPT_SENTINEL,
        workspace,
        confined=confined,
        task=opts.get("task"),
        chosen=route,
        **({"resume_session": resume} if resume else {}),
    )
    compiled_prompt = _compile_outbound_prompt(
        prompt,
        route.get("agent") or "universal",
        opts,
        audit_id=audit_id,
        model=route.get("model") or "",
        route_fingerprint=route.get("fingerprint") or "",
    )
    bundle = _write_agent_command_bundle(workspace, compiled_prompt, agent_argv)
    try:
        if wrap:
            sandbox_workspace = "%s/%s" % (
                _SANDBOX_WORKDIR,
                _workspace_basename(workspace),
            )
            result = _run_sandboxed(
                runner,
                bundle.argv(sandbox_workspace=sandbox_workspace),
                workspace,
                audit_id,
                _opts_with_route(opts, route),
            )
            return result
        result = runner(
            _unsandboxed_agent_argv(
                bundle.argv(),
                break_glass_authorization=break_glass_authorization,
            ),
            workspace,
            audit_id,
            {
                **_opts_with_route(opts, route),
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
        if expected_git_control_digest:
            setattr(result, "mac_read_only_git_control_digest", expected_git_control_digest)
        return result
    finally:
        bundle.cleanup()


def _agent_timeout() -> Optional[float]:
    """Bound a single agent run so a wedged TokenHub turn can't hang the loop
    forever. Default 7200s; set MAC_EXECUTOR_AGENT_TIMEOUT=0 to disable."""
    raw = env_str("MAC_EXECUTOR_AGENT_TIMEOUT")
    if not raw:
        return 7200.0
    try:
        val = float(raw)
    except ValueError:
        return 7200.0
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
                _write_startup_failclose_evidence(Path(workspace_raw), resolved_task_id, detail)
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
                task_workspace=task_workspace,
                task_id=task_id,
            )
        finally:
            revoke_task_inference_token()
            relay_observability.flush()
    return rc


def _read_only_report_repository_violation(task: Any, expected_git_control_digest: str = "") -> str:
    """Return a fail-closed reason when an inspection checkout was mutated."""

    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not metadata_declares_read_only_report_repository(metadata):
        return ""
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else None
    worktree_raw = env_str("MAC_TASK_REPO_WORKTREE") or (
        str(runtime.get("repository_worktree") or "") if isinstance(runtime, dict) else ""
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
        str(runtime.get("repository_base_sha") or "") if isinstance(runtime, dict) else ""
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
        str(runtime.get("repository_base_tree") or "") if isinstance(runtime, dict) else ""
    )
    tree = _git_for_read_only_verifier(worktree, ["rev-parse", "HEAD^{tree}"])
    if tree.returncode != 0 or not base_tree or tree.stdout.strip() != base_tree:
        return "read-only repository report changed repository tree"
    expected_refs_digest = env_str("MAC_TASK_REPO_REFS_DIGEST") or (
        str(runtime.get("repository_refs_digest") or "") if isinstance(runtime, dict) else ""
    )
    refs = _git_for_read_only_verifier(
        worktree, ["for-each-ref", "--format=%(refname) %(objectname)"]
    )
    observed_refs_digest = (
        hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest() if refs.returncode == 0 else ""
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
    # it is safe to remove the remaining disposable/ignored output.
    cleaned = _git_for_read_only_verifier(worktree, ["clean", "-fdx"])
    if cleaned.returncode != 0:
        return "could not clean read-only repository report disposable outputs"
    expected_content_digest = env_str("MAC_TASK_REPO_CONTENT_DIGEST") or (
        str(runtime.get("repository_content_digest") or "") if isinstance(runtime, dict) else ""
    )
    try:
        observed_content_digest = read_only_repository_content_digest(worktree)
    except OSError:
        observed_content_digest = ""
    if not expected_content_digest or observed_content_digest != expected_content_digest:
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


def _write_repository_verification_failure_manifest(
    task_workspace: Path,
    task: Any,
    failure: Mapping[str, Any],
) -> None:
    """Replace model-authored success with the authoritative verifier failure."""

    failure_payload = {
        "schema": str(failure.get("schema") or "mac.openshell_repository_verification.v1"),
        "passed": False,
        "failure_class": str(failure.get("failure_class") or "verifier_infrastructure"),
        "detail": str(
            failure.get("detail") or "repository verification failed without a causal detail"
        ),
        "retryable": bool(failure.get("retryable")),
        "attempt_count": max(0, int(failure.get("attempt_count") or 0)),
    }
    detail = "%s: %s" % (
        failure_payload["failure_class"],
        failure_payload["detail"],
    )
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "invalid",
        "evidence_type": "repo_change",
        "summary": "OpenShell repository verification failed.",
        "problems": [detail],
        "repository_verification": failure_payload,
        "task": {
            "id": task.get("id") if isinstance(task, dict) else None,
            "title": task.get("title") if isinstance(task, dict) else None,
            "project": task.get("project") if isinstance(task, dict) else None,
        },
    }
    (task_workspace / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Continuation: finish the work in the same Claude Code session
# ---------------------------------------------------------------------------
#: How many times one attempt may hand the agent its gate failure or the
#: judge's next steps and resume the same session before the attempt ends.
_CONTINUATION_ROUNDS_ENV = "MAC_CLAUDE_CONTINUATION_ROUNDS"
_DEFAULT_CONTINUATION_ROUNDS = 3
#: The judge's last verdict for this executor's task. Held in memory, never
#: in the workspace: the agent can write the workspace, and the verdict must
#: be the host's own observation when it is signed into the evidence.
_LAST_JUDGE_VERDICT: Dict[str, Any] = {}


def _continuation_rounds() -> int:
    try:
        return max(0, int(env_str(_CONTINUATION_ROUNDS_ENV) or _DEFAULT_CONTINUATION_ROUNDS))
    except ValueError:
        return _DEFAULT_CONTINUATION_ROUNDS


def _task_board_call(task_id: str, method: str, body: Optional[Dict[str, Any]] = None) -> Any:
    """The task board as this worker (which owns the task), or None."""
    base_url, token = _hub_env()
    if not base_url or not token or not task_id:
        return None
    from urllib.parse import quote

    url = "%s/tasks/%s/messages" % (base_url.rstrip("/"), quote(task_id, safe=""))
    if method == "GET":
        url += "?after=0&limit=500"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8") or "{}")


def _board_messages(task_id: str) -> List[Dict[str, Any]]:
    try:
        page = _task_board_call(task_id, "GET") or {}
    except Exception as exc:  # noqa: BLE001 - the board is advisory to the loop
        sys.stderr.write("[executor] task board unreadable: %s\n" % exc.__class__.__name__)
        return []
    return [m for m in page.get("messages") or [] if isinstance(m, dict)]


def _post_board_as_hub(task_id: str, kind: str, body: str, **metadata: Any) -> None:
    try:
        _task_board_call(
            task_id,
            "POST",
            {"kind": kind, "body": body, "author_kind": "hub", "metadata": metadata},
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[executor] could not post %s to the board: %s\n" % (kind, exc))


def _open_blocking_question(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The agent's latest blocking question, if nobody has answered it yet."""
    answered = {m.get("reply_to") for m in messages if m.get("kind") == "answer"}
    for message in reversed(messages):
        if (
            message.get("kind") == "question"
            and (message.get("metadata") or {}).get("blocking")
            and message.get("id") not in answered
        ):
            return message
    return None


_NEEDS_INPUT_MARKER = "needs-input.json"


def _write_needs_input_marker(workspace: Path, question: Mapping[str, Any]) -> None:
    """Tell the worker to park the task on this question (see worker)."""
    metadata = question.get("metadata") if isinstance(question.get("metadata"), dict) else {}
    item: Dict[str, Any] = {"question": str(question.get("body") or "")[:2000]}
    if metadata.get("options"):
        item["options"] = list(metadata["options"])[:12]
    marker = {
        "questions": [item],
        "why": "the agent asked on the task board (message #%s) and cannot continue without "
        "an answer; reply with `mac task say <task> --answer %s \"...\"`"
        % (question.get("id"), question.get("id")),
        "board_message_id": question.get("id"),
    }
    try:
        (workspace / _NEEDS_INPUT_MARKER).write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write("[executor] could not record the blocking question: %s\n" % exc)


def _latest_agent_handoff(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("author_kind") == "agent" and message.get("kind") in ("done", "status"):
            return str(message.get("body") or "")
    return ""


def _owner_direction(messages: List[Dict[str, Any]]) -> str:
    return "\n".join(
        "- %s" % m.get("body")
        for m in messages
        if m.get("author_kind") == "human" and m.get("kind") in ("directive", "answer")
    )


def _repository_context(workspace: Path) -> Tuple[Optional[Path], str]:
    try:
        context = json.loads((workspace / "repository-worktree.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, ""
    if not isinstance(context, dict) or not context.get("repository_worktree"):
        return None, ""
    return Path(str(context["repository_worktree"])), str(context.get("repository_base_sha") or "")


def _judge_task_change(task: Any, workspace: Path, messages: List[Dict[str, Any]]) -> Any:
    from . import task_judge

    worktree, base_sha = _repository_context(workspace)
    change = task_judge.collect_change(worktree, base_sha) if worktree is not None else ""
    base_url, token = _hub_env()
    if not base_url or not token:
        return task_judge.Verdict("unavailable", "no hub to reach a judge model through")
    return task_judge.judge(
        task if isinstance(task, dict) else {},
        change,
        task_judge.hub_completion(base_url, token),
        model=env_str(task_judge.JUDGE_MODEL_ENV) or task_judge.DEFAULT_JUDGE_MODEL,
        agent_summary=_latest_agent_handoff(messages),
        gate_summary="passed",
        board_direction=_owner_direction(messages),
    )


_CONTINUE_FOOTER = (
    "\n\nContinue in this session; your earlier work is all still here. When it is "
    "done, post `.mac-agent/board done \"...\"` again and stop."
)


def _continue_claude_session(
    runner: Callable[..., Any],
    task: Any,
    workspace: Path,
    task_id: str,
    result: Any,
    opts: Dict[str, Any],
) -> Any:
    """Hand gate failures and judge verdicts back to the same session.

    A repository test failure, or a ``not_met`` verdict, is not the end of
    the attempt: the agent resumes the session it worked in with the failure
    output or the judge's next steps, and keeps going. The attempt ends when
    the judge says ``met`` (or cannot run), when the agent is waiting on a
    blocking question, or after ``MAC_CLAUDE_CONTINUATION_ROUNDS`` rounds.
    """
    from . import coding_agent as _ca

    if _ca.selected_agent() != _ca.CLAUDE_AGENT:
        return result
    try:
        session_id = (workspace / _ca.CLAUDE_AGENT_DIR / "session-id").read_text(encoding="utf-8").strip()
    except OSError:
        return result
    if not session_id:
        return result
    for round_number in range(1, _continuation_rounds() + 1):
        messages = _board_messages(task_id)
        question = _open_blocking_question(messages)
        if question is not None:
            emit_telemetry("continuation_waiting_on_question", task_id=task_id, round=round_number)
            _write_needs_input_marker(workspace, question)
            return result
        gate_failure = getattr(result, "mac_repository_verification_failure", None)
        if isinstance(gate_failure, dict):
            if gate_failure.get("failure_class") != "repository_test_failed":
                # The verifier itself broke; that is not the agent's to fix.
                return result
            feedback = (
                "The repository's own test gate failed on your change:\n%s\n\nFix the change so "
                "the gate passes." % str(gate_failure.get("detail") or "")[-6000:]
            )
            _post_board_as_hub(task_id, "verdict", "repository gate failed; sent back to the agent", gate="failed")
        else:
            verdict = _judge_task_change(task, workspace, messages)
            _LAST_JUDGE_VERDICT.clear()
            _LAST_JUDGE_VERDICT.update({**verdict.to_dict(), "round": round_number})
            emit_telemetry("judge_verdict", task_id=task_id, round=round_number, verdict=verdict.verdict)
            if verdict.verdict == "unavailable":
                _post_board_as_hub(task_id, "verdict", "judge unavailable: %s" % verdict.reason, verdict="unavailable")
                return result
            _post_board_as_hub(
                task_id,
                "verdict",
                "%s: %s%s"
                % (
                    verdict.verdict,
                    verdict.reason,
                    ("\nNext: " + verdict.next_steps) if verdict.next_steps and not verdict.met else "",
                ),
                verdict=verdict.verdict,
                model=verdict.model,
            )
            if verdict.met:
                return result
            feedback = (
                "An independent judge reviewed your change against the task and found it not "
                "yet done.\nWhy: %s\nDo this next: %s" % (verdict.reason, verdict.next_steps)
            )
        emit_telemetry("continuation_resumed", task_id=task_id, round=round_number)
        result = _invoke_agent(
            runner,
            feedback + _CONTINUE_FOOTER,
            workspace,
            task_id or None,
            {**opts, "resume_session": session_id},
        )
    return result


def _record_judge_verdict_in_evidence(workspace: Path) -> None:
    """Put the judge's verdict into the evidence manifest the worker signs."""
    if not _LAST_JUDGE_VERDICT:
        return
    path = workspace / "mac-evidence.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(manifest, dict):
        return
    manifest["judge"] = dict(_LAST_JUDGE_VERDICT)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_executor(
    *,
    runner: Callable[..., Any],
    task: Any,
    task_workspace: Path,
    task_id: Any,
) -> int:
    """Inner executor body extracted so the relay scope wraps the whole run."""
    started = time.monotonic()
    break_glass_authorization = _validated_host_break_glass_authorization(task)
    # Planning-phase flag — determined after the scope estimate below.
    _is_planning = False
    _wanted_planning = False
    # Memory feed (in): recall prior deployment lessons so the agent works
    # with the fleet's hindsight. Best-effort — never blocks the run. On a
    # retry, lead with THIS task's own prior-attempt outcome (exact match,
    # highest-value hindsight) before the project-wide lessons.
    prior_attempt = recall_prior_attempt_lessons(task)
    project_lessons = recall_deployment_lessons(task)
    lessons: List[str] = prior_attempt + [
        lesson for lesson in project_lessons if lesson not in prior_attempt
    ]
    emit_telemetry(
        "started",
        task_id=task_id,
        kind="task",
        recalled_lessons=len(lessons),
        sandboxed=_openshell_enabled() and break_glass_authorization is None,
        execution_boundary=("host" if break_glass_authorization is not None else "sandbox"),
        break_glass_authorization_id=(
            break_glass_authorization.get("id") if break_glass_authorization is not None else None
        ),
    )

    # Scope-estimate preflight (scope-01): on the FIRST attempt of a task,
    # compute a deterministic scope estimate and record it as
    # metadata.scope_estimate on the hub.  Best-effort — never blocks the run.
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
    # metadata.plan_first=true, the first run PLANS instead of executing —
    # but only when this process can actually write children to the hub.
    _hub_capability: Dict[str, Any] = {}
    try:
        _hub_capability = hub_write_capability()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("hub connectivity probe failed: %s\n" % exc)
        _hub_capability = {
            "schema": "mac.sandbox_hub_connectivity.v1",
            "ready": False,
            "reason": "hub_probe_exception",
        }
    try:
        (task_workspace / "sandbox-hub-connectivity.json").write_text(
            json.dumps(_hub_capability, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        sys.stderr.write("hub connectivity record failed: %s\n" % exc)
    emit_telemetry(
        "sandbox_hub_connectivity",
        task_id=task_id,
        level="info" if _hub_capability.get("ready") else "warning",
        **{key: value for key, value in _hub_capability.items() if key != "schema"},
    )
    try:
        _wanted_planning = is_planning_phase(task)
        _is_planning = should_enter_planning_phase(task, hub_capability=_hub_capability)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("planning phase check failed: %s\n" % exc)
        _wanted_planning = False
        _is_planning = False
    if _wanted_planning and not _is_planning:
        emit_telemetry(
            "planning_phase_skipped",
            task_id=task_id,
            level="warning",
            reason=str(_hub_capability.get("reason") or "hub_writes_unavailable"),
            environment_fault=True,
        )

    if _is_planning:
        # plan-learn-01: enrich the planning prompt with prior decomposition
        # shapes for similar tasks so the second big migration starts from
        # the first one's shape.  Best-effort — never blocks the run.
        try:
            plan_lessons = recall_plan_lessons(task)
        except Exception:  # noqa: BLE001
            plan_lessons = []
        combined_lessons = (lessons or []) + (plan_lessons or [])
        prompt = build_planning_prompt(task, combined_lessons)
        emit_telemetry("planning_phase_started", task_id=task_id, level="info")
    else:
        prompt = build_task_prompt(task, lessons)
        if _wanted_planning:
            prompt = planning_phase_skip_notice(_hub_capability) + "\n\n" + prompt

    if break_glass_authorization is not None:
        prompt += _break_glass_prompt(break_glass_authorization)

    result = _invoke_agent(
        runner,
        prompt,
        task_workspace,
        str(task_id) if task_id else None,
        {
            "execution_kind": "task",
            "timeout": _agent_timeout(),
            "task": task,
        },
    )
    _task_metadata = task.get("metadata") if isinstance(task, dict) else None
    if (
        not _is_planning
        and break_glass_authorization is None
        and not metadata_declares_read_only_report_repository(_task_metadata)
        and not metadata_declares_report_deliverable(_task_metadata)
    ):
        result = _continue_claude_session(
            runner,
            task,
            task_workspace,
            str(task_id or ""),
            result,
            {"execution_kind": "task", "timeout": _agent_timeout(), "task": task},
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
    repository_verification_failure = getattr(result, "mac_repository_verification_failure", None)
    if not isinstance(repository_verification_failure, dict):
        repository_verification_failure = None
    authoritative_read_only_failure = bool(read_only_violation or read_only_verification_failure)
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
    elif repository_verification_failure is not None:
        _write_repository_verification_failure_manifest(
            task_workspace,
            task,
            repository_verification_failure,
        )
        emit_telemetry(
            "repository_verification_failed",
            task_id=task_id,
            level="warning",
            **repository_verification_failure,
        )
    clean_agent_failure = bool(getattr(result, "mac_clean_agent_failure", False))

    if repository_verification_failure is not None:
        emit_telemetry(
            "executor_finalization_skipped",
            task_id=task_id,
            level="warning",
            reason="repository_verification_failed",
            returncode=result.returncode,
        )
    elif clean_agent_failure:
        emit_telemetry(
            "executor_finalization_skipped",
            task_id=task_id,
            level="warning",
            reason="clean_agent_failure",
            returncode=result.returncode,
        )
    elif (
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
    else:
        try:
            # Never treat zero-child plan_decomposed as a completed plan.
            reject_empty_plan_decomposed_evidence(task_workspace)
            # Planning-phase runs produce evidence_type=plan_decomposed, not a
            # repo change.  Skip the git finalizer so a clean worktree is not
            # treated as a failure.  The host executor posts titled children
            # from the plan manifest.
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

    # Task-sizing: if the agent wrote plan_steps in its evidence, auto-post them
    # as child tasks so the parent blocks on the children.  Best-effort.
    if (
        not clean_agent_failure
        and not authoritative_read_only_failure
        and repository_verification_failure is None
    ):
        try:
            decomposed = maybe_auto_decompose(task_workspace, task)
            if decomposed:
                emit_telemetry("plan_decomposed", task_id=task_id, level="info")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("auto-decompose failed: %s\n" % exc)

    write_fallback_evidence_manifest(task_workspace, task, result, None)
    _record_judge_verdict_in_evidence(task_workspace)

    # loop-01 resilience: if the run was bounded/failed (e.g. a wedged TokenHub
    # trailing turn) but the agent or a deterministic finalizer already wrote a
    # complete, typed manifest, don't discard that verified work — finalize as
    # success. The downstream verification gate still validates the content.
    rc = result.returncode
    if (
        rc != 0
        and not authoritative_read_only_failure
        and repository_verification_failure is None
        and _manifest_is_complete(task_workspace)
    ):
        emit_telemetry(
            "evidence_salvaged", task_id=task_id, level="warning", original_returncode=rc
        )
        rc = 0

    # Memory feed (out): distill this run's outcome into a deployment lesson so
    # the fleet's recall gets richer with every task.
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
        "deployment_learning",
    ):
        record_deployment_learning(task, outcome)

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
