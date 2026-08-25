from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mac import read_only_report_verifier as verifier


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "verifier@example.invalid")
    _git(repo, "config", "user.name", "Verifier")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("exact base\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "--detach", "HEAD")
    _git(repo, "update-ref", "-d", "refs/heads/main")
    observed = verifier.exact_identity(repo)
    expected = {
        key: observed[key]
        for key in (
            "git_control_digest",
            "head",
            "tree",
            "refs_digest",
            "content_digest",
        )
    }
    return workspace, repo, expected


def _control(
    workspace: Path,
    repo: Path,
    expected: dict[str, str],
    *,
    problems: list[str] | None = None,
) -> dict:
    workspace_fd = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    worktree_fd = os.open(repo, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    return {
        "workspace": str(workspace),
        "worktree": str(repo),
        "workspace_fd": workspace_fd,
        "worktree_fd": worktree_fd,
        "expected": expected,
        "allowed_outputs": [],
        "problems": problems or [],
        "bootstrap": None,
        "test": {
            "command": "true",
            "returncode": 0,
            "status": "pass",
            "stdout": "",
            "stderr": "",
        },
        "environment_delta": {},
        "cgroup_quiescent": True,
    }


def _revalidate(control: dict) -> int:
    try:
        return verifier.revalidate_and_write(control)
    finally:
        os.close(control["worktree_fd"])
        os.close(control["workspace_fd"])


def test_permanent_tracked_mutation_cannot_produce_pass(tmp_path: Path) -> None:
    workspace, repo, expected = _repository(tmp_path)
    (repo / "tracked.txt").write_text("mutated\n", encoding="utf-8")

    assert _revalidate(_control(workspace, repo, expected)) != 0

    payload = json.loads((workspace / verifier.RESULT_NAME).read_text())
    assert payload["status"] == "fail"
    assert payload["returncode"] != 0
    assert payload["integrity"]["exact_base_revalidated"] is False
    assert any(
        "repository content" in problem or "status" in problem
        for problem in payload["integrity"]["problems"]
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")
def test_transient_modify_pass_restore_is_latched(tmp_path: Path) -> None:
    workspace, repo, _expected = _repository(tmp_path)
    monitor = verifier.ProtectedInputMonitor(workspace, repo, verifier.tracked_paths(repo))
    original = (repo / "tracked.txt").read_text(encoding="utf-8")
    try:
        (repo / "tracked.txt").write_text("transient mutation\n", encoding="utf-8")
        (repo / "tracked.txt").write_text(original, encoding="utf-8")
        violations = monitor.drain(settle_seconds=0.01)
    finally:
        monitor.close()

    assert any("tracked.txt" in item for item in violations)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")
def test_ignored_compile_output_passes_and_is_cleaned(tmp_path: Path) -> None:
    workspace, repo, expected = _repository(tmp_path)
    monitor = verifier.ProtectedInputMonitor(workspace, repo, verifier.tracked_paths(repo))
    try:
        output = repo / "build" / "object.o"
        output.parent.mkdir()
        output.write_bytes(b"compiled")
        violations = monitor.drain(settle_seconds=0.01)
    finally:
        monitor.close()

    assert violations == []
    assert _revalidate(_control(workspace, repo, expected)) == 0
    assert not (repo / "build").exists()
    payload = json.loads((workspace / verifier.RESULT_NAME).read_text())
    assert payload["status"] == "pass"
    assert payload["integrity"] == {
        "schema": verifier.INTEGRITY_SCHEMA,
        "immutable_inputs": True,
        "cgroup_quiescent": True,
        "fresh_control_process": True,
        "raw_git_control_first": True,
        "exact_base_revalidated": True,
        "problems": [],
    }


def test_detached_double_fork_writer_is_killed_before_result_race(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "detached.pid"
    raced_result = tmp_path / verifier.RESULT_NAME
    script = """
import os, pathlib, time
if os.fork():
    raise SystemExit(0)
os.setsid()
if os.fork():
    raise SystemExit(0)
pathlib.Path(%r).write_text(str(os.getpid()))
time.sleep(0.5)
pathlib.Path(%r).write_text('{"status":"pass","forged":true}')
""" % (str(pidfile), str(raced_result))
    subprocess.run([sys.executable, "-c", script], check=True)
    deadline = time.monotonic() + 2
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    detached_pid = int(pidfile.read_text())
    killed = False

    def candidates() -> list[int]:
        return [] if killed else [detached_pid]

    def kill(pid: int, signum: int) -> None:
        nonlocal killed
        assert pid == detached_pid
        assert signum == signal.SIGKILL
        os.kill(pid, signum)
        killed = True

    verifier.quiesce_sandbox_cgroup(
        candidate_provider=candidates,
        kill_fn=kill,
        settle_seconds=0.01,
    )
    time.sleep(0.6)

    assert killed is True
    assert not raced_result.exists()


def test_atomic_result_replaces_symlink_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("untouched\n", encoding="utf-8")
    result = tmp_path / verifier.RESULT_NAME
    result.symlink_to(outside)

    verifier.atomic_write_result(result, {"schema": "proof", "ok": True})

    assert not result.is_symlink()
    assert json.loads(result.read_text()) == {"schema": "proof", "ok": True}
    assert outside.read_text() == "untouched\n"


def test_cgroup_selection_ignores_sessions_and_process_groups(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"

    def process(pid: int, parent: int, cgroup: str = "/sandbox") -> None:
        root = proc / str(pid)
        root.mkdir(parents=True)
        (root / "status").write_text(
            "Uid:\t%d\t%d\t%d\t%d\nPPid:\t%d\n"
            % (os.getuid(), os.getuid(), os.getuid(), os.getuid(), parent)
        )
        (root / "cgroup").write_text("0::%s\n" % cgroup)

    process(100, 50)
    process(50, 1)
    process(1, 0)
    process(200, 1)  # double-forked/reparented peer, no ancestry relationship
    process(300, 1, "/other")
    assert verifier.sandbox_cgroup_candidates(current_pid=100, proc_root=proc) == [200]


def test_clip_and_trusted_git_fail_closed(monkeypatch, tmp_path: Path) -> None:
    assert verifier._clip("short", 10) == "short"
    clipped = verifier._clip("0123456789abcdef", 8)
    assert "chars omitted" in clipped

    monkeypatch.setattr(verifier.shutil, "which", lambda *_args, **_kwargs: None)
    with pytest.raises(verifier.VerificationError, match="unavailable"):
        verifier._absolute_git()

    writable = tmp_path / "git"
    writable.write_text("not really git", encoding="utf-8")
    writable.chmod(0o777)
    monkeypatch.setattr(verifier.shutil, "which", lambda *_args, **_kwargs: str(writable))
    with pytest.raises(verifier.VerificationError, match="immutable regular file"):
        verifier._absolute_git()


def test_git_helpers_reject_untrusted_or_failed_controls(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="not a directory"):
        verifier._git(repo, ["status"])

    failed = subprocess.CompletedProcess([], 1, "", "failure")
    monkeypatch.setattr(verifier, "_git", lambda *_args, **_kwargs: failed)
    with pytest.raises(verifier.VerificationError, match="enumerate tracked"):
        verifier.tracked_paths(repo)
    monkeypatch.setattr(verifier, "raw_git_control_digest", lambda _root: "raw")
    with pytest.raises(verifier.VerificationError, match="trusted Git head failed"):
        verifier.exact_identity(repo)


def test_identity_problems_reports_every_exact_base_violation() -> None:
    expected = {
        "git_control_digest": "a",
        "head": "b",
        "tree": "c",
        "refs_digest": "d",
        "content_digest": "e",
    }
    observed = {
        "git_control_digest": "wrong-a",
        "head": "wrong-b",
        "tree": "wrong-c",
        "refs_digest": "wrong-d",
        "content_digest": "wrong-e",
        "status": " M tracked.txt\n",
        "remotes": "origin\n",
    }

    problems = verifier.identity_problems(observed, expected)

    assert len(problems) == 7
    assert any("Git controls" in problem for problem in problems)
    assert any("HEAD" in problem for problem in problems)
    assert any("tree" in problem for problem in problems)
    assert any("refs" in problem for problem in problems)
    assert any("repository content" in problem for problem in problems)
    assert any("status" in problem for problem in problems)
    assert any("remote" in problem for problem in problems)


def test_monitor_path_classification_covers_all_security_boundaries() -> None:
    monitor = object.__new__(verifier.ProtectedInputMonitor)
    monitor.worktree_relative = "repo"
    monitor.tracked = {"tracked.txt", "tracked-dir/child.txt"}

    assert monitor._is_protected(verifier.RESULT_NAME) is True
    assert monitor._is_protected("repo/%s" % verifier.RESULT_NAME) is True
    assert monitor._is_protected("control.json") is True
    assert monitor._is_protected("repo") is True
    assert monitor._is_protected("repo/.git/index") is True
    assert monitor._is_protected("repo/tracked.txt") is True
    assert monitor._is_protected("repo/tracked-dir") is True
    assert monitor._is_protected("repo/build/object.o") is False


def test_monitor_requires_linux(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(verifier.sys, "platform", "darwin")
    with pytest.raises(verifier.VerificationError, match="requires Linux"):
        verifier.ProtectedInputMonitor(workspace, repo, [])


def test_proc_helpers_handle_missing_and_cyclic_process_metadata(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    assert verifier._proc_cgroup(10, proc) == ()
    assert verifier._proc_uid_and_parent(10, proc) is None

    for pid, parent in ((10, 20), (20, 10)):
        root = proc / str(pid)
        root.mkdir()
        (root / "status").write_text(
            "Uid:\t%d\nPPid:\t%d\n" % (os.getuid(), parent), encoding="utf-8"
        )
        (root / "cgroup").write_text("0::/sandbox\n", encoding="utf-8")
    assert verifier._ancestor_pids(10, proc) == {10, 20}


def test_cgroup_selection_filters_uid_metadata_and_nested_cgroups(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"

    def process(
        pid: int,
        parent: int,
        *,
        uid: int | None = None,
        cgroups: str = "0::/sandbox\n",
        include_status: bool = True,
    ) -> None:
        root = proc / str(pid)
        root.mkdir(parents=True)
        if include_status:
            value = os.getuid() if uid is None else uid
            (root / "status").write_text(
                "Uid:\t%d\nPPid:\t%d\n" % (value, parent), encoding="utf-8"
            )
        (root / "cgroup").write_text(cgroups, encoding="utf-8")

    process(100, 50)
    process(50, 1)
    process(1, 0)
    process(200, 1, cgroups="0::/sandbox/child\n")
    process(201, 1, uid=os.getuid() + 1)
    process(202, 1, include_status=False)
    process(203, 1, cgroups="0::/sandbox\n1:name:/extra\n")
    (proc / "not-a-pid").mkdir()

    assert verifier.sandbox_cgroup_candidates(current_pid=100, proc_root=proc) == [200]

    with pytest.raises(verifier.VerificationError, match="identify verifier"):
        verifier.sandbox_cgroup_candidates(current_pid=999, proc_root=proc)


def test_cgroup_selection_fails_closed_when_proc_cannot_be_enumerated(
    monkeypatch,
) -> None:
    class BrokenProc:
        def iterdir(self):
            raise OSError("unavailable")

    monkeypatch.setattr(verifier, "_proc_cgroup", lambda *_args: ("/sandbox",))
    monkeypatch.setattr(verifier, "_ancestor_pids", lambda *_args: {1})
    with pytest.raises(verifier.VerificationError, match="enumerate sandbox"):
        verifier.sandbox_cgroup_candidates(current_pid=10, proc_root=BrokenProc())


def test_quiescence_ignores_disappeared_process_and_rejects_permission() -> None:
    scans = iter(([200], [], [], []))

    def disappeared(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    verifier.quiesce_sandbox_cgroup(
        candidate_provider=lambda: next(scans),
        kill_fn=disappeared,
        settle_seconds=0,
        rounds=4,
    )

    with pytest.raises(verifier.VerificationError, match="could not terminate"):
        verifier.quiesce_sandbox_cgroup(
            candidate_provider=lambda: [200],
            kill_fn=lambda *_args: (_ for _ in ()).throw(PermissionError()),
            settle_seconds=0,
            rounds=3,
        )


def test_quiescence_rejects_remaining_and_unstable_empty_scans() -> None:
    with pytest.raises(verifier.VerificationError, match="remaining pids: 200"):
        verifier.quiesce_sandbox_cgroup(
            candidate_provider=lambda: [200],
            kill_fn=lambda *_args: None,
            settle_seconds=0,
            rounds=3,
        )

    scans = iter(([], [200], [], []))
    with pytest.raises(verifier.VerificationError, match="stably quiescent"):
        verifier.quiesce_sandbox_cgroup(
            candidate_provider=lambda: next(scans),
            kill_fn=lambda *_args: None,
            settle_seconds=0,
            rounds=3,
        )


def test_bounded_command_fences_credentials_and_records_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "must-not-leak")
    passed = verifier._run_bounded('printf "%s" "${GH_TOKEN-unset}"', tmp_path, timeout=5)
    assert passed["status"] == "pass"
    assert passed["stdout"] == "unset"
    assert passed["returncode"] == 0

    failed = verifier._run_bounded("printf failure >&2; exit 3", tmp_path, timeout=5)
    assert failed["status"] == "fail"
    assert failed["stderr"] == "failure"
    assert failed["returncode"] == 3


def test_bounded_command_kills_timeout(tmp_path: Path) -> None:
    result = verifier._run_bounded("sleep 2", tmp_path, timeout=0)
    assert result["status"] == "fail"
    assert result["returncode"] == 124
    assert "timed out" in result["error"]


@pytest.mark.parametrize(
    "raw, message",
    [
        ("/absolute", "unsafe declared"),
        ("../escape", "unsafe declared"),
        (".", "unsafe declared"),
        (".git", "overlaps Git"),
        (".git/index", "overlaps Git"),
    ],
)
def test_output_normalization_rejects_unsafe_paths(raw: str, message: str) -> None:
    with pytest.raises(verifier.VerificationError, match=message):
        verifier._normalized_output_paths(raw)


def test_output_normalization_deduplicates_paths() -> None:
    assert verifier._normalized_output_paths("build/out\nbuild/out\n") == ["build/out"]


def test_remove_nofollow_handles_missing_files_symlinks_and_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("preserved", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside)
    verifier._remove_nofollow(link, root)
    assert not link.exists()
    assert outside.read_text(encoding="utf-8") == "preserved"

    directory = root / "nested"
    directory.mkdir()
    (directory / "file").write_text("remove", encoding="utf-8")
    verifier._remove_nofollow(directory, root)
    assert not directory.exists()
    verifier._remove_nofollow(root / "missing", root)

    bad_parent = root / "bad-parent"
    bad_parent.symlink_to(tmp_path)
    with pytest.raises(verifier.VerificationError, match="non-directory"):
        verifier._remove_nofollow(bad_parent / "child", root)


def test_clean_outputs_rejects_git_failures_and_tracked_overlap(
    monkeypatch, tmp_path: Path
) -> None:
    failed = subprocess.CompletedProcess([], 1, "", "failed")
    monkeypatch.setattr(verifier, "_git", lambda *_args, **_kwargs: failed)
    with pytest.raises(verifier.VerificationError, match="could not clean"):
        verifier.clean_allowed_outputs(tmp_path, [])

    calls = iter(
        (
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "tracked.txt\n", ""),
        )
    )
    monkeypatch.setattr(verifier, "_git", lambda *_args, **_kwargs: next(calls))
    with pytest.raises(verifier.VerificationError, match="overlaps tracked"):
        verifier.clean_allowed_outputs(tmp_path, ["tracked.txt"])


def test_atomic_result_rejects_non_directory_descriptor(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_text("file", encoding="utf-8")
    descriptor = os.open(regular, os.O_RDONLY)
    try:
        with pytest.raises(verifier.VerificationError, match="not a directory"):
            verifier.atomic_write_result(
                tmp_path / verifier.RESULT_NAME,
                {"status": "fail"},
                directory_fd=descriptor,
            )
    finally:
        os.close(descriptor)


def test_expected_exact_base_environment_requires_every_value(monkeypatch) -> None:
    names = {
        "head": "MAC_TASK_REPO_BASE_SHA",
        "tree": "MAC_TASK_REPO_BASE_TREE",
        "refs_digest": "MAC_TASK_REPO_REFS_DIGEST",
        "content_digest": "MAC_TASK_REPO_CONTENT_DIGEST",
        "git_control_digest": "MAC_TASK_REPO_GIT_CONTROL_DIGEST",
    }
    for name in names.values():
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(verifier.VerificationError, match="context is incomplete"):
        verifier._expected_from_environment()
    for key, name in names.items():
        monkeypatch.setenv(name, key)
    assert verifier._expected_from_environment() == {key: key for key in names}


def test_revalidation_records_missing_test_bootstrap_and_mutation(
    tmp_path: Path,
) -> None:
    workspace, repo, expected = _repository(tmp_path)
    control = _control(
        workspace,
        repo,
        expected,
        problems=["transient tracked mutation"],
    )
    control["test"] = {}
    control["bootstrap"] = {
        "command": "make build",
        "returncode": 9,
        "status": "fail",
    }

    assert _revalidate(control) != 0
    payload = json.loads((workspace / verifier.RESULT_NAME).read_text())
    assert payload["status"] == "fail"
    assert payload["bootstrap"]["returncode"] == 9
    assert payload["integrity"]["immutable_inputs"] is False
    assert "repository contract test.command is missing" in payload["integrity"]["problems"]
    assert "repository bootstrap command failed" in payload["integrity"]["problems"]


def test_revalidation_rejects_missing_or_non_directory_descriptors(
    tmp_path: Path,
) -> None:
    with pytest.raises(verifier.VerificationError, match="descriptors are missing"):
        verifier.revalidate_and_write({})

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    first_fd = os.open(first, os.O_RDONLY)
    second_fd = os.open(second, os.O_RDONLY)
    try:
        with pytest.raises(verifier.VerificationError, match="changed type"):
            verifier.revalidate_and_write({"workspace_fd": first_fd, "worktree_fd": second_fd})
    finally:
        os.close(first_fd)
        os.close(second_fd)


def test_main_fails_closed_for_bad_controls_and_orchestrator_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verifier.sys, "stdin", io.StringIO("[]"))
    assert verifier.main(["--revalidate"]) == 70
    assert "not an object" in capsys.readouterr().err

    monkeypatch.setattr(
        verifier,
        "orchestrate",
        lambda: (_ for _ in ()).throw(verifier.VerificationError("boom")),
    )
    assert verifier.main([]) == 70
    assert "authoritative verifier failed: boom" in capsys.readouterr().err

    monkeypatch.setattr(verifier, "orchestrate", lambda: 7)
    assert verifier.main([]) == 7


def _orchestrator_fixture(
    monkeypatch,
    tmp_path: Path,
    *,
    monitor_problems: list[str] | None = None,
) -> tuple[Path, Path, dict[str, str], list[dict], list[str]]:
    workspace = tmp_path / "workspace"
    worktree = workspace / "repo"
    worktree.mkdir(parents=True)
    expected = {
        "head": "head",
        "tree": "tree",
        "refs_digest": "refs",
        "content_digest": "content",
        "git_control_digest": "controls",
    }
    observed = {**expected, "status": "", "remotes": ""}
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(workspace))
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(worktree))
    environment_names = {
        "MAC_TASK_REPO_BASE_SHA": "head",
        "MAC_TASK_REPO_BASE_TREE": "tree",
        "MAC_TASK_REPO_REFS_DIGEST": "refs",
        "MAC_TASK_REPO_CONTENT_DIGEST": "content",
        "MAC_TASK_REPO_GIT_CONTROL_DIGEST": "controls",
    }
    for name, value in environment_names.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(verifier, "exact_identity", lambda _worktree: observed)
    monkeypatch.setattr(verifier, "tracked_paths", lambda _worktree: {"tracked"})

    class Monitor:
        def __init__(self, *_args, **_kwargs):
            self.closed = False

        def drain(self):
            return list(monitor_problems or [])

        def close(self):
            self.closed = True

    monkeypatch.setattr(verifier, "ProtectedInputMonitor", Monitor)
    quiescence_calls: list[str] = []
    monkeypatch.setattr(
        verifier,
        "quiesce_sandbox_cgroup",
        lambda: quiescence_calls.append("quiesced"),
    )
    controls: list[dict] = []

    def fresh_control(_args, **kwargs):
        controls.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(verifier.subprocess, "run", fresh_control)
    return workspace, worktree, expected, controls, quiescence_calls


def test_orchestrate_fails_bootstrap_missing_outputs_and_preserves_evidence(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    workspace, _worktree, _expected, controls, quiescence = _orchestrator_fixture(
        monkeypatch,
        tmp_path,
        monitor_problems=["transient mutation"],
    )
    monkeypatch.setenv("MAC_REPO_BOOTSTRAP_COMMAND", "make build")
    monkeypatch.setenv("MAC_REPO_BOOTSTRAP_CREATES", "build/output")
    monkeypatch.setenv("MAC_REPO_TEST_COMMAND", "make smoke")
    monkeypatch.setenv("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "invalid")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    (toolchain / "environment-delta.json").write_text('{"added": ["qemu"]}\n', encoding="utf-8")
    monkeypatch.setenv("MAC_TOOLCHAIN_ROOT", str(toolchain))
    monkeypatch.setattr(
        verifier,
        "_run_bounded",
        lambda command, _worktree, timeout: {
            "command": command,
            "returncode": 0,
            "status": "pass",
            "stdout": "built",
            "stderr": "",
            "duration_ms": int(timeout),
        },
    )

    def failing_control(_args, **kwargs):
        controls.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess([], 17, "", "fresh control failed")

    monkeypatch.setattr(verifier.subprocess, "run", failing_control)

    assert verifier.orchestrate() == 17
    assert "fresh control failed" in capsys.readouterr().err
    assert quiescence == ["quiesced", "quiesced"]
    control = controls[0]
    assert control["bootstrap"]["status"] == "fail"
    assert control["bootstrap"]["missing_after"] == ["build/output"]
    assert control["test"]["stderr"] == ("repository bootstrap failed before verification tests")
    assert control["problems"] == ["transient mutation"]
    assert control["environment_delta"] == {"added": ["qemu"]}
    assert not (workspace / verifier.RESULT_NAME).exists()


def test_orchestrate_skips_existing_bootstrap_and_runs_test(monkeypatch, tmp_path: Path) -> None:
    _workspace, worktree, _expected, controls, quiescence = _orchestrator_fixture(
        monkeypatch, tmp_path
    )
    output = worktree / "build" / "output"
    output.parent.mkdir()
    output.write_text("existing", encoding="utf-8")
    monkeypatch.setenv("MAC_REPO_BOOTSTRAP_COMMAND", "make build")
    monkeypatch.setenv("MAC_REPO_BOOTSTRAP_CREATES", "build/output")
    monkeypatch.setenv("MAC_REPO_TEST_COMMAND", "make smoke")
    monkeypatch.setenv("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "12")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    (toolchain / "environment-delta.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("MAC_TOOLCHAIN_ROOT", str(toolchain))
    bounded_calls: list[tuple[str, float]] = []

    def bounded(command: str, _worktree: Path, timeout: float) -> dict:
        bounded_calls.append((command, timeout))
        return {
            "command": command,
            "returncode": 0,
            "status": "pass",
            "stdout": "tested",
            "stderr": "",
        }

    monkeypatch.setattr(verifier, "_run_bounded", bounded)

    assert verifier.orchestrate() == 0
    assert bounded_calls == [("make smoke", 12.0)]
    assert quiescence == ["quiesced", "quiesced"]
    control = controls[0]
    assert control["bootstrap"]["status"] == "skipped"
    assert control["bootstrap"]["reason"] == ("declared bootstrap outputs already exist")
    assert control["test"]["stdout"] == "tested"
    assert control["environment_delta"] == {}


def test_orchestrate_records_missing_test_without_bootstrap(monkeypatch, tmp_path: Path) -> None:
    _workspace, _worktree, _expected, controls, quiescence = _orchestrator_fixture(
        monkeypatch, tmp_path
    )
    monkeypatch.delenv("MAC_REPO_BOOTSTRAP_COMMAND", raising=False)
    monkeypatch.delenv("MAC_REPO_BOOTSTRAP_CREATES", raising=False)
    monkeypatch.delenv("MAC_REPO_TEST_COMMAND", raising=False)
    monkeypatch.setenv("MAC_TOOLCHAIN_ROOT", str(tmp_path / "missing-toolchain"))
    monkeypatch.setattr(
        verifier,
        "_run_bounded",
        lambda *_args, **_kwargs: pytest.fail("missing command must not run"),
    )

    assert verifier.orchestrate() == 0
    assert quiescence == ["quiesced"]
    control = controls[0]
    assert control["bootstrap"] is None
    assert control["test"]["status"] == "fail"
    assert control["test"]["returncode"] == 1
    assert control["environment_delta"] == {}


def test_orchestrate_rejects_initial_identity_before_monitor(monkeypatch, tmp_path: Path) -> None:
    _workspace, _worktree, _expected, controls, quiescence = _orchestrator_fixture(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        verifier,
        "exact_identity",
        lambda _worktree: {
            "head": "different",
            "tree": "tree",
            "refs_digest": "refs",
            "content_digest": "content",
            "git_control_digest": "controls",
            "status": "",
            "remotes": "",
        },
    )
    with pytest.raises(verifier.VerificationError, match="HEAD differs"):
        verifier.orchestrate()
    assert controls == []
    assert quiescence == []
