# mac on Kubernetes

Stateless `mac-api` Deployment backed by an externally-managed Postgres
17 database, plus the `mac-k8s-orchestrator` Deployment that claims tasks,
creates Jobs, opens review ticks, and reconciles stuck Jobs. Designed for the K8s-native rewrite Phases 3-5
in [`docs/k8s-native-rewrite-plan.md`](../../docs/k8s-native-rewrite-plan.md).

The Postgres cluster itself is **not** managed from this repo. Bring
your own — CloudNativePG, RDS, Cloud SQL, a vendor-managed cluster,
whatever your platform team owns — and supply the DSN via the
`mac-api-config` Secret (key `MAC_DATABASE_URL`). Likewise, ArgoCD
`Application` manifests are not shipped here; if you sync with ArgoCD,
point one Application per kustomize tree from your platform-config
repo at `deploy/k8s/mac-api` and `deploy/k8s/mac-runner`.

## Architecture

```
                    ┌────────────┐   ┌────────────┐   ┌────────────┐
  ingress ─────▶    │ mac-api-0  │   │ mac-api-1  │   │ mac-api-N  │   (stateless)
                    └─────┬──────┘   └─────┬──────┘   └─────┬──────┘
                          │                │                │
                          └────────┬───────┴────────┬───────┘
                                   ▼                ▼
                        ┌─────────────────────────────────────┐
                        │   Postgres 17 (externally managed)  │
                        │   DSN: mac-api-config / MAC_DATABASE_URL
                        └─────────────────────────────────────┘
```

Every `mac-api` replica is interchangeable — there is no leader and no
PVC on the application pods. Lease, claim, and dispatch coordination
happen at the database layer (partial unique index on `leases`,
`ON CONFLICT` upserts, the `task_history` outbox), so the app scales
horizontally without an application-level lock.

### Work-package pipeline activation

The stock K8s base deliberately sets
`MAC_WORK_PACKAGE_PIPELINE_ENABLED=false` and
`MAC_WORK_PACKAGE_LANDING_ENABLED=false`. Do not flip those values in this
stateless base: automatic certification currently invokes the OpenShell CLI in
the control-plane process, while this image has neither that CLI/runtime nor a
durable certification-bundle volume or controller Git credentials.

Kubernetes activation therefore remains fail-closed until a platform overlay
provides all of the following as one reviewed unit:

1. a reachable, independently managed OpenShell certification runtime and its
   CLI in the API image;
2. an existing `ReadWriteMany` PVC mounted at an absolute
   `MAC_WORK_PACKAGE_BUNDLE_DIR`, owned by UID/GID 1000 and restricted to mode
   `0700` (bundle files are forced to `0400` by MAC);
3. controller-only Git credentials capable of reading protected candidate refs
   and compare-and-swap pushing the canonical ref; never expose these to the
   certifier;
4. repository registrations containing a secret-free canonical remote, a
   `landing_certification_policy_id`, and a fully valid
   `mac.work_package.certification_contract.v1` with a digest-pinned image;
5. both activation variables set to `true` only after the preceding probes
   pass.

Until that overlay exists, use the explicit admin certification/finalization
surfaces for controlled trials or run the automatic pipeline on a prepared
bare-metal hub. A PVC alone is not activation: it would make data durable while
still leaving certification or landing authority incomplete.

## Layout

```
deploy/k8s/
├── README.md                              ← you are here
├── mac-api/                               ← Phase 3: stateless coordinator
│   ├── namespace.yaml
│   ├── deployment.yaml                    ← replicas: 2, no PVC
│   ├── service.yaml
│   ├── secret.example.yaml                ← copy + fill + apply out-of-band
│   └── kustomization.yaml
└── mac-runner/                            ← orchestrator + job-per-task runner
    ├── serviceaccount.yaml                ← mac-k8s-orchestrator + mac-task-runner SAs
    ├── rbac.yaml                          ← Jobs CRUD + Deployment scale in namespace
    ├── deployment.yaml                    ← replicas: 2, claims tasks → creates Jobs
    └── kustomization.yaml
```

### How execution flows

```
  mac-api  ←─────── claim-next, lease renew, evidence/transition ────┐
    ▲                                                                │
    │                                                                │
  mac-k8s-orchestrator ─ kubectl-create ─►   batch/v1 Job  ───►  mac-task-runner
    │                                            ▲
    │                                            │
    └──────── reconciles stuck Jobs + scales worker pools
```

## Prerequisites

1. **A Postgres 17 cluster** reachable from the `mac` namespace. The
   DSN goes into the `mac-api-config` Secret under key
   `MAC_DATABASE_URL`. Cluster provisioning is out of scope here.
2. The operator-supplied `mac-api-config` Secret carrying
   `MAC_DATABASE_URL`, `MAC_SECRET_KEY`, a temporary compatibility
   `MAC_WORKER_TOKEN`, and `MAC_API_TOKENS` registering that same token with
   non-admin worker scopes — see `mac-api/secret.example.yaml`. The API reads
   `MAC_API_TOKENS`; `MAC_WORKER_TOKEN` alone does not authorize API requests.
   Deliver the Secret with `kubectl create secret`, Sealed Secrets,
   ExternalSecrets, SOPS, or the equivalent for your platform.
