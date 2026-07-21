from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-contract-tests.sh"


def _run_with_fake_python(
    tmp_path: Path,
    *,
    jobs: str | None = None,
    coverage: str | None = None,
    disable_groups: str | None = None,
    combine_output: str = "Combined data file .coverage.fake",
    combine_status: int = 0,
    json_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "python.log"
    fake_python = bin_dir / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf 'PYTEST_ADDOPTS=%s\tDISABLE=%s\t%s\n' "${PYTEST_ADDOPTS-<unset>}" "${MAC_TEST_DISABLE_GROUPS-<unset>}" "$*" >> "$FAKE_PY_LOG"
case "$*" in
    *os.cpu_count*)
        # The runner computes its headroom-aware default worker count with a
        # ``python -c`` probe; answer it deterministically for the test.
        printf '%s\n' "${FAKE_DEFAULT_JOBS:-6}"
        exit 0
        ;;
esac
if [ "$*" = "-m coverage combine" ]; then
    printf '%s\n' "$FAKE_COMBINE_OUTPUT"
    exit "$FAKE_COMBINE_STATUS"
fi
if [ "$*" = "-m coverage json -o coverage.json" ]; then
    exit "$FAKE_JSON_STATUS"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_PY_LOG": str(log_path),
        "FAKE_COMBINE_OUTPUT": combine_output,
        "FAKE_COMBINE_STATUS": str(combine_status),
        "FAKE_JSON_STATUS": str(json_status),
        # Deterministic answer to the runner's headroom-default cpu probe.
        "FAKE_DEFAULT_JOBS": "6",
        # This must never leak into either pytest phase.
        "PYTEST_ADDOPTS": "-n auto",
    }
    if jobs is not None:
        env["MAC_TEST_JOBS"] = jobs
    if coverage is not None:
        env["MAC_TEST_COVERAGE"] = coverage
    if disable_groups is not None:
        env["MAC_TEST_DISABLE_GROUPS"] = disable_groups
    completed = subprocess.run(
        [str(RUNNER)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # The merge-gate guard can exit before any python is invoked, so the log may
    # not exist; treat that as "no pytest phases ran".
    calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return completed, calls


def test_contract_runner_defaults_to_headroom_workers_and_protects_serial_phase(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    pytest_calls = [line for line in calls if "-m coverage run -m pytest" in line]
    assert len(pytest_calls) == 2
    # Unset MAC_TEST_JOBS => the runner computes a headroom-aware default via a
    # cpu_count probe (faked to 6 here) so the bulk slice runs wide without
    # saturating every core; the serial phase stays unparallelised.
    assert "-n 6 --dist loadscope" in pytest_calls[0]
    assert "not (process_e2e or postgres or container_contract or docker_e2e)" in pytest_calls[0]
    assert "-n " not in pytest_calls[1]
    assert "-m process_e2e or postgres or container_contract or docker_e2e" in pytest_calls[1]
    assert all(line.startswith("PYTEST_ADDOPTS=<unset>\t") for line in pytest_calls)


def test_contract_runner_fast_mode_skips_coverage_and_policy(tmp_path):
    """MAC_TEST_COVERAGE=0 keeps the bulk+serial split but drops coverage.

    Rollout verification and dev loops need pass/fail, not the coverage floor
    gate, so the fast path must run pytest directly (no ``coverage run`` wrapper)
    and skip combine/json/report/policy entirely.
    """

    completed, calls = _run_with_fake_python(tmp_path, coverage="0")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    # Two plain pytest phases, never wrapped in ``coverage run``.
    assert not any("-m coverage run -m pytest" in line for line in calls)
    pytest_calls = [
        line
        for line in calls
        if "-m pytest" in line and "coverage" not in line and "--version" not in line
    ]
    assert len(pytest_calls) == 2
    assert "-n 6 --dist loadscope" in pytest_calls[0]
    assert "not (process_e2e or postgres or container_contract or docker_e2e)" in pytest_calls[0]
    assert "-n " not in pytest_calls[1]
    assert "-m process_e2e or postgres or container_contract or docker_e2e" in pytest_calls[1]
    # No coverage pipeline runs at all.
    assert not any("-m coverage combine" in line for line in calls)
    assert not any("-m coverage json" in line for line in calls)
    assert not any("scripts/coverage-policy.py" in line for line in calls)


def test_contract_runner_refuses_disable_groups_under_coverage(tmp_path):
    """Disabling a namespace drops ITS coverage, so honoring it with coverage on
    would let a green gate skip contracts. The runner must hard-refuse (exit 2)
    before running anything — the merge gate stays exhaustive."""
    completed, calls = _run_with_fake_python(tmp_path, disable_groups="fleet")

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "MAC_TEST_DISABLE_GROUPS is only honored in fast mode" in completed.stderr
    assert not any("-m pytest" in line for line in calls)


def test_contract_runner_honors_disable_groups_in_fast_mode(tmp_path):
    """On the non-gating fast path (MAC_TEST_COVERAGE=0) the switch is allowed and
    re-exported past the hermetic MAC_* sweep into the pytest child."""
    completed, calls = _run_with_fake_python(
        tmp_path, coverage="0", disable_groups="fleet,heavy_e2e"
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    pytest_calls = [
        line
        for line in calls
        if "-m pytest" in line and "coverage" not in line and "--version" not in line
    ]
    assert pytest_calls, "expected fast-mode pytest phases"
    assert all("DISABLE=fleet,heavy_e2e" in line for line in pytest_calls)


def test_contract_runner_preserves_explicit_worker_override(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path, jobs="3")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    bulk = next(line for line in calls if "-m coverage run -m pytest -n" in line)
    assert "-n 3 --dist loadscope" in bulk


def test_contract_runner_honors_explicit_auto_override(tmp_path):
    # Power users on hosts without the sub-second timing tests can still opt into
    # one worker per core.
    completed, calls = _run_with_fake_python(tmp_path, jobs="auto")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    bulk = next(line for line in calls if "-m coverage run -m pytest -n" in line)
    assert "-n auto --dist loadscope" in bulk


def test_contract_runner_rejects_partial_combine_even_when_coverage_exits_zero(tmp_path):
    completed, calls = _run_with_fake_python(
        tmp_path,
        combine_output="Combined 34 files, skipped 1899, 1 file errored",
    )

    assert completed.returncode != 0
    assert not any("-m coverage json" in line for line in calls)
    assert not any("scripts/coverage-policy.py" in line for line in calls)


def test_contract_runner_propagates_coverage_json_failure(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path, json_status=17)

    assert completed.returncode == 17
    assert any("-m coverage json" in line for line in calls)
    assert not any("scripts/coverage-policy.py" in line for line in calls)
