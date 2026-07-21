from __future__ import annotations

import ast
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
    select_base: str | None = None,
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
printf 'PYTEST_ADDOPTS=%s\tDISABLE=%s\tCOVERAGE_FILE=%s\t%s\n' "${PYTEST_ADDOPTS-<unset>}" "${MAC_TEST_DISABLE_GROUPS-<unset>}" "${COVERAGE_FILE-<unset>}" "$*" >> "$FAKE_PY_LOG"
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
case "$*" in
    "-m coverage json -o "*) exit "$FAKE_JSON_STATUS" ;;
esac
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
    if select_base is not None:
        env["MAC_TEST_SELECT_BASE"] = select_base
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


def test_contract_runner_isolates_coverage_state_per_invocation(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first, first_calls = _run_with_fake_python(first_root)
    second, second_calls = _run_with_fake_python(second_root)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    first_files = {
        line.split("COVERAGE_FILE=", 1)[1].split("\t", 1)[0]
        for line in first_calls
        if "-m coverage" in line and "--version" not in line
    }
    second_files = {
        line.split("COVERAGE_FILE=", 1)[1].split("\t", 1)[0]
        for line in second_calls
        if "-m coverage" in line and "--version" not in line
    }
    assert len(first_files) == 1
    assert len(second_files) == 1
    assert first_files != second_files
    assert next(iter(first_files)).endswith("/.coverage")


def test_contract_runner_honors_explicit_auto_override(tmp_path):
    # Power users can trade the default subprocess/memory headroom for one
    # worker per reported core.
    completed, calls = _run_with_fake_python(tmp_path, jobs="auto")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    bulk = next(line for line in calls if "-m coverage run -m pytest -n" in line)
    assert "-n auto --dist loadscope" in bulk


def test_suite_never_asserts_wall_clock_performance() -> None:
    """Semantic tests use events/virtual clocks; real time is anti-hang only."""

    clock_methods = {"monotonic", "perf_counter", "time"}
    elapsed_name_fragments = {"elapsed", "duration", "latency", "runtime", "wall_time"}
    violations: list[str] = []

    def has_clock_call(node: ast.AST) -> bool:
        return any(
            isinstance(item, ast.Call)
            and (
                (
                    isinstance(item.func, ast.Attribute)
                    and item.func.attr in clock_methods
                )
                or (
                    isinstance(item.func, ast.Name)
                    and item.func.id in clock_methods
                )
            )
            for item in ast.walk(node)
        )

    for path in sorted((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            timed_names: set[str] = set()
            assignments = [
                item
                for item in ast.walk(function)
                if isinstance(item, (ast.Assign, ast.AnnAssign))
            ]
            changed = True
            while changed:
                changed = False
                for assignment in assignments:
                    value = assignment.value
                    if value is None:
                        continue
                    depends_on_clock = has_clock_call(value) or any(
                        isinstance(item, ast.Name) and item.id in timed_names
                        for item in ast.walk(value)
                    )
                    if not depends_on_clock:
                        continue
                    targets = (
                        assignment.targets
                        if isinstance(assignment, ast.Assign)
                        else [assignment.target]
                    )
                    for target in targets:
                        for item in ast.walk(target):
                            if isinstance(item, ast.Name) and item.id not in timed_names:
                                timed_names.add(item.id)
                                changed = True
            for assertion in (
                item for item in ast.walk(function) if isinstance(item, ast.Assert)
            ):
                if has_clock_call(assertion.test) or any(
                    isinstance(item, ast.Name)
                    and item.id in timed_names
                    and any(
                        fragment in item.id.lower()
                        for fragment in elapsed_name_fragments
                    )
                    for item in ast.walk(assertion.test)
                ):
                    violations.append(
                        "%s:%d" % (path.relative_to(ROOT), assertion.lineno)
                    )

    assert violations == [], (
        "wall-clock assertions are forbidden; use explicit synchronization, "
        "a virtual clock, or a generous anti-hang timeout: %s" % violations
    )


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


def test_contract_runner_select_base_falls_through_to_full_gate_when_unresolved(tmp_path):
    """MAC_TEST_SELECT_BASE asks the resolver for an impact subset, but a resolver
    that yields no ``focused`` document (here the fake python cannot emit one) must
    fail closed: the runner runs the whole-repo two-phase coverage gate exactly as
    if selection were never requested. This is the fail-closed guarantee — a
    broken/absent resolver can never silently shrink the merge gate."""
    completed, calls = _run_with_fake_python(tmp_path, select_base="origin/main")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    # The resolver was consulted...
    assert any("scripts/resolve-impacted-tests.py --base origin/main" in line for line in calls)
    # ...and, unresolved, the full whole-repo gate still ran both coverage phases.
    assert "full run required" in completed.stdout
    pytest_calls = [line for line in calls if "-m coverage run -m pytest" in line]
    assert len(pytest_calls) == 2
    assert "not (process_e2e or postgres or container_contract or docker_e2e)" in pytest_calls[0]
