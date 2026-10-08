"""The judge, and an attempt that continues instead of failing.

Before this, a gate failure or an incomplete change ended the attempt, and the
retry started cold. Now the same Claude Code session is resumed with the gate
output or the judge's next steps, the judge's verdict is signed into the
evidence the hub reviews, and a block that only restates a harness decision
is retried rather than treated as bad work.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from mac import executor_sandbox as ex
from mac import task_judge
from mac.executor_prompt import _review_feedback_section
from mac.services import _blocked_attempt_retry_kind
from mac.worker import _only_harness_finalization_problems


# -- the judge ---------------------------------------------------------------


def test_judge_prompt_carries_the_task_criteria_direction_and_change():
    task = {
        "title": "Fix parser",
        "description": "1. reject empty input\n2. add a test",
        "metadata": {"acceptance_criteria": ["empty input exits 2"]},
    }
    prompt = task_judge.build_prompt(
        task, "## diff\n+if (!n) exit(2);", agent_summary="done", board_direction="- keep the API"
    )
    for needle in ("reject empty input", "empty input exits 2", "keep the API", "exit(2)", '"verdict"'):
        assert needle in prompt


@pytest.mark.parametrize(
    "reply,verdict,next_steps",
    [
        ('{"verdict": "met", "reason": "both done", "next": ""}', "met", ""),
        ('Sure.\n{"verdict":"not_met","reason":"no test","next":"add tests/test_empty.c"}', "not_met", "add tests/test_empty.c"),
        ('{"verdict":"not_met","reason":"requirement 2 missing"}', "not_met", "requirement 2 missing"),
        ("I think it is fine", "unavailable", ""),
        ('{"verdict":"maybe"}', "unavailable", ""),
    ],
)
def test_judge_reply_parsing(reply, verdict, next_steps):
    parsed = task_judge.parse_reply(reply, model="m")
    assert parsed.verdict == verdict
    assert parsed.next_steps == next_steps


def test_an_unreachable_judge_is_unavailable_not_a_failure():
    def broken(model, prompt):
        raise OSError("down")

    assert task_judge.judge({"title": "t"}, "", broken).verdict == "unavailable"


def test_collect_change_shows_tracked_and_new_files_without_staging(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "base")
    (repo / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "a")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "new.c").write_text("int main(){}\n")
    change = task_judge.collect_change(repo, base)
    assert "+two" in change and "new file new.c" in change and "int main" in change
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "?? new.c" in status  # still untracked: nothing was staged


# -- the continuation loop -------------------------------------------------------


class _Result:
    def __init__(self, returncode: int = 0, gate: Any = None) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""
        if gate is not None:
            self.mac_repository_verification_failure = gate


@pytest.fixture
def claude_workspace(tmp_path, monkeypatch):
    # The judge loop follows the CLI that actually ran this attempt.
    monkeypatch.setitem(ex._LAST_CODING_ROUTE, "agent", "claude")
    (tmp_path / ".mac-agent").mkdir()
    (tmp_path / ".mac-agent" / "session-id").write_text("sess-1\n")
    ex._LAST_JUDGE_VERDICT.clear()
    posts: List[Dict[str, Any]] = []
    monkeypatch.setattr(ex, "_board_messages", lambda task_id: [])
    monkeypatch.setattr(ex, "_post_board_as_hub", lambda task_id, kind, body, **m: posts.append({"kind": kind, "body": body, **m}))
    monkeypatch.setattr(ex, "emit_telemetry", lambda *a, **k: None)
    return tmp_path, posts


def test_a_red_gate_resumes_the_same_session_with_the_failure(claude_workspace, monkeypatch):
    workspace, posts = claude_workspace
    calls: List[Dict[str, Any]] = []
    verdicts = iter([task_judge.Verdict("met", "all there")])
    monkeypatch.setattr(ex, "_judge_task_change", lambda task, ws, messages: next(verdicts))

    def invoke(runner, prompt, ws, audit_id, opts):
        calls.append(
            {"prompt": prompt, "resume": opts.get("resume_session"), "only": opts.get("only_agent")}
        )
        return _Result()

    monkeypatch.setattr(ex, "_invoke_agent", invoke)
    red = _Result(68, gate={"failure_class": "repository_test_failed", "detail": "FAILED test_parse"})
    out = ex._continue_claude_session(None, {"title": "t"}, workspace, "task_1", red, {})
    assert out.returncode == 0
    assert calls[0]["resume"] == "sess-1"
    # A resumed Claude session must resume in Claude, whatever the list says.
    assert calls[0]["only"] == "claude"
    assert "FAILED test_parse" in calls[0]["prompt"]
    assert ex._LAST_JUDGE_VERDICT["verdict"] == "met"
    assert [p.get("verdict") for p in posts if p["kind"] == "verdict"][-1] == "met"


def test_not_met_hands_the_next_steps_back_until_met(claude_workspace, monkeypatch):
    workspace, posts = claude_workspace
    verdicts = iter(
        [task_judge.Verdict("not_met", "no test", "add a test for empty input"), task_judge.Verdict("met", "ok")]
    )
    monkeypatch.setattr(ex, "_judge_task_change", lambda task, ws, messages: next(verdicts))
    prompts: List[str] = []
    monkeypatch.setattr(ex, "_invoke_agent", lambda r, prompt, ws, a, opts: prompts.append(prompt) or _Result())
    ex._continue_claude_session(None, {}, workspace, "task_1", _Result(), {})
    assert len(prompts) == 1 and "add a test for empty input" in prompts[0]
    assert "Next: add a test for empty input" in posts[0]["body"]


def test_the_loop_is_bounded_and_stops_for_a_blocking_question(claude_workspace, monkeypatch):
    workspace, _ = claude_workspace
    monkeypatch.setenv("MAC_CLAUDE_CONTINUATION_ROUNDS", "2")
    monkeypatch.setattr(ex, "_judge_task_change", lambda *a: task_judge.Verdict("not_met", "no", "more"))
    calls: List[str] = []
    monkeypatch.setattr(ex, "_invoke_agent", lambda r, p, w, a, o: calls.append(p) or _Result())
    ex._continue_claude_session(None, {}, workspace, "task_1", _Result(), {})
    assert len(calls) == 2
    calls.clear()
    question = {"id": 7, "kind": "question", "author_kind": "agent", "metadata": {"blocking": True}}
    monkeypatch.setattr(ex, "_board_messages", lambda task_id: [question])
    ex._continue_claude_session(None, {}, workspace, "task_1", _Result(), {})
    assert calls == []


def test_verifier_infrastructure_is_not_handed_to_the_agent(claude_workspace, monkeypatch):
    workspace, _ = claude_workspace
    monkeypatch.setattr(ex, "_invoke_agent", lambda *a: pytest.fail("must not resume"))
    broken = _Result(68, gate={"failure_class": "verifier_infrastructure", "detail": "upload failed"})
    assert ex._continue_claude_session(None, {}, workspace, "task_1", broken, {}) is broken


def test_opencode_runs_are_untouched(claude_workspace, monkeypatch):
    workspace, _ = claude_workspace
    monkeypatch.setitem(ex._LAST_CODING_ROUTE, "agent", "opencode")
    monkeypatch.setattr(ex, "_invoke_agent", lambda *a: pytest.fail("must not resume"))
    result = _Result()
    assert ex._continue_claude_session(None, {}, workspace, "task_1", result, {}) is result


def test_the_signed_evidence_carries_the_hosts_verdict_not_the_workspaces(tmp_path):
    (tmp_path / "mac-evidence.json").write_text(json.dumps({"schema": "x", "status": "complete"}))
    ex._LAST_JUDGE_VERDICT.clear()
    ex._LAST_JUDGE_VERDICT.update({"verdict": "not_met", "reason": "r", "next": "n"})
    ex._record_judge_verdict_in_evidence(tmp_path)
    assert json.loads((tmp_path / "mac-evidence.json").read_text())["judge"]["verdict"] == "not_met"
    ex._LAST_JUDGE_VERDICT.clear()


# -- what happens after ----------------------------------------------------------


def test_harness_only_problems_retry_and_real_ones_still_stop():
    harness = [
        "repo evidence requires pushed=true with remote_ref, or pr_url",
        "repo_change evidence requires a repository verifier test result for repo.head_sha; "
        "none qualifies (tests[0] (repository gate): status is None, not 'pass'; executed_head_sha is missing)",
    ]
    wrong_head = (
        "repo_change evidence requires a repository verifier test result for repo.head_sha; none "
        "qualifies (tests[0] (gate): executed_head_sha bbbb is not repo.head_sha aaaa)"
    )
    assert not _only_harness_finalization_problems([wrong_head])
    assert _only_harness_finalization_problems(
        ["repo_change evidence requires a repository verifier test result for repo.head_sha; "
         "verification.tests is empty"]
    )
    assert _only_harness_finalization_problems(harness)
    assert not _only_harness_finalization_problems(harness + ["repo evidence requires changed files"])
    assert not _only_harness_finalization_problems([])
    assert _blocked_attempt_retry_kind(
        {"reason": "harness_finalization_incomplete", "problems": harness}
    ) == "infrastructure_transient"
    assert _blocked_attempt_retry_kind(
        {"reason": "verification_contract_failed", "problems": harness}
    ) == "non_retryable"


def test_the_next_attempt_starts_from_the_review():
    task = {"metadata": {"review_feedback": {"latest": {"summary": "independent judge: not met: no test; next: add one"}}}}
    section = _review_feedback_section(task)
    assert "not accepted" in section and "add one" in section
    assert _review_feedback_section({"metadata": {}}) == ""
