from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac import inference_provider as provider
from mac.models import ValidationError
from mac.provider_router import providers_from_env


IMAGE = "vllm/vllm-openai@sha256:" + "a" * 64


def spec(**changes):
    values = dict(
        provider_id="local-a",
        image=IMAGE,
        model="org/model-a",
        model_revision="0123456789abcdef",
        router_base_url="http://127.0.0.1:18000/v1",
        port=18000,
        gpu_count=1,
        min_free_memory_mib=8000,
    )
    values.update(changes)
    return provider.ProviderSpec.parse(values)


class FakeRunner:
    def __init__(self, inspect=None, memory="24576\n"):
        self.inspect = inspect
        self.memory = memory
        self.commands = []

    def __call__(self, command):
        self.commands.append(list(command))
        if command[:2] == ["docker", "inspect"]:
            if self.inspect is None:
                return provider.CommandResult(1, stderr="not found")
            return provider.CommandResult(0, json.dumps([self.inspect]))
        if command[0] == "nvidia-smi":
            return provider.CommandResult(0, self.memory)
        return provider.CommandResult(0, "ok")


def inspected(item, *, running=True, fingerprint=None):
    return {
        "Config": {"Labels": {"mac.provider.fingerprint": fingerprint or item.fingerprint}},
        "State": {"Running": running},
    }


@pytest.mark.parametrize(
    "change,message",
    [
        ({"image": "vllm/vllm-openai:latest"}, "sha256"),
        ({"model_revision": "main"}, "immutable"),
        ({"router_base_url": "127.0.0.1:8000"}, "http"),
        ({"router_base_url": "http://user:secret@host/v1"}, "credentials"),
        ({"model": "org/model,models=*"}, "exact"),
    ],
)
def test_typed_resource_rejects_unpinned_or_ambiguous_identity(change, message):
    with pytest.raises(ValidationError, match=message):
        spec(**change)


def test_load_specs_requires_schema_and_unique_provider_ids(tmp_path):
    path = tmp_path / "providers.json"
    raw = {"schema": provider.SCHEMA, "providers": [spec().__dict__, spec().__dict__]}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValidationError, match="unique"):
        provider.load_specs(path)


def test_dry_run_is_non_mutating_and_plans_pinned_install(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_HOME", str(tmp_path))
    runner = FakeRunner()
    result = provider.reconcile_one(spec(), runner=runner, dry_run=True)
    assert result == {
        "provider_id": "local-a",
        "status": "planned",
        "actions": ["pull pinned image", "create provider container"],
    }
    assert not (tmp_path / "inference-providers").exists()
    assert all(
        command[:2] not in (["docker", "pull"], ["docker", "run"]) for command in runner.commands
    )


def test_capacity_failure_happens_before_install_mutation():
    runner = FakeRunner(memory="4096\n")
    with pytest.raises(RuntimeError, match="requires 1 GPU"):
        provider.reconcile_one(spec(), runner=runner, dry_run=False)
    assert all(
        command[:2] not in (["docker", "pull"], ["docker", "run"]) for command in runner.commands
    )


def test_exact_running_resource_is_idempotent():
    item = spec()
    runner = FakeRunner(inspected(item))
    result = provider.reconcile_one(
        item, runner=runner, dry_run=False, health=lambda unused: (True, "ready")
    )
    assert result["status"] == "healthy"
    assert result["actions"] == []
    assert runner.commands == [["docker", "inspect", item.container_name]]


def test_transient_health_failure_is_observed_without_destructive_action():
    item = spec()
    runner = FakeRunner(inspected(item))
    result = provider.reconcile_one(
        item, runner=runner, dry_run=False, health=lambda unused: (False, "timeout")
    )
    assert result["status"] == "degraded"
    assert result["health"] == "timeout"
    assert runner.commands == [["docker", "inspect", item.container_name]]


def test_drift_requires_explicit_upgrade_and_leaves_container_alone():
    item = spec()
    runner = FakeRunner(inspected(item, fingerprint="old"))
    result = provider.reconcile_one(item, runner=runner, dry_run=False)
    assert result["status"] == "upgrade_required"
    assert runner.commands == [["docker", "inspect", item.container_name]]


def test_failed_explicit_upgrade_rolls_back_previous_container(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_HOME", str(tmp_path))
    item = spec()
    runner = FakeRunner(inspected(item, fingerprint="old"))
    with pytest.raises(RuntimeError, match="replacement failed health"):
        provider.reconcile_one(
            item,
            runner=runner,
            dry_run=False,
            allow_upgrade=True,
            health=lambda unused: (False, "warming failed"),
            health_attempts=1,
        )
    commands = runner.commands
    assert ["docker", "pull", IMAGE] in commands
    assert ["docker", "stop", item.container_name] in commands
    assert ["docker", "rename", item.container_name + "-rollback", item.container_name] in commands
    assert ["docker", "start", item.container_name] in commands


def test_router_registration_is_exact_multi_provider_and_preserves_unmanaged(tmp_path):
    env = tmp_path / "mac.env"
    env.write_text("MAC_ROUTER_PROVIDERS=cloud=https://cloud.invalid/v1,20,models=*\nOTHER=value\n")
    first = spec()
    second = spec(
        provider_id="local-b",
        model="org/model-b",
        port=18001,
        router_base_url="http://127.0.0.1:18001/v1",
        priority=1,
    )
    rendered = provider.register_router([first, second], dry_run=False, path=env)
    assert rendered.split(";") == [
        "cloud=https://cloud.invalid/v1,20,models=*",
        "local-a=http://127.0.0.1:18000/v1,0,models=org/model-a",
        "local-b=http://127.0.0.1:18001/v1,1,models=org/model-b",
    ]
    assert "OTHER=value" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    parsed = providers_from_env({"MAC_ROUTER_PROVIDERS": rendered})
    assert [(item.name, item.models, item.api_key_env) for item in parsed] == [
        ("cloud", ("*",), ""),
        ("local-a", ("org/model-a",), ""),
        ("local-b", ("org/model-b",), ""),
    ]


def test_removal_unregisters_only_managed_provider(tmp_path):
    env = tmp_path / "mac.env"
    env.write_text(
        "MAC_ROUTER_PROVIDERS="
        "cloud=https://cloud.invalid/v1,20,models=*;"
        "local-a=http://127.0.0.1:18000/v1,0,models=org/model-a\n"
    )
    result = provider.unregister_router([spec()], dry_run=False, path=env)
    assert result == "cloud=https://cloud.invalid/v1,20,models=*"
    assert "local-a=" not in env.read_text()


def test_removal_is_explicit_and_preserves_persistent_cache():
    item = spec()
    runner = FakeRunner(inspected(item))
    dry = provider.remove_provider(item, runner=runner, dry_run=True)
    assert dry["cache_preserved"] is True
    assert runner.commands == [["docker", "inspect", item.container_name]]
    provider.remove_provider(item, runner=runner, dry_run=False)
    assert ["docker", "rm", "-f", item.container_name] in runner.commands