3. A built `mac` image with the `[postgres]` extra. The repo Dockerfile
   already installs it; tag and push to your registry, then replace the
   `image:` placeholder in `mac-api/deployment.yaml` and
   `mac-runner/deployment.yaml`.
   The helper script `scripts/build-and-push-image.sh` handles the
   common case (Apple Silicon dev machine → linux/amd64 K8s nodes via
   `docker buildx`, optional `--push` and `--update-manifests`):

   ```bash
   # build only, into the local daemon (no push):
   scripts/build-and-push-image.sh --registry ghcr.io/your-org

   # build + push + pin the digest in both deployment.yaml files:
   scripts/build-and-push-image.sh \
     --registry ghcr.io/your-org \
     --tag v0.1.0 \
     --update-manifests
   ```

## Apply order

`mac-api` will CrashLoop on connect errors until the `mac-api-config`
Secret contains a working `MAC_DATABASE_URL`. Create the Secret first.

```bash
# 1. (Once) create the API Secret. The compatibility token is deliberately
#    non-admin and exists only to register identities/bootstrap ordinary work.
kubectl create namespace mac
BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
kubectl -n mac create secret generic mac-api-config \
  --from-literal=MAC_DATABASE_URL='postgresql://user:pass@host:5432/mac' \
  --from-literal=MAC_SECRET_KEY="$(openssl rand -base64 48)" \
  --from-literal=MAC_WORKER_TOKEN="$BOOTSTRAP_TOKEN" \
  --from-literal=MAC_API_TOKENS="{\"$BOOTSTRAP_TOKEN\":[\"agent\",\"dispatch\",\"read\",\"write\"]}"

# 2. mac-api Deployment + Service.
kubectl apply -k deploy/k8s/mac-api

# 3. Run mac-k8s-bootstrap with the platform's MAC_CONFIG_FILE so the exact
#    mac-k8s-orchestrator and role-agent rows exist. Do not apply the runner yet.
#    The bootstrap may run from a trusted admin pod or workstation:
MAC_CONFIG_FILE=/secure/mac-k8s-config.yaml \
MAC_URL=http://mac-api.mac.svc:80 \
MAC_WORKER_TOKEN="$BOOTSTRAP_TOKEN" \
  mac-k8s-bootstrap

# 4. From a trusted host that has the same Postgres DSN and kubectl context,
#    issue and install the exact orchestrator credential. SOURCE_COMMIT is the
#    image's source revision; RUNTIME_DIGEST is its immutable image digest.
export MAC_DATABASE_URL='postgresql://user:pass@host:5432/mac'
python -m mac.worker_credentials issue \
  --agent-id mac-k8s-orchestrator \
  --environment k8s \
  --expected-source-commit "$SOURCE_COMMIT" \
  --expected-runtime-digest "$RUNTIME_DIGEST" \
  --capability work_package_v1 \
  --package-capable \
  --manifest-out /secure/mac-k8s-orchestrator.json
PRINCIPAL_ID="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["principal_id"])' \
  /secure/mac-k8s-orchestrator.json)"
python -m mac.worker_credentials install-k8s \
  --manifest /secure/mac-k8s-orchestrator.json \
  --agent-id mac-k8s-orchestrator \
  --namespace mac \
  --receipt-out /secure/mac-k8s-orchestrator-receipt.json

# 5. The orchestrator now starts with only its agent-bound Secret. It claims
#    tasks, creates one batch/v1 Job per lease, opens reviews, and reconciles
#    stuck Jobs.
kubectl apply -k deploy/k8s/mac-runner

# 6. After its authenticated heartbeat is visible, activate the installed
#    version (which atomically supersedes the previous one) and refresh the
#    reviewed package-readiness membership. The one-time manifest is consumed
#    only after successful activation.
python -m mac.worker_credentials activate \
  --agent-id mac-k8s-orchestrator \
  --principal-id "$PRINCIPAL_ID" \
  --receipt /secure/mac-k8s-orchestrator-receipt.json \
  --manifest /secure/mac-k8s-orchestrator.json
python -m mac.worker_credentials set-mode compatibility --review-live
```

`MAC_RUNNER_AGENT_TOKEN_SECRETS` maps ordinary delegated role/reviewer agents
to their own Secrets. Provision each mapping with the same issue → install →
heartbeat → activate sequence. Package-linked tasks deliberately keep the
dispatcher identity and Secret because the immutable assignment audit is bound
to the claiming dispatcher; role selection still chooses the task image and
executor. Missing mappings retain legacy ordinary-task compatibility but never
authorize package work.

## Schema bootstrap

`PostgresStore.initialize()` runs the bundled
[`src/mac/data/postgres/schema.sql`](../../src/mac/data/postgres/schema.sql)
at process startup. Every statement uses `IF NOT EXISTS` /
`CREATE OR REPLACE`, so multiple `mac-api` replicas racing each other
on first boot is safe — Postgres internal locking serialises the DDL.
The cluster owner role pointed to by `MAC_DATABASE_URL` must have
permission to create tables, functions, triggers, and views in its
default schema.

## Watching the rollout

```bash
kubectl -n mac get pods
kubectl -n mac logs -l app.kubernetes.io/name=mac-api -f
kubectl -n mac logs -l app.kubernetes.io/name=mac-k8s-orchestrator -f
```

## Health

`mac-api` exposes `GET /health` on `:8000`. The Deployment uses it for
both `readinessProbe` and `livenessProbe`. The Service publishes on
`:80` -> `:8000` inside the cluster (no Ingress is included; add one
according to your cluster's ingress controller).
