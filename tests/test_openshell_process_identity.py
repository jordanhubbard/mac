from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from mac.executor_sandbox import _sandbox_label_argv
from mac.openshell_sandbox_gc import (
    _darwin_process_identity,
    _linux_process_identity,
    _process_identity,
    classify_orphan_task_sandbox,
)


def _labels(argv: list[str]) -> dict[str, str]:
    return dict(value.split("=", 1) for value in argv[1::2])


def test_linux_identity_uses_boot_and_process_start(tmp_path) -> None:
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-1\n", encoding="ascii")
    (proc / "41").mkdir()
    fields_after_comm = ["S", *(["0"] * 18), "4242"]
    (proc / "41/stat").write_text(
        "41 (worker ) name) " + " ".join(fields_after_comm) + "\n", encoding="ascii"
    )

    assert _linux_process_identity(41, proc_root=str(proc), kill=lambda _pid, _sig: None) == (
        "present",
        "boot-1:4242",
    )


def test_darwin_identity_is_bounded_and_label_safe() -> None:
    calls: list[tuple[list[str], float]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        output = (
            "{ sec = 123, usec = 4 }\n" if "sysctl" in argv[0] else "Mon Sep 22 01:02:03 2026\n"
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    state, identity = _darwin_process_identity(41, kill=lambda _pid, _sig: None, run=run)

    assert state == "present"
    boot_id, start = identity.split(":", 1)
    assert boot_id.startswith("darwin-")
    assert start.startswith("start-")
    assert len(boot_id) == len("darwin-") + 64
    assert len(start) == len("start-") + 64
    assert [timeout for _argv, timeout in calls] == [2, 2]


def test_darwin_identity_timeout_is_unknown_but_not_absent() -> None:
    def timeout(_argv, **_kwargs):
        raise subprocess.TimeoutExpired("identity-probe", 2)

    assert _darwin_process_identity(41, kill=lambda _pid, _sig: None, run=timeout) == (
        "unknown",
        "",
    )


def test_unknown_platform_identity_is_explicit() -> None:
    assert _process_identity(os.getpid(), system_name="plan9") == ("unknown", "")


def test_unknown_creator_identity_does_not_block_sandbox_creation() -> None:
    labels = _labels(_sandbox_label_argv("task", process_identity=lambda _pid: ("unknown", "")))

    assert labels["mac.pid.identity"] == "unknown"
    assert "mac.boot.id" not in labels
    assert "mac.pid.start" not in labels

    # If that PID is later reused, missing incarnation proof preserves the
    # sandbox instead of deleting work owned by an unrelated live process.
    row = {"name": "mac-task-fixture", "phase": "Ready", "labels": labels}
    record = classify_orphan_task_sandbox(
        row, process_identity=lambda _pid: ("present", "another-boot:another-start")
    )
    assert record["reap"] is False
    assert "has no process identity" in record["reason"]


def test_parallel_label_creation_tolerates_identity_introspection_races() -> None:
    outcomes = [("present", "boot:42"), ("unknown", ""), ("absent", "")]

    def create(index: int) -> dict[str, str]:
        outcome = outcomes[index % len(outcomes)]
        return _labels(_sandbox_label_argv("task", process_identity=lambda _pid: outcome))

    # This is intentionally large enough for xdist workers to exercise the
    # creator path repeatedly while remaining deterministic without xdist.
    with ThreadPoolExecutor(max_workers=16) as pool:
        labels = list(pool.map(create, range(256)))

    assert len(labels) == 256
    assert {row["mac.pid.identity"] for row in labels} == {
        "verified",
        "unknown",
        "absent",
    }
