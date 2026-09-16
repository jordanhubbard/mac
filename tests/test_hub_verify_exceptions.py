"""A failed verifier retains useful evidence without signing a test verdict."""

from __future__ import annotations

from pathlib import Path
import subprocess
import types

import pytest

from mac import gitops, services


HEAD = "a" * 40


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_timeout_keeps_redacted_partial_output_and_cleans_up(
    monkeypatch, caplog, as_bytes, cleanup_fails
):
    calls = []
    observations = []
    stdout = (
        "verification started\nAuthorization: Bearer private-auth-value\n"
        "MAC_API_TOKEN=private-env-value\n"
        + "progress\n" * 3000
        + "short test summary info\nFAILED tests/test_example.py::test_progress\n"
        + "coverage row\n" * 2000
    )
    stderr = (
        "database wait: postgresql://user:private-db-value@localhost/test\n"
        '{"api_key": "private-json-value"}\n'
        "last diagnostic\n"
    )
    if as_bytes:
        stdout, stderr = stdout.encode() + b"\xff", stderr.encode()
    failure = subprocess.TimeoutExpired(
        ["openshell", "--env", "UNCLASSIFIED=private-argv-value"],
        123,
        output=stdout,
        stderr=stderr,
    )

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "git" and "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, HEAD + "\n", "")
        if "create" in argv:
            raise failure
        if cleanup_fails and "delete" in argv and any("create" in a for a in calls):
            raise subprocess.TimeoutExpired(
                ["cleanup", "private-cleanup-argv"], 60, stderr=b"token=private-cleanup-value"
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setattr(gitops, "askpass_remote_auth", lambda url: (url, {}))
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE",
        "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
    )
    monkeypatch.delenv("MAC_OPENSHELL_GC", raising=False)
    monkeypatch.delenv("MAC_HUB_VERIFY_PROFILE", raising=False)
    plane = types.SimpleNamespace(
        _hub_review_test_command=lambda task, info: "run-tests",
        _record_default_review_observation=lambda *args: observations.append(args),
    )
    plane._hub_verify_run_contract_test = types.MethodType(
        services.ControlPlane._hub_verify_run_contract_test, plane
    )
    result = services.ControlPlane._run_hub_review_verification_locked(
        plane,
        types.SimpleNamespace(id="task", metadata={}),
        types.SimpleNamespace(id="review"),
        types.SimpleNamespace(id="executor-evidence"),
        "operator",
        {"remote_url": "https://example.invalid/repo.git", "branch": "branch", "head_sha": HEAD},
        "unused-signing-key",
    )

    # No signing/evidence-writing methods exist on this plane: the exception
    # must return before approval or rejection can be manufactured.
    assert result is None
    assert len(observations) == 1
    assert observations[0][1] == "workflow.default_review.hub_verify_error"
    detail = observations[0][3]
    assert detail["review_id"] == "review"
    assert detail["error_type"] == "TimeoutExpired"
    assert detail["timeout_seconds"] == 123
    assert "verification started" in detail["output_excerpt"]
    assert "FAILED tests/test_example.py::test_progress" in detail["output_excerpt"]
    assert "last diagnostic" in detail["output_excerpt"]
    assert len(detail["output_excerpt"]) < 5000
    assert "private-" not in str(detail) + caplog.text
    assert "<redacted>" in detail["output_excerpt"]
    assert len([a for a in calls if "delete" in a]) == 2
    clone = next(a for a in calls if "clone" in a)
    assert not Path(clone[-1]).parent.exists()


@pytest.mark.parametrize(
    "failure,expected",
    [
        (subprocess.TimeoutExpired(["private-argv-value"], 17), "TimeoutExpired"),
        (
            subprocess.CalledProcessError(9, ["private-argv-value"], stderr="token=private-value"),
            "CalledProcessError",
        ),
        (OSError("database unavailable: password=private-value"), "OSError"),
    ],
)
def test_exception_without_stdout_has_bounded_safe_details(failure, expected):
    detail = services._hub_verify_exception_detail(failure)
    assert detail["error_type"] == expected
    assert "private-" not in str(detail)
    assert "output_excerpt" in detail
    if isinstance(failure, subprocess.CalledProcessError):
        assert detail["returncode"] == 9


def test_shared_diagnostic_tail_uses_the_same_credential_redaction():
    tail, reason = services._diagnostic_output_tail(
        {
            "output": 'Authorization: Bearer private-header\n{"api_key": "private-key"}\n'
            "MAC_API_TOKEN=private-env\npostgresql://user:private-db@localhost/test"
        }
    )
    assert not reason
    assert "private-" not in tail
    assert "localhost/test" in tail
