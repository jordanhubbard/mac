# Vendored Omniverse 3D agent skills (GPU nodes)

`omniverse-skills.tar.gz` bundles the NVIDIA Omniverse + physical-AI agent skills
plus our authored `omniverse-kit-app` build skill. The deploy
(`install_omniverse_gpu_skills` in `deploy-mac-fleet.sh`) extracts it into
`$HOME/.hermes/skills/` **only on agents with an NVIDIA GPU** (`nvidia-smi`
gate) — Omniverse Kit / CUDA can't run elsewhere — and re-extracts on every
deploy, so the install is durable + repeatable + GPU-scoped.

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
