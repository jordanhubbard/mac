"""Dedicated Linux/KVM verifier. Repository code executes only inside a fresh VM.

The SSH controller supplies one JSON line followed by a length-bound tarball.
Host configuration and the qualified, flattened base image are operator-owned.
There are no host directory mounts, guest credentials for the host, or guest
egress. Dependencies must already be present in the qualified base image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

SCHEMA = "mac.vm_verifier.v1"
MAX_ARCHIVE = 512 * 1024 * 1024
SANITIZER_PREFLIGHT = """set -eu
mkdir -p /tmp/mac-verifier-sanitizer
cd /tmp/mac-verifier-sanitizer
cat > probe.c <<'EOF'
#include <stdlib.h>
__attribute__((noinline)) static void allocate(int release) {
    void *p = malloc(37);
    if (!p) exit(99);
    ((volatile char *)p)[0] = 1;
    if (release) free(p);
}
int main(int argc, char **argv) { (void)argv; allocate(argc > 1); return 0; }
EOF
gcc -O0 -g -fsanitize=address,undefined probe.c -o probe
./probe clean > clean.log 2>&1
if ./probe > leak.log 2>&1; then
    echo 'LeakSanitizer did not reject the leaking control' >&2; exit 96
fi
grep -q 'LeakSanitizer: detected memory leaks' leak.log
echo 'VM sanitizer controls: clean=pass intentional-leak=rejected'
"""


def output_limit() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def private_config(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}:
        raise ValueError("verifier configuration must be an operator-owned regular file")
    if info.st_mode & 0o077:
        raise ValueError("verifier configuration must be private (mode 0600)")
    data = json.loads(path.read_text())
    if data.get("schema") != SCHEMA:
        raise ValueError("unsupported VM verifier configuration")
    return data


def validate_request(request: dict[str, Any], config: dict[str, Any]) -> None:
    if request.get("schema") != SCHEMA:
        raise ValueError("unsupported verification request")
    for key, length in (
        ("head_sha", 40),
        ("tree_sha", 40),
        ("nonce", 32),
        ("archive_sha256", 64),
        ("base_sha256", 64),
    ):
        if not re.fullmatch(r"[0-9a-f]{%d}" % length, str(request.get(key, ""))):
            raise ValueError(f"invalid verification identity: {key}")
    if request["remote_url"] not in config["repositories"]:
        raise ValueError("repository is not authorized for this VM verifier")
    if request["base_sha256"] != config["base_sha256"]:
        raise ValueError("controller and VM host disagree on qualified base identity")
    if not 0 < request["archive_size"] <= MAX_ARCHIVE:
        raise ValueError("repository archive exceeds verification limit")
    if not 1 <= request["timeout_seconds"] <= config.get("max_timeout_seconds", 7200):
        raise ValueError("verification timeout outside configured bound")
    for key in ("test_command", "bootstrap_command"):
        if not isinstance(request.get(key), str) or len(request[key]) > 16384:
            raise ValueError("invalid repository command")
    if not request["test_command"].strip():
        raise ValueError("empty repository test command")


def cloud_seed(directory: Path, client_public_key: str) -> None:
    """Generate per-boot server identity; the client private key stays on host."""
    host_key = directory / "host_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
        capture_output=True,
    )
    data = {
        "disable_root": False,
        "ssh_pwauth": False,
        "users": [
            {
                "name": "root",
                "lock_passwd": True,
                "ssh_authorized_keys": [client_public_key.strip()],
            }
        ],
        "ssh_keys": {
            "ed25519_private": host_key.read_text(),
            "ed25519_public": host_key.with_suffix(".pub").read_text(),
        },
    }
    (directory / "user-data").write_text("#cloud-config\n" + json.dumps(data))
    (directory / "meta-data").write_text(json.dumps({"instance-id": directory.name}))
    subprocess.run(
        [
            "xorriso",
            "-as",
            "mkisofs",
            "-quiet",
            "-V",
            "cidata",
            "-o",
            str(directory / "seed.iso"),
            "-J",
            "-r",
            str(directory / "user-data"),
            str(directory / "meta-data"),
        ],
        check=True,
        capture_output=True,
    )


def qemu_command(directory: Path, port: int, config: dict[str, Any]) -> list[str]:
    memory = int(config.get("memory_mib", 16384))
    cpus = int(config.get("cpus", 8))
    if not 1024 <= memory <= 32768 or not 1 <= cpus <= 12:
        raise ValueError("VM resource allocation outside verifier bounds")
    # QEMU's comma-separated option syntax needs separate escaping from argv.
    disk = str(directory / "overlay.qcow2").replace(",", ",,")
    seed = str(directory / "seed.iso").replace(",", ",,")
    firmware = str(Path(config["firmware_code"])).replace(",", ",,")
    variables = str(directory / "uefi-vars.fd").replace(",", ",,")
    return [
        "qemu-system-x86_64",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-m",
        str(memory),
        "-smp",
        str(cpus),
        "-nodefaults",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "file:" + str(directory / "serial.log"),
        "-drive",
        f"file={disk},format=qcow2,if=virtio",
        "-drive",
        f"file={seed},format=raw,media=cdrom,readonly=on",
        "-netdev",
        f"user,id=net,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-device",
        "virtio-net-pci,netdev=net",
        "-no-reboot",
        "-drive",
        f"if=pflash,format=raw,unit=0,readonly=on,file={firmware}",
        "-drive",
        f"if=pflash,format=raw,unit=1,file={variables}",
    ]


def prepare_firmware(directory: Path, config: dict[str, Any]) -> None:
    for key in ("firmware_code", "firmware_vars"):
        path = Path(config[key])
        if path.is_symlink() or not path.is_file() or digest(path) != config[key + "_sha256"]:
            raise ValueError("qualified VM firmware identity mismatch")
    shutil.copyfile(config["firmware_vars"], directory / "uefi-vars.fd")


def guest_command(request: dict[str, Any]) -> str:
    """No controller environment or sanitizer overrides cross into the guest."""
    source_checks = (
        "cd /work/repo && "
        f'test "$(git rev-parse HEAD)" = {shlex.quote(request["head_sha"])} && '
        f'test "$(git rev-parse HEAD^{{tree}})" = {shlex.quote(request["tree_sha"])} && '
        'test "$(id -u)" != 0 && '
    )
    command = source_checks
    if request["bootstrap_command"]:
        command += "( " + request["bootstrap_command"] + "\n) && "
    command += "( " + request["test_command"] + "\n)"
    return shlex.join(
        [
            "runuser",
            "-u",
            "verifier",
            "--",
            "env",
            "-i",
            "HOME=/home/verifier",
            "USER=verifier",
            "LOGNAME=verifier",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "CARGO_NET_OFFLINE=true",
            "CARGO_BUILD_JOBS=8",
            "LANG=C.UTF-8",
            "/bin/bash",
            "-c",
            command,
        ]
    )


def verify(
    config: dict[str, Any], request: dict[str, Any], archive: Path, directory: Path
) -> dict[str, Any]:
    validate_request(request, config)
    base = Path(config["base_image"])
    if base.is_symlink() or not base.is_file() or digest(base) != config["base_sha256"]:
        raise ValueError("qualified VM base image identity mismatch")
    info = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", str(base)]))
    if info.get("format") != "qcow2" or info.get("backing-filename"):
        raise ValueError("qualified VM base must be a flattened qcow2 image")
    if (
        archive.stat().st_size != request["archive_size"]
        or digest(archive) != request["archive_sha256"]
    ):
        raise ValueError("repository archive identity mismatch")
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            str(directory / "overlay.qcow2"),
        ],
        check=True,
        capture_output=True,
    )
    key = directory / "client_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    cloud_seed(directory, key.with_suffix(".pub").read_text())
    prepare_firmware(directory, config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    known = directory / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} " + (directory / "host_key.pub").read_text())
    ssh = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ConnectTimeout=5",
        "-i",
        str(key),
        "-p",
        str(port),
        "root@127.0.0.1",
    ]
    deadline = time.monotonic() + request["timeout_seconds"]

    def call(
        command: str, *, stdin: Any = subprocess.DEVNULL, cap: int = 300
    ) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("dedicated VM verification budget exhausted")
        return subprocess.run(
            [*ssh, command],
            stdin=stdin,
            capture_output=True,
            timeout=min(cap, remaining),
            check=False,
        )

    with (directory / "qemu.log").open("wb") as qemu_log:
        vm = subprocess.Popen(
            qemu_command(directory, port, config),
            stdin=subprocess.DEVNULL,
            stdout=qemu_log,
            stderr=subprocess.STDOUT,
        )
        try:
            boot_deadline = min(deadline, time.monotonic() + 180)
            while time.monotonic() < boot_deadline:
                if vm.poll() is not None:
                    raise RuntimeError("dedicated verifier VM exited during boot")
                ready = call("test -f /var/lib/cloud/instance/boot-finished && id verifier", cap=8)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise TimeoutError("dedicated verifier VM did not become ready")
            sanitizer = call(
                shlex.join(
                    [
                        "runuser",
                        "-u",
                        "verifier",
                        "--",
                        "env",
                        "-i",
                        "HOME=/home/verifier",
                        "PATH=/usr/local/bin:/usr/bin:/bin",
                        "/bin/bash",
                        "-c",
                        SANITIZER_PREFLIGHT,
                    ]
                )
            )
            if sanitizer.returncode:
                raise RuntimeError(
                    "dedicated VM sanitizer controls failed: "
                    + sanitizer.stderr.decode(errors="replace")[-2000:]
                )
            with archive.open("rb") as source:
                uploaded = call("cat > /var/tmp/repository.tgz", stdin=source)
            if uploaded.returncode:
                raise RuntimeError("dedicated VM source upload failed")
            setup = call(
                "set -eu; "
                f"echo '{request['archive_sha256']}  /var/tmp/repository.tgz' | sha256sum -c -; "
                "mkdir -p /work; tar xzf /var/tmp/repository.tgz -C /work; "
                "chown -R verifier:verifier /work/repo; "
                'test "$(id -u verifier)" != 0; '
                "! id -nG verifier | tr ' ' '\\n' | grep -Ex 'sudo|wheel|disk|docker'"
            )
            if setup.returncode:
                raise RuntimeError("dedicated VM source or unprivileged identity preflight failed")
            # Test stdout cannot author the result: the SSH process exit status
            # supplies the verdict and controller-owned identity supplies provenance.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("dedicated VM budget exhausted before test execution")
            with (directory / "test.log").open("wb") as test_log:
                result = subprocess.run(
                    [*ssh, guest_command(request)],
                    stdin=subprocess.DEVNULL,
                    stdout=test_log,
                    stderr=subprocess.STDOUT,
                    timeout=remaining,
                    preexec_fn=output_limit,
                    check=False,
                )
            with (directory / "test.log").open("rb") as test_log:
                output = test_log.read(32768)
                if (directory / "test.log").stat().st_size > 32768:
                    test_log.seek(max(32768, (directory / "test.log").stat().st_size - 32768))
                    output += b"\n[see retained complete test.log]\n" + test_log.read()
            return {
                "schema": SCHEMA,
                "nonce": request["nonce"],
                "returncode": result.returncode,
                "head_sha": request["head_sha"],
                "tree_sha": request["tree_sha"],
                "base_sha256": config["base_sha256"],
                "archive_sha256": request["archive_sha256"],
                "test_command": request["test_command"],
                "bootstrap_command": request["bootstrap_command"],
                "execution_environment": "dedicated_kvm",
                "execution_attempted": True,
                "platform": "linux",
                "firmware_code_sha256": config["firmware_code_sha256"],
                "firmware_vars_sha256": config["firmware_vars_sha256"],
                "sanitizer_controls": "clean-pass,leak-rejected",
                "output": output.decode(errors="replace"),
            }
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:
                    vm.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vm.kill()
                    vm.wait(timeout=10)


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = private_config(args.config)
    request_line = sys.stdin.buffer.readline(65537)
    if len(request_line) > 65536 or not request_line.endswith(b"\n"):
        raise ValueError("invalid VM verification request header")
    request = json.loads(request_line)
    validate_request(request, config)
    workspace = Path(config["workspace"])
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A dedicated VM host accepts one review at a time. No fleet-wide resource
    # or service changes are needed; concurrent callers get an explicit failure.
    with (workspace / "verifier.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix="review-", dir=workspace) as temporary:
            directory = Path(temporary)
            archive = directory / "repository.tgz"
            remaining = request["archive_size"]
            with archive.open("wb") as output:
                while remaining:
                    block = sys.stdin.buffer.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("truncated VM verification archive")
                    output.write(block)
                    remaining -= len(block)
            if sys.stdin.buffer.read(1):
                raise ValueError("trailing VM verification request bytes")
            receipts = workspace / "receipts" / request["nonce"]
            receipts.mkdir(mode=0o700, parents=True, exist_ok=False)
            try:
                result = verify(config, request, archive, directory)
                result["evidence_directory"] = str(receipts)
                (receipts / "result.json").write_text(json.dumps(result, indent=2))
                print(json.dumps(result))
            finally:
                (receipts / "request.json").write_text(json.dumps(request, indent=2))
                for name in ("test.log", "qemu.log", "serial.log"):
                    if (directory / name).is_file():
                        shutil.copyfile(directory / name, receipts / name)


if __name__ == "__main__":
    main()
