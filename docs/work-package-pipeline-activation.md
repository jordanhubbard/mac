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

This repository declares the reproducible, image-owned certification harness
under `deploy/certifier/` and publishes it only from the tested main-branch CI
path. The checked-in `.mac/project.yaml` intentionally omits the activation
extension until a concrete workflow publication has been independently
reviewed and its registry digest verified. Source scaffolding or a mutable tag
is not an activation artifact.

## 1. Prepare the hub runtime first

Use a durable control-plane host. The current automatic certifier invokes the
OpenShell CLI in the hub process, so the stock stateless K8s API image is not an
activation target. Bootstrap and validate OpenShell in a first deployment while
the pipeline remains disabled:

```bash
MAC_DEPLOY_OPENSHELL=1 \
MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE='ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<reviewed-digest>' \
MAC_DEPLOY_OPENSHELL_ARGS='--enable --fail-closed' \
deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
```

Tested-main CI also publishes the worker runtime as
`ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<digest>` with multi-arch
provenance and SBOM attestations. Production deployment accepts only that
repository-owned digest, never a tag or a locally rebuilt lookalike. Supply the
reviewed publication receipt through `MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE`.
The deployer requires this digest for every OpenShell-enabled or
`openshell_required` production node. Per-host rebuilding is available only as
an explicit development escape hatch through
`MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD=1`; it is not a fleet mode.

Before deploying, prove the package is anonymously readable with an empty
Docker configuration. A successful authenticated workflow push is not enough:

```bash
tmp="$(mktemp -d)"
printf '{}' > "$tmp/config.json"
DOCKER_CONFIG="$tmp" docker pull \
  'ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<reviewed-digest>'
rm -rf "$tmp"
```

The bootstrap publishes the exact CLI at `~/.mac/bin/openshell`, which is on
the managed MAC service path. Confirm the confinement probe passed and that the
service user, not merely an interactive login shell, can run
`openshell --version`.

## 2. Pin a certifier image and policy

Build and publish the certifier image described in
`deploy/certifier/README.md`. It contains the locked test tools and the
image-owned `/opt/mac-certifier/bin/run-contract-tests` launcher. The
controller binds the batch's exact `assembly_base_sha` as a reserved
`--base-sha` argument; the contract cannot supply it. The launcher verifies its
frozen baseline, proves the base object exists and is an ancestor, and selects
from `base...HEAD` rather than guessing from a merge commit's parents.
Candidate-owned tests remain worker evidence but cannot replace independent
certification. Record
the registry-provided immutable reference in
`name@sha256:<64 lowercase hex>` form. A tag, local image ID, or placeholder is
rejected before downstream WIP is transferred.

The tested-main CI publication builds `ghcr.io/jordanhubbard/mac`,
`ghcr.io/jordanhubbard/mac-openshell-runtime`, and
`ghcr.io/jordanhubbard/mac-certifier` from the same tested commit, emits SBOM
and provenance attestations, and uploads exact digest references as receipts.
The certifier job is scoped only to its frozen inputs, so writing a
verified digest into `.mac/project.yaml` cannot trigger a new image and create
a digest-update loop. Verify registry read-back and compute the exact policy
checksum before editing the contract:

The verifier uses an empty Docker configuration and a network-disabled image
self-test. This anonymous readback is required because the remote OpenShell
gateway receives no GHCR credential.

```bash
scripts/verify-certifier-publication.py \
  ghcr.io/jordanhubbard/mac-certifier@sha256:<64-lowercase-hex> \
  --expected-revision <40-character-tested-main-sha>
```

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

Do not add `--base-sha` to this YAML. Preparation rejects any repository-owned
attempt to set it, appends the immutable batch value exactly once, and includes
the resolved argv in the command and job digests.

The frozen selector records a `mac.certifier_phase_manifest.v1` in the result
and station receipt. Docs-only and mapped source changes use a focused
root-owned phase. Unmapped source uses one authoritative full phase;
root-visible deploy/config changes use one supplemental full phase. A mixed
unmapped-source/root-visible candidate is rejected for splitting instead of
running two full suites. The receipt's `full_suite_count` is therefore always
zero or one.

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
remotes receive only `SSH_AUTH_SOCK` from the ambient service environment.
HTTPS landing is deliberately narrower: only a validated `https://github.com/`
remote is supported, `GH_TOKEN` must be present, and the controller injects the
package-owned `mac-git-askpass` executable resolved beside `sys.executable`.
The helper reads the token only from its one-process environment and responds
only to GitHub username/password prompts. Ambient `GIT_ASKPASS`, `SSH_ASKPASS`,
`GIT_CONFIG_*`, `GITHUB_TOKEN`, and generic token fallbacks are not accepted.
Credential values must never enter Git argv, authenticated URLs, repository
metadata, the task ledger, pipeline status, or the certifier bundle.

`main` is the tested publication ref, not currently a GitHub-protected branch.
Do not describe it as protected until the repository has a rule that requires
the PR jobs that actually run. The present controller and workers share an
owner/admin credential: disabling admin enforcement would not constrain that
credential, while enabling it would block the controller's direct compare-and-
swap landing. The durable protection migration therefore requires a dedicated
least-privilege finalizer identity and an explicit ruleset bypass for that
identity before admin enforcement can be enabled.

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
MAC_DEPLOY_EXECUTION_COHORT_REVISION=1 \
MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT=50 \
MAC_DEPLOY_EXECUTION_COHORT_SEED="${MAC_DEPLOY_EXECUTION_COHORT_SEED:?set a stable 32+ character pilot seed}" \
deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
```

The runtime also fails closed if one switch or the bundle path is missing. A
spoke ignores these activation values and remains off. Generate the pilot seed
once, retain it in the owner-only deployment secret store, and reuse it for the
whole revision. The deployer sends it only to the hub through its one-use SSH
stdin secret file; it never enters a remote command or spoke environment.
Changing either the seed or allocation percentage requires a new revision.

Read status with a read-scoped token and trigger one nonblocking pass only with
an admin-scoped token:

```bash
curl -fsS -H "Authorization: Bearer $MAC_API_TOKEN" \
  "$MAC_URL/work-package-pipeline/status"
curl -fsS -X POST -H "Authorization: Bearer $MAC_API_TOKEN" \
  "$MAC_URL/work-package-pipeline/trigger"
```

Before releasing real work, verify that status reports `enabled: true` with no
configuration error. Use the checked-in canary helper in its default read-only
mode to review the exact negative and positive plans:

```bash
scripts/work-package-canary.py --hub-url "$MAC_URL"
```

The negative candidate deliberately masks a publication-lane regression in its
candidate-owned test, so the worker gate passes but the frozen image baseline
must reject it. The helper requires explicit `--execute`, `--confirm-live`, and
`--confirm-exclusive-main-window` flags before it can admit work. It records
the exact canonical SHA before and after each case: the negative case requires
a certification rejection/station receipt and no Git movement or publication
receipt; the positive docs-only case requires the landing read-back and final
publication receipts, with remote SHA equal to `observed_sha`. Run them in that
order during an ingress freeze so unrelated writers cannot invalidate the
before/after proof.

## Kubernetes boundary

`deploy/k8s/mac-api/deployment.yaml` keeps both switches explicitly false. The
stock image has no OpenShell CLI/runtime, no controller Git credential, and no
persistent bundle volume. A platform overlay must add those prerequisites and
an existing ReadWriteMany PVC before it may opt in. See
`deploy/k8s/README.md`; changing only the two booleans is an invalid deployment.
