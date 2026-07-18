"""Subprocess execution helpers extracted from worker.py.

Contains:
  - _terminate_process_tree: recursive process-tree termination utility
  - SubprocessExecutor: callable executor that runs a subprocess for each task

These are imported back into worker.py and re-exported so callers that import
from mac.worker see no change.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mac.api_client import MacApiError
from mac.models import (
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    REPORT_REPOSITORY_READ_ONLY_MODE,
    metadata_declares_read_only_report_repository,
)
from mac.repository_access_env import fence_read_only_repository_environment
from mac.trusted_artifact import (
    nofollow_regular_file_identity,
    nofollow_source_bundle_digest,
)

JsonDict = Dict[str, Any]
CommandAuditSink = Callable[[JsonDict], None]

_PYTHON_IMPORT_OVERRIDE_ENV = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
    }
)


def _assert_approved_read_only_report_host_executor(
    argv: List[str], environment: Dict[str, str]
) -> None:
    """Revalidate every host-side executable/import artifact before spawn."""

    if len(argv) != 1 or Path(argv[0]).name != "mac-task-executor":
        raise RuntimeError(
            "read-only repository reports require the approved direct executor"
        )
    expected = {
        "executor_path": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_HOST_EXECUTOR_PATH", ""
        ),
        "executor_sha256": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_HOST_EXECUTOR_SHA256", ""
        ),
        "python_path": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_PYTHON_PATH", ""
        ),
        "python_sha256": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_PYTHON_SHA256", ""
        ),
        "script_path": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_PATH", ""
        ),
        "script_sha256": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_SHA256", ""
        ),
        "source_root": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_ROOT", ""
        ),
        "source_sha256": environment.get(
            "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_BUNDLE_SHA256", ""
        ),
    }
    if not all(expected.values()):
        raise RuntimeError(
            "read-only repository report lacks approved host artifact identities"
        )
    executor_path, executor_sha256 = nofollow_regular_file_identity(argv[0])
    python_candidate = environment.get("MAC_TASK_EXECUTOR_PYTHON", "")
    script_candidate = environment.get("MAC_TASK_EXECUTOR_SCRIPT", "")
    source_candidate = environment.get("MAC_SELF_UPDATE_REPO", "")
    if not python_candidate or not script_candidate or not source_candidate:
        raise RuntimeError(
            "read-only repository report host artifact paths are not configured"
        )
    python_path, python_sha256 = nofollow_regular_file_identity(
        Path(python_candidate).expanduser().resolve(strict=True)
    )
    script_path, script_sha256 = nofollow_regular_file_identity(script_candidate)
    source_root, source_sha256 = nofollow_source_bundle_digest(source_candidate)
    observed = {
        "executor_path": executor_path,
        "executor_sha256": executor_sha256,
        "python_path": python_path,
        "python_sha256": python_sha256,
        "script_path": script_path,
        "script_sha256": script_sha256,
        "source_root": source_root,
        "source_sha256": source_sha256,
    }
    if observed != expected:
        raise RuntimeError(
            "read-only repository report host artifacts differ from hub approval"
        )


def _terminate_process_tree(
    process: subprocess.Popen[Any], *, grace_seconds: float = 1.0
) -> None:
    """Terminate *process* and every descendant, including new sessions.

    Executor children are allowed to create their own process groups (the test
    watchdog does this deliberately), so killing only the executor's process
    group is insufficient.  ``psutil`` is a runtime dependency and gives us a
    cross-platform recursive tree walk; the process-group fallback also catches
    descendants that race between the walk and termination.
    """
    try:
        import psutil

        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.terminate()
        except psutil.NoSuchProcess:
            pass
        # Do not let psutil reap the direct child: ``subprocess.Popen`` must do
        # that itself or CPython can observe ChildProcessError and substitute a
        # false zero return code.  Waiting on descendants is safe.
        _, alive = psutil.wait_procs(
            descendants, timeout=max(0.0, float(grace_seconds))
        )
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
    except Exception:  # noqa: BLE001 - process cleanup must retain a fallback.
        pass

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if process.poll() is None:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _cargo_path_dirs() -> List[str]:
    """Return existing Rust/cargo tool bin directories, in priority order.

    launchd and other non-login-shell workers often run with a narrow PATH that
    excludes ``~/.cargo/bin``, so coding-agent children cannot find
    ``cargo``/``rustc``/``rustup`` even though the worker's own command
    inventory detects them.  This mirrors the ``_RUST_TOOL_CANDIDATES`` probe in
    worker.py: ``$CARGO_HOME/bin`` (falling back to ``~/.cargo/bin``),
    ``/usr/local/bin``, and ``/opt/homebrew/bin``.  Only directories that exist
    on disk are returned, and duplicates are dropped while preserving order.
    """
    cargo_home = os.environ.get("CARGO_HOME")
    cargo_bin = (
        Path(cargo_home) / "bin" if cargo_home else Path.home() / ".cargo" / "bin"
    )
    candidates = [cargo_bin, Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    dirs: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        try:
            if candidate.is_dir():
                dirs.append(text)
        except OSError:
            continue
    return dirs


class SubprocessExecutor:
    def __init__(self, argv: List[str], timeout: Optional[float] = None) -> None:
        if not argv:
            raise MacApiError("executor command is required")
        self.argv = argv
        self.timeout = timeout
        self.audit_sink: Optional[CommandAuditSink] = None
        self.audit_context: JsonDict = {}
        self._process_lock = threading.Lock()
        self._active_process: Optional[subprocess.Popen[Any]] = None
        self._cancel_reason = ""

    def has_active_process(self) -> bool:
        with self._process_lock:
            return self._active_process is not None

    def cancel_current(self, reason: str = "task assignment cancelled") -> bool:
        """Cancel the active executor and its complete descendant tree."""
        with self._process_lock:
            process = self._active_process
            if process is None:
                return False
            self._cancel_reason = str(reason or "task assignment cancelled")
        _terminate_process_tree(process)
        return True

    def __call__(self, task: JsonDict, task_dir: Path) -> Any:
        # Import helpers lazily to avoid a circular import: worker.py imports
        # SubprocessExecutor at module load time, and these functions live in
        # worker.py.  By deferring to call time, both modules are fully
        # initialised before the first real execution.
        from mac.worker import (  # noqa: PLC0415
            WorkerExecution,
            _audit_safe_argv,
            _coerce_process_output,
            _command_audit_id,
            _load_repository_context,
            _repository_context_audit_metadata,
            _repository_context_env,
            _repository_context_is_read_only_report,
            _sha256_text,
            _summary_from_output,
            _task_iteration_override,
            _task_model_override,
            _utcnow,
        )

        env = os.environ.copy()
        # These are task-scoped inputs, not worker defaults.  A long-lived
        # worker may itself have been launched from an operator shell (or an
        # older service definition) that carried values from a previous task.
        # Clear them before resolving the current task so an unpinned task
        # falls back to the coding agent's configured default and remains
        # inside that agent credential's model policy.
        env.pop("MAC_TASK_MODEL", None)
        env.pop("MAC_TASK_MAX_ITERATIONS", None)
        # Ensure Rust/cargo tool bin dirs are on the child PATH.  A
        # launchd/non-login-shell worker often runs with a narrow PATH that
        # excludes ~/.cargo/bin, so coding-agent children cannot find
        # cargo/rustc/rustup even though the worker's command inventory detects
        # them.  Prepend any existing-and-not-already-present cargo bin dir to
        # the child env's PATH, preserving the current entries and their order
        # after the injected dirs.  This is process-local: os.environ is never
        # mutated.
        current_path = env.get("PATH", "")
        existing_entries = current_path.split(os.pathsep) if current_path else []
        injected: List[str] = []
        for cargo_dir in _cargo_path_dirs():
            if cargo_dir not in existing_entries and cargo_dir not in injected:
                injected.append(cargo_dir)
        if injected:
            env["PATH"] = os.pathsep.join(injected + existing_entries)
        repository_context = _load_repository_context(task_dir)
        env.update(
            {
                "MAC_TASK_ID": task["id"],
                "MAC_TASK_FILE": str(task_dir / "task.json"),
                "MAC_TASK_WORKSPACE": str(task_dir),
            }
        )
        # Lease id and agent id ride into the child env so LLM completions carry
        # full route-context attribution (agent/task/lease) to the fleet router.
        lease_id = str(self.audit_context.get("lease_id") or "").strip()
        if lease_id:
            env["MAC_LEASE_ID"] = lease_id
        agent_id_ctx = str(self.audit_context.get("agent_id") or "").strip()
        if agent_id_ctx:
            env["MAC_AGENT_ID"] = agent_id_ctx
        # Per-task model override (metadata.model / metadata.runtime.model):
        # coding-CLI argv builders pass it as --model/-m, so a task
        # that declares a cheaper (or stronger) model gets it without any
        # fleet-wide config change. llm.route records requested/resolved
        # model per completion, so the override is visible in observability.
        model_override = _task_model_override(task, hub_client=getattr(self, "client", None))
        if model_override:
            env["MAC_TASK_MODEL"] = model_override
        iteration_override = _task_iteration_override(task)
        if iteration_override is not None:
            env["MAC_TASK_MAX_ITERATIONS"] = str(iteration_override)
        if repository_context:
            env.update(_repository_context_env(repository_context))
        read_only_repository = metadata_declares_read_only_report_repository(
            task.get("metadata") if isinstance(task, dict) else None
        )
        if read_only_repository:
            # Review tasks intentionally have no publication-shaped ``repo``
            # anchor, but retain the original report access declaration. Stamp
            # the mode from trusted task metadata so their OpenShell child is
            # fenced even when no repository context is attached.
            env["MAC_TASK_REPO_ACCESS_SCHEMA"] = REPORT_REPOSITORY_ACCESS_SCHEMA
            env["MAC_TASK_REPO_ACCESS_MODE"] = REPORT_REPOSITORY_READ_ONLY_MODE
        if read_only_repository or _repository_context_is_read_only_report(
            repository_context
        ):
            # Repository authentication has no role in an inspection-only
            # report.  Withhold every supported Git credential source from the
            # executor before it enters OpenShell (or an approved direct test
            # boundary), while leaving model-provider credentials untouched.
            fence_read_only_repository_environment(env)
        if read_only_repository:
            for name in _PYTHON_IMPORT_OVERRIDE_ENV:
                env.pop(name, None)
            env["PYTHONNOUSERSITE"] = "1"
            _assert_approved_read_only_report_host_executor(self.argv, env)
        command_id = _command_audit_id()
        started_at = _utcnow()
        started_monotonic = time.monotonic()
        base_record: JsonDict = {
            "command_id": command_id,
            "argv": _audit_safe_argv(self.argv),
            "cwd": str(task_dir),
            "task_id": self.audit_context.get("task_id") or task.get("id"),
            "lease_id": self.audit_context.get("lease_id"),
            "started_at": started_at,
            "metadata": {
                "argv_sha256": _sha256_text(json.dumps(self.argv, separators=(",", ":"))),
                **_repository_context_audit_metadata(repository_context),
                **_ensure_json_object(self.audit_context.get("metadata")),
            },
        }
        self._emit_audit({**base_record, "phase": "started"})
        try:
            # Files, rather than pipes, ensure a descendant that inherited
            # stdout/stderr cannot keep ``communicate`` blocked after the
            # declared executor exits.  The executor starts a new session so
            # timeout/cancellation has an additional process-group fence.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
                with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
                    process = subprocess.Popen(
                        self.argv,
                        cwd=str(task_dir),
                        env=env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                        start_new_session=True,
                    )
                    with self._process_lock:
                        self._cancel_reason = ""
                        self._active_process = process
                    timed_out = False
                    try:
                        process.wait(timeout=self.timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        _terminate_process_tree(process)
                        process.wait()
                    finally:
                        # A successful command may still have background
                        # descendants.  Retire them before releasing the task.
                        _terminate_process_tree(process, grace_seconds=0.0)
                        with self._process_lock:
                            cancel_reason = self._cancel_reason
                            self._active_process = None
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read()
                    stderr = stderr_file.read()
                    if timed_out:
                        raise subprocess.TimeoutExpired(
                            self.argv,
                            self.timeout or 0.0,
                            output=stdout,
                            stderr=stderr,
                        )
                    completed = subprocess.CompletedProcess(
                        self.argv,
                        int(process.returncode),
                        stdout,
                        stderr,
                    )
        except subprocess.TimeoutExpired as exc:
            completed_at = _utcnow()
            stdout = _coerce_process_output(exc.stdout)
            stderr = _coerce_process_output(exc.stderr)
            self._emit_audit(
                {
                    **base_record,
                    "phase": "timeout",
                    "completed_at": completed_at,
                    "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                    "stdout_sha256": _sha256_text(stdout),
                    "stderr_sha256": _sha256_text(stderr),
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr.encode("utf-8")),
                    "metadata": {
                        **base_record["metadata"],
                        "timeout_seconds": self.timeout,
                    },
                }
            )
            raise
        except OSError as exc:
            completed_at = _utcnow()
            self._emit_audit(
                {
                    **base_record,
                    "phase": "error",
                    "completed_at": completed_at,
                    "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                    "metadata": {**base_record["metadata"], "error": str(exc)},
                }
            )
            raise
        completed_at = _utcnow()
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        cancelled = bool(cancel_reason)
        self._emit_audit(
            {
                **base_record,
                "phase": (
                    "cancelled"
                    if cancelled
                    else "completed" if completed.returncode == 0 else "failed"
                ),
                "completed_at": completed_at,
                "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                "returncode": completed.returncode,
                "stdout_sha256": _sha256_text(stdout),
                "stderr_sha256": _sha256_text(stderr),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
                "metadata": {
                    **base_record["metadata"],
                    **({"cancel_reason": cancel_reason} if cancelled else {}),
                },
            }
        )
        return WorkerExecution(
            returncode=completed.returncode,
            summary=_summary_from_output(completed.returncode, stdout, stderr),
            stdout=stdout,
            stderr=stderr,
            metadata={
                "executor": _audit_safe_argv(self.argv),
                "executor_argv_sha256": base_record["metadata"]["argv_sha256"],
            },
        )

    def _emit_audit(self, record: JsonDict) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink(record)
        except Exception:
            pass


def _ensure_json_object(value: Any) -> JsonDict:
    """Return *value* as a dict, falling back to an empty dict."""
    if isinstance(value, dict):
        return value
    return {}
