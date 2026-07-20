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
printf 'PYTEST_ADDOPTS=%s\t%s\n' "${PYTEST_ADDOPTS-<unset>}" "$*" >> "$FAKE_PY_LOG"
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
        # This must never leak into either pytest phase.
        "PYTEST_ADDOPTS": "-n auto",
    }
    if jobs is not None:
        env["MAC_TEST_JOBS"] = jobs
    completed = subprocess.run(
        [str(RUNNER)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, log_path.read_text(encoding="utf-8").splitlines()


def test_contract_runner_defaults_to_two_workers_and_protects_serial_phase(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    pytest_calls = [line for line in calls if "-m coverage run -m pytest" in line]
    assert len(pytest_calls) == 2
    assert "-n 2 --dist loadscope" in pytest_calls[0]
    assert "not (process_e2e or postgres or container_contract or docker_e2e)" in pytest_calls[0]
    assert "-n " not in pytest_calls[1]
    assert "-m process_e2e or postgres or container_contract or docker_e2e" in pytest_calls[1]
    assert all(line.startswith("PYTEST_ADDOPTS=<unset>\t") for line in pytest_calls)


def test_contract_runner_preserves_explicit_worker_override(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path, jobs="3")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    bulk = next(line for line in calls if "-m coverage run -m pytest -n" in line)
    assert "-n 3 --dist loadscope" in bulk


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
