"""One sandbox-image pin per worker (task_b1828d67).

``~/.mac/openshell/runtime-image-ref`` is the pin. ``MAC_OPENSHELL_CREATE_ARGS
--from`` is derived from it, ``MAC_HUB_VERIFY_IMAGE`` is dropped on a worker,
the test gate reads the pin, and pins that disagree are refused.
"""

from __future__ import annotations

import json

import pytest

from mac import executor_sandbox, openshell_image_pin

OLD = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64
NEW = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "b" * 64
INPUT = "sha256:" + "c" * 64
REVISION = "d" * 40


def _home(tmp_path, env: str, ref: str = NEW):
    home = tmp_path / ".mac"
    (home / "openshell").mkdir(parents=True)
    if ref:
        (home / "openshell" / "runtime-image-ref").write_text(ref + "\n", encoding="utf-8")
    (home / "mac.env").write_text(env, encoding="utf-8")
    (home / "mac.env").chmod(0o600)
    return home


def test_sync_derives_from_and_drops_the_gate_pin(tmp_path):
    home = _home(
        tmp_path,
        "MAC_OPENSHELL_SANDBOX=1\n"
        'MAC_OPENSHELL_CREATE_ARGS="--from %s --gpu"\n'
        "MAC_HUB_VERIFY_IMAGE=%s\n"
        "export MAC_HUB_VERIFY_IMAGE=%s\n"
        "OTHER=1\n" % (OLD, OLD, OLD),
    )

    result = openshell_image_pin.sync(home)

    assert result["changed"] is True
    assert result["removed_hub_verify_image"] == 2
    text = (home / "mac.env").read_text(encoding="utf-8")
    assert text == (
        'MAC_OPENSHELL_SANDBOX=1\nMAC_OPENSHELL_CREATE_ARGS="--from %s --gpu"\nOTHER=1\n' % NEW
    )
    assert (home / "mac.env").stat().st_mode & 0o777 == 0o600
    backup = home / result["backup"].rsplit("/", 1)[1]
    assert "MAC_HUB_VERIFY_IMAGE" in backup.read_text(encoding="utf-8")
    # Idempotent: a second sync changes nothing and writes no backup.
    assert openshell_image_pin.sync(home) == {"status": "ok", "pin": NEW, "changed": False}


def test_sync_adds_a_missing_from_and_reads_unquoted_values(tmp_path):
    home = _home(tmp_path, "MAC_OPENSHELL_CREATE_ARGS=--gpu\n")
    openshell_image_pin.sync(home)
    assert (home / "mac.env").read_text(encoding="utf-8") == (
        'MAC_OPENSHELL_CREATE_ARGS="--from %s --gpu"\n' % NEW
    )


@pytest.mark.parametrize("ref", ["", "localhost/mac-hermes:net"])
def test_a_host_without_a_managed_pin_is_left_alone(tmp_path, ref):
    env = "MAC_HUB_VERIFY_IMAGE=%s\nMAC_OPENSHELL_CREATE_ARGS='--from localhost/x'\n" % OLD
    home = _home(tmp_path, env, ref=ref)
    result = openshell_image_pin.sync(home)
    assert result["changed"] is False
    assert (home / "mac.env").read_text(encoding="utf-8") == env


class _Docker:
    def __init__(self, *, revision=REVISION, input_sha=INPUT, pull_rc=0):
        self.calls = []
        self.revision, self.input_sha, self.pull_rc = revision, input_sha, pull_rc

    def __call__(self, cmd, env=None):
        self.calls.append(list(cmd))
        if cmd[1] == "pull":
            assert env and env["DOCKER_CONFIG"]
            return self.pull_rc, "pulled"
        if cmd[1:3] == ["image", "inspect"]:
            if "org.opencontainers.image.revision" in cmd[4]:
                return 0, self.revision
            return 0, self.input_sha
        return 0, ""


