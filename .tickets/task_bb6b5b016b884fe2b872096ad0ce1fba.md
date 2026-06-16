---
id: task_bb6b5b016b884fe2b872096ad0ce1fba
status: open
deps: []
links: []
created: 2026-06-14T19:26:13.008806+00:00
type: task
priority: 0
mac-task-id: task_bb6b5b016b884fe2b872096ad0ce1fba
---
# OpenShell: complete natasha (GB10 ARM64) GPU sandbox rollout

Complete OpenShell sandbox enforcement rollout on natasha (sparky.local, GB10 ARM64).

Groundwork done (this session):
- openshell CLI 0.0.62 (uv) + openshell-gateway 0.0.62 (prebuilt aarch64 release asset) installed.
- GPU passthrough de-risked: works via docker --gpus (RTX/GB devices visible). podman 4.9.3 CDI is too old for nvidia-ctk 1.19.1's 0.7.0 specs (fails even at 0.6.0) -> natasha MUST use the docker compute driver.
- #155 task_executor.py + openshell_runtime.py NOT yet deployed to natasha's mac source (do this).

Remaining (the hardest host — three genuine unknowns):
1. Docker-driver gateway: write ~/.mac/openshell/gateway.toml with compute_drivers=["docker"] + the [drivers.docker] schema (research OpenShell docs / `openshell-gateway --help`), generate-certs, systemd --user service, firewall (block public/LAN :17670 — note sparky's interfaces), `openshell gateway add http://127.0.0.1:17670` + select.
2. ARM64 mac-hermes image: the x86 image won't run on aarch64. rocky's image COPYs a mac wheel + supervisor binary — needs an ARM rebuild (debian-arm64 base + python venv at /opt/mac-venv + mac install + iproute2/git/curl + sandbox user/group). Confirm the build recipe (podman history on rocky's image) and rebuild on natasha.
3. OpenShell --gpu with the docker driver: verify `openshell sandbox create --gpu` injects the GPU when the gateway uses the docker driver (OpenShell --gpu docs say it uses CDI device IDs for both docker+podman; natasha's podman CDI is broken but docker's nvidia-runtime works — confirm which path --gpu takes with the docker driver).

Then: render policy (remote hub egress 100.125.137.89:8789, like bullwinkle), rewritten/no-rewrite hermes config, deploy #155 code, env recipe (MAC_OPENSHELL_SANDBOX=1, MAC_HERMES_PYTHON=/opt/mac-venv/bin/python, MAC_OPENSHELL_POLICY, MAC_OPENSHELL_BIN absolute, MAC_OPENSHELL_CREATE_ARGS quoted with --gpu + config upload + HOME=/tmp), validate a real GPU task end-to-end, flip + fail-closed (MAC_ALLOW_UNSANDBOXED_YOLO=0).

Reference: rocky + bullwinkle are DONE (this session) — mirror their setup; deploy recipe in evidence on task_9babe9013f134283b32eda92bd2bfa90.
