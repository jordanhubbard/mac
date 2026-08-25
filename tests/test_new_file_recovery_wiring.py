"""The new-file-refusal recovery must actually be WIRED into finalization.

task_e2ce62d9 implemented recover_from_new_file_refusal but nothing called it —
the executor still discarded finished work whenever the agent created new files
(observed live 2026-07-14: an implementation was written and thrown away three
times as verification_contract_failed). finalize_with_new_file_recovery is the
wiring: finalize -> attempt recovery (fail-closed) -> re-finalize on success.
"""

from __future__ import annotations

import pytest

import mac.executor_sandbox as es
from mac.repository_recovery import RepositoryRecoveryError


@pytest.fixture
def calls(monkeypatch):
    log = {"finalize": 0, "recover": 0, "telemetry": []}

    monkeypatch.setattr(
        es,
        "run_deterministic_git_finalizer",
        lambda ws, task: log.__setitem__("finalize", log["finalize"] + 1),
    )
    monkeypatch.setattr(
        es, "emit_telemetry", lambda name, **kw: log["telemetry"].append((name, kw))
    )
    return log


def test_non_new_file_refusal_leaves_single_finalize(calls, monkeypatch):
    def refuse(ws, task):
        calls["recover"] += 1
        raise RepositoryRecoveryError("preserved refusal is not a new-file refusal")

    monkeypatch.setattr(es, "recover_from_new_file_refusal", refuse)
    es.finalize_with_new_file_recovery("/ws", {"id": "task_x"}, "task_x")
    assert calls["finalize"] == 1  # no second finalize
    assert calls["recover"] == 1  # recovery attempted, fail-closed
    assert calls["telemetry"] == []  # no recovery event


def test_successful_recovery_refinalizes_and_emits(calls, monkeypatch):
    monkeypatch.setattr(
        es,
        "recover_from_new_file_refusal",
        lambda ws, task: {"recovered_files": ["src/new.py", "tests/test_new.py"]},
    )
    es.finalize_with_new_file_recovery("/ws", {"id": "task_x"}, "task_x")
    assert calls["finalize"] == 2  # refusal recovered -> finalizer re-run
    names = [n for n, _ in calls["telemetry"]]
    assert "new_file_refusal_recovered" in names
    _, kw = calls["telemetry"][0]
    assert kw.get("recovered_files") == 2


def test_unexpected_recovery_error_does_not_mask_the_run(calls, monkeypatch, capsys):
    monkeypatch.setattr(
        es,
        "recover_from_new_file_refusal",
        lambda ws, task: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    es.finalize_with_new_file_recovery("/ws", {"id": "task_x"}, "task_x")
    assert calls["finalize"] == 1
    assert "new-file recovery failed" in capsys.readouterr().err
