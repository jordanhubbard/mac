# Vendored Omniverse 3D agent skills (GPU nodes)

`omniverse-skills.tar.gz` bundles the NVIDIA Omniverse + physical-AI agent skills
plus our authored `omniverse-kit-app` build skill. Install it by hand, and
**only on agents with an NVIDIA GPU** (`nvidia-smi` works) — Omniverse Kit /
CUDA can't run elsewhere:

```bash
mkdir -p ~/.hermes/skills
tar xzf ~/.mac/src/mac/deploy/skills/omniverse-skills.tar.gz -C ~/.hermes/skills
```

Re-run it after the tarball changes. (The deleted fleet installer used to do
this on every deploy.)

## Contents
- `omniverse-kit-app` — authored here: build/run/package a 3D app via the Kit SDK
  (`kit-app-template`, OpenUSD, RTX).
- From [`NVIDIA/skills`](https://github.com/NVIDIA/skills) (Apache-2.0, trimmed of
  `evals/` + `*.oms.sig`):
  - `omniverse-realtime-viewer`, `omniverse-cad-to-simready`,
    `omniverse-usd-performance-tuning`
  - `physical-ai-neural-reconstruction`, `physical-ai-defect-image-generation`,
    `physical-ai-video-data-augmentation`,
    `physical-ai-infrastructure-setup-and-resilient-scaling`

## Re-vendor (refresh from upstream)
```bash
git clone --depth 1 https://github.com/NVIDIA/skills /tmp/nv-skills
mkdir -p /tmp/omv-stage
for s in omniverse-realtime-viewer omniverse-cad-to-simready omniverse-usd-performance-tuning \
         physical-ai-neural-reconstruction physical-ai-defect-image-generation \
         physical-ai-video-data-augmentation physical-ai-infrastructure-setup-and-resilient-scaling; do
  rsync -a --exclude 'evals/' --exclude '*.oms.sig' "/tmp/nv-skills/skills/$s" /tmp/omv-stage/
done
# keep the authored kit-app skill:
tar xzf deploy/skills/omniverse-skills.tar.gz -C /tmp/omv-stage omniverse-kit-app 2>/dev/null || true
tar czf deploy/skills/omniverse-skills.tar.gz -C /tmp/omv-stage .
```

## Fleet-wide skills (`fleet/`, no GPU gate)

`deploy/skills/fleet/<skill>/` belongs in `$HOME/.hermes/skills` on **every**
agent (copy it there by hand; the deleted fleet installer's `install_fleet_skills`
used to). These drive the hub's hosted models via the in-mac router and need no
local GPU:

- `nvidia-inference-multimodal` — vision (send images in chat) + image generation
  (`/v1/genai` proxy). Image-gen needs the hub's `nvidia-image` key to have public
  image-API (`ai.api.nvidia.com`) access; the internal chat-gateway key returns 401.
