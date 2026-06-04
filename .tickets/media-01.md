---
id: media-01
status: open
deps: []
links: [in-mac-router-vault]
created: 2026-06-04T00:00:00Z
type: epic
priority: 2
audit: media-routing-architecture
discovered_via: image-gen-fragmentation
---
# EPIC: unify media routing — operation → [provider binding] table + adapters

## Why this exists

Image/audio/video generation is wired three disjoint ways, and they don't
agree, so "the backend works but the agent can't reach it" is a recurring bug
(e.g. `/v1/genai` returns a real JPEG, yet agents report "no FAL_KEY"):

1. **Chat router** — `MAC_ROUTER_PROVIDERS="nvidia=url,0,key=secret:nvidia-upstream;madmax=…"`.
   Rich: ordered providers, priority, per-model routing, vault-ref keys. *This
   is the model to copy.*
2. **Media proxies** — `MAC_ROUTER_{IMAGE,AUDIO,VIDEO}_UPSTREAM` + one flat
   `…_KEY` each (router_app.py `_MODALITY_PROXIES`). A **dumb pass-through**:
   one upstream, one key, no model list, and the caller must speak the
   upstream's dialect (`POST /v1/genai/black-forest-labs/flux.1-schnell`).
3. **Agent tool** — the `ImageGenProvider` registry (`_hermes/agent/
   image_gen_registry.py`), default-fallback `fal` (needs `FAL_KEY`), with no
   wiring to #2. So #2 works, agents use #3, #3 is unkeyed → no image.

A single key per modality can't express reality: a key belongs to a *provider*;
a provider serves *many* operations; an operation has *many* candidate
providers (many-to-many). And the providers don't share a wire format, so a
generic proxy + single key can't translate requests.

## Design

Route on **`(modality, operation)`**; each route resolves to an **ordered list
of provider bindings** (priority = fallback/robustness). Generalize the chat
provider model to all modalities.

```yaml
router:
  media:
    image.generate:                 # text→image
      - {provider: bullwinkle-local, base_url: http://bullwinkle:8189, model: sdxl-turbo,
         key: "", adapter: openai_images, priority: 0, when: {agent_up: bullwinkle}}
      - {provider: nvidia-nim, base_url: https://ai.api.nvidia.com/v1/genai,
         model: black-forest-labs/flux.1-schnell, key: secret:nvidia-image, adapter: nvidia_genai, priority: 1}
      - {provider: fal, base_url: https://fal.run, model: fal-ai/flux-2/klein/9b,
         key: secret:fal, adapter: fal, priority: 2}
    image.understand:               # vision/VLM → a CHAT model, not genai
      - {provider: nvidia-chat, model: "*", adapter: openai_chat_vision, key: secret:nvidia-upstream}
    audio.tts:   [...]
    audio.asr:   [...]
    video.generate: [...]
```

Each binding beyond `(operation, url, key)`:
- **`model`** separate from `base_url` — one provider has many models; one op may try several.
- **`key: secret:<name>`** — never inline; resolved by `SecretsService` (already
  exists). Rotation is a vault op, no config edit (see media-02).
- **`adapter`** — the missing piece. Translates a *canonical* request
  (`{operation, prompt, image?, size, steps, …}`) ↔ each provider's schema
  (NVIDIA genai's flux vs stability dialects, FAL, OpenAI-images, a local SDXL
  server). Callers become provider-agnostic; providers are swappable.
- **`priority` + `when`** — try local GPU first, fall back to cloud; **skip a
  provider whose key is missing/invalid** (a 401 becomes graceful fallback, not
  a hard failure).

### Unify the code paths
- Router: replace the three flat modality proxies with this table; expose a
  canonical `POST /v1/media/{op}` (normalized body) that walks the bindings in
  priority order, applies the adapter, falls back on failure. The current flat
  `MAC_ROUTER_*_{UPSTREAM,KEY}` become the degenerate single-binding case
  (back-compat).
