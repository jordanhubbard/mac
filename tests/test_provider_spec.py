"""Contract tests for the data-driven provider wrapper (``mac.provider_spec``).

Three groups, in the order the ADR argues them:

1. **Validation** -- a spec that cannot be proved to be within bounds does not
   load. This is the review story for third-party spec files, so it is tested
   as behaviour rather than trusted as documentation.
2. **Shipped templates** -- ``aws``/``azure``/``gcp``/``nvidia`` all parse, all
   name themselves after their file, and the ``nvidia`` profile builds the same
   argv the hard-wired :mod:`mac.hgx_provider` builds today.
3. **End to end, no mac source change** -- a *user-authored* spec for a
   fictional non-NVIDIA cloud drives create/list/status/exec/attest/delete
   against a real executable through real ``subprocess`` calls. Nothing in this
   group imports the provider it is exercising; the only mac code involved is
   the interpreter.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mac.provider_spec import (
    CRUD_VERBS,
    PROVIDER_SPEC_SCHEMA,
    SHIPPED_SPEC_DIR,
    SPEC_PATH_ENV_VAR,
    ProviderAmbiguousNameError,
    ProviderCapabilityError,
    ProviderCommandError,
    ProviderInstanceNotFoundError,
    ProviderSpec,
    ProviderSpecValidationError,
    SpecProvider,
    discover_specs,
    load_spec,
    spec_search_path,
)


def _minimal(**overrides):
    payload = {
        "schema": PROVIDER_SPEC_SCHEMA,
        "name": "demo",
        "kind": "external",
        "binary": "demotool",
        "parameters": {
            "instance_id": {"required": True},
            "flavor": {"required": True, "default": "small"},
        },
        "fields": {"id": ["id"], "name": ["name"], "state": ["state"]},
        "verbs": {
            "create": {"args": ["create", "--type", "{flavor}"]},
            "status": {"args": ["show", "{instance_id}"]},
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# 1. Validation: fail closed.
# --------------------------------------------------------------------------
def test_minimal_spec_round_trips():
    spec = ProviderSpec.from_mapping(_minimal())
    assert spec.name == "demo"
    assert spec.kind == "external"
    assert spec.has_verb("create")
    assert not spec.has_verb("delete")
    assert spec.observable()["schema"] == PROVIDER_SPEC_SCHEMA


def test_wrong_schema_is_refused():
    with pytest.raises(ProviderSpecValidationError, match="schema must be"):
        ProviderSpec.from_mapping(_minimal(schema="mac.provider_spec.v99"))


@pytest.mark.parametrize(
    "binary",
    ["/usr/bin/aws", "../../bin/sh", "aws;rm -rf /", "aws tool", "", "a" * 200],
)
def test_binary_must_be_a_bare_bounded_command_name(binary):
    """A spec cannot choose a path. Which binary a name resolves to is the
    operator's PATH decision, not a downloaded file's."""
    with pytest.raises(ProviderSpecValidationError, match="bare command name"):
        ProviderSpec.from_mapping(_minimal(binary=binary))


def test_undeclared_placeholder_is_refused():
    payload = _minimal(verbs={"create": {"args": ["create", "--name", "{whatever}"]}})
    with pytest.raises(ProviderSpecValidationError, match="undeclared parameter"):
        ProviderSpec.from_mapping(payload)


def test_unknown_verb_is_refused():
    payload = _minimal(verbs={"exfiltrate": {"args": ["go"]}})
    with pytest.raises(ProviderSpecValidationError, match="is not one of"):
        ProviderSpec.from_mapping(payload)


def test_credential_shaped_parameter_is_refused():
    """Secrets travel in the child environment, by name. A spec that wants one
    in argv is refused outright rather than redacted after the fact."""
    payload = _minimal(
        parameters={"api_key": {"required": True}, "instance_id": {}, "flavor": {}},
        verbs={"create": {"args": ["create", "--key", "{api_key}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="looks like a credential"):
        ProviderSpec.from_mapping(payload)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("timeout_seconds", 0, "timeout_seconds must be within"),
        ("timeout_seconds", 99999, "timeout_seconds must be within"),
        ("env_passthrough", ["lower_case"], "not a valid environment variable name"),
        ("credential_env_var", "not-an-env-var", "not a valid environment variable name"),
        ("kind", "sideways", "kind must be"),
        ("name", "Not Valid", "name must be"),
    ],
)
def test_bounded_scalars_are_enforced(field, value, match):
    with pytest.raises(ProviderSpecValidationError, match=match):
        ProviderSpec.from_mapping(_minimal(**{field: value}))


def test_verbs_are_capped():
    payload = _minimal(verbs={})
    with pytest.raises(ProviderSpecValidationError, match="at least one verb"):
        ProviderSpec.from_mapping(payload)


def test_argv_token_count_is_capped():
    payload = _minimal(verbs={"create": {"args": ["arg"] * 65}})
    with pytest.raises(ProviderSpecValidationError, match="over the 64 ceiling"):
        ProviderSpec.from_mapping(payload)


def test_field_map_keys_are_closed():
    with pytest.raises(ProviderSpecValidationError, match="is not one of"):
        ProviderSpec.from_mapping(_minimal(fields={"arbitrary": ["x"]}))


def test_oversized_spec_file_is_refused(tmp_path):
    path = tmp_path / "big.json"
    path.write_text(json.dumps({"pad": "x" * (300 * 1024)}), encoding="utf-8")
    with pytest.raises(ProviderSpecValidationError, match="over the .* byte ceiling"):
        ProviderSpec.from_file(path)


def test_unparseable_spec_file_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProviderSpecValidationError, match="not readable JSON"):
        ProviderSpec.from_file(path)


# --------------------------------------------------------------------------
# argv construction bounds.
# --------------------------------------------------------------------------
def _provider(**overrides) -> SpecProvider:
    return SpecProvider(ProviderSpec.from_mapping(_minimal(**overrides)))


def test_argv_is_a_list_and_shell_metacharacters_stay_inert():
    """The value is passed through as one argv item. There is no shell to
    re-split it, so ';' and '$(...)' are ordinary characters in a filename-ish
    string -- and the pattern still governs which are accepted at all."""
    provider = _provider(
        parameters={
            "instance_id": {"required": True, "pattern": "^.{1,64}$"},
            "flavor": {"default": "small"},
        }
    )
    argv = provider.build_argv("status", {"instance_id": "a;b$(id)"})
    assert argv == ["demotool", "show", "a;b$(id)"]


def test_value_starting_with_dash_is_refused_by_default():
    """Shell-free execve's live injection risk is a value the target tool
    re-reads as a FLAG."""
    provider = _provider(
        parameters={"instance_id": {"required": True, "pattern": "^.{1,64}$"}, "flavor": {}}
    )
    with pytest.raises(ProviderSpecValidationError, match="starts with '-'"):
        provider.build_argv("status", {"instance_id": "--output-file=/etc/passwd"})


def test_leading_dash_is_allowed_when_the_spec_says_so():
    provider = _provider(
        parameters={
            "instance_id": {
                "required": True,
                "pattern": "^-{0,2}[A-Za-z0-9=/._-]{1,64}$",
                "allow_leading_dash": True,
            },
            "flavor": {},
        }
    )
    assert provider.build_argv("status", {"instance_id": "--verbose"})[-1] == "--verbose"


def test_value_must_match_its_declared_pattern():
    provider = _provider()
    with pytest.raises(ProviderSpecValidationError, match="does not match its declared pattern"):
        provider.build_argv("status", {"instance_id": "has spaces"})


def test_nul_byte_is_refused():
    provider = _provider(
        parameters={"instance_id": {"required": True, "pattern": "^.{1,64}$"}, "flavor": {}}
    )
    with pytest.raises(ProviderSpecValidationError, match="NUL byte"):
        provider.build_argv("status", {"instance_id": "a\x00b"})


def test_required_is_per_verb_not_per_spec():
    """``flavor`` is required to create and meaningless when showing."""
    provider = _provider()
    assert provider.build_argv("status", {"instance_id": "i-1"}) == ["demotool", "show", "i-1"]
    with pytest.raises(ProviderSpecValidationError, match="requires parameter 'instance_id'"):
        provider.build_argv("status", {})


def test_missing_verb_is_a_capability_error():
    provider = _provider()
    with pytest.raises(ProviderCapabilityError, match="does not describe verb 'delete'"):
        provider.build_argv("delete", {"instance_id": "i-1"})


def test_splat_expands_a_list_into_separate_argv_items():
    provider = _provider(
        parameters={
            "instance_id": {"required": True},
            "flavor": {},
            "command": {"splat": True, "pattern": "^[^\\x00]{1,64}$", "default": []},
        },
        verbs={"exec": {"args": ["ssh", "{instance_id}", "--", "{command...}"]}},
    )
    argv = provider.build_argv("exec", {"instance_id": "i-1", "command": ["printf", "a b"]})
    assert argv == ["demotool", "ssh", "i-1", "--", "printf", "a b"]


def test_splat_must_be_the_whole_token():
    payload = _minimal(
        parameters={"instance_id": {}, "flavor": {}, "command": {"splat": True}},
        verbs={"exec": {"args": ["ssh", "x{command...}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="must be the whole argv token"):
        ProviderSpec.from_mapping(payload)


def test_splat_items_are_capped():
    provider = _provider(
        parameters={
            "instance_id": {"required": True},
            "flavor": {},
            "command": {"splat": True, "pattern": "^[^\\x00]{1,64}$", "default": []},
        },
        verbs={"exec": {"args": ["ssh", "{instance_id}", "--", "{command...}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="over the 64 ceiling"):
        provider.build_argv("exec", {"instance_id": "i-1", "command": ["x"] * 65})


def test_child_environment_is_only_what_the_spec_declared(monkeypatch):
    monkeypatch.setenv("DEMO_REGION", "eu-west-1")
    monkeypatch.setenv("UNRELATED_FLEET_TOKEN", "super-secret")
    provider = _provider(env_passthrough=["DEMO_REGION"])
    env = provider._child_env()
    assert env["DEMO_REGION"] == "eu-west-1"
    assert "UNRELATED_FLEET_TOKEN" not in env
    assert "PATH" in env


def test_missing_binary_is_a_command_error_naming_argv():
    provider = _provider(binary="definitely-not-installed-xyz")
    with pytest.raises(ProviderCommandError) as excinfo:
        provider.run("status", {"instance_id": "i-1"})
    assert excinfo.value.argv[0] == "definitely-not-installed-xyz"


# --------------------------------------------------------------------------
# 2. Shipped templates.
# --------------------------------------------------------------------------
SHIPPED = ("aws", "azure", "gcp", "nvidia")


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_template_parses_and_matches_its_filename(name):
    spec = ProviderSpec.from_file(SHIPPED_SPEC_DIR / ("%s.json" % name))
    assert spec.name == name
    assert spec.kind == "external"
    assert spec.description, "a shipped template must say what it is and what to edit"
    assert set(spec.verbs) <= set(CRUD_VERBS)
    # Every template must be able to create, enumerate, inspect and destroy.
    assert {"create", "list", "status", "delete"} <= set(spec.verbs)


def test_shipped_templates_are_discoverable_together(monkeypatch):
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(SHIPPED_SPEC_DIR))
    monkeypatch.setenv("MAC_HOME", "/nonexistent-mac-home-for-tests")
    assert set(discover_specs(strict=True)) >= set(SHIPPED)


def test_nvidia_template_builds_the_same_argv_as_the_hard_wired_adapter(monkeypatch):
    """The migration claim, tested rather than asserted: the shipped nvidia
    profile issues byte-identical argv to what :mod:`mac.hgx_provider` issues,
    so the hgx consumers can move onto the interpreter without a behaviour
    change."""
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(SHIPPED_SPEC_DIR))
    provider = SpecProvider(load_spec("nvidia"))
    assert provider.build_argv("create", {"flavor": "standard-dind", "instance_name": "w1"}) == [
        "hgx",
        "--json",
        "create",
        "--type",
        "standard-dind",
        "--name",
        "w1",
    ]
    assert provider.build_argv("list") == ["hgx", "--json", "list"]
    assert provider.build_argv("status", {"instance_id": "s-1"}) == [
        "hgx",
        "--json",
        "status",
        "s-1",
    ]
    assert provider.build_argv("delete", {"instance_id": "s-1"}) == ["hgx", "delete", "s-1"]
    assert provider.build_argv("exec", {"instance_id": "s-1", "command": ["printf", "x"]}) == [
        "hgx",
        "ssh",
        "s-1",
        "--",
        "printf",
        "x",
    ]


def test_nvidia_template_has_no_verb_that_could_invoke_hgx_info(monkeypatch):
    """``hgx info`` can echo a fallback bootstrap password. The hard-wired
    adapter bans the verb; the spec simply has no way to reach it."""
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(SHIPPED_SPEC_DIR))
    spec = load_spec("nvidia")
    for verb in spec.verbs.values():
        assert "info" not in verb.args


def test_templates_without_an_exec_verb_refuse_attestation(monkeypatch):
    """A provider that cannot be reached cannot be attested, and says so,
    instead of treating a zero exit as readiness."""
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(SHIPPED_SPEC_DIR))
    provider = SpecProvider(load_spec("aws"))
    with pytest.raises(ProviderCapabilityError, match="no 'exec' verb"):
        provider.attest("i-0123456789abcdef")


# --------------------------------------------------------------------------
# Discovery and precedence.
# --------------------------------------------------------------------------
def test_search_path_is_env_then_user_home_then_shipped(monkeypatch, tmp_path):
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(tmp_path / "a") + os.pathsep + str(tmp_path / "b"))
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    assert spec_search_path() == [
        tmp_path / "a",
        tmp_path / "b",
        tmp_path / "home" / "provider-specs",
        SHIPPED_SPEC_DIR,
    ]


def test_a_user_spec_shadows_a_shipped_template_of_the_same_name(monkeypatch, tmp_path):
    user_dir = tmp_path / "specs"
    user_dir.mkdir()
    override = _minimal(name="nvidia", binary="hgx-wrapper")
    (user_dir / "nvidia.json").write_text(json.dumps(override), encoding="utf-8")
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(user_dir))
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    assert load_spec("nvidia").binary == "hgx-wrapper"


def test_spec_name_must_match_its_filename(monkeypatch, tmp_path):
    """Otherwise a file could shadow a name the operator cannot see in `ls`."""
    user_dir = tmp_path / "specs"
    user_dir.mkdir()
    (user_dir / "harmless.json").write_text(json.dumps(_minimal(name="nvidia")), encoding="utf-8")
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(user_dir))
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    with pytest.raises(ProviderSpecValidationError, match="must match its filename stem"):
        discover_specs(strict=True)
    assert "nvidia" not in {
        name: spec for name, spec in discover_specs().items() if spec.binary == "demotool"
    }


def test_one_bad_spec_does_not_hide_the_good_ones(monkeypatch, tmp_path):
    user_dir = tmp_path / "specs"
    user_dir.mkdir()
    (user_dir / "good.json").write_text(json.dumps(_minimal(name="good")), encoding="utf-8")
    (user_dir / "broken.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(user_dir))
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    found = discover_specs()
    assert "good" in found
    with pytest.raises(ProviderSpecValidationError):
        discover_specs(strict=True)


def test_unknown_provider_name_names_the_search_path(monkeypatch, tmp_path):
    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(tmp_path))
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    with pytest.raises(ProviderSpecValidationError, match="no provider spec named 'nope'"):
        load_spec("nope")


# --------------------------------------------------------------------------
# 3. End to end against a real executable, from a user-authored spec.
# --------------------------------------------------------------------------
# A fictional non-NVIDIA cloud VM service. It is a real program: it is executed
# by real subprocess calls, keeps real state on disk between verbs, and runs the
# argv it is handed after `--` for `exec`. Nothing about it is known to mac.
_FAKE_CLI = '''#!/usr/bin/env python3
"""democloud - a tiny stand-in for a third-party cloud VM CLI."""
import json
import os
import subprocess
import sys

STATE = os.path.join(os.environ["DEMOCLOUD_STATE_DIR"], "vms.json")


def load():
    try:
        with open(STATE) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return []


def save(rows):
    with open(STATE, "w") as handle:
        json.dump(rows, handle)


def main(argv):
    if not argv:
        return 2
    verb, rest = argv[0], argv[1:]
    rows = load()
    if verb == "vm-create":
        opts = dict(zip(rest[::2], rest[1::2]))
        row = {
            "uuid": "vm-%04d" % (len(rows) + 1),
            "label": opts.get("--label", ""),
            "size": opts.get("--size", ""),
            "phase": "running",
            "ipv4": "10.0.0.%d" % (len(rows) + 1),
            "login": "democloud",
            "root_password": "hunter2",
        }
        rows.append(row)
        save(rows)
        print(json.dumps({"vm": row}))
        return 0
    if verb == "vm-list":
        print(json.dumps({"vms": rows}))
        return 0
    if verb == "vm-show":
        match = [r for r in rows if r["uuid"] == rest[0]]
        if not match:
            print("no such vm", file=sys.stderr)
            return 1
        print(json.dumps({"vm": match[0]}))
        return 0
    if verb == "vm-destroy":
        save([r for r in rows if r["uuid"] != rest[0]])
        return 0
    if verb == "vm-run":
        target, command = rest[0], rest[rest.index("--") + 1:]
        if not any(r["uuid"] == target for r in rows):
            print("no such vm", file=sys.stderr)
            return 1
        return subprocess.call(command)
    print("unknown verb %s" % verb, file=sys.stderr)
    return 2


sys.exit(main(sys.argv[1:]))
'''

_USER_SPEC = {
    "schema": PROVIDER_SPEC_SCHEMA,
    "name": "democloud",
    "kind": "external",
    "description": "A user-authored external provider for a fictional cloud VM service.",
    "binary": "democloud",
    "timeout_seconds": 60,
    "env_passthrough": ["DEMOCLOUD_STATE_DIR"],
    "parameters": {
        "instance_id": {"required": True, "pattern": "^vm-[0-9]{4}$"},
        "instance_name": {"pattern": "^[a-z0-9-]{1,32}$", "default": "mac-worker"},
        "flavor": {"required": True, "pattern": "^[a-z0-9-]{1,32}$", "default": "medium"},
        "command": {"splat": True, "pattern": "^[^\\x00\\n\\r]{1,128}$", "default": []},
    },
    "fields": {
        "id": ["uuid"],
        "name": ["label"],
        "flavor": ["size"],
        "state": ["phase"],
        "host": ["ipv4"],
        "user": ["login"],
    },
    "verbs": {
        "create": {
            "args": ["vm-create", "--label", "{instance_name}", "--size", "{flavor}"],
            "parse": {"format": "json", "select": "vm"},
        },
        "list": {"args": ["vm-list"], "parse": {"format": "json", "select": "vms"}},
        "status": {
            "args": ["vm-show", "{instance_id}"],
            "parse": {"format": "json", "select": "vm"},
        },
        "delete": {"args": ["vm-destroy", "{instance_id}"], "parse": {"format": "none"}},
        "exec": {
            "args": ["vm-run", "{instance_id}", "--", "{command...}"],
            "parse": {"format": "none"},
        },
    },
}


@pytest.fixture()
def democloud(monkeypatch, tmp_path) -> SpecProvider:
    """A provider driven entirely by a user-authored JSON file on disk."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "democloud"
    cli.write_text(_FAKE_CLI, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    spec_dir = tmp_path / "provider-specs"
    spec_dir.mkdir()
    (spec_dir / "democloud.json").write_text(json.dumps(_USER_SPEC, indent=2), encoding="utf-8")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    monkeypatch.setenv(SPEC_PATH_ENV_VAR, str(spec_dir))
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DEMOCLOUD_STATE_DIR", str(state_dir))
    return SpecProvider(load_spec("democloud"))


def test_user_authored_spec_is_discovered_without_any_mac_source_change(democloud):
    assert democloud.spec.name == "democloud"
    assert democloud.spec.source_path is not None
    assert democloud.spec.source_path.name == "democloud.json"


def test_end_to_end_create_list_status_attest_delete(democloud):
    created = democloud.create(instance_name="mac-worker-1", flavor="medium")
    assert created.instance_id == "vm-0001"
    assert created.provider == "democloud"
    assert created.name == "mac-worker-1"
    assert created.flavor == "medium"
    assert created.state == "running"
    assert created.endpoint is not None
    assert created.endpoint.user_host == "democloud@10.0.0.1"

    listed = democloud.list()
    assert [item.instance_id for item in listed] == ["vm-0001"]

    fetched = democloud.status("vm-0001")
    assert fetched.instance_id == "vm-0001"
    assert fetched.state == "running"

    # Real remote execution, proved by a nonce the caller did not tell the CLI.
    assert democloud.attest("vm-0001") == "vm-0001"

    assert democloud.delete("vm-0001") == "vm-0001"
    assert democloud.list() == []


def test_end_to_end_output_never_carries_the_providers_credential(democloud):
    """The fake CLI emits ``root_password`` on every record, exactly like real
    provider CLIs do. It must be recorded as scrubbed and never carried."""
    created = democloud.create(instance_name="mac-worker-1", flavor="medium")
    assert created.scrubbed_fields == ["root_password"]
    assert created.credential_present is True
    observable = created.observable()
    assert "hunter2" not in json.dumps(observable)
    assert observable["scrubbed_fields"] == ["root_password"]


def test_end_to_end_exec_runs_the_argv_it_was_given(democloud):
    democloud.create(instance_name="mac-worker-1", flavor="medium")
    assert democloud.exec("vm-0001", ["printf", "hello"]).strip() == "hello"


def test_end_to_end_attestation_fails_closed_when_the_nonce_is_not_echoed(
    democloud, monkeypatch
):
    """A zero exit is not readiness. Swap exec for a command that succeeds
    silently and attestation must still refuse."""
    democloud.create(instance_name="mac-worker-1", flavor="medium")
    monkeypatch.setattr(SpecProvider, "exec", lambda self, instance_id, command, **kw: "")
    with pytest.raises(Exception, match="nonce was not returned"):
        democloud.attest("vm-0001")


def test_end_to_end_nonzero_exit_becomes_a_command_error_without_leaking_stderr(democloud):
    with pytest.raises(ProviderCommandError) as excinfo:
        democloud.status("vm-9999")
    error = excinfo.value
    assert error.returncode == 1
    assert error.argv[:2] == ["democloud", "vm-show"]
    # stderr is available to the operator's terminal but is not part of any
    # structure mac persists.
    assert "no such vm" in error.stderr


def test_end_to_end_ambiguous_name_refuses_to_guess(democloud):
    democloud.create(instance_name="twin", flavor="medium")
    democloud.create(instance_name="twin", flavor="medium")
    with pytest.raises(ProviderAmbiguousNameError) as excinfo:
        democloud.resolve_instance_id("twin")
    assert excinfo.value.instance_ids == ["vm-0001", "vm-0002"]
    assert democloud.resolve_instance_id("vm-0002") == "vm-0002"


def test_end_to_end_unique_name_resolves_to_its_immutable_id(democloud):
    democloud.create(instance_name="solo", flavor="medium")
    assert democloud.resolve_instance_id("solo") == "vm-0001"
    with pytest.raises(ProviderInstanceNotFoundError):
        democloud.resolve_instance_id("absent")


def test_end_to_end_instance_id_must_match_the_declared_shape(democloud):
    with pytest.raises(ProviderSpecValidationError, match="does not match its declared pattern"):
        democloud.status("not-a-vm-id")


def test_end_to_end_argv_is_inspectable_before_it_runs(democloud):
    """The review story: an operator can see exactly what a third-party spec
    would execute without executing it."""
    assert democloud.build_argv("create", {"instance_name": "w1", "flavor": "medium"}) == [
        "democloud",
        "vm-create",
        "--label",
        "w1",
        "--size",
        "medium",
    ]


def test_shipped_spec_dir_is_packaged():
    assert SHIPPED_SPEC_DIR.is_dir()
    assert sorted(p.stem for p in SHIPPED_SPEC_DIR.glob("*.json")) == sorted(SHIPPED)
    assert Path(SHIPPED_SPEC_DIR).parent.name == "data"


# --------------------------------------------------------------------------
# Remaining validation branches and parse paths.
#
# These use a canned-stdout provider rather than a real binary: the point under
# test is the interpreter's own decisions, not the subprocess plumbing that the
# end-to-end group above already exercises for real.
# --------------------------------------------------------------------------
def _canned(stdout: str, **overrides) -> SpecProvider:
    provider = _provider(**overrides)
    provider.run = lambda verb, values=None: stdout  # type: ignore[assignment]
    return provider


def test_non_object_spec_is_refused():
    with pytest.raises(ProviderSpecValidationError, match="must be a JSON object"):
        ProviderSpec.from_mapping(["not", "an", "object"])  # type: ignore[arg-type]


def test_missing_spec_file_is_refused(tmp_path):
    with pytest.raises(ProviderSpecValidationError, match="unreadable"):
        ProviderSpec.from_file(tmp_path / "absent.json")


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"timeout_seconds": "soon"}, "timeout_seconds must be a number"),
        ({"env_passthrough": "AWS_REGION"}, "env_passthrough must be a list"),
        ({"env_passthrough": ["V%d" % i for i in range(65)]}, "over the 64 ceiling"),
        ({"parameters": ["nope"]}, "parameters must be an object"),
        ({"fields": ["nope"]}, "fields must be an object"),
        ({"verbs": ["nope"]}, "verbs must be an object"),
    ],
)
def test_container_types_are_enforced(payload, match):
    with pytest.raises(ProviderSpecValidationError, match=match):
        ProviderSpec.from_mapping(_minimal(**payload))


