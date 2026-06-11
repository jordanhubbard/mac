# GPU worker nodes

Turn a fleet GPU host (madmax, natasha) into a Kubernetes **GPU worker node** so
the elastic executor tier ([ADR 0005](../../../docs/adr/0005-elastic-executor-tier-vs-static-fleet.md))
can run `nvidia.com/gpu`-requesting Jobs on it.

> **Why this is now worth doing for madmax.** The thing that used to "block" the
> GPU — the long-lived vLLM "LLM brain" Deployment — serves **≈0 hub traffic**
> (live `llm.route` telemetry, 2026-06-11: 994/1000 routed to cloud `nvidia`, 0
> to madmax's vLLM, because no caller requests its `Qwen/...` model). See the ADR
> Evidence section. So the GPU can be **dedicated** to executor Jobs rather than
> carefully time-sliced around an idle service.

This tree contains the substrate only. It does **not** install drivers (already
present on the GPU hosts) and does **not** itself schedule GPU work — wiring the
MAC runner to request GPUs is the documented draft in
[§ Make GPU Jobs request the GPU](#make-gpu-jobs-request-the-gpu) below.

## Layout

```
deploy/k8s/gpu-worker/
├── README.md                     ← you are here
├── nvidia-device-plugin.yaml     ← DaemonSet: advertises nvidia.com/gpu on labeled nodes
├── label-gpu-node.sh             ← imperative: label (+ optional taint) a node
└── kustomization.yaml            ← applies into kube-system
```

## Prerequisites (per host)

Verified present on madmax (RTX 6000 Ada) and natasha (GB10) in ADR 0005:

- NVIDIA driver loaded (`nvidia-smi` works).
- **NVIDIA Container Toolkit** (`nvidia-ctk`) + **containerd**, with the `nvidia`
  runtime wired as containerd's default (or as a `RuntimeClass`). This is the
  exact mechanism that *cannot* exist on macOS — which is why **bullwinkle is not
  a candidate** (its Metal GPU is unschedulable; its image-gen stays an
  off-cluster native service).
- The node has **joined the cluster** as a worker (kubelet running). `rocky` is
  the intended control-plane / CPU node; it has no container runtime today, so
  add containerd there before it can host anything.

natasha caveats (from ADR 0005): it's **arm64** (mixed-arch cluster → build
workload images for arm64 too) and runs a **kernel-coupled driver** (`6.17-nvidia`
+ driver 580) — keep the per-kernel module package matched (see the
`reference-fleet-gpu-capability` memory for the exact `apt-get` fix).

## Apply

```bash
# 1. Label the node (run from a context with the cluster's kubeconfig).
#    Plain label  -> GPU shareable with other pods.
#    --taint      -> dedicate the node to GPU work (CPU executor Jobs stay off it).
deploy/k8s/gpu-worker/label-gpu-node.sh madmax --taint

# 2. Roll out the device plugin. It only lands on nodes carrying the
#    mac.fleet/capability-gpu=true label set in step 1.
kubectl apply -k deploy/k8s/gpu-worker

# 3. Verify the node now advertises GPUs as a schedulable resource.
kubectl describe node madmax | grep -A2 'Capacity:' | grep nvidia.com/gpu
#   nvidia.com/gpu:  1
kubectl -n kube-system get pods -l app.kubernetes.io/name=nvidia-device-plugin -o wide
```

A quick end-to-end check that GPUs are actually schedulable:

```bash
kubectl run gpu-smoke --rm -it --restart=Never \
  --image=nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 \
  --overrides='{"spec":{"nodeSelector":{"mac.fleet/capability-gpu":"true"},
    "tolerations":[{"key":"mac.fleet/gpu-only","operator":"Exists"}],
    "containers":[{"name":"gpu-smoke","image":"nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04",
      "command":["nvidia-smi"],"resources":{"limits":{"nvidia.com/gpu":1}}}]}}'
```

## Conventions

- **Label:** `mac.fleet/capability-gpu=true` — the k8s analogue of the
  `capability-gpu` node label proven in ADR 0005's kind/colima experiment, in the
  same `mac.*` namespace as the runner's Job labels (`mac.task.id`, `mac.role`, …).
- **Optional taint:** `mac.fleet/gpu-only=true:NoSchedule` — keeps non-GPU Jobs
  off the scarce GPU host. GPU Jobs (and the device plugin) must tolerate it.
- **Namespace:** the device plugin is node infra → `kube-system`. App workloads
  (mac-api, mac-runner, task Jobs) stay in `mac`.

## Make GPU Jobs request the GPU (draft — not yet wired)

The device plugin only *advertises* GPUs; the MAC runner must *request* them for
GPU-tagged tasks. Today `build_job_spec()` in
[`src/mac/k8s/runner.py`](../../../src/mac/k8s/runner.py) emits a CPU-only pod
template via `_build_executor_pod_template()`. The minimal change: when a task's
`required_capabilities` contains `gpu`, inject a nodeSelector, the taint
toleration, and an `nvidia.com/gpu` limit. Sketch:

```python
# in _build_executor_pod_template(...), after the base pod spec is built:
needs_gpu = "gpu" in {str(c) for c in (task.get("required_capabilities") or [])}
if needs_gpu:
    pod_spec["nodeSelector"] = {"mac.fleet/capability-gpu": "true"}
    pod_spec.setdefault("tolerations", []).append(
        {"key": "mac.fleet/gpu-only", "operator": "Exists", "effect": "NoSchedule"}
    )
    container["resources"].setdefault("limits", {})["nvidia.com/gpu"] = "1"
    # nvidia.com/gpu is integer-only and not over-committable: requests==limits
    # is enforced by k8s, so do NOT also set it under requests.
```

This keeps capability matching in the ledger (a task labeled `gpu` lands on a GPU
node) without a second scheduler — consistent with ADR 0005's "the MAC ledger
stays the single dispatch brain." Land it behind a test in `tests/` alongside the
existing `build_job_spec` coverage before enabling.

## What about the vLLM brain?

Because it serves ≈0 traffic, the simplest path is to **stop the vLLM Deployment
and dedicate madmax's GPU to executor Jobs**. Before decommissioning, confirm
there's no out-of-band direct consumer (the hub telemetry can't see processes
that hit `madmax:8000` directly):

```bash
ssh jkh@madmax.local "curl -s localhost:8000/metrics | grep request_success_total; ss -tnp | grep :8000"
```

If you still want a local-LLM fallback, keep a **right-sized** vLLM and share the
GPU via [time-slicing / MPS](https://github.com/NVIDIA/k8s-device-plugin#shared-access-to-gpus)
(a `ConfigMap` for the device plugin) instead of letting one model server hold the
whole card — but don't let a 0-traffic service block the node.
