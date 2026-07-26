"""Repository worktree preparation helpers extracted from worker.py.

Contains:
  - RepoPrepMixin: mixin that provides all repository-worktree preparation
    methods to MacWorker

These are imported back into worker.py; callers that import from mac.worker
see no change.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from mac.fleet_learning import (
    RepositoryAccessError,
    build_repository_access_learning,
    build_repository_access_memory_payload,
    classify_repository_access_failure,
    resolve_git_remote_access,
)
from mac.repository_contract import (
    remote_branch_from_ref as _remote_branch_from_ref,
)
from mac.models import (
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    REPORT_REPOSITORY_READ_ONLY_MODE,
    metadata_declares_read_only_report_repository,
)
from mac.repository_access_env import read_only_repository_content_digest

JsonDict = Dict[str, Any]


# git emits these when a write fails because the filesystem is full. The
# message text is stable across git versions (it surfaces the underlying
# ``ENOSPC``/``errno 28`` from the OS), so a substring match is sufficient and
# does not depend on locale-translated errno strings.
_DISK_FULL_MARKERS = (
    "no space left on device",
    "errno 28",
    "enospc",
)


def _current_read_only_repository_contract(task: JsonDict) -> JsonDict:
    """Return only the current execution contract for an opted-in report.

    Historical origin contracts and runtime environment values are useful for
    ordinary compatibility paths, but they are not authority for this
    security boundary.  An incomplete current contract therefore fails closed
    instead of silently reviving a stale repository identity.
    """

    metadata = task.get("metadata") if isinstance(task, dict) else None
    execution = (
        metadata.get("execution_contract")
        if isinstance(metadata, dict)
        else None
    )
    contract = (
        execution.get("repository_contract")
        if isinstance(execution, dict)
        else None
    )
    if not isinstance(contract, dict):
        raise RuntimeError(
            "read-only repository report requires the current "
            "execution_contract.repository_contract"
        )
    return contract


def _read_only_contract_default_branch(task: JsonDict, _origin: JsonDict) -> str:
    contract = _current_read_only_repository_contract(task)
    branch = str(
        contract.get("default_branch") or contract.get("canonical_branch") or ""
    ).strip()
    if not branch:
        raise RuntimeError(
            "read-only repository report current "
            "execution_contract.repository_contract has no canonical branch"
        )
    return branch


def _scrub_read_only_git_transport_residue(
    worktree: Path, *, forbidden_values: tuple[str, ...]
) -> None:
    """Remove one-off fetch traces and fail if transport identity leaked to Git metadata."""

    git_dir = worktree / ".git"
    (git_dir / "FETCH_HEAD").unlink(missing_ok=True)
    shutil.rmtree(git_dir / "logs", ignore_errors=True)
    candidates = [
        git_dir / "config",
        git_dir / "FETCH_HEAD",
        git_dir / "objects" / "info" / "alternates",
    ]
    forbidden = [
        value.encode("utf-8", errors="surrogateescape")
        for value in forbidden_values
        if value
    ]
    for path in candidates:
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                "could not inspect isolated read-only Git metadata"
            ) from exc
        if any(value in payload for value in forbidden):
            raise RuntimeError(
                "isolated read-only Git metadata retained transport identity"
            )


def _finish_read_only_checkout(
    worktree: Path,
    *,
    base_sha: str,
    temporary_ref: str,
    forbidden_values: tuple[str, ...],
) -> tuple[str, str, str]:
    """Detach at *base_sha*, erase refs/transports, and return tree/ref/content proofs."""

    from mac.worker import _run_git  # noqa: PLC0415

    checkout = _run_git(worktree, ["checkout", "--detach", base_sha])
    if checkout.returncode != 0:
        raise RuntimeError("could not detach isolated read-only checkout at exact base")
    deleted = _run_git(worktree, ["update-ref", "-d", temporary_ref])
    if deleted.returncode != 0:
        raise RuntimeError("could not erase isolated read-only temporary ref")
    _scrub_read_only_git_transport_residue(
        worktree, forbidden_values=forbidden_values
    )
    remotes = _run_git(worktree, ["remote"])
    if remotes.returncode != 0 or remotes.stdout.strip():
        raise RuntimeError("isolated read-only repository unexpectedly has a remote")
    tree = _run_git(worktree, ["rev-parse", "HEAD^{tree}"])
    if tree.returncode != 0 or not tree.stdout.strip():
        raise RuntimeError("could not resolve isolated read-only repository base tree")
    refs = _run_git(
        worktree, ["for-each-ref", "--format=%(refname) %(objectname)"]
    )
    if refs.returncode != 0 or refs.stdout.strip():
        raise RuntimeError("isolated read-only repository unexpectedly contains refs")
    exclude = worktree / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".codegraph/" not in existing.splitlines():
        exclude.write_text(
            existing.rstrip("\n") + "\n.codegraph/\n", encoding="utf-8"
        )
    return (
        tree.stdout.strip(),
        hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest(),
        read_only_repository_content_digest(worktree),
    )


def _is_disk_full_error(text: str) -> bool:
    """Return True when *text* looks like a filesystem-full failure.

    ``git worktree add`` (and the ref-lock writes it performs) fail hard with
    these markers when the worker host disk is exhausted. Recognising them lets
    the caller reclaim stale workspaces just-in-time and retry instead of
    wedging the task into a permanent ``worker_exception``.
    """
    low = (text or "").lower()
    return any(marker in low for marker in _DISK_FULL_MARKERS)


class RepoPrepMixin:
    """Mixin that provides repository-worktree preparation to MacWorker.

    Relies on the following attributes being set by MacWorker.__init__:
      self.client, self.agent_id, self.self_update_repo
    """

    def _prepare_repository_worktree(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
    ) -> Optional[JsonDict]:
        from mac.worker import (  # noqa: PLC0415
            _inject_git_remote_auth,
            _redact_git_remote_auth,
            _redact_git_remote_auth_in_text,
            _repository_contract_canonical_remote,
            _repository_source_candidates,
            _repository_task_origin,
            _run_git,
            _run_git_in,
            _safe_path_component,
            _task_worktree_branch,
            _validate_git_ref,
            _validate_git_remote_url,
        )

        read_only_report = metadata_declares_read_only_report_repository(
            task.get("metadata")
        )
        origin = _repository_task_origin(task)
        if read_only_report:
            # Report inspection is deliberately independent of every registered
            # host checkout.  Parallel workers routinely leave those checkouts
            # dirty or advance their private refs; consulting one here would
            # make a credential-free analysis lane inherit unrelated repository
            # churn and even a read-only fetch would mutate its object store.
            # Resolve the authoritative current contract and prepare a wholly
            # disposable clone directly from its canonical remote instead.
            # The current execution contract is sufficient authority; a stale
            # origin is neither required nor consulted for repository identity.
            report_origin = origin if isinstance(origin, dict) else {}
            remote_url = self._resolve_repository_remote_url(task, report_origin)
            if not remote_url:
                raise RuntimeError(
                    "read-only repository report requires an authoritative "
                    "canonical remote URL"
                )
            return self._prepare_repository_worktree_from_remote(
                task, lease, task_dir, report_origin, remote_url
            )
        if origin is None:
            return None
        # K8s mode: when there is no usable local source on disk, fall
        # back to ``git clone <remote>`` into the task workspace. The
        # local-path branch is preferred when both are available (host
        # workers continue to use their pre-existing checkout). See
        # CLAUDE.md fork-audit notes for context.
        repository_path = str(origin.get("repository_path") or "").strip()
        local_source: Optional[Path] = None
        if repository_path:
            candidate = self._resolve_repository_source_path(origin)
            if candidate.exists():
                local_source = candidate
        if local_source is None:
            remote_url = self._resolve_repository_remote_url(task, origin)
            if remote_url:
                return self._prepare_repository_worktree_from_remote(
                    task, lease, task_dir, origin, remote_url
                )
            if repository_path:
                raise RuntimeError(
                    "repository source path does not exist: %s; tried %s"
                    % (
                        repository_path,
                        ", ".join(
                            str(c)
                            for c in _repository_source_candidates(origin, self.self_update_repo)
                        ),
                    )
                )
            raise RuntimeError(
                "repository task origin has neither a local repository_path "
                "nor a repository_url, repository contract canonical_remote_url, "
                "or MAC_TASK_REPO_URL env"
            )
        source = local_source

        top_level = _run_git(source, ["rev-parse", "--show-toplevel"])
        if top_level.returncode != 0 or not top_level.stdout.strip():
            raise RuntimeError(
                "repository source path is not a git worktree: %s" % source
            )
        source_root = Path(top_level.stdout.strip()).resolve()
        inside = _run_git(source_root, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeError(
                "repository source path is not a git worktree: %s" % source_root
            )

        dirty = _run_git(source_root, ["status", "--porcelain"])
        if dirty.returncode != 0:
            raise RuntimeError(
                "could not inspect repository source status: %s"
                % ((dirty.stderr or dirty.stdout or "").strip() or source_root)
            )
        dirty_paths = [line.strip() for line in dirty.stdout.splitlines() if line.strip()]
        if dirty_paths:
            self._observe_log(
                "worker.repository.source_dirty",
                level="warning",
                subject_type="task",
                subject_id=str(task.get("id") or ""),
                detail={
                    "repository_path": str(source_root),
                    "dirty_paths": dirty_paths[:50],
                    "dirty_path_count": len(dirty_paths),
                },
            )
            raise RuntimeError(
                "repository source checkout is dirty; refusing to run task outside an isolated clean base: %s"
                % source_root
            )

        # --- Canonical-base fetch (mac-stale-base fix) ---
        # Resolve the canonical remote and branch from the task contract.
        # Precedence: canonical_remote_url > origin.default_branch (for branch) / "origin" (for remote).
        # An explicit canonical URL that fails validation is a hard error (fail closed);
        # we do NOT silently fall back to the local "origin" remote so that an
        # operator misconfiguration cannot recreate the stale-HEAD problem.
        canonical_remote = _repository_contract_canonical_remote(task)
        if not canonical_remote:
            canonical_remote = str(origin.get("repository_url") or "").strip()
        # No explicit URL: resolve the real URL of the named "origin" remote so we
        # can validate and redact it.  An invalid or missing URL fails closed.
        if not canonical_remote:
            _origin_url_result = _run_git(source_root, ["remote", "get-url", "origin"])
            if _origin_url_result.returncode != 0 or not _origin_url_result.stdout.strip():
                raise RuntimeError(
                    "could not resolve URL for named 'origin' remote in %s; "
                    "refusing to fetch without a validated remote URL" % source_root
                )
            canonical_remote = _origin_url_result.stdout.strip()

        # Validate the resolved remote URL; raises ValueError on bad URL (fail closed).
        _validate_git_remote_url(canonical_remote)
        fetch_remote = _inject_git_remote_auth(canonical_remote)
        canonical_remote_display = _redact_git_remote_auth(fetch_remote)

        canonical_branch = (
            _read_only_contract_default_branch(task, origin)
            if metadata_declares_read_only_report_repository(task.get("metadata"))
            else str(origin.get("default_branch") or "").strip()
        )
        if not canonical_branch:
            canonical_branch = os.environ.get("MAC_TASK_REPO_DEFAULT_BRANCH", "").strip()
        if not canonical_branch:
            canonical_branch = "main"
        _validate_git_ref(canonical_branch)

        # Determine the per-lease fetch ref name before acquiring the lock so the
        # finally clause can reference it unconditionally.
        lease_id = str(lease.get("id") or "lease")
        tmp_ref = "refs/mac/fetch/%s" % _safe_path_component(lease_id)

        # Resolve the common git directory correctly even when .git is a file
        # (linked worktree).  Failure is a hard error — we must not fall back to
        # source_root, which would create a non-shared lock that cannot protect
        # concurrent access to the same shared git object store.
        _git_common_dir_result = _run_git(source_root, ["rev-parse", "--git-common-dir"])
        if _git_common_dir_result.returncode != 0 or not _git_common_dir_result.stdout.strip():
            raise RuntimeError(
                "could not resolve git common directory for %s: %s"
                % (
                    source_root,
                    (_git_common_dir_result.stderr or _git_common_dir_result.stdout or "").strip(),
                )
            )
        _raw_gcd = _git_common_dir_result.stdout.strip()
        _gcd = (
            (source_root / _raw_gcd).resolve()
            if not Path(_raw_gcd).is_absolute()
            else Path(_raw_gcd)
        )
        if not _gcd.exists():
            raise RuntimeError(
                "git common directory %r does not exist for repository %s" % (str(_gcd), source_root)
            )
        # Correction 1: require the resolved common git path to be a directory.
        # A path that exists but is a file must fail closed; we must never lock
        # its parent directory because that would create a non-shared lock that
        # cannot protect concurrent access to the same shared git object store.
        if not _gcd.is_dir():
            raise RuntimeError(
                "git common directory %r is not a directory for repository %s; "
                "refusing to lock its parent (fail closed)" % (str(_gcd), source_root)
            )
        _lock_dir = _gcd
        _lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = _lock_dir / "mac_prepare_worktree.lock"
        lock_fh = open(lock_path, "w")  # noqa: WPS515
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)

            # Resolve local prior SHA inside the lock so no concurrent fetch can
            # race the read.  A missing or unresolvable HEAD is a hard error.
            head = _run_git(source_root, ["rev-parse", "HEAD"])
            if head.returncode != 0 or not head.stdout.strip():
                raise RuntimeError(
                    "could not resolve repository source HEAD: %s"
                    % ((head.stderr or head.stdout or "").strip() or source_root)
                )
            local_prior_sha = head.stdout.strip()
            if not re.match(r"^[0-9a-f]{40}$", local_prior_sha):
                raise RuntimeError(
                    "repository source HEAD is not a valid commit SHA: %r" % local_prior_sha
                )

            # Fetch the canonical branch into a per-lease named ref to avoid
            # sharing FETCH_HEAD across concurrent preparations.
            fetch = _run_git(
                source_root,
                ["fetch", "--no-write-fetch-head", "--no-tags", fetch_remote,
                 "+refs/heads/%s:%s" % (canonical_branch, tmp_ref)],
            )
            _fetch_ok = fetch.returncode == 0
            if not _fetch_ok:
                if metadata_declares_read_only_report_repository(
                    task.get("metadata")
                ):
                    raise RuntimeError(
                        "could not fetch canonical base for read-only repository report: %s"
                        % _redact_git_remote_auth_in_text(
                            (fetch.stderr or fetch.stdout or "").strip()
                            or canonical_remote_display
                        )
                    )
                # Fetch failed — network may be unavailable (offline node) or
                # the remote is temporarily unreachable.  Log a warning and fall
                # back to the local HEAD so the task can still proceed.  This is
                # intentionally lenient: an offline worker should not block ALL
                # tasks; any resulting staleness is surfaced via
                # repository_local_prior_sha / repository_behind in the context.
                self._observe_log(
                    "worker.repository.worktree_fetch_failed",
                    level="warning",
                    subject_type="task",
                    subject_id=str(task.get("id") or ""),
                    detail={
                        "canonical_remote": canonical_remote_display,
                        "canonical_branch": canonical_branch,
                        "fetch_error": (fetch.stderr or fetch.stdout or "").strip() or str(source_root),
                        "fallback": "local_head",
                        "local_prior_sha": local_prior_sha,
                    },
                )
                base_sha = local_prior_sha
            else:
                fetched_sha_result = _run_git(source_root, ["rev-parse", tmp_ref])
                if fetched_sha_result.returncode != 0 or not fetched_sha_result.stdout.strip():
                    raise RuntimeError(
                        "could not resolve fetched ref %r after fetch from remote: %s"
                        % (
                            tmp_ref,
                            (fetched_sha_result.stderr or fetched_sha_result.stdout or "").strip(),
                        )
                    )
                base_sha = fetched_sha_result.stdout.strip()
                # Validate the fetched SHA is a well-formed commit SHA before using it.
                if not re.match(r"^[0-9a-f]{40}$", base_sha):
                    raise RuntimeError(
                        "fetched ref %r resolved to an invalid commit SHA: %r" % (tmp_ref, base_sha)
                    )

            # Correction 3: Verify the fetched ref resolves to a commit object,
            # not merely a well-formed 40-hex SHA.  Tags, blobs, and trees
            # resolve to 40-hex SHAs via rev-parse but are not commit objects;
            # a task worktree must always be based on a real commit.
            # Skip when falling back to local HEAD (which was already validated
            # as a commit via rev-parse HEAD above).
            if _fetch_ok:
                commit_verify_result = _run_git(
                    source_root, ["rev-parse", "--verify", "%s^{commit}" % tmp_ref]
                )
                if commit_verify_result.returncode != 0:
                    raise RuntimeError(
                        "fetched ref %r does not resolve to a commit object; "
                        "refusing to create task worktree on a non-commit object" % tmp_ref
                    )

            # Correction 2: Require rev-list ahead/behind to succeed and parse
            # exactly two non-negative integers.  Failure or malformed output is
            # a hard error — evidence must never emit null counts.
            ahead_behind_result = _run_git(
                source_root,
                ["rev-list", "--left-right", "--count",
                 "%s...%s" % (local_prior_sha, base_sha)],
            )
            if ahead_behind_result.returncode != 0 or not ahead_behind_result.stdout.strip():
                raise RuntimeError(
                    "could not compute ahead/behind counts for %s...%s: %s"
                    % (
                        local_prior_sha,
                        base_sha,
                        (ahead_behind_result.stderr or ahead_behind_result.stdout or "").strip(),
                    )
                )
            _ab_parts = ahead_behind_result.stdout.strip().split()
            if len(_ab_parts) != 2:
                raise RuntimeError(
                    "rev-list --left-right --count produced malformed output %r "
                    "(expected exactly two integers)" % ahead_behind_result.stdout.strip()
                )
            try:
                ahead_count: int = int(_ab_parts[0])
                behind_count: int = int(_ab_parts[1])
            except ValueError as _exc:
                raise RuntimeError(
                    "rev-list --left-right --count produced non-integer output %r: %s"
                    % (ahead_behind_result.stdout.strip(), _exc)
                ) from _exc
            if ahead_count < 0 or behind_count < 0:
                raise RuntimeError(
                    "rev-list --left-right --count produced negative counts %r; "
                    "this is unexpected and indicates a corrupt result" % ahead_behind_result.stdout.strip()
                )

            self._observe_log(
                "worker.repository.worktree_base_fetched",
                subject_type="task",
                subject_id=str(task.get("id") or ""),
                detail={
                    "local_prior_sha": local_prior_sha,
                    "base_sha": base_sha,
                    "canonical_remote": canonical_remote_display,
                    "canonical_branch": canonical_branch,
                    "ahead": ahead_count,
                    "behind": behind_count,
                    "source": "fetch_named_ref" if _fetch_ok else "local_head_fallback",
                },
            )

            worktree_dir = task_dir / ("repo-" + _safe_path_component(str(lease.get("id") or "lease")))
            if worktree_dir.exists():
                # The directory is lease-scoped and leases are exclusive, so an
                # existing dir here is OUR OWN debris from an interrupted prior
                # run of this same assignment (worker restart mid-attempt) —
                # not a foreign process to protect. Hard-failing here wedged
                # tasks every time a worker restarted while executing
                # (observed live: worker_exception -> blocked after each fleet
                # deploy). The ledger is canonical and the worktree is
                # attempt-local scratch: clean deterministically and re-prepare.
                existing_head = _run_git(worktree_dir, ["rev-parse", "HEAD"])
                if existing_head.returncode == 0 and existing_head.stdout.strip():
                    self._observe_log(
                        "worker.repository.stale_lease_worktree_reclaimed",
                        subject_type="task",
                        subject_id=str(task.get("id") or ""),
                        detail={
                            "worktree": str(worktree_dir),
                            "stale_head": existing_head.stdout.strip(),
                            "lease_id": str(lease.get("id") or ""),
                        },
                    )
                shutil.rmtree(worktree_dir)
            if metadata_declares_read_only_report_repository(task.get("metadata")):
                # A report inspection checkout is an independent clone, not a
                # linked worktree in the registered repository's object store.
                # It has no remote and is detached at the fetched canonical
                # base, so neither commits nor pushes can affect source state.
                initialize = _run_git_in(
                    task_dir,
                    [
                        "init",
                        "--quiet",
                        "--",
                        str(worktree_dir),
                    ],
                )
                if initialize.returncode != 0:
                    raise RuntimeError(
                        "could not initialize isolated read-only repository clone: %s"
                        % (
                            (initialize.stderr or initialize.stdout or "").strip()
                            or worktree_dir
                        )
                    )
                clone_tmp_ref = "refs/mac/read-only/base"
                fetch_base = _run_git(
                    worktree_dir,
                    [
                        "fetch",
                        "--no-write-fetch-head",
                        "--no-tags",
                        "--",
                        str(source_root),
                        "+%s:%s" % (tmp_ref, clone_tmp_ref),
                    ],
                )
                if fetch_base.returncode != 0:
                    raise RuntimeError(
                        "could not copy read-only repository base into isolated clone: %s"
                        % ((fetch_base.stderr or fetch_base.stdout or "").strip() or base_sha)
                    )
                fetched_base = _run_git(
                    worktree_dir, ["rev-parse", "--verify", "%s^{commit}" % clone_tmp_ref]
                )
                if (
                    fetched_base.returncode != 0
                    or fetched_base.stdout.strip() != base_sha
                ):
                    raise RuntimeError(
                        "isolated read-only repository fetch did not resolve the exact prepared base"
                    )
                base_tree, refs_digest, content_digest = _finish_read_only_checkout(
                    worktree_dir,
                    base_sha=base_sha,
                    temporary_ref=clone_tmp_ref,
                    forbidden_values=(str(source_root), fetch_remote),
                )
                context = {
                    "schema": "mac.repository_task_worktree.v1",
                    "checkout_policy": "task_owned_read_only_clone",
                    "repository_declared_path": "",
                    "repository_source_path": str(worktree_dir),
                    "repository_worktree": str(worktree_dir),
                    "repository_branch": "",
                    "repository_lease_id": lease_id,
                    "repository_base_sha": base_sha,
                    "repository_base_tree": base_tree,
                    "repository_refs_digest": refs_digest,
                    "repository_content_digest": content_digest,
                    "repository_local_prior_sha": local_prior_sha,
                    "repository_canonical_branch": canonical_branch,
                    "repository_canonical_remote_url": canonical_remote,
                    "repository_canonical_remote": canonical_remote_display,
                    "repository_ahead": ahead_count,
                    "repository_behind": behind_count,
                    "repository_origin_remote": canonical_remote_display,
                    "repository_access_mode": REPORT_REPOSITORY_READ_ONLY_MODE,
                    "repository_access_schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
                }
                self._observe_log(
                    "worker.repository.worktree_prepared",
                    subject_type="task",
                    subject_id=str(task.get("id") or ""),
                    detail=context,
                )
                return context

            branch = _task_worktree_branch(self.agent_id, str(task.get("id") or ""), str(lease.get("id") or ""))
            # mac-3qv6: prune any orphaned worktree registration in
            # source_root/.git/worktrees that points at the now-deleted
            # directory. Without this, `git worktree add` below fails with
            # "already exists" even though the on-disk directory is gone.
            _run_git(source_root, ["worktree", "prune"])

            def _add_worktree():
                # -B (not -b): the branch may survive from a reclaimed
                # interrupted run of this same lease; force-reset it to the
                # fresh base rather than failing on "branch already exists".
                return _run_git(
                    source_root,
                    ["worktree", "add", "-B", branch, str(worktree_dir), base_sha],
                )

            add = _add_worktree()
            if add.returncode != 0 and _is_disk_full_error(add.stderr or add.stdout or ""):
                # The worker host disk is full, so git could not write the
                # checkout or its ref lock. Historically this raised a bare
                # RuntimeError that the poll loop reported as a generic
                # ``worker_exception`` and re-blocked forever WITHOUT ever
                # freeing space (observed live across three attempts of a
                # dream-repair task). Reclaim stale completed-task workspaces
                # just-in-time (the same free-space-aware sweep the periodic
                # GC uses) and retry once before giving up.
                freed = self._reclaim_disk_for_worktree(
                    task_id=str(task.get("id") or ""),
                    worktree_dir=worktree_dir,
                )
                if freed:
                    _run_git(source_root, ["worktree", "prune"])
                    add = _add_worktree()
            if add.returncode != 0:
                raise RuntimeError(
                    "could not create repository task worktree: %s"
                    % ((add.stderr or add.stdout or "").strip() or worktree_dir)
                )
            context: JsonDict = {
                "schema": "mac.repository_task_worktree.v1",
                "checkout_policy": "task_owned_git_worktree",
                "repository_declared_path": str(origin.get("repository_path") or ""),
                "repository_source_path": str(source_root),
                "repository_worktree": str(worktree_dir),
                "repository_branch": branch,
                "repository_lease_id": lease_id,
                "repository_base_sha": base_sha,
                "repository_local_prior_sha": local_prior_sha,
                "repository_canonical_branch": canonical_branch,
                "repository_canonical_remote_url": canonical_remote,
                "repository_canonical_remote": canonical_remote_display,
                "repository_ahead": ahead_count,
                "repository_behind": behind_count,
                "repository_origin_remote": canonical_remote_display,
            }
            self._observe_log(
                "worker.repository.worktree_prepared",
                subject_type="task",
                subject_id=str(task.get("id") or ""),
                detail=context,
            )
            return context
        finally:
            try:
                # Delete the tmp fetch ref to keep the source repo tidy.
                _run_git(source_root, ["update-ref", "-d", tmp_ref])
            except Exception:  # noqa: BLE001
                pass
            lock_fh.close()

    def _reclaim_disk_for_worktree(self, *, task_id: str, worktree_dir: Path) -> bool:
        """Free workspace disk just-in-time after a full-disk worktree failure.

        Delegates to the free-space-aware ``WorkspaceGCMixin`` sweep when it is
        available (MacWorker mixes both in), which prunes completed-task
        workspaces while protecting the active task and the most-recent window.
        Returns True when a reclaim was attempted so the caller can retry the
        worktree add. Best-effort and never raises: a reclaim failure must not
        mask the original disk-full error.
        """
        gc_once = getattr(self, "_gc_workspaces_once", None)
        if not callable(gc_once):
            return False
        try:
            result = gc_once()
        except Exception as exc:  # noqa: BLE001 - reclaim is best-effort.
            self._observe_log(
                "worker.repository.disk_reclaim_failed",
                level="warning",
                subject_type="task",
                subject_id=task_id,
                detail={"worktree": str(worktree_dir), "error": str(exc)},
            )
            return True
        self._observe_log(
            "worker.repository.disk_reclaim_attempted",
            level="warning",
            subject_type="task",
            subject_id=task_id,
            detail={"worktree": str(worktree_dir), "gc": result},
        )
        return True

    def _resolve_repository_source_path(self, origin: JsonDict) -> Path:
        # Share the single source-resolution surface with the worktree
        # preservation primitive so a replaced/relocated source stays resolvable
        # across the same candidate locations (declared path, .mac-home
        # relative, mac_home/src/<name>, self_update_repo).
        from mac.worktree_preservation import resolve_source_path  # noqa: PLC0415

        return resolve_source_path(origin, self.self_update_repo)

    def _decide_source_worktree_preservation(
        self,
        origin: JsonDict,
        *,
        previous_source: Optional[Path] = None,
    ):
        """Decide whether linked worktrees must be preserved for this source.

        Thin wrapper over the injectable
        :func:`mac.worktree_preservation.decide_source_worktree_preservation`
        primitive so callers keep worktrees usable when the resolved source path
        changes.  Fail-closed on missing/unresolvable source.
        """
        from mac.worktree_preservation import (  # noqa: PLC0415
            decide_source_worktree_preservation,
        )

        return decide_source_worktree_preservation(
            origin,
            self.self_update_repo,
            previous_source=previous_source,
        )

    def _resolve_repository_remote_url(self, task: JsonDict, origin: JsonDict) -> str:
        """Return the remote clone URL for a worker without a local source.

        Explicit read-only reports accept only their current execution
        contract.  Ordinary tasks preserve the compatibility order of origin,
        durable contract, then ``MAC_TASK_REPO_URL``. Empty string means no
        ordinary-task remote is available. Invalid values fail closed without
        echoing possible secrets.
        """
        from mac.worker import (  # noqa: PLC0415
            _repository_contract_canonical_remote,
            _validate_git_remote_url,
        )

        read_only_report = metadata_declares_read_only_report_repository(
            task.get("metadata")
        )
        if read_only_report:
            contract = _current_read_only_repository_contract(task)
            raw = str(contract.get("canonical_remote_url") or "").strip()
            source = (
                "current execution_contract.repository_contract "
                "canonical_remote_url"
            )
            if not raw:
                raise RuntimeError(
                    "read-only repository report current "
                    "execution_contract.repository_contract has no "
                    "canonical_remote_url"
                )
        else:
            raw = str(origin.get("repository_url") or "").strip()
            source = "origin.repository_url"
        if not raw and not read_only_report:
            raw = _repository_contract_canonical_remote(task)
            source = "repository contract canonical_remote_url"
        if not raw and not read_only_report:
            raw = str(origin.get("repository_url") or "").strip()
            source = "origin.repository_url"
        if not raw and not read_only_report:
            raw = os.environ.get("MAC_TASK_REPO_URL", "").strip()
            source = "MAC_TASK_REPO_URL"
        if not raw:
            return ""
        try:
            return _validate_git_remote_url(raw)
        except ValueError:
            raise ValueError(
                "%s is invalid (value redacted); expected a supported git remote "
                "without embedded credentials" % source
            ) from None

    def _prepare_repository_worktree_from_remote(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
        origin: JsonDict,
        remote_url: str,
    ) -> JsonDict:
        """K8s-mode repository preparation: clone the remote into a
        per-lease directory and check out a task branch.

        This produces the same ``mac.repository_task_worktree.v1`` context
        shape as the local-worktree branch so downstream evidence,
        ``_load_repository_context`` and verification stay unchanged.
        """
        from mac.worker import (  # noqa: PLC0415
            _inject_git_remote_auth,
            _redact_git_remote_auth,
            _redact_git_remote_auth_in_text,
            _run_git,
            _run_git_in,
            _safe_path_component,
            _task_worktree_branch,
            _validate_git_ref,
        )

        worktree_dir = task_dir / (
            "repo-" + _safe_path_component(str(lease.get("id") or "lease"))
        )
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir)
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        read_only_report = metadata_declares_read_only_report_repository(
            task.get("metadata")
        )
        default_branch = (
            _read_only_contract_default_branch(task, origin)
            if read_only_report
            else str(origin.get("default_branch") or "").strip()
        )
        if not default_branch:
            default_branch = os.environ.get("MAC_TASK_REPO_DEFAULT_BRANCH", "").strip()
        if not default_branch:
            default_branch = "main"
        _validate_git_ref(default_branch)

        auth_url = _inject_git_remote_auth(remote_url)
        remote_display = _redact_git_remote_auth(auth_url)
        if read_only_report:
            initialize = _run_git_in(
                task_dir, ["init", "--quiet", "--", str(worktree_dir)]
            )
            if initialize.returncode != 0:
                raise RuntimeError(
                    "could not initialize isolated read-only repository clone: %s"
                    % (
                        (initialize.stderr or initialize.stdout or "").strip()
                        or worktree_dir
                    )
                )
            temporary_ref = "refs/mac/read-only/base"
            fetch = _run_git(
                worktree_dir,
                [
                    "fetch",
                    "--depth=1",
                    "--no-write-fetch-head",
                    "--no-tags",
                    "--",
                    auth_url,
                    "+refs/heads/%s:%s" % (default_branch, temporary_ref),
                ],
            )
            if fetch.returncode != 0:
                raise RuntimeError(
                    "could not fetch canonical base for read-only repository report: %s"
                    % _redact_git_remote_auth_in_text(
                        (fetch.stderr or fetch.stdout or "").strip()
                        or remote_display
                    )
                )
            fetched_base = _run_git(
                worktree_dir,
                ["rev-parse", "--verify", "%s^{commit}" % temporary_ref],
            )
            if fetched_base.returncode != 0 or not fetched_base.stdout.strip():
                raise RuntimeError(
                    "could not resolve read-only repository clone canonical base: %s"
                    % _redact_git_remote_auth_in_text(
                        (fetched_base.stderr or fetched_base.stdout or "").strip()
                        or worktree_dir
                    )
                )
            fetched_sha = fetched_base.stdout.strip()
        else:
            clone_args = [
                "clone",
                "--depth=1",
                "--branch",
                default_branch,
                "--",
                auth_url,
                str(worktree_dir),
            ]
            # ``git -C`` requires an existing directory; clone runs from the
            # parent so we use a separate code path (the helper expects the
            # repo arg to be cwd, so call git directly here).
            clone = _run_git_in(task_dir, clone_args)
            if clone.returncode != 0:
                raise RuntimeError(
                    "could not clone repository for K8s task: %s"
                    % _redact_git_remote_auth_in_text(
                        (clone.stderr or clone.stdout or "").strip() or remote_display
                    )
                )

        if read_only_report:
            base_tree, refs_digest, content_digest = _finish_read_only_checkout(
                worktree_dir,
                base_sha=fetched_sha,
                temporary_ref=temporary_ref,
                forbidden_values=(auth_url,),
            )
        head = _run_git(worktree_dir, ["rev-parse", "HEAD"])
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(
                "could not resolve cloned repository HEAD: %s"
                % ((head.stderr or head.stdout or "").strip() or worktree_dir)
            )
        base_sha = head.stdout.strip()
        if read_only_report:
            context: JsonDict = {
                "schema": "mac.repository_task_worktree.v1",
                "checkout_policy": "task_owned_read_only_clone",
                "repository_declared_path": "",
                "repository_source_path": str(worktree_dir),
                "repository_worktree": str(worktree_dir),
                "repository_branch": "",
                "repository_lease_id": str(lease.get("id") or ""),
                "repository_base_sha": base_sha,
                "repository_base_tree": base_tree,
                "repository_refs_digest": refs_digest,
                "repository_content_digest": content_digest,
                "repository_canonical_branch": default_branch,
                "repository_canonical_remote_url": remote_url,
                "repository_canonical_remote": remote_display,
                "repository_origin_remote": remote_display,
                "repository_access_mode": REPORT_REPOSITORY_READ_ONLY_MODE,
                "repository_access_schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
            }
            self._observe_log(
                "worker.repository.worktree_prepared",
                subject_type="task",
                subject_id=str(task.get("id") or ""),
                detail=context,
            )
            return context
        branch = _task_worktree_branch(
            self.agent_id, str(task.get("id") or ""), str(lease.get("id") or "")
        )
        checkout = _run_git(worktree_dir, ["checkout", "-b", branch])
        if checkout.returncode != 0:
            raise RuntimeError(
                "could not create task branch in cloned repository: %s"
                % ((checkout.stderr or checkout.stdout or "").strip() or branch)
            )

        # Mirror the local-worktree context shape exactly; downstream
        # readers (evidence validators, _load_repository_context) treat
        # the K8s clone identically to a host-mode git worktree.
        context: JsonDict = {
            "schema": "mac.repository_task_worktree.v1",
            "checkout_policy": "k8s_task_owned_clone",
            "repository_declared_path": str(origin.get("repository_path") or ""),
            "repository_source_path": str(worktree_dir),
            "repository_worktree": str(worktree_dir),
            "repository_branch": branch,
            "repository_lease_id": str(lease.get("id") or ""),
            "repository_base_sha": base_sha,
            "repository_canonical_branch": default_branch,
            "repository_canonical_remote_url": remote_url,
            "repository_canonical_remote": remote_display,
            "repository_origin_remote": remote_display,
        }
        self._observe_log(
            "worker.repository.worktree_prepared",
            subject_type="task",
            subject_id=str(task.get("id") or ""),
            detail=context,
        )
        return context

    def _prepare_review_workspace(
        self,
        task_id: str,
        review_id: str,
        executor_evidence_id: str,
        task_detail: JsonDict,
        message: JsonDict,
        claim_result: Optional[JsonDict] = None,
    ) -> Path:
        from mac.worker import (  # noqa: PLC0415
            _review_claim_identity,
            _review_input_task,
            _safe_path_component,
            _task_detail_evidence,
            ensure_json_object,
        )

        task_dir = self.workspace / "_reviews" / _safe_path_component(review_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        claim = ensure_json_object(claim_result)
        review_repository_context = self._prepare_review_repository_worktree(
            task_dir,
            task_detail,
            executor_evidence_id,
            review_id,
        )
        # Write the specific evidence and the original task as discrete workspace
        # files so the hermes executor can read them on demand.  This keeps the
        # review_context — and therefore the hermes --query prompt — to IDs only,
        # avoiding ARG_MAX blowup as evidence accumulates over a task's lifetime.
        executor_evidence = _task_detail_evidence(task_detail, executor_evidence_id)
        (task_dir / "executor-evidence.json").write_text(
            json.dumps(executor_evidence, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        original_task = (
            task_detail.get("task")
            if isinstance(task_detail.get("task"), dict)
            else {}
        )
        review_input_task = _review_input_task(original_task)
        (task_dir / "executor-task.json").write_text(
            json.dumps(review_input_task, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        review_context: JsonDict = {
            "task_id": task_id,
            "review_id": review_id,
            "executor_evidence_id": executor_evidence_id,
            "nudge_message_id": message.get("id"),
            "review_claim": _review_claim_identity(
                claim.get("claim")
                if isinstance(claim.get("claim"), dict)
                else {}
            ),
        }
        if review_repository_context is not None:
            review_context["review_repository_worktree"] = review_repository_context
        review_metadata = ensure_json_object(review_input_task.get("metadata"))
        original_metadata = ensure_json_object(original_task.get("metadata"))
        for key in ("review_model", "review_model_strength"):
            if original_metadata.get(key) not in (None, ""):
                review_metadata[key] = original_metadata[key]
        review_metadata["review_context"] = review_context
        task = {
            "id": "review_%s" % review_id,
            "title": "Review task %s" % task_id,
            "description": (
                "Review the executor evidence for task %s and write a signed "
                "review_verdict manifest." % task_id
            ),
            "required_capabilities": ["review"],
            "metadata": review_metadata,
        }
        if review_repository_context is not None:
            task["metadata"]["runtime"] = review_repository_context
        (task_dir / "task.json").write_text(
            json.dumps({"task": task}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

    def _prepare_review_repository_worktree(
        self,
        task_dir: Path,
        task_detail: JsonDict,
        executor_evidence_id: str,
        review_id: str,
    ) -> Optional[JsonDict]:
        from mac.worker import (  # noqa: PLC0415
            GIT_SHA_RE,
            _redact_git_remote_auth_in_text,
            _run_git,
            _task_detail_canonical_remote_url,
            _task_detail_evidence,
            _validate_git_ref,
            _validate_git_remote_url,
            ensure_json_object,
            strip_git_remote_auth,
        )

        evidence = _task_detail_evidence(task_detail, executor_evidence_id)
        manifest = ensure_json_object(
            ensure_json_object(evidence.get("metadata")).get("verification")
        )
        original_task = (
            task_detail.get("task")
            if isinstance(task_detail.get("task"), dict)
            else {}
        )
        if metadata_declares_read_only_report_repository(
            original_task.get("metadata")
        ):
            return self._prepare_read_only_review_repository_worktree(
                task_dir=task_dir,
                task_detail=task_detail,
                manifest=manifest,
                executor_evidence_id=executor_evidence_id,
                review_id=review_id,
            )
        repo = ensure_json_object(manifest.get("repo"))
        head_sha = str(repo.get("head_sha") or "").strip()
        if not GIT_SHA_RE.match(head_sha):
            return None
        # Carry the executor's TRUE base so the review can compute a non-empty
        # diff. Without this the review base defaulted to head_sha, making
        # base==head and files_changed always []. (mac review-worktree fix)
        base_sha = str(repo.get("base_sha") or "").strip()
        if base_sha and not GIT_SHA_RE.match(base_sha):
            base_sha = ""
        remote_ref = str(repo.get("remote_ref") or "").strip()
        # The task contract is the authoritative credential-free repository
        # identity.  Executor evidence can legitimately contain a display URL
        # with literal ``<redacted>`` userinfo, especially when it was produced
        # by an older fleet worker.  Prefer the contract and strip any HTTP
        # userinfo before validation so the reviewer injects its own credential
        # instead of attempting to clone a display-only value.
        remote_url = _task_detail_canonical_remote_url(task_detail)
        if not remote_url:
            remote_url = str(
                repo.get("remote_url")
                or repo.get("origin_url")
                or repo.get("clone_url")
                or ""
            ).strip()
        if not remote_url:
            repo_path_raw = str(repo.get("path") or "").strip()
            repo_path = Path(repo_path_raw).expanduser() if repo_path_raw else None
            if repo_path is not None and repo_path.exists():
                remote = _run_git(repo_path, ["remote", "get-url", "origin"])
                if remote.returncode == 0:
                    remote_url = remote.stdout.strip()
        if not remote_url:
            return None
        remote_url = strip_git_remote_auth(remote_url)

        # mac-raud: reject hostile remote_url before it reaches git argv.
        try:
            remote_url = _validate_git_remote_url(remote_url)
        except ValueError as exc:
            raise RuntimeError("refusing review clone: %s" % exc) from None
        if remote_ref:
            try:
                remote_ref = _validate_git_ref(remote_ref)
            except ValueError as exc:
                raise RuntimeError("refusing review clone: %s" % exc) from None

        task = task_detail.get("task") if isinstance(task_detail.get("task"), dict) else {}
        task_id = str(task.get("id") or "").strip()
        project = str(task.get("project") or "default").strip() or "default"
        access = resolve_git_remote_access(remote_url)

        def fail_repository_access(action: str, result: subprocess.CompletedProcess[str]) -> None:
            detail = _redact_git_remote_auth_in_text(
                (result.stderr or result.stdout or "non-zero exit").strip()
            )
            message = "%s %s: %s" % (action, access.display, detail)
            failure_class = classify_repository_access_failure(message)
            # A failed clone may leave a partial .git/config containing the
            # command-only credential. Remove the incomplete checkout before
            # writing or reporting anything about the failure.
            if review_repo.exists():
                shutil.rmtree(review_repo, ignore_errors=True)
            self._record_repository_access_learning(
                project=project,
                task_id=task_id,
                review_id=review_id,
                remote=remote_url,
                credential_source=access.credential_source,
                outcome="failure",
                error=message,
                failure_class=failure_class,
            )
            raise RepositoryAccessError(message, failure_class=failure_class)

        review_repo = task_dir / "review-repo"
        if review_repo.exists():
            shutil.rmtree(review_repo)
        # `--` separator means a remote_url that survives validation
        # still cannot be parsed as a git option.
        clone = subprocess.run(
            ["git", "clone", "--no-checkout", "--", access.remote, str(review_repo)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if clone.returncode != 0:
            fail_repository_access("could not clone review repository", clone)

        # ``git clone`` persists its source as origin. Scrub the command-only
        # credential immediately, then pass the authenticated URL explicitly
        # to later fetches so no token survives in the review checkout.
        scrub_origin = _run_git(review_repo, ["remote", "set-url", "origin", remote_url])
        if scrub_origin.returncode != 0:
            fail_repository_access("could not sanitize review repository origin", scrub_origin)

        branch = _remote_branch_from_ref(remote_ref)
        if branch:
            fetch = _run_git(
                review_repo,
                [
                    "fetch",
                    access.remote,
                    "+refs/heads/%s:refs/remotes/origin/%s" % (branch, branch),
                ],
            )
        elif remote_ref:
            # remote_ref was validated above; `--` guards against any
            # ref that pattern-matched a flag (mac-raud).
            fetch = _run_git(review_repo, ["fetch", access.remote, "--", remote_ref])
        else:
            fetch = _run_git(review_repo, ["fetch", access.remote])
        if fetch.returncode != 0:
            fail_repository_access("could not fetch reviewed ref %s from" % (remote_ref or "origin"), fetch)

        checkout = _run_git(review_repo, ["checkout", "--detach", head_sha])
        if checkout.returncode != 0:
            fail_repository_access("could not checkout reviewed head %s from" % head_sha, checkout)

        self._record_repository_access_learning(
            project=project,
            task_id=task_id,
            review_id=review_id,
            remote=remote_url,
            credential_source=access.credential_source,
            outcome="success",
        )
        context: JsonDict = {
            "schema": "mac.review_repository_worktree.v1",
            "checkout_policy": "review_git_worktree",
            "repository_worktree": str(review_repo),
            "repository_source_path": str(repo.get("path") or ""),
            "repository_branch": remote_ref or branch or "",
            "repository_base_sha": base_sha or head_sha,
            "repository_origin_remote": remote_url,
            "repository_review_id": review_id,
            "repository_executor_evidence_id": executor_evidence_id,
            "repository_reviewed_head_sha": head_sha,
            "repository_reviewed_remote_ref": remote_ref,
        }
        (task_dir / "repository-worktree.json").write_text(
            json.dumps(context, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._observe_log(
            "worker.review.repository_worktree_prepared",
            subject_type="task",
            subject_id=str((task_detail.get("task") or {}).get("id") or ""),
            detail=context,
        )
        return context

    def _prepare_read_only_review_repository_worktree(
        self,
        *,
        task_dir: Path,
        task_detail: JsonDict,
        manifest: JsonDict,
        executor_evidence_id: str,
        review_id: str,
    ) -> JsonDict:
        """Prepare a second credential-free exact-base clone for report review."""

        from mac.worker import (  # noqa: PLC0415
            GIT_SHA_RE,
            _redact_git_remote_auth_in_text,
            _run_git,
            _run_git_in,
            _validate_git_ref,
            _validate_git_remote_url,
            strip_git_remote_auth,
        )

        access_manifest = manifest.get("repository_access")
        if not isinstance(access_manifest, dict) or (
            access_manifest.get("schema") != REPORT_REPOSITORY_ACCESS_SCHEMA
            or access_manifest.get("mode") != REPORT_REPOSITORY_READ_ONLY_MODE
        ):
            raise RuntimeError(
                "read-only report review lacks host-stamped repository_access evidence"
            )
        base_sha = str(access_manifest.get("base_sha") or "").strip()
        base_tree = str(access_manifest.get("base_tree") or "").strip()
        refs_digest = str(access_manifest.get("refs_digest") or "").strip()
        content_digest = str(access_manifest.get("content_digest") or "").strip()
        if not GIT_SHA_RE.match(base_sha) or not all(
            (base_tree, refs_digest, content_digest)
        ):
            raise RuntimeError(
                "read-only report review repository_access proof is incomplete"
            )

        original_task = (
            task_detail.get("task")
            if isinstance(task_detail.get("task"), dict)
            else {}
        )
        task_id = str(original_task.get("id") or "").strip()
        project = str(original_task.get("project") or "default").strip() or "default"
        contract = _current_read_only_repository_contract(original_task)
        remote_url = str(contract.get("canonical_remote_url") or "").strip()
        if not remote_url:
            raise RuntimeError(
                "read-only report review current execution contract has no "
                "authoritative canonical remote"
            )
        remote_url = _validate_git_remote_url(strip_git_remote_auth(remote_url))
        canonical_branch = _read_only_contract_default_branch(original_task, {})
        try:
            canonical_branch = _validate_git_ref(canonical_branch)
        except ValueError as exc:
            raise RuntimeError(
                "read-only report review current execution contract has an "
                "invalid canonical branch"
            ) from exc
        evidence_remote = str(
            access_manifest.get("canonical_remote_url") or ""
        ).strip()
        if (
            not evidence_remote
            or strip_git_remote_auth(evidence_remote) != remote_url
        ):
            raise RuntimeError(
                "read-only report repository_access remote does not match current contract"
            )
        if (
            str(access_manifest.get("canonical_branch") or "").strip()
            != canonical_branch
        ):
            raise RuntimeError(
                "read-only report repository_access branch does not match current contract"
            )
        access = resolve_git_remote_access(remote_url)
        review_repo = task_dir / "review-repo"
        if review_repo.exists():
            shutil.rmtree(review_repo)

        def fail(action: str, result: subprocess.CompletedProcess[str]) -> None:
            detail = _redact_git_remote_auth_in_text(
                (result.stderr or result.stdout or "non-zero exit").strip()
            )
            message = "%s %s: %s" % (action, access.display, detail)
            failure_class = classify_repository_access_failure(message)
            shutil.rmtree(review_repo, ignore_errors=True)
            self._record_repository_access_learning(
                project=project,
                task_id=task_id,
                review_id=review_id,
                remote=remote_url,
                credential_source=access.credential_source,
                outcome="failure",
                error=message,
                failure_class=failure_class,
            )
            raise RepositoryAccessError(message, failure_class=failure_class)

        initialize = _run_git_in(
            task_dir, ["init", "--quiet", "--", str(review_repo)]
        )
        if initialize.returncode != 0:
            fail("could not initialize read-only review repository", initialize)
        temporary_ref = "refs/mac/read-only/review-base"
        fetch = _run_git(
            review_repo,
            [
                "fetch",
                "--no-write-fetch-head",
                "--no-tags",
                "--",
                access.remote,
                "+%s:%s" % (base_sha, temporary_ref),
            ],
        )
        if fetch.returncode != 0:
            fail("could not fetch exact read-only report base", fetch)
        fetched = _run_git(
            review_repo, ["rev-parse", "--verify", "%s^{commit}" % temporary_ref]
        )
        if fetched.returncode != 0 or fetched.stdout.strip() != base_sha:
            fail("read-only review fetch did not resolve exact executor base", fetched)
        observed_tree, observed_refs, observed_content = _finish_read_only_checkout(
            review_repo,
            base_sha=base_sha,
            temporary_ref=temporary_ref,
            forbidden_values=(access.remote,),
        )
        if (
            observed_tree != base_tree
            or observed_refs != refs_digest
            or observed_content != content_digest
        ):
            shutil.rmtree(review_repo, ignore_errors=True)
            raise RuntimeError(
                "independent read-only review clone does not match executor base proof"
            )
        self._record_repository_access_learning(
            project=project,
            task_id=task_id,
            review_id=review_id,
            remote=remote_url,
            credential_source=access.credential_source,
            outcome="success",
        )
        context: JsonDict = {
            "schema": "mac.review_repository_worktree.v1",
            "checkout_policy": "review_read_only_clone",
            "repository_worktree": str(review_repo),
            "repository_source_path": str(review_repo),
            "repository_branch": "",
            "repository_base_sha": base_sha,
            "repository_base_tree": base_tree,
            "repository_refs_digest": refs_digest,
            "repository_content_digest": content_digest,
            "repository_canonical_remote_url": remote_url,
            "repository_canonical_branch": canonical_branch,
            "repository_access_schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
            "repository_access_mode": REPORT_REPOSITORY_READ_ONLY_MODE,
            "repository_review_id": review_id,
            "repository_executor_evidence_id": executor_evidence_id,
        }
        (task_dir / "repository-worktree.json").write_text(
            json.dumps(context, indent=2, sort_keys=True), encoding="utf-8"
        )
        self._observe_log(
            "worker.review.read_only_repository_worktree_prepared",
            subject_type="task",
            subject_id=task_id,
            detail=context,
        )
        return context

    def _record_repository_access_learning(
        self,
        *,
        project: str,
        task_id: str,
        review_id: str,
        remote: str,
        credential_source: str,
        outcome: str,
        error: str = "",
        failure_class: str = "",
    ) -> Optional[JsonDict]:

        learning = build_repository_access_learning(
            project=project,
            remote=remote,
            operation="review_clone",
            agent_id=self.agent_id,
            outcome=outcome,
            credential_source=credential_source,
            task_id=task_id or None,
            review_id=review_id or None,
            error=error,
            failure_class=failure_class or None,
        )
        try:
            result = self.client.post(
                "/memory",
                build_repository_access_memory_payload(learning),
            )
        except Exception as exc:  # noqa: BLE001 - learning is best-effort.
            self._observe_log(
                "worker.repository_access_learning.failed",
                level="warning",
                subject_type="task" if task_id else None,
                subject_id=task_id or None,
                detail={
                    "schema": learning["schema"],
                    "operation": learning["operation"],
                    "outcome": outcome,
                    "repository_host": learning["repository_host"],
                    "error": str(exc),
                },
            )
            return None
        self._observe_log(
            "worker.repository_access_learning.recorded",
            level="info" if outcome == "success" else "warning",
            subject_type="task" if task_id else None,
            subject_id=task_id or None,
            detail={
                "schema": learning["schema"],
                "memory_id": result.get("id") if isinstance(result, dict) else None,
                "operation": learning["operation"],
                "outcome": outcome,
                "failure_class": learning["failure_class"],
                "repository_host": learning["repository_host"],
                "credential_source": learning["credential_source"],
            },
        )
        return result if isinstance(result, dict) else None
