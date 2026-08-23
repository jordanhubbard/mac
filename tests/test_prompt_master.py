from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from mac import executor_sandbox
from mac.prompt_master import MAX_INPUT_BYTES, PromptPolicyError, compile_prompt


@pytest.mark.parametrize("target", ["claude", "codex", "cursor", "opencode", "pi", "acp", "api"])
def test_compiler_is_target_aware_idempotent_and_redacts_secrets(target):
    source = "Executor policy block verbatim.\nAPI_TOKEN=super-secret\nImplement the task."
    first = compile_prompt(source, target=target, model="reasoning-model")
    second = compile_prompt(first.text, target=target, model="reasoning-model")

    assert first.text == second.text
    assert "Executor policy block verbatim." in first.text
    assert "API_TOKEN=<redacted>" in first.text
    assert "super-secret" not in first.text
    assert "chain-of-thought" in first.text
    assert first.evidence["schema"] == "mac.prompt_rewrite.v1"
    assert first.evidence["target_profile"] == target
    assert first.evidence["rewritten_sha256"] == second.evidence["rewritten_sha256"]


def test_compiler_fails_closed_on_empty_and_oversized_prompts():
    with pytest.raises(PromptPolicyError):
        compile_prompt("", target="claude")
    with pytest.raises(PromptPolicyError):
        compile_prompt("x" * (MAX_INPUT_BYTES + 1), target="claude")


def test_executor_compiles_after_route_selection_before_private_file(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(executor_sandbox, "_executor_backend", lambda: "cli")
    monkeypatch.setattr(executor_sandbox, "_openshell_enabled", lambda: False)
    monkeypatch.setattr(executor_sandbox, "_openshell_required_for_local_agent", lambda: False)
    monkeypatch.setattr(executor_sandbox, "_validated_host_break_glass_authorization", lambda task: {"id": "bg"})
    monkeypatch.setattr(executor_sandbox, "_prepare_host_break_glass_environment", lambda auth: None)
    monkeypatch.setattr(executor_sandbox, "_agent_argv", lambda *a, **kw: kw["chosen"].update({"agent": "claude", "model": "m", "fingerprint": "fp"}) or ["claude", "PROMPT"])
    monkeypatch.setattr(executor_sandbox, "_unsandboxed_agent_argv", lambda argv, **kw: argv)

    def fake_bundle(workspace, prompt, argv):
        captured["prompt"] = prompt
        class Bundle:
            def argv(self, **kwargs): return argv
            def cleanup(self): pass
        return Bundle()

    monkeypatch.setattr(executor_sandbox, "_write_agent_command_bundle", fake_bundle)
    result = executor_sandbox._invoke_agent(
        lambda *args: type("R", (), {"returncode": 0})(),
        "Do work. TOKEN=secret", tmp_path, "task_1", {"task": {"id": "task_1"}},
    )
    assert result.returncode == 0
    assert captured["prompt"].startswith("<!-- mac.prompt_master.v1 -->")
    assert "secret" not in captured["prompt"]


def test_all_known_script_dispatches_cross_compiler_boundary():
    root = Path(__file__).parents[1]
    for relative in (
        "deploy/codex-runner/mac-task-executor-opencode-build",
        "deploy/codex-runner/mac-task-executor-opencode-review",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert "compile_coding_prompt" in text
        for line in text.splitlines():
            if "opencode run" in line and not line.lstrip().startswith("#"):
                assert "${" in line

    source = inspect.getsource(executor_sandbox._invoke_agent)
    assert source.index("_agent_argv(") < source.index("_compile_outbound_prompt(")
    assert source.index("_compile_outbound_prompt(") < source.index("_write_agent_command_bundle(")
