"""Tests for scripts/coverage-policy.py safety-floor enforcement."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "coverage-policy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mac_coverage_policy", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def policy_mod():
    return _load_module()


def _coverage_doc(
    *,
    num_statements=100,
    covered_lines=95,
    num_branches=40,
    covered_branches=36,
):
    return {
        "totals": {
            "num_statements": num_statements,
            "covered_lines": covered_lines,
            "num_branches": num_branches,
            "covered_branches": covered_branches,
        }
    }


def _policy_doc(
    *,
    statement_safety_floor=90.0,
    branch_safety_floor=80.0,
    require_branch_measurement=True,
):
    return {
        "coverage": {
            "statement_safety_floor": statement_safety_floor,
            "branch_safety_floor": branch_safety_floor,
            "require_branch_measurement": require_branch_measurement,
        }
    }


def test_percentage_zero_total_is_full(policy_mod):
    assert policy_mod._percentage(0, 0) == 100.0


def test_percentage_regular_ratio(policy_mod):
    assert policy_mod._percentage(1, 4) == 25.0


def test_evaluate_pass_above_floors(policy_mod):
    result = policy_mod.evaluate(_coverage_doc(), _policy_doc())
    assert result["schema"] == policy_mod.SCHEMA
    assert result["status"] == "pass"
    assert result["failures"] == []
    assert result["policy_role"] == "regression_safety_floor_not_optimization_target"
    assert result["statements"]["percent"] == 95.0
    assert result["statements"]["total"] == 100
    assert result["statements"]["covered"] == 95
    assert result["statements"]["safety_floor"] == 90.0
    assert result["branches"]["percent"] == 90.0
    assert result["branches"]["safety_floor"] == 80.0


def test_evaluate_statement_below_floor_fails(policy_mod):
    result = policy_mod.evaluate(
        _coverage_doc(covered_lines=80),
        _policy_doc(),
    )
    assert result["status"] == "fail"
    assert any("statement coverage" in msg for msg in result["failures"])


def test_evaluate_branch_below_floor_fails(policy_mod):
    result = policy_mod.evaluate(
        _coverage_doc(covered_branches=10),
        _policy_doc(),
    )
    assert result["status"] == "fail"
    assert any("branch coverage" in msg for msg in result["failures"])


def test_evaluate_unmeasured_branches_required_fails(policy_mod):
    result = policy_mod.evaluate(
        _coverage_doc(num_branches=0, covered_branches=0),
        _policy_doc(require_branch_measurement=True),
    )
    assert result["status"] == "fail"
    assert "branch coverage was not measured" in result["failures"]


def test_evaluate_unmeasured_branches_optional_passes(policy_mod):
    result = policy_mod.evaluate(
        _coverage_doc(num_branches=0, covered_branches=0),
        _policy_doc(require_branch_measurement=False),
    )
    assert result["status"] == "pass"
    assert result["branches"]["percent"] == 100.0
    assert result["failures"] == []


def test_evaluate_exact_floor_passes(policy_mod):
    result = policy_mod.evaluate(
        _coverage_doc(covered_lines=90, covered_branches=32),
        _policy_doc(statement_safety_floor=90.0, branch_safety_floor=80.0),
    )
    assert result["status"] == "pass"
    assert result["statements"]["percent"] == 90.0
    assert result["branches"]["percent"] == 80.0


def test_evaluate_multiple_failures_reported(policy_mod):
    result = policy_mod.evaluate(
        _coverage_doc(covered_lines=10, covered_branches=1),
        _policy_doc(),
    )
    assert result["status"] == "fail"
    assert len(result["failures"]) == 2


def test_evaluate_missing_totals_raises(policy_mod):
    with pytest.raises(ValueError):
        policy_mod.evaluate({}, _policy_doc())


def test_evaluate_missing_policy_raises(policy_mod):
    with pytest.raises(ValueError):
        policy_mod.evaluate(_coverage_doc(), {})


def _write_inputs(tmp_path, coverage_doc, policy_doc):
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage_doc), encoding="utf-8")
    policy_path = tmp_path / "test-policy.toml"
    cov = policy_doc["coverage"]
    policy_path.write_text(
        "[coverage]\n"
        f"statement_safety_floor = {cov['statement_safety_floor']}\n"
        f"branch_safety_floor = {cov['branch_safety_floor']}\n"
        f"require_branch_measurement = {str(cov['require_branch_measurement']).lower()}\n",
        encoding="utf-8",
    )
    return coverage_path, policy_path


def test_main_pass_exit_zero(policy_mod, tmp_path, capsys):
    coverage_path, policy_path = _write_inputs(tmp_path, _coverage_doc(), _policy_doc())
    code = policy_mod.main(["--coverage-json", str(coverage_path), "--policy", str(policy_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "coverage safety: statements 95/100" in out


def test_main_fail_exit_one(policy_mod, tmp_path, capsys):
    coverage_path, policy_path = _write_inputs(
        tmp_path, _coverage_doc(covered_lines=10), _policy_doc()
    )
    code = policy_mod.main(["--coverage-json", str(coverage_path), "--policy", str(policy_path)])
    assert code == 1
    captured = capsys.readouterr()
    assert "statement coverage" in captured.err


def test_main_json_output(policy_mod, tmp_path, capsys):
    coverage_path, policy_path = _write_inputs(tmp_path, _coverage_doc(), _policy_doc())
    code = policy_mod.main(
        ["--coverage-json", str(coverage_path), "--policy", str(policy_path), "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["schema"] == policy_mod.SCHEMA


def test_main_missing_coverage_file_exit_two(policy_mod, tmp_path, capsys):
    _, policy_path = _write_inputs(tmp_path, _coverage_doc(), _policy_doc())
    code = policy_mod.main(
        [
            "--coverage-json",
            str(tmp_path / "missing.json"),
            "--policy",
            str(policy_path),
        ]
    )
    assert code == 2
    assert "invalid input" in capsys.readouterr().err


def test_main_invalid_coverage_json_exit_two(policy_mod, tmp_path, capsys):
    _, policy_path = _write_inputs(tmp_path, _coverage_doc(), _policy_doc())
    coverage_path = tmp_path / "broken.json"
    coverage_path.write_text("{not json", encoding="utf-8")
    code = policy_mod.main(["--coverage-json", str(coverage_path), "--policy", str(policy_path)])
    assert code == 2
    assert "invalid input" in capsys.readouterr().err


# --- Diff-coverage mode (impact-based subset gate) ---


def _diff_policy_doc(statement_floor=90.0):
    return {
        "coverage": {
            "statement_safety_floor": 90.0,
            "branch_safety_floor": 80.0,
            "require_branch_measurement": True,
            "diff": {"statement_safety_floor": statement_floor, "branch_safety_floor": 80.0},
        }
    }


def _diff_coverage_doc(files):
    return {"files": files}


def test_diff_pass_when_changed_lines_covered(policy_mod):
    doc = _diff_coverage_doc(
        {"src/mac/foo.py": {"executed_lines": [10, 11], "missing_lines": [50]}}
    )
    result = policy_mod.evaluate_diff(doc, _diff_policy_doc(), {"src/mac/foo.py": {10, 11}})
    assert result["schema"] == policy_mod.DIFF_SCHEMA
    assert result["status"] == "pass"
    assert result["statements"]["covered"] == 2
    assert result["statements"]["relevant"] == 2
    assert result["statements"]["percent"] == 100.0


def test_diff_fail_when_changed_line_uncovered(policy_mod):
    doc = _diff_coverage_doc({"src/mac/foo.py": {"executed_lines": [10], "missing_lines": [12]}})
    result = policy_mod.evaluate_diff(doc, _diff_policy_doc(), {"src/mac/foo.py": {10, 12}})
    assert result["status"] == "fail"
    assert result["statements"]["percent"] == 50.0
    assert any("diff statement coverage" in msg for msg in result["failures"])


def test_diff_unmeasured_source_file_fails(policy_mod):
    # A changed source file with no coverage entry: all changed lines uncovered.
    result = policy_mod.evaluate_diff(
        _diff_coverage_doc({}), _diff_policy_doc(), {"src/mac/new.py": {1, 2, 3}}
    )
    assert result["status"] == "fail"
    assert result["statements"]["relevant"] == 3
    assert result["statements"]["covered"] == 0


def test_diff_no_changed_lines_passes(policy_mod):
    result = policy_mod.evaluate_diff(_diff_coverage_doc({}), _diff_policy_doc(), {})
    assert result["status"] == "pass"
    assert result["statements"]["percent"] == 100.0


def test_diff_ignores_non_statement_changed_lines(policy_mod):
    # A changed line that is neither executed nor missing (blank/comment) is not
    # counted as relevant.
    doc = _diff_coverage_doc({"src/mac/foo.py": {"executed_lines": [10], "missing_lines": [11]}})
    result = policy_mod.evaluate_diff(doc, _diff_policy_doc(), {"src/mac/foo.py": {10, 99}})
    assert result["statements"]["relevant"] == 1
    assert result["status"] == "pass"


def test_diff_reports_uncovered_branches_without_enforcing(policy_mod):
    doc = _diff_coverage_doc(
        {
            "src/mac/foo.py": {
                "executed_lines": [10],
                "missing_lines": [],
                "missing_branches": [[10, 12], [99, 100]],
            }
        }
    )
    result = policy_mod.evaluate_diff(doc, _diff_policy_doc(), {"src/mac/foo.py": {10}})
    assert result["branches"]["uncovered_on_changed_lines"] == 1
    assert result["branches"]["enforced"] is False
    assert result["status"] == "pass"


def test_main_diff_mode_with_changed_lines_file(policy_mod, tmp_path, capsys):
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            _diff_coverage_doc({"src/mac/foo.py": {"executed_lines": [10], "missing_lines": [11]}})
        ),
        encoding="utf-8",
    )
    policy_path = tmp_path / "test-policy.toml"
    policy_path.write_text(
        "[coverage]\nstatement_safety_floor = 90.0\nbranch_safety_floor = 80.0\n"
        "require_branch_measurement = true\n\n"
        "[coverage.diff]\nstatement_safety_floor = 90.0\nbranch_safety_floor = 80.0\n",
        encoding="utf-8",
    )
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps({"src/mac/foo.py": [10, 11]}), encoding="utf-8")
    code = policy_mod.main(
        [
            "--coverage-json",
            str(coverage_path),
            "--policy",
            str(policy_path),
            "--mode",
            "diff",
            "--changed-lines",
            str(changed_path),
        ]
    )
    assert code == 1  # line 11 uncovered -> 50% < 90%
    assert "diff statement coverage" in capsys.readouterr().err