- Agents: the `ImageGenProvider` registry is the right seam — add ONE provider,
  `mac-hub`, whose `generate()` calls `/v1/media/image.generate`; make it the
  default. Agents then inherit the hub's vault keys and never need a local
  `FAL_KEY`. The keyless `nvidia` provider already does ~this for image (routes
  through `/v1/genai`); `mac-hub` is its generalization across modalities.
- Register **bullwinkle-local** (and any GPU agent, e.g. natasha post-driver) as
  a *binding*, not a separate code path, so the table can prefer on-box GPU.

### Where it lives
`fleets.yaml` `defaults.router.media` (+ per-agent overrides), materialized into
the hub env, keys resolved via `SecretsService` — the same pattern chat uses.

## Acceptance Criteria
- A `(modality, operation) → [binding]` table parsed from fleets.yaml, with
  ordered fallback and `secret:`-ref keys.
- A per-provider adapter interface; adapters for nvidia_genai (flux + stability),
  fal, openai_images, openai_chat_vision.
- Canonical `POST /v1/media/{op}`; flat `MAC_ROUTER_*_{UPSTREAM,KEY}` still work
  (degenerate single binding).
- A `mac-hub` ImageGenProvider that routes via the canonical endpoint; default
  for agents; no per-agent media keys required.
- Fallback skips bindings with missing/invalid keys (verified: a bad key →
  next provider, not a hard error).

## Delivered (2026-06-04)
- **Router core** (#89): `mac.media_routing` (operation→[binding] table,
  back-compat from flat `MAC_ROUTER_IMAGE_*`) + `POST /v1/media/{op}` with
  priority failover; adapters `nvidia_genai` (flux/stability) + `passthrough`.
- **Agent provider** (#90): `mac-hub` ImageGenProvider posts the canonical
  request to `/v1/media/image.generate`; the deploy defaults
  `image_gen.provider: mac-hub`. Verified end-to-end (jordanh-gke duck).
- **Default migration** (#91): a redeploy migrates the prior deploy-managed
  default (`nvidia`→`mac-hub`) forward; genuine alternatives respected.
- **More image adapters** (#92): `openai_images` (b64) + `fal` (url artifacts,
  `Key` auth via `MediaBinding.auth_scheme` + `urllib_forwarder` auth_scheme;
  the mac-hub provider downloads url artifacts). image.generate is now genuine
  multi-provider failover (e.g. nvidia→fal→openai) via `MAC_ROUTER_MEDIA_JSON`.

## Remaining — audio / video
The canonical endpoint is modality-general (any `op` + `passthrough` adapter
works today via `MAC_ROUTER_MEDIA_JSON`), so a **JSON-in/JSON-out** audio/video
NIM can already be wired with no code. The provider-specific work needs both a
configured backend to validate against (NONE is set on any fleet today —
`MAC_ROUTER_AUDIO/VIDEO_UPSTREAM` are empty) AND transport extensions the current
synchronous-JSON `urllib_forwarder` lacks:
- `audio.tts`: providers return **binary audio** (not JSON) → needs a
  binary-response forward (canonical `{audio: base64}`).
- `audio.asr`: **multipart** audio upload → a non-JSON request encoding.
- `video.generate`: **async queue** (submit → poll → url) → a polling forward.
Plus agent-side `mac-hub` providers for the existing `tts_registry` /
`transcription_registry` / `video_gen_registry` (mirror the image one). Deferred
until a live audio/video provider exists to validate against — shipping blind
transport for nonexistent backends is not worth the risk.

## Notes
- Tactical precursor (shipped): default `image_gen.provider` so agents use the
  keyless hub-routed path. This epic generalized it: `mac-hub` → `/v1/media`.
- See media-02 for the credential model (per-provider vault secrets;
  `nvidia-upstream` = chat, `nvidia-image` = genai are *distinct* entitlements —
  conflating them is what 401'd image-gen on a fresh hub).
