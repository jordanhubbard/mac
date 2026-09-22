#!/usr/bin/env python3
"""Build a candidate verifier base on an operator-controlled Linux/KVM host.

Only this trusted provisioning boot has egress. It installs pinned Rust and
distribution packages, and fetches Cargo dependencies without building repository
code. Review VMs use the separate, network-restricted runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mac.vm_verifier_host import cloud_seed, digest, prepare_firmware, qemu_command  # noqa: E402

RUST_URL = "https://static.rust-lang.org/dist/rust-1.95.0-x86_64-unknown-linux-gnu.tar.xz"
RUST_SHA256 = "2e0338f18ecbaa4a0f631b9e80e8b8e26bb6fe77dd5454fba8a70cf96c1e84a1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha512", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--sdk-archive", type=Path, required=True)
    parser.add_argument("--sdk-sha256", required=True)
    parser.add_argument(
        "--firmware-code", type=Path, default=Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
    )
    parser.add_argument(
        "--firmware-vars", type=Path, default=Path("/usr/share/OVMF/OVMF_VARS_4M.fd")
    )
    args = parser.parse_args()
    os.umask(0o077)
    if not re.fullmatch(r"[0-9a-f]{64}", args.sdk_sha256):
        parser.error("SDK SHA256 must be 64 lowercase hexadecimal characters")
    if args.output.exists():
        parser.error("refusing to replace an existing base image")
    if digest(args.sdk_archive) != args.sdk_sha256:
        parser.error("SDK archive SHA256 mismatch")
    checksum = hashlib.sha512()
    with args.source.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    if checksum.hexdigest() != args.source_sha512:
        parser.error("cloud image SHA512 mismatch")
    directory = args.output.with_suffix(".build")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    subprocess.run(
        ["qemu-img", "convert", "-O", "qcow2", str(args.source), str(directory / "overlay.qcow2")],
        check=True,
    )
    subprocess.run(["qemu-img", "resize", str(directory / "overlay.qcow2"), "64G"], check=True)
    key = directory / "client_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    cloud_seed(directory, key.with_suffix(".pub").read_text())
    firmware = {
        "firmware_code": str(args.firmware_code),
        "firmware_vars": str(args.firmware_vars),
        "firmware_code_sha256": digest(args.firmware_code),
        "firmware_vars_sha256": digest(args.firmware_vars),
    }
    prepare_firmware(directory, firmware)
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
    # This mode exists only in the operator provisioning tool, never in the
    # request-driven review backend. No repository test is run in this boot.
    argv = [part.replace("restrict=on,", "") for part in qemu_command(directory, port, firmware)]
    with (directory / "qemu.log").open("wb") as log:
        vm = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            print("Waiting for trusted provisioning VM", flush=True)
            for _ in range(120):
                if vm.poll() is not None:
                    raise RuntimeError(f"provisioning VM exited during boot: {vm.returncode}")
                ready = subprocess.run(
                    [*ssh, "test -f /var/lib/cloud/instance/boot-finished"],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("provisioning VM readiness timeout")
            with args.sdk_archive.open("rb") as sdk:
                subprocess.run(
                    [*ssh, "cat > /var/tmp/verifier-sdk.tar.gz"], stdin=sdk, timeout=120, check=True
                )
            install = f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends build-essential git curl ca-certificates \
    clang llvm lld pkg-config libssl-dev qemu-utils device-tree-compiler \
    libarchive-tools e2fsprogs dosfstools mtools zstd openssh-client python3
useradd --create-home --shell /bin/bash verifier
passwd -l verifier
echo '{args.sdk_sha256}  /var/tmp/verifier-sdk.tar.gz' | sha256sum -c -
mkdir -p /home/verifier/.cache/agentos
tar xzf /var/tmp/verifier-sdk.tar.gz -C /home/verifier/.cache/agentos
chown -R verifier:verifier /home/verifier/.cache
rm -f /var/tmp/verifier-sdk.tar.gz
curl --fail --location --retry 3 {shlex.quote(RUST_URL)} -o /tmp/rust.tar.xz
echo '{RUST_SHA256}  /tmp/rust.tar.xz' | sha256sum -c -
tar xf /tmp/rust.tar.xz -C /tmp
/tmp/rust-1.95.0-x86_64-unknown-linux-gnu/install.sh --prefix=/usr/local --disable-ldconfig \
    --components=rustc,rust-std-x86_64-unknown-linux-gnu,cargo,rustfmt-preview
rm -rf /tmp/rust-1.95.0-x86_64-unknown-linux-gnu /tmp/rust.tar.xz
runuser -u verifier -- git -c core.hooksPath=/dev/null clone --no-checkout \
    -- {shlex.quote(args.repository)} /home/verifier/dependency-source
runuser -u verifier -- git -C /home/verifier/dependency-source -c core.hooksPath=/dev/null \
    checkout --detach {shlex.quote(args.revision)}
if [ ! -f /home/verifier/dependency-source/Cargo.lock ]; then
    runuser -u verifier -- env HOME=/home/verifier PATH=/usr/local/bin:/usr/bin:/bin \
        cargo generate-lockfile --manifest-path /home/verifier/dependency-source/Cargo.toml
fi
runuser -u verifier -- env HOME=/home/verifier PATH=/usr/local/bin:/usr/bin:/bin \
    cargo fetch --locked --manifest-path /home/verifier/dependency-source/Cargo.toml
cp /home/verifier/dependency-source/Cargo.lock /var/lib/mac-verifier-Cargo.lock
dpkg-query -W > /var/lib/mac-verifier-packages.txt
rustc --version > /var/lib/mac-verifier-rust.txt
cargo --version >> /var/lib/mac-verifier-rust.txt
rm -rf /home/verifier/dependency-source
apt-get clean
sync
"""
            print("Installing toolchain and priming locked dependencies", flush=True)
            with (directory / "provision.log").open("wb") as provision_log:
                completed = subprocess.run(
                    [*ssh, "/bin/bash -se"],
                    input=install.encode(),
                    stdout=provision_log,
                    stderr=subprocess.STDOUT,
                    timeout=2400,
                    check=False,
                )
            if completed.returncode:
                raise RuntimeError(f"provisioning failed; see {directory / 'provision.log'}")
            inventory = subprocess.check_output(
                [
                    *ssh,
                    "cat /var/lib/mac-verifier-packages.txt; cat /var/lib/mac-verifier-rust.txt",
                ],
                timeout=30,
            )
            (directory / "toolchain.txt").write_bytes(inventory)
            (directory / "Cargo.lock").write_bytes(
                subprocess.check_output([*ssh, "cat /var/lib/mac-verifier-Cargo.lock"], timeout=30)
            )
            print("Shutting down candidate base", flush=True)
            subprocess.run(
                [
                    *ssh,
                    "cloud-init clean --logs --seed; "
                    "rm -f /root/.ssh/authorized_keys /etc/ssh/ssh_host_*; sync; poweroff",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            vm.wait(timeout=120)
            if vm.returncode != 0:
                raise RuntimeError("provisioning VM did not shut down cleanly")
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:
                    vm.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vm.kill()
                    vm.wait(timeout=10)
    subprocess.run(["qemu-img", "check", str(directory / "overlay.qcow2")], check=True)
    subprocess.run(
        [
            "qemu-img",
            "convert",
            "-c",
            "-O",
            "qcow2",
            str(directory / "overlay.qcow2"),
            str(args.output),
        ],
        check=True,
    )
    args.output.chmod(0o444)
    receipt = {
        "schema": "mac.vm_verifier_base.v1",
        "status": "candidate-needs-qualification",
        "source_sha512": args.source_sha512,
        "rust_url": RUST_URL,
        "rust_sha256": RUST_SHA256,
        "dependency_repository": args.repository,
        "dependency_revision": args.revision,
        "sdk_archive_sha256": args.sdk_sha256,
        "base_sha256": digest(args.output),
        "toolchain_inventory_sha256": digest(directory / "toolchain.txt"),
        "dependency_lock_sha256": digest(directory / "Cargo.lock"),
        **firmware,
    }
    args.output.with_suffix(".json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
