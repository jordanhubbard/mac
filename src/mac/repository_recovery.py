"""Explicit recovery for verified repository work refused by the finalizer.

The normal worker finalizer deliberately refuses untracked or staged-new files:
an unattended process must not decide that arbitrary new files are intended
deliverables.  This module is the bounded operator path for the narrower case
where executor work was harvested, its repository contract test passed, and
publication failed only because the coding agent forgot to
commit named new files.

Recovery never invokes an executor or model.  It validates the preserved
workspace, requires an exact allow-list of every new file, commits with
provenance, rebases onto the canonical branch, reruns the contract gate, and
uses the shared guarded-push primitive.

Preserved test evidence is validated by gate *semantics*, not by argv spelling:
an approved repository-owned runner, bound to this task's prepared base, that
actually passed.  See ``_ACCEPTED_GATE_NOTE`` below.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from mac.gitops import (
    guarded_push,
    resolve_canonical_publication_target,
    sync_worktree_with_canonical,
)


JsonDict = Dict[str, Any]
_SHA_LENGTH = 40
_ALLOWED_REFUSAL_PREFIXES = (
    "untracked files present at finalize time",
    "new files were staged at finalize time",
    "repository finalizer had local errors; refusing to push",
)

# A stalled finalizer wrote finalizer-progress.json but never reached the
# terminal "complete"/"pass" status: the deterministic host process was
# interrupted (timeout, cancellation, or crash) after harvesting verified work
# but before the guarded push confirmed a remote ref. These are the progress
# statuses that mark such an interrupted, non-published run — anything else is
# either a healthy completion or an unrecognized shape we refuse to touch.
_STALLED_FINALIZER_STATUSES = (
    "running",
    "timeout",
    "cancelled",
    "fail",
)

# The repository contract names one gate command, but the executor sandbox
# deliberately routes that gate through the repository's own fail-closed sanity
# wrapper whenever the task carries a prepared base:
# ``scripts/run-sanity-tests.sh --base <prepared base sha>`` either runs the
# impact-selected subset (with diff-coverage enforced) or execs the whole-repo
# contract runner.  Requiring the contract command's exact argv spelling refused
# preserved evidence whose wrapper had in fact run the complete suite, stranding
# verified work that only needed its new files committed.
#
# Recovery therefore accepts a preserved gate on its semantics: an approved,
# repository-owned runner, bound by ``--base`` to this task's prepared base,
# that actually passed.  Anything else — an arbitrary command, an unapproved
# wrapper argument, a wrapper that is not committed in the preserved worktree,
# a missing/stale/mismatched base, or a nonzero result — is still refused.
# Acceptance only unblocks the recovery run; recovery still reruns the FULL
# contract command after rebasing onto canonical, so a focused wrapper run can
# never publish unverified work.
_ACCEPTED_GATE_NOTE = "approved repository test gate"
_SANITY_WRAPPER_COMMAND = "scripts/run-sanity-tests.sh"
# Arguments the sanity wrapper is allowed to carry in preserved evidence.  The
# wrapper forwards everything else to the selector, so an unrecognized flag is
# an unvalidated scope change, not an approved gate.
_SANITY_WRAPPER_FLAGS = ("--base", "--changed-file")
_SANITY_WRAPPER_ASSIGNMENTS = tuple("%s=" % flag for flag in _SANITY_WRAPPER_FLAGS)
# Shortest git abbreviation accepted for the wrapper's ``--base`` argument.
_MIN_BASE_ABBREVIATION = 7
# ``status`` values that agree with returncode 0.  An item with no status is
# judged on its returncode alone.
_PASSING_TEST_STATUSES = frozenset({"pass", "passed", "ok", "success"})


class RepositoryRecoveryError(RuntimeError):
    """Preserved work is not safe or complete enough to recover."""


def _load_object(path: Path) -> JsonDict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepositoryRecoveryError("could not read %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise RepositoryRecoveryError("%s must contain a JSON object" % path)
    return value


def _task_payload(workspace: Path) -> JsonDict:
    loaded = _load_object(workspace / "task.json")
    task = loaded.get("task", loaded)
    if not isinstance(task, dict):
        raise RepositoryRecoveryError("task.json does not contain a task object")
    return dict(task)


def _nested(value: Mapping[str, Any], *keys: str) -> JsonDict:
    node: Any = value
    for key in keys:
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key)
    return dict(node) if isinstance(node, Mapping) else {}


def _contract(task: Mapping[str, Any]) -> JsonDict:
    metadata = task.get("metadata") if isinstance(task, Mapping) else None
    if not isinstance(metadata, Mapping):
        return {}
    for candidate in (
        _nested(metadata, "execution_contract", "repository_contract"),
        _nested(metadata, "origin", "repository_contract"),
        _nested(metadata, "repository_contract"),
    ):
        if candidate:
            return candidate
    return {}


def _run_git(worktree: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_stdout(worktree: Path, args: Sequence[str]) -> str:
    result = _run_git(worktree, args)
    if result.returncode != 0:
        raise RepositoryRecoveryError(
            "git %s failed: %s" % (" ".join(args), (result.stderr or result.stdout).strip())
        )
    return result.stdout.strip()


def _porcelain_paths(worktree: Path) -> tuple[List[str], List[str]]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(worktree),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RepositoryRecoveryError(
            "could not inspect recovery worktree: %s"
            % ((result.stderr or result.stdout).decode(errors="replace").strip() or worktree)
        )
    changed: List[str] = []
    new_files: List[str] = []
    entries = result.stdout.split(b"\0")
    index = 0
    while index < len(entries):
        raw_bytes = entries[index]
        index += 1
        if not raw_bytes:
            continue
        raw = raw_bytes.decode("utf-8", errors="surrogateescape")
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:]
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(entries):
                raise RepositoryRecoveryError("malformed renamed path in git status")
            index += 1
        changed.append(path)
        if status == "??" or status[0] == "A":
            new_files.append(path)
    return sorted(set(changed)), sorted(set(new_files))


def _safe_relative_path(value: str) -> str:
    path = str(value or "").strip()
    if not path or path.startswith("/"):
        raise RepositoryRecoveryError("approved new-file path must be relative: %r" % value)
    parts = Path(path).parts
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise RepositoryRecoveryError("unsafe approved new-file path: %r" % value)
    return path


def _test_command(task: Mapping[str, Any]) -> str:
    test = _contract(task).get("test")
    return str(test.get("command") or "").strip() if isinstance(test, Mapping) else ""


def _prepared_base_sha(
    task: Mapping[str, Any],
    context: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> str:
    """The base the task worktree was prepared from, per preserved records."""

    repo = manifest.get("repo")
    for candidate in (
        context.get("repository_base_sha"),
        _nested(task, "metadata", "runtime").get("repository_base_sha"),
        repo.get("base_sha") if isinstance(repo, Mapping) else "",
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _is_hex(value: str) -> bool:
    return bool(value) and all(char in "0123456789abcdef" for char in value)


def _matches_prepared_base(candidate: str, prepared_base: str) -> bool:
    """True when *candidate* names the prepared base (full sha or abbreviation)."""

    prepared = prepared_base.strip().lower()
    given = candidate.strip().lower()
    if len(prepared) != _SHA_LENGTH or not _is_hex(prepared):
        return False
    if not _is_hex(given) or not (_MIN_BASE_ABBREVIATION <= len(given) <= _SHA_LENGTH):
        return False
    return prepared.startswith(given)


def _parse_sanity_wrapper(command: str) -> JsonDict | None:
    """Parse a sanity-wrapper command line from preserved test evidence.

    Returns ``None`` when *command* does not invoke the repository's sanity
    wrapper at all.  Otherwise returns ``{"base": ..., "approved": bool}``;
    ``approved`` is false when the invocation carries an argument outside the
    wrapper's approved surface, whose scope recovery cannot validate.
    """

    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    program = argv[0] if argv else ""
    if program.startswith("./"):
        program = program[2:]
    if program != _SANITY_WRAPPER_COMMAND:
        return None
    base = ""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in _SANITY_WRAPPER_FLAGS:
            if index + 1 >= len(argv):
                return {"base": base, "approved": False}
            value = argv[index + 1]
            index += 2
        elif token.startswith(_SANITY_WRAPPER_ASSIGNMENTS):
            token, value = token.split("=", 1)
            index += 1
        else:
            return {"base": base, "approved": False}
        if token == "--base":
            if base and base != value:
                return {"base": base, "approved": False}
            base = value
            continue
        try:
            _safe_relative_path(value)
        except RepositoryRecoveryError:
            return {"base": base, "approved": False}
    return {"base": base, "approved": True}


def _sanity_wrapper_is_repository_owned(worktree: Path) -> bool:
    """True when the wrapper is committed in the preserved worktree's HEAD.

    The recovery case is defined by uncommitted new files, so an on-disk
    wrapper proves nothing: only a wrapper that the repository itself carries
    at the evidenced HEAD is an approved gate.
    """

    return (
        _run_git(worktree, ["cat-file", "-e", "HEAD:%s" % _SANITY_WRAPPER_COMMAND]).returncode == 0
    )


def _test_item_passed(item: Mapping[str, Any]) -> bool:
    try:
        returncode = int(item.get("returncode", 1))
    except (TypeError, ValueError):
        return False
    if returncode != 0:
        return False
    status = str(item.get("status") or "").strip().lower()
    return status in _PASSING_TEST_STATUSES if status else True


def _classify_preserved_test_item(
    item: Mapping[str, Any],
    *,
    contract_command: str,
    prepared_base: str,
    worktree: Path,
) -> JsonDict | None:
    """Judge one preserved test item against approved repository gate semantics.

    Returns ``None`` when the item is not an approved gate at all (an arbitrary
    command recovery must ignore), otherwise a verdict record carrying
    ``accepted`` and a human-readable ``reason``.
    """

    command = str(item.get("command") or "").strip()
    if not command:
        return None
    verdict: JsonDict = {"command": command, "gate": "contract"}
    if command != contract_command:
        wrapper = _parse_sanity_wrapper(command)
        if wrapper is None:
            return None
        verdict["gate"] = "sanity_wrapper"
        base = str(wrapper["base"] or "").strip()
        if not wrapper["approved"]:
            verdict["reason"] = "sanity wrapper carries unapproved arguments"
        elif not _sanity_wrapper_is_repository_owned(worktree):
            verdict["reason"] = (
                "%s is not committed in the preserved worktree HEAD" % _SANITY_WRAPPER_COMMAND
            )
        elif not base:
            verdict["reason"] = "sanity wrapper ran without a --base prepared-base argument"
        elif not _matches_prepared_base(base, prepared_base):
            verdict["reason"] = "sanity wrapper base %r does not match the prepared base %r" % (
                base,
                prepared_base,
            )
        if "reason" in verdict:
            verdict["accepted"] = False
            return verdict
        verdict["base"] = base
    if not _test_item_passed(item):
        verdict["accepted"] = False
        verdict["reason"] = "gate did not pass (returncode=%r, status=%r)" % (
            item.get("returncode"),
            item.get("status"),
        )
        return verdict
    verdict["accepted"] = True
    verdict["reason"] = _ACCEPTED_GATE_NOTE
    return verdict


def _accepted_test_evidence(
    tests: Any,
    *,
    contract_command: str,
    prepared_base: str,
    worktree: Path,
) -> JsonDict:
    """Return the preserved gate result that authorizes recovery, or refuse."""

    refused: List[str] = []
    for item in tests if isinstance(tests, list) else []:
        if not isinstance(item, Mapping):
            continue
        verdict = _classify_preserved_test_item(
            item,
            contract_command=contract_command,
            prepared_base=prepared_base,
            worktree=worktree,
        )
        if verdict is None:
            continue
        if verdict["accepted"]:
            return verdict
        refused.append("%s: %s" % (verdict["command"], verdict["reason"]))
    detail = "; ".join(refused)
    raise RepositoryRecoveryError(
        "preserved contract-test evidence is not passing" + (" (%s)" % detail if detail else "")
    )


def _canonical_remote(task: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    contract = _contract(task)
    return str(
        contract.get("canonical_remote_url") or context.get("repository_canonical_remote_url") or ""
    ).strip()


def _canonical_branch(task: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    contract = _contract(task)
    return str(
        contract.get("default_branch") or context.get("repository_canonical_branch") or "main"
    ).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:%s" % digest.hexdigest()


def inspect_finalizer_recovery(
    workspace: Path | str,
    *,
    approved_new_files: Iterable[str] = (),
) -> JsonDict:
    """Validate preserved evidence and return a mutation-free recovery plan."""

    root = Path(workspace).expanduser().resolve()
    task = _task_payload(root)
    context = _load_object(root / "repository-worktree.json")
    manifest = _load_object(root / "mac-evidence.json")
    worker_result = _load_object(root / "worker-result.json")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RepositoryRecoveryError("preserved task has no id")
    if int(worker_result.get("returncode", 1)) != 0:
        raise RepositoryRecoveryError("original executor did not complete successfully")
    if manifest.get("schema") != "mac.worker_evidence.v1":
        raise RepositoryRecoveryError("preserved manifest has the wrong schema")
    if str(manifest.get("status") or "").lower() != "complete":
        raise RepositoryRecoveryError("preserved manifest is not complete")

    test_command = _test_command(task)
    if not test_command:
        raise RepositoryRecoveryError("repository contract test command is missing")

    problems = manifest.get("problems") or []
    if not isinstance(problems, list) or not problems:
        raise RepositoryRecoveryError("manifest is not a finalizer-refusal case")
    unexpected = [
        str(problem)
        for problem in problems
        if not str(problem).startswith(_ALLOWED_REFUSAL_PREFIXES)
    ]
    if unexpected:
        raise RepositoryRecoveryError(
            "manifest contains non-recoverable problems: %s" % "; ".join(unexpected)
        )

    repo = manifest.get("repo")
    if not isinstance(repo, Mapping):
        raise RepositoryRecoveryError("manifest lacks a repository anchor")
    if repo.get("pushed") is True or repo.get("dirty") is not True:
        raise RepositoryRecoveryError("manifest is not an unpushed dirty-worktree refusal")

    worktree = Path(str(context.get("repository_worktree") or "")).expanduser().resolve()
    if not worktree.is_dir():
        raise RepositoryRecoveryError("preserved repository worktree is missing: %s" % worktree)
    head_sha = _git_stdout(worktree, ["rev-parse", "HEAD"])
    manifest_head = str(repo.get("head_sha") or "").strip()
    if len(manifest_head) != _SHA_LENGTH or head_sha != manifest_head:
        raise RepositoryRecoveryError("preserved worktree HEAD no longer matches executor evidence")

    # Judged after the worktree anchor is proven, because an approved sanity
    # wrapper must be a committed repository file at the evidenced HEAD.
    test_evidence = _accepted_test_evidence(
        manifest.get("tests"),
        contract_command=test_command,
        prepared_base=_prepared_base_sha(task, context, manifest),
        worktree=worktree,
    )

    changed, new_files = _porcelain_paths(worktree)
    if not changed or not new_files:
        raise RepositoryRecoveryError("worktree no longer contains refused new files")
    approved = sorted({_safe_relative_path(path) for path in approved_new_files})
    if approved and approved != new_files:
        raise RepositoryRecoveryError(
            "approved new files must exactly match the preserved set; expected %s, got %s"
            % (new_files, approved)
        )

    return {
        "schema": "mac.repository_finalizer_recovery_plan.v1",
        "eligible": True,
        "task_id": task_id,
        "workspace": str(root),
        "worktree": str(worktree),
        "branch": str(context.get("repository_branch") or "").strip(),
        "base_sha": str(context.get("repository_base_sha") or "").strip(),
        "head_sha": head_sha,
        "canonical_remote": _canonical_remote(task, context),
        "canonical_branch": _canonical_branch(task, context),
        # The gate recovery reruns after rebasing is always the full contract
        # command, whatever spelling the preserved evidence carried.
        "test_command": test_command,
        "preserved_test_evidence": test_evidence,
        "changed_files": changed,
        "new_files": new_files,
        "approved_new_files": approved,
        "original_manifest_sha256": _sha256(root / "mac-evidence.json"),
        "original_result_sha256": _sha256(root / "worker-result.json"),
    }


def _default_test_runner(command: str, worktree: Path) -> subprocess.CompletedProcess[str]:
    timeout = int(os.environ.get("MAC_RECOVERY_TEST_TIMEOUT", "1800"))
    try:
        return subprocess.run(
            ["bash", "-lc", command],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            ["bash", "-lc", command],
            124,
            exc.stdout or "",
            (exc.stderr or "") + "\nrecovery test timed out",
        )


def recover_finalizer_worktree(
    workspace: Path | str,
    *,
    approved_new_files: Iterable[str],
    original_evidence_id: str,
    execute: bool = False,
    test_runner: Callable[[str, Path], subprocess.CompletedProcess[str]] = _default_test_runner,
    canonical_syncer: Callable[[Path, str, str], JsonDict] = sync_worktree_with_canonical,
    publisher: Callable[[Any], Any] = guarded_push,
) -> JsonDict:
    """Recover a refused worktree; dry-run unless ``execute`` is true."""

    plan = inspect_finalizer_recovery(
        workspace,
        approved_new_files=approved_new_files,
    )
    if not execute:
        return plan
    evidence_id = str(original_evidence_id or "").strip()
    if not evidence_id:
        raise RepositoryRecoveryError("--evidence-id is required with --execute")
    approved = plan["approved_new_files"]
    if not approved:
        raise RepositoryRecoveryError("--execute requires every new file via --approve-new-file")
    branch = str(plan["branch"] or "").strip()
    base_sha = str(plan["base_sha"] or "").strip()
    remote = str(plan["canonical_remote"] or "").strip()
    canonical_branch = str(plan["canonical_branch"] or "").strip()
    if not branch or not base_sha or not remote or not canonical_branch:
        raise RepositoryRecoveryError("repository context is incomplete for guarded publication")

    worktree = Path(plan["worktree"])
    add_tracked = _run_git(worktree, ["add", "-u"])
    if add_tracked.returncode != 0:
        raise RepositoryRecoveryError("could not stage tracked recovery changes")
    for path in approved:
        add_new = _run_git(worktree, ["add", "--", path])
        if add_new.returncode != 0:
            raise RepositoryRecoveryError("could not stage approved new file: %s" % path)
    _, remaining_new = _porcelain_paths(worktree)
    unapproved_new = sorted(set(remaining_new) - set(approved))
    if unapproved_new:
        raise RepositoryRecoveryError(
            "unapproved new files remain after staging: %s" % unapproved_new
        )
    staged = _run_git(worktree, ["diff", "--cached", "--quiet"])
    if staged.returncode != 1:
        raise RepositoryRecoveryError("recovery produced no staged repository change")

    task_id = str(plan["task_id"])
    task = _task_payload(Path(plan["workspace"]))
    title = str(task.get("title") or task_id).strip()
    commit = _run_git(
        worktree,
        [
            "-c",
            "user.email=mac-recovery@nvidia.com",
            "-c",
            "user.name=MAC recovery",
            "commit",
            "-m",
            "Recover MAC task %s: %s" % (task_id, title[:100]),
            "-m",
            "MAC-Original-Evidence: %s\nMAC-Recovery-Reason: new-file-finalizer-refusal"
            % evidence_id,
        ],
    )
    if commit.returncode != 0:
        raise RepositoryRecoveryError(
            "recovery commit failed: %s" % (commit.stderr or commit.stdout).strip()
        )

    sync = canonical_syncer(worktree, remote, canonical_branch)
    if str(sync.get("status") or "") not in {"fresh", "rebased"}:
        raise RepositoryRecoveryError(
            "recovery could not synchronize with canonical branch: %s"
            % (sync.get("reason") or sync.get("status"))
        )
    canonical_tip = str(sync.get("canonical_tip") or "").strip()
    if not canonical_tip:
        raise RepositoryRecoveryError("canonical synchronization did not return a tip")

    test = test_runner(str(plan["test_command"]), worktree)
    root = Path(plan["workspace"])
    (root / "recovery-test.stdout.txt").write_text(test.stdout or "", encoding="utf-8")
    (root / "recovery-test.stderr.txt").write_text(test.stderr or "", encoding="utf-8")
    if test.returncode != 0:
        raise RepositoryRecoveryError(
            "recovery contract test failed with returncode %d" % test.returncode
        )

    changed = [
        line
        for line in _git_stdout(
            worktree, ["diff", "--name-only", "%s..HEAD" % canonical_tip]
        ).splitlines()
        if line.strip()
    ]
    target = resolve_canonical_publication_target(
        worktree=worktree,
        canonical_remote=remote,
        canonical_branch=canonical_branch,
        destination_branch=branch,
        prepared_base_sha=base_sha,
        isolation_key="recovery-%s-%s" % (task_id, evidence_id),
    )
    publication = publisher(target)
    if not publication.ok or not publication.remote_verified:
        raise RepositoryRecoveryError("guarded recovery push failed: %s" % publication.error)

    result: JsonDict = {
        **plan,
        "schema": "mac.repository_finalizer_recovery.v1",
        "status": "complete",
        "original_evidence_id": evidence_id,
        "recovery_head_sha": publication.head_sha,
        "canonical_tip_sha": publication.canonical_tip_sha,
        "remote_ref": "refs/heads/%s" % branch,
        "remote_verified": publication.remote_verified,
        "changed_files": changed,
        "test": {
            "command": plan["test_command"],
            "returncode": test.returncode,
            "stdout_sha256": _sha256(root / "recovery-test.stdout.txt"),
            "stderr_sha256": _sha256(root / "recovery-test.stderr.txt"),
        },
    }
    (root / "recovery-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _finalizer_progress(root: Path) -> JsonDict:
    path = root / "finalizer-progress.json"
    if not path.exists():
        return {}
    return _load_object(path)


def inspect_stalled_finalizer_recovery(
    workspace: Path | str,
    *,
    approved_new_files: Iterable[str] = (),
) -> JsonDict:
    """Validate a stalled deterministic finalizer and return a recovery plan.

    Unlike :func:`inspect_finalizer_recovery`, which handles a finalizer that ran
    to completion but *refused* to publish uncommitted new files, this path
    handles a finalizer that was *interrupted* (timeout, cancellation, or crash)
    after harvesting verified work but before confirming a remote ref.  The
    signal is a ``finalizer-progress.json`` stuck in a non-terminal status paired
    with a partial ``mac-evidence.json`` manifest, and a worktree whose HEAD still
    matches the preserved evidence.  The inspection never mutates the worktree.
    """

    root = Path(workspace).expanduser().resolve()
    task = _task_payload(root)
    context = _load_object(root / "repository-worktree.json")
    manifest = _load_object(root / "mac-evidence.json")
    progress = _finalizer_progress(root)

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RepositoryRecoveryError("preserved task has no id")

    if not progress:
        raise RepositoryRecoveryError("no finalizer-progress.json — not a stalled-finalizer case")
    progress_status = str(progress.get("status") or "").strip().lower()
    if progress_status == "pass" or progress_status == "complete":
        raise RepositoryRecoveryError("finalizer progress reports completion — nothing to recover")
    if progress_status not in _STALLED_FINALIZER_STATUSES:
        raise RepositoryRecoveryError(
            "unrecognized finalizer progress status: %r" % (progress.get("status"),)
        )
    if str(progress.get("task_id") or "").strip() not in {"", task_id}:
        raise RepositoryRecoveryError("finalizer progress belongs to a different task")

    if manifest.get("schema") != "mac.worker_evidence.v1":
        raise RepositoryRecoveryError("preserved manifest has the wrong schema")
    interrupted = manifest.get("finalizer_interrupted")
    if not isinstance(interrupted, Mapping):
        raise RepositoryRecoveryError(
            "manifest lacks a finalizer_interrupted marker — not a stalled finalizer"
        )
    if manifest.get("partial") is not True:
        raise RepositoryRecoveryError("stalled-finalizer manifest must be partial")

    test_command = _test_command(task)
    if not test_command:
        raise RepositoryRecoveryError("repository contract test command is missing")

    repo = manifest.get("repo")
    if not isinstance(repo, Mapping):
        raise RepositoryRecoveryError("manifest lacks a repository anchor")
    if repo.get("pushed") is True:
        raise RepositoryRecoveryError("manifest reports the work was already published")

    worktree = Path(str(context.get("repository_worktree") or "")).expanduser().resolve()
    if not worktree.is_dir():
        raise RepositoryRecoveryError("preserved repository worktree is missing: %s" % worktree)
    head_sha = _git_stdout(worktree, ["rev-parse", "HEAD"])
    manifest_head = str(repo.get("head_sha") or "").strip()
    if manifest_head:
        if len(manifest_head) != _SHA_LENGTH or head_sha != manifest_head:
            raise RepositoryRecoveryError(
                "preserved worktree HEAD no longer matches finalizer evidence"
            )

    base_sha = str(context.get("repository_base_sha") or repo.get("base_sha") or "").strip()
    if len(base_sha) != _SHA_LENGTH:
        raise RepositoryRecoveryError("preserved base sha is missing or malformed")

    changed, new_files = _porcelain_paths(worktree)
    committed = [
        line
        for line in _git_stdout(
            worktree, ["diff", "--name-only", "%s..HEAD" % base_sha]
        ).splitlines()
        if line.strip()
    ]
    if not changed and not committed:
        raise RepositoryRecoveryError(
            "worktree carries no recoverable work relative to the prepared base"
        )

    approved = sorted({_safe_relative_path(path) for path in approved_new_files})
    if new_files and approved and approved != new_files:
        raise RepositoryRecoveryError(
            "approved new files must exactly match the preserved set; expected %s, got %s"
            % (new_files, approved)
        )

    return {
        "schema": "mac.repository_stalled_finalizer_recovery_plan.v1",
        "eligible": True,
        "task_id": task_id,
        "workspace": str(root),
        "worktree": str(worktree),
        "branch": str(context.get("repository_branch") or "").strip(),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "canonical_remote": _canonical_remote(task, context),
        "canonical_branch": _canonical_branch(task, context),
        "test_command": test_command,
        "progress_status": progress_status,
        "progress_phase": str(progress.get("phase") or "").strip(),
        "changed_files": changed,
        "committed_files": committed,
        "new_files": new_files,
        "approved_new_files": approved,
        "original_manifest_sha256": _sha256(root / "mac-evidence.json"),
    }


def recover_stalled_finalizer(
    workspace: Path | str,
    *,
    original_evidence_id: str,
    approved_new_files: Iterable[str] = (),
    execute: bool = False,
    test_runner: Callable[[str, Path], subprocess.CompletedProcess[str]] = _default_test_runner,
    canonical_syncer: Callable[[Path, str, str], JsonDict] = sync_worktree_with_canonical,
    publisher: Callable[[Any], Any] = guarded_push,
) -> JsonDict:
    """Resume a stalled finalizer; dry-run unless ``execute`` is true.

    Stages tracked changes and every approved new file, commits any pending work
    with stalled-finalizer provenance, rebases onto the canonical branch, reruns
    the contract test, and performs the shared guarded push.
    """

    plan = inspect_stalled_finalizer_recovery(
        workspace,
        approved_new_files=approved_new_files,
    )
    if not execute:
        return plan

    evidence_id = str(original_evidence_id or "").strip()
    if not evidence_id:
        raise RepositoryRecoveryError("--evidence-id is required with --execute")

    branch = str(plan["branch"] or "").strip()
    base_sha = str(plan["base_sha"] or "").strip()
    remote = str(plan["canonical_remote"] or "").strip()
    canonical_branch = str(plan["canonical_branch"] or "").strip()
    if not branch or not base_sha or not remote or not canonical_branch:
        raise RepositoryRecoveryError("repository context is incomplete for guarded publication")

    worktree = Path(plan["worktree"])
    approved = plan["approved_new_files"]
    if plan["new_files"] and not approved:
        raise RepositoryRecoveryError("--execute requires every new file via --approve-new-file")

    add_tracked = _run_git(worktree, ["add", "-u"])
    if add_tracked.returncode != 0:
        raise RepositoryRecoveryError("could not stage tracked recovery changes")
    for path in approved:
        add_new = _run_git(worktree, ["add", "--", path])
        if add_new.returncode != 0:
            raise RepositoryRecoveryError("could not stage approved new file: %s" % path)
    _, remaining_new = _porcelain_paths(worktree)
    unapproved_new = sorted(set(remaining_new) - set(approved))
    if unapproved_new:
        raise RepositoryRecoveryError(
            "unapproved new files remain after staging: %s" % unapproved_new
        )

    task_id = str(plan["task_id"])
    task = _task_payload(Path(plan["workspace"]))
    title = str(task.get("title") or task_id).strip()
    staged = _run_git(worktree, ["diff", "--cached", "--quiet"])
    if staged.returncode == 1:
        commit = _run_git(
            worktree,
            [
                "-c",
                "user.email=mac-recovery@nvidia.com",
                "-c",
                "user.name=MAC recovery",
                "commit",
                "-m",
                "Recover MAC task %s: %s" % (task_id, title[:100]),
                "-m",
                "MAC-Original-Evidence: %s\nMAC-Recovery-Reason: stalled-finalizer" % evidence_id,
            ],
        )
        if commit.returncode != 0:
            raise RepositoryRecoveryError(
                "recovery commit failed: %s" % (commit.stderr or commit.stdout).strip()
            )
    elif staged.returncode != 0:
        raise RepositoryRecoveryError("could not inspect staged recovery changes")

    if _git_stdout(worktree, ["rev-list", "--count", "%s..HEAD" % base_sha]) == "0":
        raise RepositoryRecoveryError("no committed work ahead of the prepared base after staging")

    sync = canonical_syncer(worktree, remote, canonical_branch)
    if str(sync.get("status") or "") not in {"fresh", "rebased"}:
        raise RepositoryRecoveryError(
            "recovery could not synchronize with canonical branch: %s"
            % (sync.get("reason") or sync.get("status"))
        )
    canonical_tip = str(sync.get("canonical_tip") or "").strip()
    if not canonical_tip:
        raise RepositoryRecoveryError("canonical synchronization did not return a tip")

    test = test_runner(str(plan["test_command"]), worktree)
    root = Path(plan["workspace"])
    (root / "stalled-recovery-test.stdout.txt").write_text(test.stdout or "", encoding="utf-8")
    (root / "stalled-recovery-test.stderr.txt").write_text(test.stderr or "", encoding="utf-8")
    if test.returncode != 0:
        raise RepositoryRecoveryError(
            "recovery contract test failed with returncode %d" % test.returncode
        )

    changed = [
        line
        for line in _git_stdout(
            worktree, ["diff", "--name-only", "%s..HEAD" % canonical_tip]
        ).splitlines()
        if line.strip()
    ]
    target = resolve_canonical_publication_target(
        worktree=worktree,
        canonical_remote=remote,
        canonical_branch=canonical_branch,
        destination_branch=branch,
        prepared_base_sha=base_sha,
        isolation_key="stalled-recovery-%s-%s" % (task_id, evidence_id),
    )
    publication = publisher(target)
    if not publication.ok or not publication.remote_verified:
        raise RepositoryRecoveryError("guarded recovery push failed: %s" % publication.error)

    result: JsonDict = {
        **plan,
        "schema": "mac.repository_stalled_finalizer_recovery.v1",
        "status": "complete",
        "original_evidence_id": evidence_id,
        "recovery_head_sha": publication.head_sha,
        "canonical_tip_sha": publication.canonical_tip_sha,
        "remote_ref": "refs/heads/%s" % branch,
        "remote_verified": publication.remote_verified,
        "changed_files": changed,
        "test": {
            "command": plan["test_command"],
            "returncode": test.returncode,
            "stdout_sha256": _sha256(root / "stalled-recovery-test.stdout.txt"),
            "stderr_sha256": _sha256(root / "stalled-recovery-test.stderr.txt"),
        },
    }
    (root / "stalled-recovery-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
