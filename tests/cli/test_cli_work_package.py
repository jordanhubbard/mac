"""CLI routing coverage for the managed work-package assembly line."""

from __future__ import annotations

import json

from mac import cli


class _Plane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"method": name}

        return call


def _run(tmp_path, *args: str) -> None:
    """Parse and execute one command through the real CLI handler."""

    del tmp_path
    parsed = cli.build_parser().parse_args(list(args))
    parsed.func(parsed)


def test_all_work_package_commands_route_to_the_control_plane(
    tmp_path, monkeypatch
) -> None:
    plane = _Plane()
    outputs = []
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_print", outputs.append)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"package_id": "wp_cli"}), encoding="utf-8")
    result_path = tmp_path / "certification-result.json"
    result_path.write_text(json.dumps({"schema": "fixture"}), encoding="utf-8")

    _run(tmp_path, "work-package", "list")
    _run(
        tmp_path,
        "work-package",
        "admit",
        "--plan-file",
        str(plan_path),
        "--reason",
        "CLI coverage",
    )
    _run(tmp_path, "work-package", "show", "wp_cli")
    _run(tmp_path, "work-package", "readiness", "wp_cli")
    _run(
        tmp_path,
        "work-package",
        "activate",
        "wp_cli",
        "--plan-version",
        "1",
        "--epoch",
        "1",
    )
    _run(
        tmp_path,
        "work-package",
        "replan-preview",
        "wp_cli",
        "--plan-file",
        str(plan_path),
        "--plan-version",
        "1",
        "--epoch",
        "1",
        "--reason",
        "preview",
    )
    _run(
        tmp_path,
        "work-package",
        "pause",
        "wp_cli",
        "--plan-version",
        "1",
        "--epoch",
        "1",
        "--reason",
        "Andon",
    )
    _run(
        tmp_path,
        "work-package",
        "replan",
        "wp_cli",
        "--plan-file",
        str(plan_path),
        "--plan-version",
        "1",
        "--epoch",
        "1",
        "--reason",
        "apply",
    )
    _run(tmp_path, "work-package", "verify-output", "ev_cli")
    _run(tmp_path, "work-package", "accept-candidate", "candidate_cli")
    _run(
        tmp_path,
        "work-package",
        "reject-candidate",
        "candidate_cli",
        "--reason",
        "rework",
    )
    _run(tmp_path, "work-package", "assemble", "wp_cli", "integrate")
    _run(tmp_path, "work-package", "assembly-status", "batch_cli")
    _run(tmp_path, "work-package", "assembly-claim", "batch_cli")
    _run(tmp_path, "work-package", "assemble-batch", "batch_cli")
    _run(
        tmp_path,
        "work-package",
        "certification-prepare",
        "batch_cli",
        str(tmp_path),
    )
    _run(tmp_path, "work-package", "certification-status", "job_cli")
    _run(tmp_path, "work-package", "certification-claim", "job_cli")
    _run(
        tmp_path,
        "work-package",
        "certification-ingest",
        "job_cli",
        "--result-file",
        str(result_path),
        "--owner",
        "certifier",
        "--fence",
        "1",
    )
    _run(
        tmp_path,
        "work-package",
        "certification-run",
        "job_cli",
        str(tmp_path),
    )
    _run(
        tmp_path,
        "work-package",
        "reject-failed-certification",
        "batch_cli",
        "cert_cli",
    )
    _run(
        tmp_path,
        "work-package",
        "accept-certification",
        "batch_cli",
        "cert_cli",
    )
    _run(tmp_path, "work-package", "land", "batch_cli")
    _run(tmp_path, "work-package", "finalize-publication", "batch_cli")

    assert [name for name, _args, _kwargs in plane.calls] == [
        "list_work_packages",
        "admit_work_package",
        "describe_work_package",
        "work_package_activation_readiness",
        "activate_work_package",
        "preview_work_package_replan",
        "pause_work_package",
        "replan_work_package",
        "verify_work_package_output",
        "accept_work_package_candidate",
        "reject_work_package_candidate",
        "assemble_work_package",
        "work_package_integration_status",
        "claim_work_package_integration_batch",
        "assemble_work_package_integration_batch",
        "prepare_work_package_certification_job",
        "work_package_certification_status",
        "claim_work_package_certification_job",
        "ingest_work_package_certification_result",
        "run_work_package_certification_job",
        "reject_failed_work_package_certification",
        "accept_work_package_certification",
        "land_work_package",
        "finalize_work_package_publication",
    ]
    assert len(outputs) == len(plane.calls)
