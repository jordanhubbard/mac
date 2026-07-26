#!/usr/bin/env python3
"""Plan or run the two deterministic managed-lane cut-over canaries.

Default behavior is read-only: resolve the registered repository and canonical
SHA, then print the exact negative and positive package plans. Live admission
requires three explicit flags and reads its admin token from an environment
variable so no credential appears in the plan, ledger, output, or argv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
CANARY_TASK_PRIORITY = 1_000_000


class CanaryError(RuntimeError):
    pass


def _request(
    hub_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str = "",
    body: dict[str, Any] | None = None,
) -> Any:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        hub_url.rstrip("/") + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, UnicodeError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "reason", exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except OSError:
                pass
        raise CanaryError(f"hub request failed: {method} {path}: {detail}") from exc


def _registered_repository(
    hub_url: str,
    name: str,
    *,
    token: str = "",
) -> dict[str, Any]:
    rows = _request(hub_url, "/bridge/repositories?enabled=true", token=token)
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and (str(row.get("name") or "") == name or str(row.get("project") or "") == name)
    ]
    if len(matches) != 1:
        raise CanaryError(
            f"expected exactly one enabled repository named/project {name!r}; found {len(matches)}"
        )
    return matches[0]


def _canonical_remote(repository: dict[str, Any]) -> str:
    metadata = repository.get("metadata")
    contract = metadata.get("repository_contract") if isinstance(metadata, dict) else None
    value = contract.get("canonical_remote_url") if isinstance(contract, dict) else None
    remote = str(value or repository.get("source") or "").strip()
    if not remote or re.search(r"https?://[^/@\s]+@", remote):
        raise CanaryError("registered canonical remote is missing or embeds credentials")
    return remote


def _remote_sha(remote: str, target_ref: str) -> str:
    completed = subprocess.run(
        ["git", "ls-remote", "--exit-code", remote, target_ref],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND", "SYSTEMROOT"}
        },
    )
    if completed.returncode != 0:
        raise CanaryError("canonical Git read-back failed")
    fields = completed.stdout.strip().split()
    if len(fields) < 2 or fields[1] != target_ref or not SHA_RE.fullmatch(fields[0]):
        raise CanaryError("canonical Git read-back returned an invalid exact ref")
    return fields[0]


def _plan(
    case: str,
    *,
    run_id: str,
    repository_id: str,
    base_sha: str,
    target_ref: str,
) -> dict[str, Any]:
    package_id = f"wp_canary_{run_id}_{case}"
    if case == "positive":
        path = f"docs/canaries/{run_id}-managed-positive.md"
        instructions = (
            f"Managed-lane positive canary. Create only {path} with a short, "
            "credential-free note stating this is a disposable synchronized-pipeline "
            "canary. Do not change source, tests, configuration, or any other file. "
            "Run the repository gate and submit the exact candidate normally."
        )
        writes = [path]
        goal = "prove a docs-safe exact candidate certifies, lands, and finalizes"
    else:
        instructions = (
            "Managed-lane negative certification canary; preserve the deliberate "
            "regression. In src/mac/publication_lane.py change only "
            "lane_provides_external_certifier so it validates the lane and returns "
            "False for every valid lane. In tests/test_publication_lane.py change the "
            "managed-lane predicate assertion to expect False, allowing the "
            "candidate-owned pre-push suite to pass. Do not alter any other behavior "
            "or repair the mismatch: the image-owned frozen baseline must reject it."
        )
        writes = ["src/mac/publication_lane.py", "tests/test_publication_lane.py"]
        goal = "prove frozen independent certification rejects a candidate-masked regression"
    return {
        "schema": "mac.work_package.plan.v1",
        "package_id": package_id,
        "goal": goal,
        "project": "mac",
        "repository_id": repository_id,
        "planning_base_ref": target_ref,
        "planning_base_sha": base_sha,
        "plan_generation": 1,
        "max_in_flight": 1,
        "mutation_wip": {"max_tokens": 1},
        "integration": {"target_ref": target_ref},
        "metadata": {
            "schema": "mac.work_package.cutover_canary.v1",
            "case": case,
            "run_id": run_id,
            "expected_canonical_movement": case == "positive",
        },
        "nodes": [
            {
                "node_key": "change",
                "title": f"Produce {case} managed-lane canary candidate",
                "instructions": instructions,
                "node_type": "mutation",
                "priority": CANARY_TASK_PRIORITY,
                "effects": {"writes": writes},
                "expected_outputs": ["component-candidate"],
                "verification": {"profile": "repository-default"},
                "rework": {"max_cycles": 0},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble the exact canary candidate",
                "node_type": "integration",
                "priority": CANARY_TASK_PRIORITY,
                "depends_on": ["change"],
                "inputs": ["component-candidate"],
                "expected_outputs": ["candidate-tree"],
                "verification": {"profile": "integration-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "certify",
                "title": "Run the independent frozen certifier",
                "node_type": "certification",
                "priority": CANARY_TASK_PRIORITY,
                "depends_on": ["assemble"],
                "inputs": ["candidate-tree"],
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
                "estimates": {"confidence": "high"},
            },
        ],
    }


def _history(description: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [
        item
        for item in description.get("history", [])
        if isinstance(item, dict) and item.get("event_type") == event_type
    ]


def _assert_pipeline_ready(hub_url: str, *, token: str = "") -> dict[str, Any]:
    status = _request(hub_url, "/work-package-pipeline/status", token=token)
    runtime = status.get("runtime") if isinstance(status, dict) else None
    if (
        not status.get("thread_alive")
        or not isinstance(runtime, dict)
        or runtime.get("enabled") is not True
        or runtime.get("configuration_error")
    ):
        raise CanaryError("managed pipeline is not alive and configuration-clean")
    return status


def _run_case(
    case: str,
    plan: dict[str, Any],
    *,
    hub_url: str,
    token: str,
    remote: str,
    target_ref: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict[str, Any]:
    before = _remote_sha(remote, target_ref)
    if before != plan["planning_base_sha"]:
        raise CanaryError(
            f"canonical ref moved before {case} admission: planned={plan['planning_base_sha']} observed={before}"
        )
    admitted = _request(
        hub_url,
        "/work-packages",
        method="POST",
        token=token,
        body={
            "plan": plan,
            "actor": "cutover-canary",
            "reason": f"{case} pre-cutover managed-lane canary",
        },
    )
    package_id = plan["package_id"]
    _request(
        hub_url,
        f"/work-packages/{urllib.parse.quote(package_id, safe='')}/activate",
        method="POST",
        token=token,
        body={
            "expected_plan_version": 1,
            "expected_epoch": 1,
            "actor": "cutover-canary",
        },
    )

    deadline = time.monotonic() + timeout_seconds
    terminal: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        _request(hub_url, "/work-package-pipeline/trigger", method="POST", token=token)
        terminal = _request(
            hub_url,
            f"/work-packages/{urllib.parse.quote(package_id, safe='')}",
            token=token,
        )
        if case == "positive" and _history(terminal, "work_package.publication_finalized"):
            break
        if case == "negative" and _history(terminal, "work_package.certification_rejected"):
            break
        time.sleep(poll_seconds)
    else:
        raise CanaryError(f"{case} canary timed out before its expected terminal receipt")

    assert terminal is not None
    after = _remote_sha(remote, target_ref)
    finalized = _history(terminal, "work_package.publication_finalized")
    rejected = _history(terminal, "work_package.certification_rejected")
    if case == "negative":
        if after != before:
            raise CanaryError("negative certification canary moved canonical Git")
        if finalized:
            raise CanaryError("negative certification canary created a publication receipt")
        if not rejected or terminal.get("package", {}).get("state") != "paused":
            raise CanaryError("negative canary did not raise the certification Andon")
        detail = rejected[-1].get("detail") or {}
        for required in (
            "certification_id",
            "controller_station_receipt_id",
            "provenance_digest",
            "phase_manifest_digest",
            "changed_files_digest",
        ):
            if not detail.get(required):
                raise CanaryError(f"negative canary rejection lacks {required}")
        if detail.get("assembly_base_sha") != before:
            raise CanaryError("negative canary certifier used the wrong assembly base")
        if detail.get("selection_mode") != "source_focused":
            raise CanaryError("negative canary did not use the mapped-source fast lane")
        if detail.get("full_suite_count") != 0:
            raise CanaryError("negative canary unexpectedly ran a full certifier suite")
        terminal_receipt = detail
    else:
        if after == before:
            raise CanaryError("positive canary did not move canonical Git")
        if rejected or terminal.get("package", {}).get("state") != "completed":
            raise CanaryError("positive canary did not complete cleanly")
        detail = finalized[-1].get("detail") or {}
        for required in (
            "landing_receipt_id",
            "landing_receipt_digest",
            "observed_sha",
            "controller_station_receipt_ids",
        ):
            if not detail.get(required):
                raise CanaryError(f"positive canary finalization lacks {required}")
        if detail["observed_sha"] != after:
            raise CanaryError("positive canary receipt does not match remote read-back")
        terminal_receipt = detail
    return {
        "schema": "mac.work_package.cutover_canary_receipt.v1",
        "case": case,
        "package_id": package_id,
        "canonical_before": before,
        "canonical_after": after,
        "expected_canonical_movement": case == "positive",
        "admission": admitted,
        "terminal_receipt": terminal_receipt,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-url", default=os.environ.get("MAC_URL", ""))
    parser.add_argument("--repository-name", default="mac")
    parser.add_argument("--repository-id")
    parser.add_argument("--canonical-remote")
    parser.add_argument("--target-ref", default="refs/heads/main")
    parser.add_argument("--base-sha")
    parser.add_argument("--run-id")
    parser.add_argument("--case", choices=("negative", "positive", "both"), default="both")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--confirm-exclusive-main-window", action="store_true")
    parser.add_argument("--token-env", default="MAC_API_TOKEN")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--receipt-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        hub_url = str(args.hub_url or "").strip()
        read_token = str(os.environ.get(args.token_env) or "")
        repository: dict[str, Any] = {}
        # Only consult the hub when the repository identity was not fully
        # supplied on the command line. When both --repository-id and
        # --canonical-remote are given the plan is built entirely offline, so
        # an ambient MAC_URL (the --hub-url default) must NOT trigger a live
        # hub request. Otherwise the documented read-only "print both plans"
        # mode would silently depend on a reachable, authorized hub.
        needs_hub_discovery = not (
            str(args.repository_id or "").strip()
            and str(args.canonical_remote or "").strip()
        )
        if hub_url and needs_hub_discovery:
            repository = _registered_repository(
                hub_url,
                args.repository_name,
                token=read_token,
            )
        repository_id = str(args.repository_id or repository.get("id") or "").strip()
        remote = str(args.canonical_remote or "").strip() or (
            _canonical_remote(repository) if repository else ""
        )
        if not repository_id or not remote:
            raise CanaryError(
                "provide --hub-url, or both --repository-id and --canonical-remote"
            )
        base_sha = str(args.base_sha or "").strip() or _remote_sha(remote, args.target_ref)
        if not SHA_RE.fullmatch(base_sha):
            raise CanaryError("base SHA must be an exact lowercase Git SHA")
        run_id = str(args.run_id or f"pilot_{base_sha[:12]}").strip()
        if not RUN_ID_RE.fullmatch(run_id):
            raise CanaryError("run id must contain only lowercase letters, digits, _ or -")
        cases = ("negative", "positive") if args.case == "both" else (args.case,)
        plans = {
            case: _plan(
                case,
                run_id=run_id,
                repository_id=repository_id,
                base_sha=base_sha,
                target_ref=args.target_ref,
            )
            for case in cases
        }
        output: dict[str, Any] = {
            "schema": "mac.work_package.cutover_canary_plan.v1",
            "mode": "execute" if args.execute else "plan",
            "canonical_remote": remote,
            "canonical_before": base_sha,
            "target_ref": args.target_ref,
            "order": list(cases),
            "plans": plans,
            "assertions": {
                "negative": "certification rejection receipt exists and canonical SHA is unchanged",
                "positive": "publication/finalization receipts exist and remote SHA equals observed_sha",
            },
        }
        if args.execute:
            if not args.confirm_live or not args.confirm_exclusive_main_window:
                raise CanaryError(
                    "live execution requires --confirm-live and --confirm-exclusive-main-window"
                )
            if not hub_url:
                raise CanaryError("live execution requires --hub-url")
            token = str(os.environ.get(args.token_env) or "")
            if not token:
                raise CanaryError(f"live execution requires token in {args.token_env}")
            _assert_pipeline_ready(hub_url, token=token)
            receipts = []
            for case in cases:
                receipts.append(
                    _run_case(
                        case,
                        plans[case],
                        hub_url=hub_url,
                        token=token,
                        remote=remote,
                        target_ref=args.target_ref,
                        timeout_seconds=args.timeout_seconds,
                        poll_seconds=args.poll_seconds,
                    )
                )
            output["receipts"] = receipts
        rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
        if args.receipt_file:
            args.receipt_file.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0
    except (CanaryError, OSError, subprocess.SubprocessError) as exc:
        print(f"work-package canary failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
