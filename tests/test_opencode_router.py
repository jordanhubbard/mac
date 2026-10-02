"""opencode through the hub's model router: selection, argv, sandbox env, config.

opencode is the coding CLI. It gets its model from the hub's OpenAI-compatible
router through a generated config whose only provider is ``machub``, and it
authenticates with a per-task inference token. The worker token stays on the
host (tests/test_inference_tokens.py covers the hub side).
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml

from mac import coding_agent as ca
from mac import executor_sandbox as es
from mac import openshell_policy

_HUB_ENV = {"MAC_HUB_URL": "http://100.64.0.1:8789", "MAC_WORKER_TOKEN": "mac_worker_secret"}


def _which(*names):
    return lambda name: ("/usr/local/bin/%s" % name) if name in names else None


def test_unset_coding_agent_means_opencode(tmp_path):
    # claude is installed and authenticated, but opencode is the coding CLI.
    env = {**_HUB_ENV, "ANTHROPIC_API_KEY": "k"}
    choice = ca.resolve_coding_agent(env=env, home=tmp_path, which=_which("claude", "opencode"))
    assert choice.agent == "opencode"
    assert "MAC_CODING_AGENT unset; opencode is the coding CLI" in choice.rationale
    # Without opencode there is no silent fall-through to another CLI.
    missing = ca.resolve_coding_agent(env=env, home=tmp_path, which=_which("claude"))
    assert missing.agent == "" and missing.available is False
    # auto keeps the old multi-CLI selection until the other detectors go.
    auto = ca.resolve_coding_agent(
        env={**env, "MAC_CODING_AGENT": "auto"}, home=tmp_path, which=_which("claude")
    )
    assert auto.agent == "claude"


def test_hub_credentials_select_the_router_route(tmp_path):
    choice = ca.resolve_coding_agent(env=_HUB_ENV, home=tmp_path, which=_which("opencode"))
    assert choice.available
    assert choice.auth_source == "MAC_INFERENCE_TOKEN"
    assert choice.provider == "mac-router"
    assert choice.protocol == "openai-chat-completions"
    assert choice.endpoint == "http://100.64.0.1:8789/v1"
    assert choice.model == "gpt-5.6-sol"
    assert "mac_worker_secret" not in json.dumps(choice.observable())
    # A sandbox holds only the inference token, and still sees the same route.
    inside = ca.resolve_coding_agent(
        env={"MAC_HUB_URL": _HUB_ENV["MAC_HUB_URL"], "MAC_INFERENCE_TOKEN": "t"},
        home=tmp_path,
        which=_which("opencode"),
    )
    assert inside.route_fingerprint() == choice.route_fingerprint()


def test_without_hub_credentials_the_legacy_auth_file_route_remains(tmp_path):
    auth = tmp_path / ".local" / "share" / "opencode"
    auth.mkdir(parents=True)
    (auth / "auth.json").write_text('{"nvidia": {"type": "api", "key": "x"}}')
    choice = ca.resolve_coding_agent(env={}, home=tmp_path, which=_which("opencode"))
    assert choice.auth_source == "~/.local/share/opencode/auth.json"
    assert choice.provider == "opencode"


@pytest.mark.parametrize(
    "extra, model",
    [
        ({}, "machub/gpt-5.6-sol"),
        ({"MAC_CODING_DEFAULT_MODEL": "claude-sonnet-4-6"}, "machub/claude-sonnet-4-6"),
        (
            {"MAC_CODING_DEFAULT_MODEL": "x", "MAC_TASK_MODEL": "machub/kimi-k2"},
            "machub/kimi-k2",
        ),
    ],
)
def test_argv_runs_opencode_on_a_machub_model(tmp_path, extra, model):
    env = {**_HUB_ENV, **extra}
    choice = ca.resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode"))
    argv = ca.coding_agent_argv(choice, "do it", env=env)
    assert argv[:3] == ["/usr/local/bin/opencode", "run", "--auto"]
    assert argv[argv.index("--model") + 1] == model
    assert argv[-1] == "do it"


def test_generated_config_names_one_router_provider_and_no_secret():
    env = {
        "MAC_HUB_URL": "http://host.openshell.internal:8789/",
        "MAC_INFERENCE_TOKEN": "mac_inference_secret",
        "MAC_CODING_MODELS": "gpt-5.6-sol, claude-sonnet-4-6",
        "MAC_TASK_MODEL": "kimi-k2",
        "MAC_TASK_ID": "task_1",
    }
    config = ca.opencode_router_config(env)
    assert list(config["provider"]) == ["machub"]
    machub = config["provider"]["machub"]
    assert machub["npm"] == "@ai-sdk/openai-compatible"
    assert machub["options"]["baseURL"] == "http://host.openshell.internal:8789/v1"
    assert machub["options"]["apiKey"] == "{env:MAC_INFERENCE_TOKEN}"
    assert machub["options"]["headers"] == {"X-MAC-Task-ID": "task_1"}
    assert list(machub["models"]) == ["gpt-5.6-sol", "claude-sonnet-4-6", "kimi-k2"]
    assert machub["models"]["gpt-5.6-sol"] == {"tool_call": True}
    assert config["model"] == "machub/kimi-k2"
    assert config["autoupdate"] is False
    assert "mac_inference_secret" not in json.dumps(config)
    with pytest.raises(ValueError, match="MAC_HUB_URL"):
        ca.opencode_router_config({})


def _clear_token(monkeypatch):
    monkeypatch.setattr(es, "_TASK_INFERENCE_TOKEN", {})
    monkeypatch.delenv("MAC_INFERENCE_TOKEN", raising=False)


def test_sandbox_env_carries_the_inference_token_not_the_worker_token(monkeypatch):
    _clear_token(monkeypatch)
    monkeypatch.delenv("MAC_OPENSHELL_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:8789")
    for name in ("MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"):
        monkeypatch.setenv(name, "mac_worker_secret")
    monkeypatch.setenv("MAC_AGENT_ID", "agent_alpha")
    minted = []

    def fake_mint(hub_url, worker_token, agent_id, *, task_id="", ttl_seconds=0, timeout=0):
        minted.append((hub_url, worker_token, agent_id, task_id, ttl_seconds))
        return {"id": "inference-1", "token": "mac_inference_task"}

    monkeypatch.setattr("mac.inference_tokens.request_inference_token", fake_mint)
    es._ensure_task_inference_token("task_1")
    es._ensure_task_inference_token("task_1")  # minted once per task
    assert minted == [
        ("http://127.0.0.1:8789", "mac_worker_secret", "agent_alpha", "task_1", 21600)
    ]

    values = es._openshell_environment()
    assert values["MAC_INFERENCE_TOKEN"] == "mac_inference_task"
    assert values["MAC_HUB_URL"] == "http://host.openshell.internal:8789"
    assert not {"MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"} & set(values)
    assert "mac_worker_secret" not in json.dumps(values)

    revoked = []
    monkeypatch.setattr(
        "mac.inference_tokens.revoke_inference_token",
        lambda hub, token, agent, token_id, timeout=0: revoked.append(token_id),
    )
    es.revoke_task_inference_token()
    assert revoked == ["inference-1"]
    assert "MAC_INFERENCE_TOKEN" not in es._openshell_environment()


def test_runtime_files_point_opencode_at_the_generated_config(monkeypatch, tmp_path):
    _clear_token(monkeypatch)
    monkeypatch.delenv("MAC_OPENSHELL_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "mac_worker_secret")
    monkeypatch.setenv("MAC_INFERENCE_TOKEN", "mac_inference_task")
    monkeypatch.setenv("MAC_CODING_MODELS", "gpt-5.6-sol")

    env_file, _toolchain = es._write_sandbox_runtime_files(tmp_path, "/sandbox/ws")

    exports = {}
    for line in env_file.read_text().splitlines():
        name, _, value = line.removeprefix("export ").partition("=")
        exports[name] = shlex.split(value)[0] if value else ""
    assert exports["OPENCODE_CONFIG"] == "/sandbox/ws/.mac-opencode.json"
    assert exports["MAC_INFERENCE_TOKEN"] == "mac_inference_task"
    assert "MAC_WORKER_TOKEN" not in exports
    config = json.loads((tmp_path / ".mac-opencode.json").read_text())
    machub = config["provider"]["machub"]
    assert machub["options"]["baseURL"] == "http://host.openshell.internal:8789/v1"
    assert machub["options"]["apiKey"] == "{env:MAC_INFERENCE_TOKEN}"
    assert config["model"] == "machub/gpt-5.6-sol"
    assert "mac_inference_task" not in (tmp_path / ".mac-opencode.json").read_text()
    # A host control file: never harvested back into the task's results.
    assert es._sandbox_download_path_is_host_control(Path(".mac-opencode.json"))


def test_no_config_without_an_inference_token(monkeypatch, tmp_path):
    _clear_token(monkeypatch)
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:8789")
    assert es._write_opencode_router_config(tmp_path, "/sandbox/x", {"MAC_HUB_URL": "h"}) == {}
    assert not (tmp_path / ".mac-opencode.json").exists()


def test_preflight_mints_a_short_token_and_revokes_it(monkeypatch):
    _clear_token(monkeypatch)
    monkeypatch.delenv("MAC_OPENSHELL_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("MAC_HUB_URL", "http://100.64.0.1:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "mac_worker_secret")
    choice = ca.resolve_coding_agent(
        env={**_HUB_ENV, "MAC_CODING_AGENT": "opencode"},
        home=Path("/nonexistent"),
        which=_which("opencode"),
    )
    minted, revoked, probed = [], [], {}

    def fake_mint(*, task_id, ttl_seconds):
        minted.append(ttl_seconds)
        return {"id": "inference-probe", "token": "mac_inference_probe"}

    def fake_probe(argv, *, timeout):
        sandbox_dir = argv[argv.index("--upload") + 1].split(":")[0]
        probed["env"] = (Path(sandbox_dir) / ".mac-openshell-env.sh").read_text()
        probed["config"] = json.loads((Path(sandbox_dir) / ".mac-opencode.json").read_text())
        return 0, ca.PREFLIGHT_SENTINEL

    monkeypatch.setattr(es, "_mint_inference_token", fake_mint)
    monkeypatch.setattr(es, "_revoke_inference_token", revoked.append)
    monkeypatch.setattr(es, "_openshell_probe", fake_probe)
    monkeypatch.setattr(es, "_sandbox_step", lambda *a, **k: None)
    monkeypatch.setattr(es, "_openshell_bin", lambda: "openshell")
    monkeypatch.setattr(es, "_resolve_openshell_policy", lambda: "/policy.yaml")

    result = es._run_coding_agent_preflight_result(choice)

    assert result["verified"] is True
    assert minted == [es._PREFLIGHT_INFERENCE_TOKEN_TTL_SECONDS]
    assert revoked == ["inference-probe"]
    assert "MAC_INFERENCE_TOKEN=mac_inference_probe" in probed["env"]
    assert "mac_worker_secret" not in probed["env"]
    assert probed["config"]["provider"]["machub"]["options"]["baseURL"] == (
        "http://100.64.0.1:8789/v1"
    )

    def failing_mint(*, task_id, ttl_seconds):
        raise RuntimeError("hub down")

    monkeypatch.setattr(es, "_mint_inference_token", failing_mint)
    failed = es._run_coding_agent_preflight_result(choice)
    assert failed["verified"] is False
    assert failed["failure_class"] == "inference_token_unavailable"


def test_policy_lets_opencode_reach_only_the_router_routes():
    template = (
        Path(__file__).resolve().parents[1] / "deploy" / "openshell" / "mac-hermes-policy.yaml"
    ).read_text(encoding="utf-8")
    doc = yaml.safe_load(
        openshell_policy.render_policy(
            template, agent_user="jkh", hub_host="100.64.0.1", hub_port=8789
        )
    )
    block = doc["network_policies"]["opencode_router"]
    (endpoint,) = block["endpoints"]
    assert (endpoint["host"], endpoint["port"]) == ("100.64.0.1", 8789)
    assert "access" not in endpoint
    assert endpoint["rules"] == [
        {"allow": {"method": "POST", "path": "/v1/chat/completions"}},
        {"allow": {"method": "POST", "path": "/v1/embeddings"}},
    ]
    assert {b["path"] for b in block["binaries"]} == {
        "/usr/local/bin/opencode",
        "/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe",
    }
    # The full hub API stays with the runtime python only.
    hub_binaries = {b["path"] for b in doc["network_policies"]["mac_hub"]["binaries"]}
    assert not hub_binaries & {b["path"] for b in block["binaries"]}


def test_host_probe_env_carries_a_token_and_the_config(monkeypatch, tmp_path):
    _clear_token(monkeypatch)
    monkeypatch.setenv("MAC_HUB_URL", "http://100.64.0.1:8789")
    monkeypatch.setattr(
        es,
        "_mint_inference_token",
        lambda *, task_id, ttl_seconds: {"id": "inference-host", "token": "mac_inference_h"},
    )
    overlay, token_id = es.host_opencode_router_env(tmp_path, task_id="", ttl_seconds=600)
    assert token_id == "inference-host"
    assert overlay["MAC_INFERENCE_TOKEN"] == "mac_inference_h"
    assert overlay["OPENCODE_CONFIG"] == "%s/.mac-opencode.json" % tmp_path
    config = json.loads((tmp_path / ".mac-opencode.json").read_text())
    assert config["provider"]["machub"]["options"]["baseURL"] == "http://100.64.0.1:8789/v1"