def test_repin_verifies_the_image_then_pins_it_everywhere(tmp_path):
    home = _home(
        tmp_path,
        'MAC_OPENSHELL_CREATE_ARGS="--from %s"\nMAC_HUB_VERIFY_IMAGE=%s\n' % (OLD, OLD),
        ref=OLD,
    )
    docker = _Docker()

    result = openshell_image_pin.repin(home, NEW, INPUT, runner=docker)

    assert result["status"] == "ok" and result["changed"] is True
    assert result["previous_pin"] == OLD
    osh = home / "openshell"
    assert (osh / "runtime-image-ref").read_text(encoding="utf-8").strip() == NEW
    assert (osh / "runtime-input-sha256").read_text(encoding="utf-8").strip() == INPUT
    assert (osh / "runtime-image-build-revision").read_text(encoding="utf-8").strip() == REVISION
    assert [p.name for p in osh.glob("runtime-image-ref.bak-*")]
    assert (home / "mac.env").read_text(encoding="utf-8") == (
        'MAC_OPENSHELL_CREATE_ARGS="--from %s"\n' % NEW
    )
    assert ["docker", "tag", NEW, "localhost/mac-hermes:net"] in docker.calls


@pytest.mark.parametrize(
    "docker, error",
    [
        (_Docker(pull_rc=1), "pull failed"),
        (_Docker(revision="nope"), "build revision"),
        (_Docker(input_sha="sha256:" + "e" * 64), "frozen-input identity"),
    ],
)
def test_a_repin_that_fails_verification_changes_nothing(tmp_path, docker, error):
    env = 'MAC_OPENSHELL_CREATE_ARGS="--from %s"\n' % OLD
    home = _home(tmp_path, env, ref=OLD)

    result = openshell_image_pin.repin(home, NEW, INPUT, runner=docker)

    assert result["status"] == "error" and error in result["error"]
    assert (home / "openshell" / "runtime-image-ref").read_text(encoding="utf-8").strip() == OLD
    assert (home / "mac.env").read_text(encoding="utf-8") == env


def test_main_prints_one_json_line(tmp_path, capsys):
    home = _home(tmp_path, "OTHER=1\n")
    assert openshell_image_pin.main(["--mac-home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["pin"] == NEW
    assert openshell_image_pin.main(["--mac-home", str(home), "--image", "bad"]) == 1


def _pins(monkeypatch, tmp_path, *, create_from="", ref="", verify=""):
    ref_file = tmp_path / "runtime-image-ref"
    if ref:
        ref_file.write_text(ref + "\n", encoding="utf-8")
    monkeypatch.setenv("MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE", str(ref_file))
    if create_from:
        monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from %s" % create_from)
    else:
        monkeypatch.delenv("MAC_OPENSHELL_CREATE_ARGS", raising=False)
    if verify:
        monkeypatch.setenv("MAC_HUB_VERIFY_IMAGE", verify)
    else:
        monkeypatch.delenv("MAC_HUB_VERIFY_IMAGE", raising=False)


def test_the_gate_runs_in_the_coding_image_not_a_stale_gate_pin(monkeypatch, tmp_path):
    # The 2026-10-10 state of both workers: coding on NEW, gate pinned to OLD.
    _pins(monkeypatch, tmp_path, create_from=NEW, ref=NEW, verify=OLD)
    assert executor_sandbox.verifier_runtime_image() == (NEW, "managed_runtime_pin")


def test_disagreeing_pins_are_refused_not_guessed(monkeypatch, tmp_path):
    _pins(monkeypatch, tmp_path, create_from=OLD, ref=NEW, verify=OLD)
    with pytest.raises(executor_sandbox.RuntimeImagePinConflict, match="pins disagree"):
        executor_sandbox.verifier_runtime_image()


def test_a_host_without_a_managed_pin_uses_its_gate_image(monkeypatch, tmp_path):
    # The hub: a mutable local --from and no runtime-image-ref.
    _pins(monkeypatch, tmp_path, create_from="localhost/mac-hermes:net", verify=OLD)
    assert executor_sandbox.verifier_runtime_image() == (OLD, "MAC_HUB_VERIFY_IMAGE")
