"""Dedicated VM routing and identity failures must never fall back to host execution."""

import json
from pathlib import Path
import shlex
import subprocess

import pytest

from mac import vm_verifier as controller
from mac import vm_verifier_host as host
from mac import services


@pytest.fixture
def config(tmp_path):
    return {
        "schema": host.SCHEMA,
        "repositories": ["https://example.test/repository.git"],
        "base_sha256": "a" * 64,
        "base_image": str(tmp_path / "base.qcow2"),
        "firmware_code": str(tmp_path / "code.fd"),
        "firmware_code_sha256": "c" * 64,
        "firmware_vars": str(tmp_path / "vars.fd"),
        "firmware_vars_sha256": "d" * 64,
        "ssh_target": "operator@vm-host.example.test",
        "ssh_identity": str(tmp_path / "identity"),
        "ssh_known_hosts": str(tmp_path / "known_hosts"),
        "remote_runner": "/opt/mac/vm_verifier_host.py",
        "remote_config": "/etc/mac/vm-verifier.json",
        "controller_config_sha256": "e" * 64,
    }


@pytest.fixture
def request_data(config):
    return {
        "schema": host.SCHEMA,
        "remote_url": config["repositories"][0],
        "nonce": "1" * 32,
        "head_sha": "2" * 40,
        "tree_sha": "3" * 40,
        "base_sha256": config["base_sha256"],
        "archive_sha256": "b" * 64,
        "archive_size": 4,
        "timeout_seconds": 1200,
        "bootstrap_command": "true",
        "test_command": "make test-host",
    }