def test_parameter_count_is_capped():
    params = {"p%d" % i: {} for i in range(65)}
    with pytest.raises(ProviderSpecValidationError, match="over the 64 ceiling"):
        ProviderSpec.from_mapping(_minimal(parameters=params, verbs={"list": {"args": ["ls"]}}))


def test_parameter_name_shape_is_enforced():
    with pytest.raises(ProviderSpecValidationError, match="must match"):
        ProviderSpec.from_mapping(_minimal(parameters={"NotLower": {}}))


def test_parameter_body_must_be_an_object():
    with pytest.raises(ProviderSpecValidationError, match="must be an object"):
        ProviderSpec.from_mapping(_minimal(parameters={"flavor": "small"}))


def test_parameter_pattern_must_compile():
    with pytest.raises(ProviderSpecValidationError, match="invalid pattern"):
        ProviderSpec.from_mapping(_minimal(parameters={"flavor": {"pattern": "([unclosed"}}))


def test_field_map_accepts_a_bare_string_source_key():
    spec = ProviderSpec.from_mapping(_minimal(fields={"id": "uuid"}))
    assert spec.fields["id"] == ("uuid",)


def test_field_map_rejects_a_non_string_source():
    with pytest.raises(ProviderSpecValidationError, match="must be a source key"):
        ProviderSpec.from_mapping(_minimal(fields={"id": {"deep": "no"}}))


