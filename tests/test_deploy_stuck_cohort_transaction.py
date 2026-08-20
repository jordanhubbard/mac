"""Regression coverage for the stuck cohort transaction that blocked deploys.

A cohort transaction whose owning controller had died sat non-terminal in
``~/.mac/fleet-cohort-transactions`` for nine days.  Every deploy replayed its
pinned 8-member cohort, five of whose agents no longer existed, and the only
error an operator saw was::

    ERROR: jordanh-worker5: authoritative SSH route resolved empty

That names an agent, so three remedies were aimed at agents -- deleting them,
pruning the fleet registry, passing an explicit ``--agents`` list -- and none of
them touched the journal the deploy actually reads.

These tests drive the deploy script's own functions and assert that:

* the blocking transaction is named BY EPOCH, before any SSH is attempted;
* pinned cohort members absent from the frozen registry are named specifically,
  together with the fact that agent deletion cannot clear them;
* terminal journals age out to a bounded window, and the non-terminal one is
  reported and retained rather than replayed or deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
HELPER = ROOT / "deploy" / "fleet-cohort-transaction.py"

SOURCE_COMMIT = "a" * 40
GHOST = "jordanh-worker5"
LIVE = "rocky"


def _extract(name: str, next_marker: str) -> str:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    body = source[start:]
    return body[: body.index(next_marker)].rstrip()


def _reap_source() -> str:
    return _extract(
        "reap_terminal_cohort_journals", "\nreport_stuck_cohort_transaction() {"
    )


def _report_source() -> str:
    return _extract(
        "report_stuck_cohort_transaction",
        "\nrecover_incomplete_cohort_transaction_before_deploy() {",
    )


def _route_guard_source() -> str:
    """The exact empty-route guard an operator hits on a ghost cohort member."""

    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index('    echo "ERROR: ${agent}: authoritative SSH route resolved empty" >&2')
    body = source[start:]
    return body[: body.index("  }")]


def _preamble(journal_dir: Path, tmpdir_local: Path, registry: list[str]) -> str:
    stub = (
        "fleet_config_query() {\n"
        + "".join(f"  printf '%s\\n' {shlex.quote(name)}\n" for name in registry)
        + "  return 0\n}\n"
    )
    return "\n".join(
        [
            "set -u",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            f"COHORT_JOURNAL_HELPER={shlex.quote(str(HELPER))}",
            f"COHORT_JOURNAL_DIR={shlex.quote(str(journal_dir))}",
            f"TMPDIR_LOCAL={shlex.quote(str(tmpdir_local))}",
            "COHORT_JOURNAL_RETENTION_DAYS=14",
            "COHORT_JOURNAL_RETENTION_KEEP=2",
            "cohort_journal() {",
            '  "$PYTHON_BIN" "$COHORT_JOURNAL_HELPER" --directory "$COHORT_JOURNAL_DIR" "$@"',
            "}",
            stub,
        ]
    )


def _helper(journal_dir: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(HELPER), "--directory", str(journal_dir), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    stream = result.stdout if result.returncode == 0 else result.stderr
    payload = json.loads(stream)
    assert result.returncode == 0, payload
    return payload


def _journal_file(journal_dir: Path, epoch: str) -> Path:
    return journal_dir / (
        "transaction-" + hashlib.sha256(epoch.encode()).hexdigest() + ".json"
    )


def _cohort(tmp_path: Path, names: list[str]) -> Path:
    path = tmp_path / "cohort.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": name,
                    "stable_id": f"agent_{name.replace('-', '_')}",
                    "generation": f"generation-{index}",
                    "deployment_id": f"deployment-{index}",
                    "os": "linux",
                    "supervisor": "systemd",
                }
                for index, name in enumerate(names)
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _init_epoch(
    journal_dir: Path,
    tmp_path: Path,
    epoch: str,
    names: list[str],
    *,
    owner_pid: int,
    nonce: str = "dead-controller",
) -> None:
    _helper(
        journal_dir,
        "init",
        "--epoch",
        epoch,
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260811T211348Z",
        "--fleet",
        "mac",
        "--hub-agent",
        LIVE,
        "--cohort-file",
        str(_cohort(tmp_path, names)),
        "--owner-nonce",
        nonce,
        "--owner-pid",
        str(owner_pid),
    )


def _run(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True, check=False
    )


def _stuck_journal(tmp_path: Path) -> tuple[Path, str]:
    journal_dir = tmp_path / "fleet-cohort-transactions"
    epoch = f"{SOURCE_COMMIT}:20260811T211348Z:deadcontroller"
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _init_epoch(
            journal_dir,
            tmp_path,
            epoch,
            [LIVE, "bullwinkle", GHOST],
            owner_pid=owner.pid,
        )
    finally:
        owner.terminate()
        owner.wait(timeout=10)
    # The journal now records a controller that no longer exists.
    assert _helper(journal_dir, "discover")["active"]["owner"]["alive"] is False
    return journal_dir, epoch


def test_dead_owner_transaction_is_named_by_epoch_before_any_ssh(tmp_path) -> None:
    journal_dir, epoch = _stuck_journal(tmp_path)
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()

    snippet = "\n".join(
        [
            # The registry still knows the live members but not the ghost.
            _preamble(journal_dir, tmpdir_local, ["agent_rocky", "agent_bullwinkle"]),
            _report_source(),
            'report_stuck_cohort_transaction "$(cohort_journal discover)"',
        ]
    )
    result = _run(snippet)
    assert result.returncode == 0, result.stderr
    diagnostic = result.stderr

    # The epoch is the subject of the diagnostic, not an agent.
    assert epoch in diagnostic
    assert "cohort epoch" in diagnostic
    assert "controller is dead" in diagnostic
    assert "state=preparing" in diagnostic
    assert "(not running)" in diagnostic
    # The pinned cohort is enumerated, and the ghost member is named as absent
    # from the registry rather than surfacing later as an empty SSH route.
    assert f"pinned cohort (3): {LIVE}, bullwinkle, {GHOST}" in diagnostic
    assert (
        f"pinned members absent from the frozen fleet registry: {GHOST}" in diagnostic
    )
    # And the remedies that were tried and failed are ruled out explicitly.
    assert "removing or deleting the" in diagnostic
    assert "--agents" in diagnostic
    assert str(journal_dir) in diagnostic
    # Nothing was attempted against a node: no SSH, no mutation, no deletion.
    assert "no node or hub mutation was ever journalled" in diagnostic
    assert _journal_file(journal_dir, epoch).exists()


def test_diagnostic_is_silent_when_the_owning_controller_is_alive(tmp_path) -> None:
    journal_dir = tmp_path / "fleet-cohort-transactions"
    epoch = f"{SOURCE_COMMIT}:20260820T000000Z:livecontroller"
    _init_epoch(
        journal_dir,
        tmp_path,
        epoch,
        [LIVE],
        owner_pid=os.getpid(),
        nonce="live-controller",
    )
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()

    result = _run(
        "\n".join(
            [
                _preamble(journal_dir, tmpdir_local, ["agent_rocky"]),
                _report_source(),
                'report_stuck_cohort_transaction "$(cohort_journal discover)"',
            ]
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == ""


def test_empty_ssh_route_points_at_the_journal_not_only_the_agent(tmp_path) -> None:
    """The symptom message now carries its own cause."""

    snippet = "\n".join(
        [
            "set -u",
            "FLEET_REGISTRY_SOURCE=/home/operator/.mac/fleets.yaml",
            "COHORT_JOURNAL_DIR=/home/operator/.mac/fleet-cohort-transactions",
            "empty_route_guard() {",
            '  local agent="$1"',
            _route_guard_source(),
            "}",
            f"empty_route_guard {shlex.quote(GHOST)}",
        ]
    )
    result = _run(snippet)
    assert result.returncode == 1
    assert f"{GHOST}: authoritative SSH route resolved empty" in result.stderr
    assert "/home/operator/.mac/fleets.yaml" in result.stderr
    assert "pinned cohort of an incomplete cohort transaction" in result.stderr
    assert "/home/operator/.mac/fleet-cohort-transactions" in result.stderr


def test_retention_bounds_terminal_journals_and_retains_the_stuck_one(
    tmp_path,
) -> None:
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()
    journal_dir = tmp_path / "fleet-cohort-transactions"

    # An incomplete epoch blocks `init`, so the finished history is built
    # first and the stuck transaction is layered on top -- the same order in
    # which a real directory accumulated 638 files behind one blocked epoch.
    aged = []
    for index in range(4):
        epoch = f"{SOURCE_COMMIT}:2026071{index}T000000Z:controller{index}"
        _init_epoch(
            journal_dir,
            tmp_path,
            epoch,
            [LIVE],
            owner_pid=os.getpid(),
            nonce=f"controller{index}",
        )
        _helper(
            journal_dir,
            "abort",
            "--epoch",
            epoch,
            "--expected-revision",
            "0",
            "--operation-id",
            f"abort-{index}",
            "--owner-nonce",
            f"controller{index}",
        )
        path = _journal_file(journal_dir, epoch)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updated_at"] = f"2026-07-{19 + index:02d}T05:45:00Z"
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
        aged.append(epoch)

    _journal_dir, stuck_epoch = _stuck_journal(tmp_path)
    assert _journal_dir == journal_dir

    result = _run(
        "\n".join(
            [
                _preamble(journal_dir, tmpdir_local, ["agent_rocky"]),
                _reap_source(),
                "reap_terminal_cohort_journals",
            ]
        )
    )
    assert result.returncode == 0, result.stderr
    assert "cohort journal retention aged out 2 terminal epoch journal(s)" in (
        result.stdout
    )
    # The two oldest terminal journals are gone; the newest two are the window.
    assert not _journal_file(journal_dir, aged[0]).exists()
    assert not _journal_file(journal_dir, aged[1]).exists()
    assert _journal_file(journal_dir, aged[2]).exists()
    assert _journal_file(journal_dir, aged[3]).exists()
    # The non-terminal, dead-owner transaction is never a retention target.
    assert _journal_file(journal_dir, stuck_epoch).exists()
    assert _helper(journal_dir, "discover")["stuck"]["epoch_id"] == stuck_epoch


def test_retention_failure_never_blocks_a_deploy(tmp_path) -> None:
    """Retention is bookkeeping: an unusable journal directory is not a gate."""

    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()
    unusable = tmp_path / "missing-parent" / "journal"

    result = _run(
        "\n".join(
            [
                _preamble(unusable, tmpdir_local, ["agent_rocky"]),
                _reap_source(),
                "reap_terminal_cohort_journals",
                'echo "continued=$?"',
            ]
        )
    )
    assert result.returncode == 0, result.stderr
    assert "continued=0" in result.stdout
    assert "cohort journal retention did not run" in result.stderr
