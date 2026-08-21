"""Diagnosis and bounded retention for the cohort transaction journal.

Two defects made a single cohort transaction block every deploy for nine days:

A. A non-terminal transaction whose owning controller is dead was never
   reaped and never surfaced. The journal knew the owner was gone
   (``owner.alive`` is computed, not stored), but no command reported it, so
   the only symptom an operator saw was a per-agent SSH route failure from a
   cohort pinned days earlier.

B. Nothing aged out terminal journals. The directory reached 638 files
   spanning three months.

``diagnose`` answers A by naming the epoch, and ``reap`` answers B with a
bounded window that can never remove a live or unparseable journal.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-cohort-transaction.py"
SOURCE_COMMIT = "a" * 40


@pytest.fixture
def journal_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "mac_fleet_cohort_transaction_reaping", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(
    directory: Path, *args: str, check: bool = True
) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(directory), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout if result.returncode == 0 else result.stderr)
    if check:
        assert result.returncode == 0, payload
        assert payload["ok"] is True
    return payload


def dead_pid() -> int:
    """A pid that has exited, so ``os.kill(pid, 0)`` raises."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def cohort(names: list[str]) -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "stable_id": f"agent_{name}",
            "generation": f"generation-{index}",
            "deployment_id": f"deployment-{index}",
            "os": "linux",
            "supervisor": "systemd",
        }
        for index, name in enumerate(names)
    ]


def create(
    tmp_path: Path,
    directory: Path,
    epoch: str,
    *,
    names: list[str] | None = None,
) -> dict[str, Any]:
    cohort_file = tmp_path / f"cohort-{abs(hash(epoch))}.json"
    cohort_file.write_text(
        json.dumps(cohort(names or ["node-a"]), sort_keys=True), encoding="utf-8"
    )
    cohort_file.chmod(0o600)
    return run_cli(
        directory,
        "init",
        "--epoch",
        epoch,
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260719T054500Z",
        "--fleet",
        "rocky",
        "--hub-agent",
        "rocky",
        "--cohort-file",
        str(cohort_file),
        "--owner-nonce",
        "controller-nonce",
        "--owner-pid",
        str(os.getpid()),
    )["journal"]


def abort(directory: Path, epoch: str, revision: int) -> dict[str, Any]:
    return run_cli(
        directory,
        "abort",
        "--epoch",
        epoch,
        "--expected-revision",
        str(revision),
        "--operation-id",
        f"abort-{revision}",
        "--owner-nonce",
        "controller-nonce",
    )["journal"]


def journal_path(module: Any, directory: Path, epoch: str) -> Path:
    return directory / module._journal_name(epoch)