def test_field_map_rejects_an_empty_source_list():
    with pytest.raises(ProviderSpecValidationError, match="names no source key"):
        ProviderSpec.from_mapping(_minimal(fields={"id": ["", "  "]}))


def test_verb_count_is_capped():
    verbs = {verb: {"args": ["x"]} for verb in CRUD_VERBS}
    spec = ProviderSpec.from_mapping(
        _minimal(verbs=verbs, parameters={"flavor": {}, "instance_id": {}})
    )
    assert len(spec.verbs) == len(CRUD_VERBS)


@pytest.mark.parametrize(
    "verbs,match",
    [
        ({"create": "go"}, "must be an object"),
        ({"create": {"args": []}}, "non-empty args list"),
        ({"create": {"args": [""]}}, "must be non-empty strings"),
        ({"create": {"args": ["x" * 600]}}, "over 512 characters"),
        ({"create": {"args": ["x"], "parse": "json"}}, "parse must be an object"),
        ({"create": {"args": ["x"], "parse": {"format": "yaml"}}}, "parse.format must be"),
        ({"create": {"args": ["x"], "parse": {"select": 7}}}, "parse.select must be"),
    ],
)
def test_verb_bodies_are_validated(verbs, match):
    with pytest.raises(ProviderSpecValidationError, match=match):
        ProviderSpec.from_mapping(_minimal(verbs=verbs))


