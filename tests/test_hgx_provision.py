from __future__ import annotations

import shlex

import pytest

from mac.hgx_provision import (
    CODEGRAPH_VERSION,
    PLACEHOLDER_HEALTH_STATUS,
    PLACEHOLDER_STATUS,
    PLAN_SCHEMA,
    PYTHON_VERSION,
    RESOURCE_SCHEMA,
    SESSION_SCHEMA,
    UV_VERSION,
    HgxSession,
    OnboardingPlan,
    VolumeLayout,
    plan_fungible_onboarding,
)
from mac.models import AgentInstanceKind, ValidationError


def _session(**overrides):
    kwargs = dict(session_id="hgx-sess-1", ssh_user="sandbox", ssh_host="10.0.0.5")
    kwargs.update(overrides)
    return HgxSession(**kwargs)


def test_session_defaults_and_derived_properties() -> None:
    session = _session()
    assert session.ssh_port == 22
    assert session.home == "/home"
    assert session.account_home == "/home/sandbox"
    assert session.ssh_destination == "sandbox@10.0.0.5"


def test_session_strips_and_normalizes_home() -> None:
    session = _session(home="/srv/nodes/", ssh_port=2200)
    assert session.home == "/srv/nodes"
    assert session.account_home == "/srv/nodes/sandbox"
    assert session.ssh_port == 2200


def test_session_root_home_normalization() -> None:
    session = _session(home="/")
    assert session.home == "/"
    assert session.account_home == "/sandbox"


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("session_id", "", "session_id must not be empty"),
        ("session_id", 5, "session_id must be a string"),
        ("session_id", "bad id!", "session_id has an unsupported shape"),
        ("ssh_user", "1root", "ssh_user has an unsupported shape"),
        ("ssh_host", "-bad-", "ssh_host has an unsupported shape"),
        ("home", "relative/path", "home must be an absolute path"),
    ],
)
def test_session_rejects_bad_fields(field, value, message) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _session(**{field: value})
    assert message in str(excinfo.value)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_session_rejects_out_of_range_port(port) -> None:
    with pytest.raises(ValidationError, match="within 1..65535"):
        _session(ssh_port=port)


@pytest.mark.parametrize("port", [True, 2.0, "22"])
def test_session_rejects_non_integer_port(port) -> None:
    with pytest.raises(ValidationError, match="ssh_port must be an integer"):
        _session(ssh_port=port)


def test_from_mapping_accepts_schema_and_string_port() -> None:
    session = HgxSession.from_mapping(
        {
            "schema": SESSION_SCHEMA,
            "session_id": "hgx-2",
            "ssh_user": "worker",
            "ssh_host": "node.example.com",
            "ssh_port": "2022",
            "home": "/home",
        }
    )
    assert session.ssh_port == 2022
    assert session.ssh_destination == "worker@node.example.com"


def test_from_mapping_defaults_port_and_home() -> None:
    session = HgxSession.from_mapping(
        {"session_id": "hgx-3", "ssh_user": "worker", "ssh_host": "1.2.3.4"}
    )
    assert session.ssh_port == 22
    assert session.account_home == "/home/worker"


def test_from_mapping_rejects_non_mapping() -> None:
    with pytest.raises(ValidationError, match="must be a mapping"):
        HgxSession.from_mapping(["not", "a", "mapping"])


def test_from_mapping_rejects_unexpected_schema() -> None:
    with pytest.raises(ValidationError, match="unexpected hgx session schema"):
        HgxSession.from_mapping({"schema": "other.v1", "session_id": "x"})


def test_from_mapping_rejects_non_numeric_string_port() -> None:
    with pytest.raises(ValidationError, match="ssh_port must be an integer"):
        HgxSession.from_mapping(
            {"session_id": "hgx", "ssh_user": "w", "ssh_host": "h", "ssh_port": "abc"}
        )


def test_volume_layout_matches_onboarding_helper() -> None:
    layout = VolumeLayout.for_account_home("/home/worker")
    assert layout.mac_home == "/home/worker/.mac"
    assert layout.source == "/home/worker/.mac/src/mac"
    assert layout.venv == "/home/worker/.mac/venv"
    assert layout.mac_bin == "/home/worker/.local/bin/mac"
    assert layout.codegraph_bin == "/home/worker/.mac/bin/codegraph"
    assert layout.gh_bin == "/home/worker/.mac/bin/gh"
    assert layout.receipt == "/home/worker/.mac/machine-onboarding-receipt.json"
    assert layout.lock == "/home/worker/.mac/.machine-onboarding.lock"
    assert set(layout.as_dict()) == {
        "home",
        "mac_home",
        "source",
        "venv",
        "local_bin",
        "mac_bin",
        "codegraph_bin",
        "gh_bin",
        "receipt",
        "lock",
    }


def test_volume_layout_root_home() -> None:
    layout = VolumeLayout.for_account_home("/")
    assert layout.mac_home == "/.mac"
    assert layout.local_bin == "/.local/bin"


def test_volume_layout_rejects_relative_home() -> None:
    with pytest.raises(ValidationError, match="must be an absolute path"):
        VolumeLayout.for_account_home("relative")


def test_volume_layout_rejects_empty_home() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        VolumeLayout.for_account_home("   ")


