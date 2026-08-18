from __future__ import annotations

import ast
import os
import subprocess
import sys
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
    nested_pytest: bool = False,
    pytest_status: int = 0,
    combine_output: str = "Combined data file .coverage.fake",
    combine_status: int = 0,
    json_status: int = 0,
    preflight_status: int = 0,
    checkpoint: str | None = None,
    checkpoint_plan_status: int = 10,
    triage_pytest_status: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "python.log"
    fake_python = bin_dir / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf 'PYTEST_ADDOPTS=%s\tDISABLE=%s\tCOVERAGE_FILE=%s\tSKIPFILE=%s\t%s\n' "${PYTEST_ADDOPTS-<unset>}" "${MAC_TEST_DISABLE_GROUPS-<unset>}" "${COVERAGE_FILE-<unset>}" "${MAC_TEST_CHECKPOINT_SKIP_FILE-<unset>}" "$*" >> "$FAKE_PY_LOG"
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
    *"scripts/test-docs.py --static-only"*|\
    *"scripts/generate-env-config-registry.py --check"*|\
    *"scripts/generate-docs-reference.py --check"*|\
    *"tests/test_control_plane_public_contract.py"*)
        exit "$FAKE_PREFLIGHT_STATUS"
        ;;
esac
case "$*" in
    "-m coverage json -o "*) exit "$FAKE_JSON_STATUS" ;;
esac
case "$*" in
    *"scripts/test-checkpoint.py"*" plan"*)
        # 0 => resume from the checkpoint, 10 (and anything else) => run
        # everything. The default is 10 because failing OPEN is the property.
        exit "$FAKE_CHECKPOINT_PLAN_STATUS"
        ;;
    *"scripts/test-checkpoint.py"*" record"*) exit 0 ;;
esac
if [ -n "$FAKE_TRIAGE_PYTEST_STATUS" ]; then
    case "$*" in
        # The interpreter capability probe must keep answering normally.
        *"--version"*) ;;
        # A bare `-m pytest ...` under the coverage gate is the checkpoint
        # triage pass; the measured phases all go through `-m coverage run`.
        "-m pytest"*) exit "$FAKE_TRIAGE_PYTEST_STATUS" ;;
    esac