def test_configuration_routes_only_explicit_repositories(tmp_path, monkeypatch, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setenv("MAC_HUB_VERIFY_VM_CONFIG", str(path))
    assert controller.configured_vm_verifier("https://different.test/repo.git") is None
    selected = controller.configured_vm_verifier(config["repositories"][0])
    assert selected["base_sha256"] == config["base_sha256"]
    assert selected["controller_config_sha256"] == host.digest(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        controller.configured_vm_verifier(config["repositories"][0])


def test_symlink_configuration_is_rejected(tmp_path, config):
    path = tmp_path / "real.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular file"):
        host.private_config(link)


@pytest.mark.parametrize(
    "key,value",
    [
        ("head_sha", "not-a-commit"),
        ("tree_sha", "f" * 39),
        ("nonce", "../../outside"),
        ("archive_size", host.MAX_ARCHIVE + 1),
        ("archive_size", 0),
        ("timeout_seconds", 7201),
        ("timeout_seconds", 0),
        ("test_command", ""),
        ("base_sha256", "0" * 64),
        ("remote_url", "https://unapproved.test/repo.git"),
    ],
)
def test_host_rejects_unbound_or_unbounded_requests(config, request_data, key, value):
    request_data[key] = value
    with pytest.raises(ValueError):
        host.validate_request(request_data, config)


def test_guest_command_keeps_shell_code_in_unprivileged_guest(request_data):
    request_data["test_command"] = "printf '%s' 'a; $(not-a-host-command)' && make test-host"
    argv = shlex.split(host.guest_command(request_data))
    assert argv[:6] == ["runuser", "-u", "verifier", "--", "env", "-i"]
    assert argv[-3:-1] == ["/bin/bash", "-c"]
    assert request_data["test_command"] in argv[-1]
    assert request_data["head_sha"] in argv[-1]
    assert request_data["tree_sha"] in argv[-1]
    assert not any(
        value.startswith(("ASAN_OPTIONS=", "LSAN_OPTIONS=", "UBSAN_OPTIONS=")) for value in argv
    )


def test_qemu_has_bounded_resources_and_only_loopback_management(tmp_path, config):
    argv = host.qemu_command(tmp_path, 12345, config)
    assert "user,id=net,restrict=on,hostfwd=tcp:127.0.0.1:12345-:22" in argv
    assert argv[argv.index("-m") + 1] == "16384"
    assert argv[argv.index("-smp") + 1] == "8"
    assert not set(argv) & {"-virtfs", "-fsdev", "-chardev", "-enable-kvm"}
    config["cpus"] = 100
    with pytest.raises(ValueError, match="resource"):
        host.qemu_command(tmp_path, 12345, config)


def invoke_controller(config, tmp_path, monkeypatch, *, corrupt=None, rc=0):
    archive = tmp_path / "repository.tgz"
    archive.write_bytes(b"data")

    def transport(argv, **kwargs):
        assert argv[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in argv
        assert "ForwardAgent=no" in argv
        remote = shlex.split(argv[-1])
        assert remote == [
            "/usr/bin/python3",
            config["remote_runner"],
            "--config",
            config["remote_config"],
        ]
        payload = kwargs["stdin"]
        request = json.loads(payload.readline())
        assert payload.read() == b"data"
        assert request["archive_sha256"] == host.digest(archive)
        response = dict(
            request,
            returncode=rc,
            output="complete suite output",
            execution_environment="dedicated_kvm",
            execution_attempted=True,
            sanitizer_controls="clean-pass,leak-rejected",
            firmware_code_sha256=config["firmware_code_sha256"],
            firmware_vars_sha256=config["firmware_vars_sha256"],
        )
        if corrupt:
            response[corrupt] = "wrong"
        return subprocess.CompletedProcess(argv, 0, json.dumps(response).encode(), b"")

    monkeypatch.setattr(controller.subprocess, "run", transport)
    identity = {}
    result = controller.run_staged_vm_verification(
        config,
        archive,
        remote_url=config["repositories"][0],
        head_sha="2" * 40,
        tree_sha="3" * 40,
        test_command="make test-host",
        bootstrap_command="true",
        timeout_seconds=1200,
        verifier_identity=identity,
    )
    return result, identity


def test_vm_result_preserves_real_failure(config, tmp_path, monkeypatch):
    result, identity = invoke_controller(config, tmp_path, monkeypatch, rc=7)
    assert result == (7, "complete suite output")
    assert identity["execution_environment"] == "dedicated_kvm"
    assert identity["execution_attempted"] is True


@pytest.mark.parametrize(
    "field",
    [
        "nonce",
        "head_sha",
        "tree_sha",
        "base_sha256",
        "archive_sha256",
        "test_command",
        "bootstrap_command",
        "sanitizer_controls",
        "execution_environment",
        "execution_attempted",
        "firmware_code_sha256",
        "firmware_vars_sha256",
        "returncode",
    ],
)
def test_vm_success_with_wrong_identity_is_not_a_pass(config, tmp_path, monkeypatch, field):
    result, identity = invoke_controller(config, tmp_path, monkeypatch, corrupt=field)
    assert result[0] != 0
    assert "unavailable" in result[1]
    assert identity == {}


def test_setup_failure_stops_only_the_created_vm(tmp_path, config, request_data, monkeypatch):
    base = Path(config["base_image"])
    base.write_bytes(b"base")
    config["base_sha256"] = request_data["base_sha256"] = host.digest(base)
    archive = tmp_path / "repository.tgz"
    archive.write_bytes(b"data")
    request_data["archive_sha256"] = host.digest(archive)

    class VM:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            assert self.stopped

    vm = VM()

    def run(argv, **kwargs):
        if argv[0] == "ssh-keygen":
            Path(argv[-1] + ".pub").write_text("ssh-ed25519 public-key")
        failed = argv[0] == "ssh" and "chown -R" in argv[-1]
        return subprocess.CompletedProcess(argv, 1 if failed else 0, b"", b"")

    monkeypatch.setattr(host.subprocess, "run", run)
    monkeypatch.setattr(host.subprocess, "check_output", lambda *a, **kw: b'{"format":"qcow2"}')
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **kw: vm)
    monkeypatch.setattr(host, "prepare_firmware", lambda *a: None)
    monkeypatch.setattr(
        host, "cloud_seed", lambda directory, key: (directory / "host_key.pub").write_text(key)
    )
    with pytest.raises(RuntimeError, match="preflight"):
        host.verify(config, request_data, archive, tmp_path)
    assert vm.stopped


@pytest.mark.parametrize("wrong_tree", [False, True])
def test_service_stages_exact_source_without_openshell(tmp_path, monkeypatch, config, wrong_tree):
    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Verifier test")
    git("config", "user.email", "verifier@example.invalid")
    (repo / "source.txt").write_text("exact source\n")
    git("add", "source.txt")
    git("commit", "-qm", "fixture")
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    monkeypatch.setattr(controller, "configured_vm_verifier", lambda remote: config)
    monkeypatch.delenv("MAC_HUB_VERIFY_IMAGE", raising=False)
    calls = []

    def verify(selected, archive, **kwargs):
        import tarfile

        with tarfile.open(archive) as staged:
            assert staged.extractfile("repo/source.txt").read() == b"exact source\n"
            assert "repo/.git/HEAD" in staged.getnames()
        calls.append(kwargs)
        return 7, "unchanged suite failed"

    monkeypatch.setattr(controller, "run_staged_vm_verification", verify)
    result = services.run_repository_contract_test_in_openshell(
        config["repositories"][0],
        "main",
        head,
        "make test-host",
        local_repository=repo,
        expected_tree_sha="f" * 40 if wrong_tree else tree,
        timeout_seconds=60,
    )
    if wrong_tree:
        assert not calls
        assert "source tree mismatch" in result[1]
    else:
        assert result == (7, "unchanged suite failed")
        assert calls[0]["head_sha"] == head
        assert calls[0]["tree_sha"] == tree
        assert calls[0]["test_command"] == "make test-host"
