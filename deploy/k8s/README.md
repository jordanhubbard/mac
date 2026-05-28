# mac on Kubernetes

Stateless `mac-api` Deployment backed by an externally-managed Postgres
17 database, plus the Phase 4 `mac-k8s-runner` and Phase 5
`mac-k8s-controller`. Designed for the K8s-native rewrite Phases 3-5
in [`docs/k8s-native-rewrite-plan.md`](../../docs/k8s-native-rewrite-plan.md).

The Postgres cluster itself is **not** managed from this repo. Bring
your own — CloudNativePG, RDS, Cloud SQL, a vendor-managed cluster,
whatever your platform team owns — and supply the DSN via the
`mac-api-config` Secret (key `MAC_DATABASE_URL`). Likewise, ArgoCD
`Application` manifests are not shipped here; if you sync with ArgoCD,
point one Application per kustomize tree from your platform-config
repo at `deploy/k8s/mac-api`, `deploy/k8s/mac-runner`, and
`deploy/k8s/mac-controller`.

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
├── mac-runner/                            ← Phase 4: job-per-task runner
│   ├── serviceaccount.yaml                ← mac-k8s-runner + mac-task-runner SAs
│   ├── rbac.yaml                          ← batch.jobs CRUD in namespace
│   ├── deployment.yaml                    ← replicas: 2, claims tasks → creates Jobs
│   └── kustomization.yaml
└── mac-controller/                        ← Phase 5: reconciler
    ├── serviceaccount.yaml
    ├── rbac.yaml                          ← Jobs delete + Deployments scale
    ├── deployment.yaml                    ← singleton, recreates stuck Jobs
    └── kustomization.yaml
```

### How execution flows

```
  mac-api  ←─────── claim-next, lease renew, evidence/transition ────┐
    ▲                                                                │
    │                                                                │
  mac-k8s-runner  ──── kubectl-create  ───►   batch/v1 Job  ───►  mac-task-runner
    │                                            ▲
    │                                            │  reconciler
    └────────────────►  mac-k8s-controller  ─────┘
                       (deletes stuck Jobs,
                        scales worker pools)
```

## Prerequisites

1. **A Postgres 17 cluster** reachable from the `mac` namespace. The
   DSN goes into the `mac-api-config` Secret under key
   `MAC_DATABASE_URL`. Cluster provisioning is out of scope here.
2. The operator-supplied `mac-api-config` Secret carrying
   `MAC_DATABASE_URL`, `MAC_SECRET_KEY`, `MAC_WORKER_TOKEN`, and
   optionally `MAC_API_TOKENS` — see `mac-api/secret.example.yaml` for
   the schema. Use whichever delivery mechanism you prefer (`kubectl
   create secret`, Sealed Secrets, ExternalSecrets, SOPS).
3. A built `mac` image with the `[postgres]` extra. The repo Dockerfile
   already installs it; tag and push to your registry, then replace the
   `image:` placeholder in `mac-api/deployment.yaml`,
   `mac-runner/deployment.yaml`, and `mac-controller/deployment.yaml`.
   The helper script `scripts/build-and-push-image.sh` handles the
   common case (Apple Silicon dev machine → linux/amd64 K8s nodes via
   `docker buildx`, optional `--push` and `--update-manifests`):

   ```bash
   # build only, into the local daemon (no push):
   scripts/build-and-push-image.sh --registry ghcr.io/your-org

   # build + push + pin the digest in all three deployment.yaml files:
   scripts/build-and-push-image.sh \
     --registry ghcr.io/your-org \
     --tag v0.1.0 \
     --update-manifests
   ```

## Apply order

`mac-api` will CrashLoop on connect errors until the `mac-api-config`
Secret contains a working `MAC_DATABASE_URL`. Create the Secret first.

```bash
# 1. (Once) create the Secret with MAC_DATABASE_URL + MAC_SECRET_KEY
#    [+ MAC_API_TOKENS]. Use your ExternalSecrets backend or create
#    it imperatively:
kubectl create namespace mac
kubectl -n mac create secret generic mac-api-config \
  --from-literal=MAC_DATABASE_URL='postgresql://user:pass@host:5432/mac' \
  --from-literal=MAC_SECRET_KEY="$(openssl rand -base64 48)" \
  --from-literal=MAC_WORKER_TOKEN="$(openssl rand -hex 32)"

# 2. mac-api Deployment + Service.
kubectl apply -k deploy/k8s/mac-api

# 3. (Phase 4) mac-k8s-runner Deployment: claims tasks and creates
#    one batch/v1 Job per claimed lease.
kubectl apply -k deploy/k8s/mac-runner

# 4. (Phase 5) mac-k8s-controller Deployment: reconciles stuck Jobs
#    and (optionally) scales worker-pool Deployments.
kubectl apply -k deploy/k8s/mac-controller
```

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
kubectl -n mac logs -l app.kubernetes.io/name=mac-k8s-runner -f
kubectl -n mac logs -l app.kubernetes.io/name=mac-k8s-controller -f
```

## Health

`mac-api` exposes `GET /health` on `:8000`. The Deployment uses it for
both `readinessProbe` and `livenessProbe`. The Service publishes on
`:80` -> `:8000` inside the cluster (no Ingress is included; add one
according to your cluster's ingress controller).