def test_plan_from_session_object_full_shape() -> None:
    plan = plan_fungible_onboarding(
        agent="agent_headless",
        session=_session(ssh_port=2222),
        hub_agent="agent_hub",
        capabilities="python,ops",
    )
    assert isinstance(plan, OnboardingPlan)
    doc = plan.as_dict()
    assert doc["schema"] == PLAN_SCHEMA
    assert doc["instance_kind"] == AgentInstanceKind.FUNGIBLE.value
    assert doc["agent"] == "agent_headless"
    assert doc["hub_agent"] == "agent_hub"
    assert doc["fleet_name"] == "mac"
    assert doc["capabilities"] == ["python", "ops"]
    assert doc["session"] == {
        "schema": SESSION_SCHEMA,
        "session_id": "hgx-sess-1",
        "ssh_destination": "sandbox@10.0.0.5",
        "ssh_port": 2222,
    }
    assert doc["toolchain"] == {
        "uv": UV_VERSION,
        "python": PYTHON_VERSION,
        "codegraph": CODEGRAPH_VERSION,
    }
    assert doc["placeholder"] == {
        "schema": RESOURCE_SCHEMA,
        "status": "prepared",
        "instance_kind": AgentInstanceKind.FUNGIBLE.value,
        "barrier": {
            "status": PLACEHOLDER_STATUS,
            "health_status": PLACEHOLDER_HEALTH_STATUS,
        },
    }
    assert doc["services_started"] is False
    assert doc["volume"]["mac_home"] == "/home/sandbox/.mac"


def test_plan_deploy_command_is_argv_safe() -> None:
    plan = plan_fungible_onboarding(
        agent="agent_headless",
        session=_session(),
        hub_agent="agent_hub",
    )
    assert plan.deploy_command() == [
        "deploy/deploy-mac-fleet.sh",
        "--hub",
        "agent_hub",
        "--prepare-fungible-onboarding",
        "agent_headless",
    ]
    rendered = plan.deploy_command_str()
    assert rendered == plan.as_dict()["deploy_command_str"]
    assert shlex.split(rendered) == plan.deploy_command()


def test_plan_accepts_mapping_session() -> None:
    plan = plan_fungible_onboarding(
        agent="agent_headless",
        session={
            "session_id": "hgx-map",
            "ssh_user": "worker",
            "ssh_host": "1.2.3.4",
        },
        hub_agent="agent_hub",
    )
    assert plan.session.session_id == "hgx-map"
    assert plan.layout.mac_home == "/home/worker/.mac"


def test_plan_string_instance_kind_and_default_capabilities() -> None:
    plan = plan_fungible_onboarding(
        agent="agent_headless",
        session=_session(),
        hub_agent="agent_hub",
        instance_kind="fungible",
    )
    assert plan.capabilities == ()
    assert plan.as_dict()["capabilities"] == []


def test_plan_capability_list_dedup_and_trim() -> None:
    plan = plan_fungible_onboarding(
        agent="agent_headless",
        session=_session(),
        hub_agent="agent_hub",
        capabilities=[" python ", "ops", "python", ""],
    )
    assert plan.capabilities == ("python", "ops")


def test_plan_capabilities_none() -> None:
    plan = plan_fungible_onboarding(
        agent="agent_headless",
        session=_session(),
        hub_agent="agent_hub",
        capabilities=None,
    )
    assert plan.capabilities == ()


def test_plan_rejects_non_string_capability_item() -> None:
    with pytest.raises(ValidationError, match="each capability must be a string"):
        plan_fungible_onboarding(
            agent="agent_headless",
            session=_session(),
            hub_agent="agent_hub",
            capabilities=["python", 7],
        )


def test_plan_rejects_bad_capability_type() -> None:
    with pytest.raises(ValidationError, match="capabilities must be a string"):
        plan_fungible_onboarding(
            agent="agent_headless",
            session=_session(),
            hub_agent="agent_hub",
            capabilities={"python"},
        )


def test_plan_refuses_static_instance_kind() -> None:
    with pytest.raises(ValidationError, match="refuses a non-fungible"):
        plan_fungible_onboarding(
            agent="agent_headless",
            session=_session(),
            hub_agent="agent_hub",
            instance_kind=AgentInstanceKind.STATIC,
        )


def test_plan_rejects_unknown_instance_kind() -> None:
    with pytest.raises(ValueError):
        plan_fungible_onboarding(
            agent="agent_headless",
            session=_session(),
            hub_agent="agent_hub",
            instance_kind="ephemeral",
        )


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("agent", "", "agent must not be empty"),
        ("agent", "bad agent!", "agent has an unsupported shape"),
        ("hub_agent", "  ", "hub_agent must not be empty"),
        ("hub_agent", "bad hub!", "hub_agent has an unsupported shape"),
        ("fleet_name", "", "fleet_name must not be empty"),
    ],
)
def test_plan_rejects_bad_identifiers(field, value, message) -> None:
    kwargs = dict(
        agent="agent_headless",
        session=_session(),
        hub_agent="agent_hub",
        fleet_name="mac",
    )
    kwargs[field] = value
    with pytest.raises(ValidationError) as excinfo:
        plan_fungible_onboarding(**kwargs)
    assert message in str(excinfo.value)
