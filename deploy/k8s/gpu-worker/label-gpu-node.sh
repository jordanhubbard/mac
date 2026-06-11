#!/usr/bin/env bash
# Mark a fleet host as a MAC GPU worker node.
#
# Node labels can't be expressed as an applyable manifest (Node objects are
# created by the kubelet at join), so the capability label + the optional
# "dedicate to GPU work" taint are applied imperatively here.
#
# Usage:
#   ./label-gpu-node.sh madmax           # label only (GPU shared with other pods)
#   ./label-gpu-node.sh madmax --taint   # label + taint (GPU node runs ONLY gpu jobs)
#   ./label-gpu-node.sh madmax --unset   # remove label + taint
#
# After labeling, `kubectl apply -k deploy/k8s/gpu-worker` rolls the device
# plugin onto the node, and `kubectl describe node <name>` should then show
# `nvidia.com/gpu` under Capacity/Allocatable.
set -euo pipefail

NODE="${1:?usage: label-gpu-node.sh <node-name> [--taint|--unset]}"
MODE="${2:-label}"

CAP_LABEL="mac.fleet/capability-gpu"
GPU_ONLY_TAINT="mac.fleet/gpu-only"

case "$MODE" in
  --unset)
    echo "==> removing GPU worker label + taint from node/$NODE"
    kubectl label  node "$NODE" "${CAP_LABEL}-"            --overwrite || true
    kubectl taint  node "$NODE" "${GPU_ONLY_TAINT}-"                   || true
    ;;
  --taint)
    echo "==> labeling node/$NODE as GPU worker AND tainting it gpu-only"
    kubectl label  node "$NODE" "${CAP_LABEL}=true" --overwrite
    # NoSchedule => only pods that tolerate mac.fleet/gpu-only land here, so the
    # scarce GPU host isn't filled with CPU executor Jobs.
    kubectl taint  node "$NODE" "${GPU_ONLY_TAINT}=true:NoSchedule" --overwrite
    ;;
  label|*)
    echo "==> labeling node/$NODE as GPU worker (GPU shareable with other pods)"
    kubectl label  node "$NODE" "${CAP_LABEL}=true" --overwrite
    ;;
esac

echo "==> current GPU-relevant state of node/$NODE:"
kubectl get node "$NODE" -o jsonpath='{.metadata.labels.mac\.fleet/capability-gpu}{"\n"}' \
  | sed 's/^/   capability-gpu label: /'
kubectl describe node "$NODE" | grep -E 'Taints:|nvidia\.com/gpu' || true