def patch(path: Path, **fields: Any) -> dict[str, Any]:
    """Rewrite journal fields the binding digest deliberately does not cover.

    ``owner`` and ``updated_at`` sit outside ``_binding_projection``, so a
    controller can die and a journal can age without invalidating the file.
    That is exactly the state under test.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in fields.items():
        if key == "owner":
            payload["owner"].update(value)
        else:
            payload[key] = value
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return payload


def clone_journal(module: Any, directory: Path, source: str, epoch: str) -> Path:
    """Durably fork a journal onto a second epoch id.

    ``init`` refuses to open a second incomplete epoch, which is precisely why
    a directory holding two live epochs is a state only a crash can produce --
    and the state diagnosis has to survive.
    """
    payload = json.loads(
        journal_path(module, directory, source).read_text(encoding="utf-8")
    )
    payload["epoch_id"] = epoch
    payload["binding_sha256"] = module._sha256(module._binding_projection(payload))
    target = journal_path(module, directory, epoch)
    target.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    return target


def make_dead_owner(module: Any, directory: Path, epoch: str) -> int:
    """Mark the owning controller gone the way a crash does."""
    pid = dead_pid()
    patch(
        journal_path(module, directory, epoch),
        owner={
            "pid": pid,
            # Even if the pid is recycled, the recorded start identity no
            # longer matches, so the owner is still provably not this process.
            "process_start_sha256": "0" * 64,
        },
    )
    return pid


def age(module: Any, directory: Path, epoch: str, iso: str) -> None:
    patch(journal_path(module, directory, epoch), updated_at=iso)


def test_stuck_transaction_with_dead_owner_is_reported_by_epoch(
    journal_module: Any, tmp_path: Path
) -> None:
    """The nine-day outage, reduced: report the epoch instead of replaying it."""
    directory = tmp_path / "journal"
    epoch = f"{SOURCE_COMMIT}:20260811T211348Z:controller"
    create(tmp_path, directory, epoch, names=["natasha", "ghost-worker5"])
    pid = make_dead_owner(journal_module, directory, epoch)
    age(journal_module, directory, epoch, "2026-08-11T21:13:48Z")

    payload = run_cli(directory, "diagnose")
    assert [item["epoch_id"] for item in payload["stuck"]] == [epoch]
    stuck = payload["stuck"][0]
    assert stuck["schema"] == "mac.fleet_cohort_transaction_diagnosis.v1"
    assert stuck["terminal"] is False
    assert stuck["stuck_dead_owner"] is True
    assert stuck["owner"]["pid"] == pid
    assert stuck["owner"]["alive"] is False
    assert stuck["state"] == "preparing"
    # The pinned cohort is named, which is what connects the SSH symptom to
    # the epoch that is actually blocking the deploy.
    assert [node["name"] for node in stuck["cohort"]] == ["natasha", "ghost-worker5"]
    assert stuck["cohort_size"] == 2
    # Nothing was applied, so blocking on this epoch protects no work.
    assert stuck["applied_node_count"] == 0
    assert stuck["hub_committed"] is False
    assert stuck["age_seconds"] > 86400

    # `discover` still reports the same epoch as active; diagnosis adds the
    # dead-owner verdict rather than replacing the existing view.
    assert run_cli(directory, "discover")["active"]["epoch_id"] == epoch


def test_live_owner_and_terminal_epochs_are_never_called_stuck(
    journal_module: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "journal"
    live = f"{SOURCE_COMMIT}:20260820T000000Z:live"
    journal = create(tmp_path, directory, live)
    payload = run_cli(directory, "diagnose")
    assert payload["stuck"] == []
    assert [item["epoch_id"] for item in payload["active"]] == [live]
    assert payload["journals"][0]["owner"]["alive"] is True

    # A terminal epoch owned by a dead controller is finished, not stuck.
    abort(directory, live, journal["revision"])
    make_dead_owner(journal_module, directory, live)
    payload = run_cli(directory, "diagnose")
    assert payload["stuck"] == []
    assert payload["active"] == []
    assert payload["journals"][0]["terminal"] is True


def test_diagnose_survives_an_unreadable_journal_and_multiple_live_epochs(
    journal_module: Any, tmp_path: Path
) -> None:
    """Diagnosis runs when things are already wrong, so it must not fail closed."""
    directory = tmp_path / "journal"
    first = f"{SOURCE_COMMIT}:20260811T000000Z:one"
    second = f"{SOURCE_COMMIT}:20260812T000000Z:two"
    create(tmp_path, directory, first)
    make_dead_owner(journal_module, directory, first)
    clone_journal(journal_module, directory, first, second)
    corrupt = directory / journal_module._journal_name(f"{SOURCE_COMMIT}:x:corrupt")
    corrupt.write_text("{not json", encoding="utf-8")
    corrupt.chmod(0o600)

    # `discover` refuses this directory outright, and its refusal names no epoch.
    failed = run_cli(directory, "discover", check=False)
    assert failed["ok"] is False

    payload = run_cli(directory, "diagnose")
    assert sorted(item["epoch_id"] for item in payload["stuck"]) == sorted(
        [first, second]
    )
    assert [entry["file"] for entry in payload["unreadable"]] == [corrupt.name]
    assert payload["unreadable"][0]["code"] == "invalid_schema"


def test_reap_ages_out_terminal_journals_and_keeps_recent_history(
    journal_module: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "journal"
    old = []
    for index in range(4):
        epoch = f"{SOURCE_COMMIT}:2026071{index}T000000Z:old{index}"
        journal = create(tmp_path, directory, epoch)
        abort(directory, epoch, journal["revision"])
        age(journal_module, directory, epoch, f"2026-07-1{index}T00:00:00Z")
        old.append(epoch)
    recent = f"{SOURCE_COMMIT}:20260820T000000Z:recent"
    journal = create(tmp_path, directory, recent)
    abort(directory, recent, journal["revision"])

    payload = run_cli(directory, "reap", "--max-age-days", "14", "--keep", "1")
    assert payload["schema"] == "mac.fleet_cohort_transaction_reap.v1"
    assert payload["changed"] is True
    # The newest terminal journal is kept by --keep; the four aged ones go.
    assert sorted(item["epoch_id"] for item in payload["removed"]) == sorted(old)
    assert [item["epoch_id"] for item in payload["retained"]] == [recent]
    for epoch in old:
        assert not journal_path(journal_module, directory, epoch).exists()
    assert journal_path(journal_module, directory, recent).exists()

    # Reaping is idempotent: a second pass has nothing left to do.
    again = run_cli(directory, "reap", "--max-age-days", "14", "--keep", "1")
    assert again["removed"] == []
    assert again["changed"] is False


def test_reap_never_removes_a_live_or_unparseable_journal(
    journal_module: Any, tmp_path: Path
) -> None:
    """The stuck epoch itself must survive reaping -- it is state, not evidence."""
    directory = tmp_path / "journal"
    stuck = f"{SOURCE_COMMIT}:20260811T211348Z:stuck"
    create(tmp_path, directory, stuck)
    make_dead_owner(journal_module, directory, stuck)
    age(journal_module, directory, stuck, "2020-01-01T00:00:00Z")
    corrupt = directory / journal_module._journal_name(f"{SOURCE_COMMIT}:x:corrupt")
    corrupt.write_text("{not json", encoding="utf-8")
    corrupt.chmod(0o600)

    payload = run_cli(directory, "reap", "--max-age-days", "0", "--keep", "0")
    assert payload["removed"] == []
    assert [item["epoch_id"] for item in payload["retained_non_terminal"]] == [stuck]
    assert [entry["file"] for entry in payload["unreadable"]] == [corrupt.name]
    assert journal_path(journal_module, directory, stuck).exists()
    assert corrupt.exists()


def test_reap_dry_run_removes_nothing_and_orphan_plans_are_collected(
    journal_module: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "journal"
    epoch = f"{SOURCE_COMMIT}:20260701T000000Z:old"
    journal = create(tmp_path, directory, epoch)
    abort(directory, epoch, journal["revision"])
    age(journal_module, directory, epoch, "2026-07-01T00:00:00Z")
    # A plan file left behind by a crashed controller, keyed to this epoch.
    plan = directory / journal_module._release_plan_name(epoch)
    plan.write_text('{"schema":"mac.test_plan.v1"}\n', encoding="utf-8")
    plan.chmod(0o600)

    preview = run_cli(
        directory, "reap", "--max-age-days", "14", "--keep", "0", "--dry-run"
    )
    assert [item["epoch_id"] for item in preview["removed"]] == [epoch]
    assert preview["removed_plans"] == [plan.name]
    assert preview["dry_run"] is True
    assert preview["changed"] is False
    assert journal_path(journal_module, directory, epoch).exists()
    assert plan.exists()

    payload = run_cli(directory, "reap", "--max-age-days", "14", "--keep", "0")
    assert [item["epoch_id"] for item in payload["removed"]] == [epoch]
    assert payload["removed_plans"] == [plan.name]
    assert not journal_path(journal_module, directory, epoch).exists()
    assert not plan.exists()
    # The lock is directory machinery, not retention state.
    assert (directory / journal_module.LOCK_NAME).exists()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_reap_rejects_an_out_of_range_retention_window(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    for argument in ("--max-age-days", "--keep"):
        payload = run_cli(directory, "reap", argument, "-1", check=False)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_input"
    payload = run_cli(directory, "reap", "--max-age-days", "nonsense", check=False)
    assert payload["error"]["code"] == "invalid_input"


def test_retention_window_defaults_come_from_the_environment(
    journal_module: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "journal"
    environment = dict(os.environ)
    environment["MAC_FLEET_COHORT_JOURNAL_RETENTION_DAYS"] = "3"
    environment["MAC_FLEET_COHORT_JOURNAL_RETENTION_KEEP_COUNT"] = "2"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(directory), "reap"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["max_age_days"] == 3
    assert payload["keep"] == 2
    assert journal_module.DEFAULT_RETENTION_DAYS == 14
    assert journal_module.DEFAULT_RETENTION_KEEP == 5
