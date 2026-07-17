# Work-Package Pipeline Activation

The work-package assembly line is default-off. A new hub receives
`MAC_WORK_PACKAGE_PIPELINE_ENABLED=0` and
`MAC_WORK_PACKAGE_LANDING_ENABLED=0`; spokes are forced to keep both off even
if they inherit stale hub environment. This is deliberate: enabling the loop
also authorizes controller-owned integration, external code execution,
certification, and compare-and-swap publication.

Activation is complete only when every gate below passes. Do not treat a
running controller thread, a writable Git checkout, or an allocated bundle
directory as sufficient proof.

This repository does not currently declare or publish the required
digest-pinned, image-owned certification harness. Consequently the checked-in
`.mac/project.yaml` intentionally omits the activation extension and production
landing must remain disabled until that external artifact exists and is
reviewed.

## 1. Prepare the hub runtime first

Use a durable control-plane host. The current automatic certifier invokes the
OpenShell CLI in the hub process, so the stock stateless K8s API image is not an
activation target. Bootstrap and validate OpenShell in a first deployment while
the pipeline remains disabled:

```bash
MAC_DEPLOY_OPENSHELL=1 \
MAC_DEPLOY_OPENSHELL_ARGS='--enable --fail-closed' \
deploy/deploy-mac-fleet.sh <hub-agent>
```

The bootstrap publishes the exact CLI at `~/.mac/bin/openshell`, which is on
the managed MAC service path. Confirm the confinement probe passed and that the
service user, not merely an interactive login shell, can run
`openshell --version`.

## 2. Pin a certifier image and policy

Build and publish a certifier image that contains the repository's declared
test tools and an image-owned launcher. The launcher must select or verify the
trusted-base test harness before executing candidate code; pointing directly at
a script that the candidate can replace is not an independent certification
boundary. Record the registry-provided immutable reference in
`name@sha256:<64 lowercase hex>` form. A tag, local image ID, or placeholder is
rejected before downstream WIP is transferred.

Use a network-disabled OpenShell policy with hard Landlock enforcement, a
non-root user/group, and only `/tmp`, `/dev`, or `/sandbox` as writable roots.
`src/mac/openshell/default-policy.yaml` is the reviewed lockdown starting point.
Compute the digest over the exact UTF-8 bytes embedded in `policy_text`:

```bash
python3 - <<'PY'
import hashlib
from pathlib import Path

value = Path("src/mac/openshell/default-policy.yaml").read_bytes()
print("sha256:" + hashlib.sha256(value).hexdigest())
PY
```

Add both fields below to `.mac/project.yaml`. They are one atomic extension: a
partial pair, unknown field, duplicate command ID, invalid policy, or mutable
image is rejected by the same normalizer used immediately before job
preparation.

```yaml
landing_certification_policy_id: mac-work-package-v1
work_package_certification:
  schema: mac.work_package.certification_contract.v1
  policy:
    policy_id: mac-work-package-v1
    version: 1
    checksum: sha256:<digest-of-exact-policy-text>
  policy_text: |
    version: 1
    filesystem_policy:
      include_workdir: true
      read_only:
        - /usr
        - /lib
        - /lib64
        - /bin
        - /sbin
        - /etc
        - /proc
      read_write:
        - /tmp
        - /dev
    landlock:
      compatibility: hard_requirement
    process:
      run_as_user: sandbox
      run_as_group: sandbox
    network_policies: {}
  image_ref: registry.example/mac-certifier@sha256:<64-lowercase-hex>
  controller_commands:
    - command_id: contract-tests
      argv:
        - /opt/mac-certifier/bin/run-contract-tests
      timeout_seconds: 3600
```

Validate the checked-in contract before refreshing registration:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from mac.services import _load_repository_contract

contract = _load_repository_contract(Path.cwd())
assert contract["canonical_remote_url"]
assert contract["landing_certification_policy_id"]
assert contract["work_package_certification"]["image_ref"].count("@sha256:") == 1
print("repository certification contract: valid")
PY
```

Refresh the hub's repository registration so its durable metadata contains the
new contract, then inspect it before activation:

```bash
mac bridge repository register mac "$PWD" --project=mac
mac bridge repository repos
```

## 3. Prove controller Git authority

The registered `canonical_remote_url` must be secret-free and identify the same
remote the landing controller will update. From the MAC service environment,
prove non-interactive read access and a separately reviewed write path. SSH
deployments normally expose only `SSH_AUTH_SOCK` and an optional
`GIT_SSH_COMMAND`; HTTPS deployments use an allowlisted askpass/token source.
Credential values must never enter repository metadata, the task ledger,
pipeline status, or the certifier bundle.

The external certifier receives only the exact candidate Git bundle. It must
have no hub token, repository URL, Git credential, or canonical landing
authority.

## 4. Provision durable bundle storage

On a bare-metal fleet hub the managed default is
`~/.mac/work-package-bundles`. Deployment creates or verifies it as a regular,
non-symlink directory owned by the service user with mode `0700`; individual
bundles are mode `0400` and revalidated by digest and exact candidate SHA.

For a system package deployment, prepare the configured location explicitly:

```bash
sudo install -d -o mac -g mac -m 0700 /var/lib/mac/work-package-bundles
```

The database remains lifecycle authority. The directory is durable,
rebuildable content cache, but deleting or substituting a prepared bundle is
still detected because the job stores its exact digest.

## 5. Enable in a second deployment

Only after the preceding checks pass, enable pipeline and landing together:

```bash
MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=1 \
MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=1 \
MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR="$HOME/.mac/work-package-bundles" \
deploy/deploy-mac-fleet.sh <hub-agent>
```

The runtime also fails closed if one switch or the bundle path is missing. A
spoke ignores these activation values and remains off.

Read status with a read-scoped token and trigger one nonblocking pass only with
an admin-scoped token:

```bash
curl -fsS -H "Authorization: Bearer $MAC_API_TOKEN" \
  "$MAC_URL/work-package-pipeline/status"
curl -fsS -X POST -H "Authorization: Bearer $MAC_API_TOKEN" \
  "$MAC_URL/work-package-pipeline/trigger"
```

Before releasing real work, verify that status reports `enabled: true` with no
configuration error. Then use one disposable package to prove the complete
chain: exact attempt ref, controller acceptance, integration candidate, bundle,
external certification, landing intent, remote read-back receipt, publication
finalization, and WIP drain. Also run a failing-certification canary and prove
that canonical Git does not move and no landing intent or receipt is created.

## Kubernetes boundary

`deploy/k8s/mac-api/deployment.yaml` keeps both switches explicitly false. The
stock image has no OpenShell CLI/runtime, no controller Git credential, and no
persistent bundle volume. A platform overlay must add those prerequisites and
an existing ReadWriteMany PVC before it may opt in. See
`deploy/k8s/README.md`; changing only the two booleans is an invalid deployment.