def test_select_accepts_a_list_form():
    spec = ProviderSpec.from_mapping(
        _minimal(verbs={"list": {"args": ["ls"], "parse": {"select": ["data", "rows"]}}})
    )
    assert spec.verbs["list"].select == ("data", "rows")


def test_splat_flag_must_agree_between_declaration_and_use():
    payload = _minimal(
        parameters={"instance_id": {}, "flavor": {}, "command": {"splat": True}},
        verbs={"exec": {"args": ["ssh", "{command}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="splat flag"):
        ProviderSpec.from_mapping(payload)


def test_load_spec_requires_a_name():
    with pytest.raises(ProviderSpecValidationError, match="a provider name is required"):
        load_spec("   ")


def test_splat_default_of_none_expands_to_nothing():
    provider = _provider(
        parameters={"instance_id": {"required": True}, "flavor": {}, "command": {"splat": True}},
        verbs={"exec": {"args": ["ssh", "{instance_id}", "{command...}"]}},
    )
    assert provider.build_argv("exec", {"instance_id": "i-1"}) == ["demotool", "ssh", "i-1"]


def test_splat_requires_a_sequence():
    provider = _provider(
        parameters={"instance_id": {"required": True}, "flavor": {}, "command": {"splat": True}},
        verbs={"exec": {"args": ["ssh", "{instance_id}", "{command...}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="needs a sequence of strings"):
        provider.build_argv("exec", {"instance_id": "i-1", "command": "printf x"})


def test_optional_parameter_without_a_value_or_default_is_an_error():
    provider = _provider(
        parameters={"instance_id": {"required": True}, "flavor": {}},
        verbs={"create": {"args": ["create", "--type", "{flavor}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="no value and no default"):
        provider.build_argv("create", {})


def test_boolean_values_are_refused():
    provider = _provider()
    with pytest.raises(ProviderSpecValidationError, match="must be a string or number"):
        provider.build_argv("status", {"instance_id": True})


def test_oversized_value_is_refused():
    provider = _provider(
        parameters={"instance_id": {"required": True, "pattern": "^.*$"}, "flavor": {}}
    )
    with pytest.raises(ProviderSpecValidationError, match="over the 1024 ceiling"):
        provider.build_argv("status", {"instance_id": "x" * 1100})


def test_numeric_values_are_stringified():
    provider = _provider(
        parameters={"instance_id": {"required": True, "pattern": "^[0-9]+$"}, "flavor": {}}
    )
    assert provider.build_argv("status", {"instance_id": 42})[-1] == "42"


def test_credential_env_var_reaches_the_child_environment(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "t0ken")
    provider = _provider(credential_env_var="DEMO_TOKEN")
    assert provider._child_env()["DEMO_TOKEN"] == "t0ken"


def test_execution_oserror_becomes_a_command_error(monkeypatch, tmp_path):
    """A binary that exists but cannot be executed is a command error, not a
    traceback out of subprocess."""
    not_executable = tmp_path / "demotool"
    not_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    not_executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    def boom(*args, **kwargs):
        raise OSError("exec format error")

    provider = _provider()
    monkeypatch.setattr("mac.provider_spec.subprocess.run", boom)
    with pytest.raises(ProviderCommandError, match="could not be executed"):
        provider.run("status", {"instance_id": "i-1"})


def test_execution_timeout_becomes_a_command_error(monkeypatch, tmp_path):
    import subprocess as sp

    slow = tmp_path / "demotool"
    slow.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    def timeout(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="demotool", timeout=1)

    provider = _provider()
    monkeypatch.setattr("mac.provider_spec.subprocess.run", timeout)
    with pytest.raises(ProviderCommandError, match="timed out"):
        provider.run("status", {"instance_id": "i-1"})


@pytest.mark.parametrize("stdout", ["", "not json at all", "null", "42"])
def test_unparseable_or_scalar_output_yields_no_rows(stdout):
    provider = _canned(stdout)
    assert provider._rows("status", stdout) == []


def test_parse_format_none_never_parses():
    provider = _canned(
        '{"id": "i-1"}',
        verbs={"delete": {"args": ["rm", "{instance_id}"], "parse": {"format": "none"}}},
    )
    assert provider._rows("delete", '{"id": "i-1"}') == []


def test_select_indexes_into_a_list():
    provider = _canned(
        "x",
        verbs={"status": {"args": ["show", "{instance_id}"], "parse": {"select": "rows.0"}}},
    )
    rows = provider._rows("status", '{"rows": [{"id": "i-7"}]}')
    assert rows == [{"id": "i-7"}]


def test_select_that_misses_yields_no_rows():
    provider = _canned(
        "x",
        verbs={"status": {"args": ["show", "{instance_id}"], "parse": {"select": "absent"}}},
    )
    assert provider._rows("status", '{"rows": []}') == []
    assert provider._rows("status", '["a", "b"]') == []


def test_output_without_a_mapped_id_is_refused():
    provider = _canned('{"other": "x"}')
    with pytest.raises(ProviderSpecValidationError, match="has no immutable id"):
        provider._instance("status", {"other": "x"})


def test_integer_fields_are_read_and_a_port_is_composed():
    provider = _canned(
        "x",
        fields={
            "id": ["id"],
            "user": ["user"],
            "host": ["host"],
            "port": ["port"],
        },
    )
    instance = provider._instance("status", {"id": 7, "user": "ops", "host": "h1", "port": 2201})
    assert instance.instance_id == "7"
    assert instance.endpoint is not None
    assert instance.endpoint.user_host == "ops@h1"
    assert instance.endpoint.port == 2201
    assert instance.observable()["endpoint"] == {"user_host": "ops@h1", "port": 2201}


def test_an_unusable_endpoint_is_dropped_rather_than_guessed():
    provider = _canned("x", fields={"id": ["id"], "endpoint": ["ssh"]})
    assert provider._instance("status", {"id": "i-1", "ssh": "   "}).endpoint is None


def test_create_without_a_parseable_instance_is_an_error():
    provider = _canned("null")
    with pytest.raises(Exception, match="returned no parseable instance"):
        provider.create(flavor="small")


def test_status_for_a_missing_instance_is_not_found():
    provider = _canned("null")
    with pytest.raises(ProviderInstanceNotFoundError, match="has no instance"):
        provider.status("i-1")


def test_status_returning_a_different_id_is_not_found():
    provider = _canned('{"id": "i-2"}')
    with pytest.raises(ProviderInstanceNotFoundError, match="returned id"):
        provider.status("i-1")


@pytest.mark.parametrize("verb", ["update", "stop", "start"])
def test_side_effect_verbs_return_the_id_they_acted_on(verb):
    provider = _canned(
        "",
        verbs={verb: {"args": [verb, "{instance_id}"], "parse": {"format": "none"}}},
    )
    assert getattr(provider, verb)("i-1") == "i-1"


def test_exec_requires_an_argv_sequence():
    provider = _canned(
        "",
        parameters={"instance_id": {"required": True}, "flavor": {}, "command": {"splat": True}},
        verbs={"exec": {"args": ["ssh", "{instance_id}", "{command...}"]}},
    )
    with pytest.raises(ProviderSpecValidationError, match="non-empty argv sequence"):
        provider.exec("i-1", "printf x")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_selectors_are_refused(blank):
    provider = _canned("[]")
    with pytest.raises(ProviderInstanceNotFoundError, match="immutable instance id is required"):
        provider.status(blank)
    with pytest.raises(ProviderInstanceNotFoundError, match="instance name is required"):
        provider.resolve_instance_id(blank)


def test_spec_observable_is_secret_free_and_complete():
    spec = ProviderSpec.from_mapping(
        _minimal(env_passthrough=["DEMO_REGION"], credential_env_var="DEMO_TOKEN")
    )
    observed = spec.observable()
    assert observed["env_passthrough"] == ["DEMO_REGION"]
    assert observed["credential_env_var"] == "DEMO_TOKEN"
    assert observed["verbs"] == ["create", "status"]
    assert observed["source_path"] is None