fi
case "$*" in
    *"--version"*) ;;
    *"-m pytest"*) exit "$FAKE_PYTEST_STATUS" ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        # Keep the runner's interpreter resolution hermetic: point the pre-baked
        # runtime-venv override at a path that cannot exist so the ONLY resolvable
        # interpreter is the fake python3 staged on PATH above. Without this, a
        # host that actually ships /opt/mac-venv (the OpenShell task sandbox and
        # CI images do) would resolve the real interpreter and run the whole
        # suite instead of the fake, so every assertion here would observe the
        # real gate rather than the runner's own dispatch decisions.
        "MAC_CONTRACT_RUNTIME_VENV": str(tmp_path / "nonexistent-runtime-venv"),
        "FAKE_PY_LOG": str(log_path),
        "FAKE_COMBINE_OUTPUT": combine_output,
        "FAKE_COMBINE_STATUS": str(combine_status),
        "FAKE_JSON_STATUS": str(json_status),
        "FAKE_PREFLIGHT_STATUS": str(preflight_status),
        "FAKE_PYTEST_STATUS": str(pytest_status),
        "FAKE_CHECKPOINT_PLAN_STATUS": str(checkpoint_plan_status),
        "FAKE_TRIAGE_PYTEST_STATUS": (
            "" if triage_pytest_status is None else str(triage_pytest_status)
        ),
        # Deterministic answer to the runner's headroom-default cpu probe.
        "FAKE_DEFAULT_JOBS": "6",
        # This must never leak into either pytest phase.
        "PYTEST_ADDOPTS": "-n auto",
    }
    for marker in (
        "PYTEST_CURRENT_TEST",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        env.pop(marker, None)
    if jobs is not None:
        env["MAC_TEST_JOBS"] = jobs
    if coverage is not None:
        env["MAC_TEST_COVERAGE"] = coverage
    if disable_groups is not None:
        env["MAC_TEST_DISABLE_GROUPS"] = disable_groups
    if select_base is not None:
        env["MAC_TEST_SELECT_BASE"] = select_base
    env.pop("MAC_TEST_CHECKPOINT", None)
    env.pop("MAC_TEST_CHECKPOINT_DIR", None)
    if checkpoint is not None:
        env["MAC_TEST_CHECKPOINT"] = checkpoint
        env["MAC_TEST_CHECKPOINT_DIR"] = str(tmp_path / "ckpt")
    if nested_pytest:
        env["PYTEST_CURRENT_TEST"] = (
            "tests/test_contract_test_runner.py::test_nested (call)"
        )
        env["PYTEST_XDIST_WORKER"] = "gw0"
        env["PYTEST_XDIST_WORKER_COUNT"] = "6"
        env["PYTEST_XDIST_TESTRUNUID"] = "outer-run"
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


def test_contract_runner_preflight_failure_stops_before_full_gate(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path, preflight_status=23)

    assert completed.returncode == 23, completed.stdout + completed.stderr
    assert "running fail-fast repository contract preflight" in completed.stdout
    assert sum("scripts/test-docs.py --static-only" in line for line in calls) == 1
    assert not any("generate-env-config-registry.py" in line for line in calls)
    assert not any("-m coverage run -m pytest" in line for line in calls)
    assert not any("-m coverage combine" in line for line in calls)
    assert not any("scripts/coverage-policy.py" in line for line in calls)


def test_contract_runner_preflight_completes_before_full_gate(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected = (
        "scripts/test-docs.py --static-only",
        "scripts/generate-env-config-registry.py --check",
        "scripts/generate-docs-reference.py --check",
        "tests/test_control_plane_public_contract.py",
    )
    positions = [
        next(index for index, line in enumerate(calls) if command in line)
        for command in expected
    ]
    first_full = next(
        index for index, line in enumerate(calls) if "-m coverage run -m pytest" in line
    )
    assert positions == sorted(positions)
    assert positions[-1] < first_full


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
        and "test_control_plane_public_contract.py" not in line
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
        and "test_control_plane_public_contract.py" not in line
    ]
    assert pytest_calls, "expected fast-mode pytest phases"
    assert all("DISABLE=fleet,heavy_e2e" in line for line in pytest_calls)


def test_contract_runner_preserves_explicit_worker_override(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path, jobs="3")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    bulk = next(line for line in calls if "-m coverage run -m pytest -n" in line)
    assert "-n 3 --dist loadscope" in bulk


def test_contract_runner_disables_nested_xdist_and_preserves_hard_failure(tmp_path):
    completed, calls = _run_with_fake_python(
        tmp_path,
        jobs="3",
        nested_pytest=True,
        pytest_status=19,
    )

    assert completed.returncode == 19, completed.stdout + completed.stderr
    assert "nested pytest detected" in completed.stderr
    assert "single" in completed.stderr and "serial owner" in completed.stderr
    pytest_calls = [
        line for line in calls if "-m coverage run -m pytest" in line
    ]
    # Exactly one serial owner: NO second xdist controller, NO worker pool.
    assert len(pytest_calls) == 1
    assert "-n " not in pytest_calls[0]
    assert "--dist" not in pytest_calls[0]
    assert "PYTEST_ADDOPTS=<unset>" in pytest_calls[0]


def test_contract_runner_nested_empty_selection_exit5_is_not_a_failure(tmp_path):
    """Nested inside a pytest/xdist worker, a serial owner that collects zero
    items (pytest exit 5) reflects a legitimately empty OUTER selection in this
    child, not a broken gate. The runner must remap exit 5 -> 0 ONLY when nested,
    run a single serial owner (no second controller), and say why."""
    completed, calls = _run_with_fake_python(
        tmp_path,
        jobs="4",
        nested_pytest=True,
        pytest_status=5,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "nested pytest collected 0 items (exit 5)" in completed.stderr
    pytest_calls = [line for line in calls if "-m coverage run -m pytest" in line]
    assert len(pytest_calls) == 1
    assert "-n " not in pytest_calls[0]
    assert "--dist" not in pytest_calls[0]


def test_contract_runner_nested_real_failure_still_fails(tmp_path):
    """The exit-5 remap must be surgical: a nested serial owner that reports a
    genuine test failure (exit 1) still fails the gate — no masking."""
    completed, calls = _run_with_fake_python(
        tmp_path,
        nested_pytest=True,
        pytest_status=1,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "treating the empty outer selection" not in completed.stderr
    pytest_calls = [line for line in calls if "-m coverage run -m pytest" in line]
    assert len(pytest_calls) == 1


def test_contract_runner_top_level_empty_selection_exit5_stays_hard_failure(tmp_path):
    """Outside a nested context, an empty whole-suite selection (exit 5) is a
    genuine misconfiguration and MUST remain a hard failure — the remap is
    scoped strictly to the nested case."""
    completed, calls = _run_with_fake_python(
        tmp_path,
        pytest_status=5,
    )

    assert completed.returncode == 5, completed.stdout + completed.stderr
    assert "treating the empty outer selection" not in completed.stderr


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


# --- Environment-prerequisite bootstrap-on-demand (interpreter resolution) ---
#
# The runner resolves a usable interpreter and, when the resolved one cannot run
# the suite (missing coverage/pytest/project deps), bootstraps the hermetic
# .venv the execution contract promises. A missing .venv was the original
# rc-127 that blocked in-sandbox verification; a PRE-EXISTING but BROKEN .venv
# (stale deps / half-written interpreter) is the residual gap — bootstrap only
# rebuilds a venv whose bin/python is absent, so the runner must discard the
# unusable .venv itself before bootstrapping or the gate dead-ends at exit 1.

_GOOD_PY_BODY = """#!/bin/sh
# A "healthy" interpreter: satisfies the runner's _py_can_run_suite probe
# (coverage --version, pytest --version, import cryptography/fastapi/yaml) and
# answers the cpu-count and pytest phases so the gate can complete.
case "$*" in
    *os.cpu_count*) printf '2\\n'; exit 0 ;;
    *"--version"*) exit 0 ;;
    *"import cryptography"*) exit 0 ;;
    "-m coverage combine") printf 'Combined data file .coverage.fake\\n'; exit 0 ;;
    "-m coverage json -o "*) exit 0 ;;
    *"-m pytest"*) exit 0 ;;
esac
exit 0
"""

_BROKEN_PY_BODY = """#!/bin/sh
# A pre-existing but unusable interpreter: every suite probe fails, so the
# runner must not accept it and must not leave it in place.
case "$*" in
    *"--version"*) exit 1 ;;
    *"import cryptography"*) exit 1 ;;
esac
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _stage_interpreter_repo(
    tmp_path: Path, *, broken_venv: bool, provide_builder: bool
) -> tuple[Path, dict[str, str]]:
    """Stage a throwaway repo that runs the REAL runner through its interpreter
    resolution. A fake bootstrap-project.py writes a healthy .venv/bin/python
    (mirroring the real --venv-only build) so a successful bootstrap yields a
    runnable gate; ``provide_builder`` toggles whether any non-.venv interpreter
    is available to perform that bootstrap."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='fake'\n", encoding="utf-8")
    (repo / "scripts" / "run-contract-tests.sh").write_text(
        RUNNER.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "scripts" / "run-contract-tests.sh").chmod(0o755)
    # bootstrap-project.py builds a healthy .venv when invoked by the runner.
    (repo / "scripts" / "bootstrap-project.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "py = root / '.venv' / 'bin' / 'python'\n"
        "py.parent.mkdir(parents=True, exist_ok=True)\n"
        "py.write_text(%r)\n"
        "os.chmod(py, 0o755)\n"
        "print('bootstrap-project.py built .venv', file=sys.stderr)\n"
        % _GOOD_PY_BODY,
        encoding="utf-8",
    )
    if broken_venv:
        _write_exec(repo / ".venv" / "bin" / "python", _BROKEN_PY_BODY)

    path_bin = tmp_path / "pathbin"
    path_bin.mkdir()
    if not provide_builder:
        # The runner's interpreter resolution falls back to ``command -v
        # python3 || command -v python`` on PATH. With no builder staged the
        # ambient host interpreter (a real /usr/bin/python3 on CI images) would
        # otherwise resolve, pass the probe, and successfully bootstrap the
        # .venv — defeating the "no interpreter can run the suite" scenario
        # (the runner's PATH fallback is correct production behavior; the
        # fixture just failed to isolate it). Shadow both names with the same
        # unusable-interpreter stub as a broken .venv: it fails every
        # _py_can_run_suite probe (so the runner rejects it) yet exits 0 for the
        # bootstrap invocation without building a .venv (so the runner does not
        # abort under ``set -e`` and instead reaches its fail-closed
        # diagnostic). The gate then fails closed on every host, with or without
        # an ambient interpreter.
        _write_exec(path_bin / "python3", _BROKEN_PY_BODY)
        _write_exec(path_bin / "python", _BROKEN_PY_BODY)
    if provide_builder:
        # A working python3 on PATH: it both passes the probe and, crucially,
        # actually executes bootstrap-project.py (which builds the healthy
        # .venv). A plain "exit 0" stub would swallow the bootstrap, so this
        # builder dispatches the bootstrap script to a real interpreter.
        _write_exec(
            path_bin / "python3",
            "#!/bin/sh\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in *bootstrap-project.py)\n"
            "    exec \"$REAL_PY\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            + _GOOD_PY_BODY.split("\n", 1)[1],
        )
    else:
        # Shadow any host python3 (notably macOS /usr/bin/python3) with an
        # unusable interpreter. The fixture is proving the no-builder path,
        # so its result must not depend on what the host happens to install.
        _write_exec(
            path_bin / "python3",
            "#!/bin/sh\n"
            "case \"$*\" in *bootstrap-project.py*) exit 0 ;; esac\n"
            "exit 1\n",
        )

    env = {
        "PATH": f"{path_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        # Force interpreter resolution to ignore any real /opt/mac-venv.
        "MAC_CONTRACT_RUNTIME_VENV": str(tmp_path / "nonexistent-runtime-venv"),
        # A real interpreter the fake PATH builder execs to run bootstrap; kept
        # off PATH so it never resolves as the runner's own interpreter.
        "REAL_PY": sys.executable,
    }
    (tmp_path / "home").mkdir()
    for marker in (
        "PYTEST_CURRENT_TEST",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        env.pop(marker, None)
    return repo, env


def test_contract_runner_rebuilds_a_broken_preexisting_venv(tmp_path):
    """A pre-existing .venv/bin/python that cannot run the suite must be
    discarded and re-bootstrapped, not treated as a fatal dead end. Before the
    repair the runner skipped bootstrap whenever .venv/bin/python existed and
    exited 1; now it rebuilds the unusable venv and the gate proceeds."""
    repo, env = _stage_interpreter_repo(
        tmp_path, broken_venv=True, provide_builder=True
    )

    completed = subprocess.run(
        [str(repo / "scripts" / "run-contract-tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "bootstrapping .venv" in completed.stderr
    assert "bootstrap-project.py built .venv" in completed.stderr
    # The rebuilt .venv is the healthy one that ran the gate.
    assert (repo / ".venv" / "bin" / "python").exists()
    assert "no interpreter can run the suite" not in completed.stderr


def test_contract_runner_reports_when_no_interpreter_can_run_the_suite(tmp_path):
    """With a broken .venv and no non-.venv interpreter available to bootstrap
    with, the runner must fail closed with the diagnostic (exit 1) rather than
    silently running a gate it cannot measure."""
    repo, env = _stage_interpreter_repo(
        tmp_path, broken_venv=True, provide_builder=False
    )

    completed = subprocess.run(
        [str(repo / "scripts" / "run-contract-tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "no interpreter can run the suite" in completed.stderr


def test_contract_runner_scrubs_provider_credentials_from_route_detection(tmp_path):
    """The hermetic sweep must clear the non-MAC_-prefixed coding-agent
    provider credentials that ``coding_agent`` route detection fingerprints.

    A fleet worker / codex-runner host carries live provider secrets in its
    environment. ``coding_agent._detect_codex`` honors ``OPENAI_API_KEY`` *and*
    ``CODEX_API_KEY``; if either leaks past the sweep the codex route's
    ``auth_source`` (and thus its route fingerprint) shifts and hermetic
    detection tests that pass on tokenless hosts start failing on any host with
    a configured coding route. Prove every provider knob the runner promises to
    clear is actually ``<unset>`` by the time the pytest phase runs.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env_log = tmp_path / "provider-env.log"
    fake_python = bin_dir / "python3"
    # The runner still runs its preflight python calls first; only record the
    # provider environment once, on the first real ``-m pytest`` invocation, and
    # answer the cpu_count probe deterministically so worker sizing is stable.
    fake_python.write_text(
        """#!/bin/sh
case "$*" in
    *os.cpu_count*) printf '6\\n'; exit 0 ;;
esac
case "$*" in
    *"-m pytest"*|*"-m coverage run -m pytest"*)
        if [ ! -f "$PROVIDER_ENV_LOG" ]; then
            for _v in OPENAI_BASE_URL OPENAI_API_KEY CODEX_API_KEY \\
                      ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN CURSOR_AUTH_TOKEN \\
                      CURSOR_API_KEY \\
                      MAC_CODEX_TOKEN; do
                eval "_val=\\${$_v-<unset>}"
                printf '%s=%s\\n' "$_v" "$_val" >> "$PROVIDER_ENV_LOG"
            done
        fi
        exit 0
        ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "MAC_CONTRACT_RUNTIME_VENV": str(tmp_path / "nonexistent-runtime-venv"),
        "PROVIDER_ENV_LOG": str(env_log),
        # Live provider secrets that a real fleet host would carry. None of
        # these may survive into the route-detection tests.
        "OPENAI_BASE_URL": "https://router.example/v1",
        "OPENAI_API_KEY": "sk-openai-should-not-leak",
        "CODEX_API_KEY": "sk-codex-should-not-leak",
        "ANTHROPIC_API_KEY": "sk-anthropic-should-not-leak",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-should-not-leak",
        "CURSOR_AUTH_TOKEN": "cursor-auth-should-not-leak",
        "CURSOR_API_KEY": "cursor-should-not-leak",
        "MAC_CODEX_TOKEN": "mac-codex-should-not-leak",
    }
    for marker in (
        "PYTEST_CURRENT_TEST",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        env.pop(marker, None)

    completed = subprocess.run(
        [str(RUNNER)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert env_log.exists(), completed.stdout + completed.stderr
    recorded = dict(
        line.split("=", 1)
        for line in env_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    for var in (
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CURSOR_AUTH_TOKEN",
        "CURSOR_API_KEY",
        "MAC_CODEX_TOKEN",
    ):
        assert recorded.get(var) == "<unset>", (var, recorded)


# --------------------------------------------------------------------------
# Test-gate checkpointing (src/mac/test_checkpoint.py).
#
# The property is not "resuming is fast", it is "a resumed run can never report
# green without the evidence a full run would have produced". The coverage gate
# is the sharp edge: whole-repo floors sit within 0.35pp of failing, so a subset
# must never be allowed to compute them.
# --------------------------------------------------------------------------


def _checkpoint_calls(calls: list[str], verb: str) -> list[str]:
    return [call for call in calls if "scripts/test-checkpoint.py" in call and verb in call]


def test_contract_runner_is_byte_identical_without_the_checkpoint_flag(tmp_path):
    """Default off: the merge gate must be unchanged unless someone opts in."""
    completed, calls = _run_with_fake_python(tmp_path)
    assert completed.returncode == 0
    assert _checkpoint_calls(calls, "plan") == []
    assert _checkpoint_calls(calls, "record") == []


def test_contract_runner_declined_checkpoint_runs_the_normal_full_gate(tmp_path):
    """Plan exit 10 means "run everything"; no triage phase may appear."""
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=10
    )
    assert completed.returncode == 0
    assert len(_checkpoint_calls(calls, "plan")) == 1
    assert "checkpoint triage pass" not in completed.stdout
    bulk = [c for c in calls if "-m coverage run -m pytest" in c]
    assert len(bulk) == 2, "the usual bulk + serial coverage phases, unchanged"
    assert not [c for c in calls if c.endswith("-m pytest") or "\t-m pytest -n" in c]


def test_contract_runner_checkpoint_triage_precedes_the_full_coverage_gate(tmp_path):
    """A green triage must still pay for the complete coverage-measured run."""
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=0, triage_pytest_status=0
    )
    assert completed.returncode == 0
    assert "checkpoint triage pass" in completed.stdout
    assert "running the" in completed.stdout and "complete coverage-measured gate" in completed.stdout
    triage = [c for c in calls if "\t-m pytest" in c]
    assert triage, "the triage phase runs pytest WITHOUT coverage"
    assert len([c for c in calls if "-m coverage run -m pytest" in c]) == 2, (
        "the complete gate still runs both measured phases in full"
    )


def test_contract_runner_checkpoint_triage_clears_the_skip_list_for_the_full_gate(tmp_path):
    """The complete gate must never see a carry-forward list.

    If the deselection hook were still armed, the coverage total would be
    computed from a subset -- the exact way a resumed run could turn a red gate
    green.
    """
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=0, triage_pytest_status=0
    )
    assert completed.returncode == 0
    measured = [c for c in calls if "-m coverage run -m pytest" in c]
    assert measured
    for call in measured:
        assert "SKIPFILE=<unset>" in call, call


def test_contract_runner_failed_triage_never_reaches_the_coverage_gate(tmp_path):
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=0, triage_pytest_status=1
    )
    assert completed.returncode == 1
    assert "checkpoint triage pass FAILED" in completed.stderr
    assert [c for c in calls if "-m coverage run -m pytest" in c] == [], (
        "a red triage must not spend an hour re-confirming the failure"
    )


def test_contract_runner_empty_triage_selection_is_not_a_failure(tmp_path):
    """Everything carried forward => pytest exit 5 => nothing to triage."""
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=0, triage_pytest_status=5
    )
    assert completed.returncode == 0
    assert len([c for c in calls if "-m coverage run -m pytest" in c]) == 2


def test_contract_runner_records_a_checkpoint_after_the_full_gate(tmp_path):
    completed, calls = _run_with_fake_python(tmp_path, checkpoint="1")
    assert completed.returncode == 0
    assert any("--gate full" in call for call in _checkpoint_calls(calls, "record"))


def test_contract_runner_fast_mode_resume_is_terminal(tmp_path):
    """No coverage is measured, so a green resumed run needs no second pass."""
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=0, coverage="0"
    )
    assert completed.returncode == 0
    assert "checkpoint triage pass" not in completed.stdout
    assert [c for c in calls if "-m coverage run" in c] == []
    assert any("--gate fast" in call for call in _checkpoint_calls(calls, "record"))


def test_contract_runner_nested_invocation_never_checkpoints(tmp_path):
    """A nested runner is a contract test of this script, not a gate run.

    Letting it write the checkpoint would record a synthetic single-owner run as
    if it were the real suite.
    """
    completed, calls = _run_with_fake_python(
        tmp_path, checkpoint="1", checkpoint_plan_status=0, nested_pytest=True
    )
    assert completed.returncode == 0
    assert _checkpoint_calls(calls, "plan") == []
    assert _checkpoint_calls(calls, "record") == []
