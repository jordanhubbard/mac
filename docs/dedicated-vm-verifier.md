# Dedicated VM repository verification

Some unchanged repository tests require LeakSanitizer process inspection, which
the mandatory OpenShell syscall policy does not permit. A dedicated KVM guest
provides a separate Linux kernel for those tests. It does not relax OpenShell
policy or change other workers.

This backend is opt-in through `MAC_HUB_VERIFY_VM_CONFIG`, a private, regular
JSON file owned by the controller user or root. Its `repositories` list matches
repository URLs exactly. Unlisted repositories continue using OpenShell.
Configuration, transport, identity, or sanitizer failures fail verification;
there is no fallback to execution on the controller host.

## Candidate image preparation

Run `scripts/provision-verifier-vm.py --help` on a dedicated Linux/KVM host.
The host needs Python 3, QEMU with KVM access, `qemu-img`, OpenSSH tools,
`xorriso`, and OVMF firmware. The controller needs Python 3 and OpenSSH.
Supply an official Debian cloud image and its independently verified SHA512,
an unused output path, and the exact dependency repository revision. Also supply
the repository's qualified SDK archive and its SHA256 pin; it is installed in
the guest's dependency cache without rebuilding kernel code. Provisioning
installs distribution packages and a SHA256-pinned Rust distribution. It fetches
Cargo dependencies without building repository code. If the repository has no
root lockfile, provisioning resolves one and records its digest and contents.
The generated lockfile is not inserted into the source under review.

Only this trusted provisioning boot has network egress. The resulting flattened
qcow2, package inventory, dependency lockfile, firmware hashes, and manifest are
candidate evidence, not a passing qualification. Qualify the candidate with the
full unchanged repository command before enabling production routing.

## Configuration

Both host and controller configurations use `schema: mac.vm_verifier.v1` and
an explicit `repositories` array. Keep each file mode 0600; never commit local
SSH identity paths, credentials, or operational host topology.

The host configuration supplies `base_image`, `base_sha256`, `firmware_code`,
`firmware_code_sha256`, `firmware_vars`, `firmware_vars_sha256`, and `workspace`.
Optional `memory_mib`, `cpus`, and `max_timeout_seconds` bound execution. Defaults
are 16 GiB and eight CPUs; hard limits are 32 GiB, twelve CPUs, and two hours.
Install the reviewed standalone `src/mac/vm_verifier_host.py` on that host.

The controller configuration supplies the same base and firmware digest pins,
`ssh_target`, `ssh_identity`, `ssh_known_hosts`, `remote_runner`, and
`remote_config`. All path fields are absolute. Use a dedicated SSH identity and
an independently enrolled host key. Host-key checking is mandatory and agent
forwarding is disabled.

## Execution and evidence

The controller stages the exact repository commit using the existing hub source
preparation path and streams its archive to the host. Each request gets a fresh
copy-on-write disk, UEFI variables, SSH management keys, and cloud-init seed.
There are no host filesystem mounts. QEMU user networking is restricted, with
only a loopback management SSH forward. The host serializes reviews with a lock.

Before executing repository code, an unprivileged sanitizer control must pass
with freed memory and reject an intentional leak. The repository command runs
as the non-administrative `verifier` user with a clean environment and offline
Cargo cache. No ASan, UBSan, or LeakSanitizer suppression is added. Exact HEAD
and tree checks run before the unchanged bootstrap and test commands.

The SSH exit status supplies the test verdict. The response binds the nonce,
commit, tree, archive, base image, firmware, and commands. The controller checks
these fields before accepting the result and includes the runtime identity in
review evidence. Logs and request/result receipts remain under the host's
`workspace/receipts`; temporary disks and management keys are removed after the
owned VM process stops. Operators must manage retained receipt disk usage.

## Rollout and rollback

Qualify clean-pass and deliberate-failure controls, source/digest rejection,
network isolation, cleanup, and the full repository suite first. Deploy the
reviewed controller and host source, enable routing for one repository, and
complete one normal hub review before submitting its backlog. Keep fleet
dispatch paused when required by the project.

Rollback removes `MAC_HUB_VERIFY_VM_CONFIG` and restarts only the controller
service using its supported deployment procedure. OpenShell policy remains
unchanged; repositories requiring LeakSanitizer will again report the original
infrastructure limitation rather than receive a passing verdict.
