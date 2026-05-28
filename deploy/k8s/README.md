# mac on Kubernetes (Phase 3 topology)

Stateless `mac-api` Deployment backed by a CloudNativePG (CNPG) Postgres 17
cluster. Designed for the K8s-native rewrite Phases 3-5 in
[`docs/k8s-native-rewrite-plan.md`](../../docs/k8s-native-rewrite-plan.md).

## Architecture

```
                    ┌────────────┐   ┌────────────┐   ┌────────────┐
  ingress ─────▶    │ mac-api-0  │   │ mac-api-1  │   │ mac-api-N  │   (stateless)
                    └─────┬──────┘   └─────┬──────┘   └─────┬──────┘
                          │                │                │
                          └────────┬───────┴────────┬───────┘
                                   ▼                ▼
                            ┌─────────────────────────────┐
                            │     mac-pg (CNPG)           │
                            │  3 instances · postgres:17  │
                            └─────────────────────────────┘
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
├── argocd/
│   └── application.yaml                   ← one Application per kustomize tree
├── cnpg/
│   ├── cluster.yaml                       ← CNPG Cluster CR
│   └── kustomization.yaml
└── mac-api/
    ├── namespace.yaml
    ├── deployment.yaml
    ├── service.yaml
    ├── externalsecret.example.yaml        ← copy + edit per env
    └── kustomization.yaml
```

## Prerequisites

1. **CloudNativePG operator** installed in the cluster:
   `kubectl apply -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.1.yaml`
2. **ExternalSecrets Operator** (optional but recommended) for `MAC_SECRET_KEY` / `MAC_API_TOKENS`.
3. A built `mac` image with the `[postgres]` extra. The repo Dockerfile
   already installs it; tag and push to your registry, then replace the
   `image:` placeholder in `mac-api/deployment.yaml`.

## Apply order

CNPG bootstrap must complete before `mac-api` pods start (the deployment
will CrashLoop on connect errors otherwise).

```bash
# 1. CNPG cluster (creates database `mac`, role `mac`, secret `mac-pg-app`).
kubectl apply -k deploy/k8s/cnpg

# 2. Wait for the cluster to be healthy.
kubectl -n mac wait --for=condition=Ready cluster/mac-pg --timeout=10m

# 3. (Once) create the ExternalSecret for MAC_SECRET_KEY / MAC_API_TOKENS:
cp deploy/k8s/mac-api/externalsecret.example.yaml /tmp/mac-api-es.yaml
$EDITOR /tmp/mac-api-es.yaml      # set your SecretStore + remoteRef keys
kubectl apply -f /tmp/mac-api-es.yaml

# 4. mac-api Deployment + Service.
kubectl apply -k deploy/k8s/mac-api
```

Or let ArgoCD manage both:

```bash
kubectl apply -f deploy/k8s/argocd/application.yaml
```

## Schema bootstrap

`PostgresStore.initialize()` runs the bundled
[`src/mac/data/postgres/schema.sql`](../../src/mac/data/postgres/schema.sql)
at process startup. Every statement uses `IF NOT EXISTS` /
`CREATE OR REPLACE`, so replicas racing each other on first boot is
safe — Postgres internal locking serialises the DDL.

## Backups

Backup config is commented out in `cnpg/cluster.yaml` so the manifest
applies clean to a fresh cluster without object-storage credentials.
Before going to production:

1. Create object-storage credentials and a Secret (`mac-pg-backup-creds`).
2. Uncomment the `backup:` section in `cluster.yaml`.
3. Verify the first WAL archive lands in the target bucket.

## Watching the rollout

```bash
kubectl -n mac get pods
kubectl -n mac logs -l app.kubernetes.io/name=mac-api -f
kubectl -n mac exec -it mac-pg-1 -- psql -U postgres -d mac -c '\dt'
```

## Health

`mac-api` exposes `GET /health` on `:8000`. The Deployment uses it for
both `readinessProbe` and `livenessProbe`. The Service publishes on
`:80` -> `:8000` inside the cluster (no Ingress is included; add one
according to your cluster's ingress controller).
