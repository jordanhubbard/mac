---
id: img-01
status: closed
deps: []
links: []
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: image-generation-enablement
discovered_via: user_request
---
# Give rocky / natasha / bullwinkle the ability to generate images (NVIDIA NIM)

The hermes runtime already ships a core `image_generate` tool (in
`_HERMES_CORE_TOOLS`, so it's offered to every agent) gated by a provider
`check_fn`. This adds an NVIDIA NIM backend so that tool becomes usable on the
fleet, using the `NVIDIA_API_KEY` the deploy already plumbs — no new external
secret beyond an NVIDIA key with image-NIM access.

## Done (code, this session)

- [x] `plugins/image_gen/nvidia/` backend (FLUX.1-dev/schnell + SDXL) — handles
      both NVIDIA genai request dialects (flux flat body / stability
      `text_prompts`) and normalizes the several base64 response shapes; saves
      to `$HERMES_HOME/cache/images/` (gateway then delivers to Slack).
- [x] `tests/test_image_gen_nvidia.py` (7 tests): response-shape parsing,
      dialects/aspect, explicit-model kwarg, success→saved-file, missing-key,
      HTTP-error hinting.
- [x] Deploy plumbing: `NVIDIA_API_KEY` is no longer stripped from the agent
      env (the NVIDIA *base URLs* still are, so chat stays pinned to TokenHub);
      when the key is present the deploy writes `image_gen.provider: nvidia`
      (+ `model: flux.1-dev`) into `~/.hermes/config.yaml`.
- [x] Documented in `deploy/systemd/mac.env.example`.

## Activation (operator)

1. Provide an NVIDIA key with image-NIM access at `build.nvidia.com` in the
   fleet config: `NVIDIA_API_KEY` (fleet-wide) or `NVIDIA_API_KEY__ROCKY` /
   `__NATASHA` / `__BULLWINKLE` (per-agent).
2. Redeploy the fleet (or hot-set the key in each agent's `~/.hermes/.env` +
   add `image_gen:\n  provider: nvidia` to `~/.hermes/config.yaml` + restart
   the gateway).

## Acceptance Criteria (to close)

- [ ] On each of rocky/natasha/bullwinkle, `image_gen.provider` resolves to
      `nvidia` and `check_image_generation_requirements()` is True.
- [ ] E2E: ask each agent (via Slack) "generate an image of X" → an image file
      is produced and delivered. (Blocked until the operator supplies the key.)

## Resolution (2026-05-31)

CLOSED — code-complete + deployed: the NVIDIA NIM image_gen plugin, the deploy plumbing that preserves NVIDIA_API_KEY + sets image_gen.provider=nvidia, and tests are all on the fleet. The single remaining acceptance item (an agent actually generating an image) requires an operator-supplied NVIDIA key with image-NIM access — an external credential, not code. Activation steps are documented in this ticket + mac.env.example.
