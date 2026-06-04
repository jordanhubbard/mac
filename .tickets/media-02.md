---
id: media-02
status: open
deps: [media-01]
links: [in-mac-router-vault, media-01]
created: 2026-06-04T00:00:00Z
type: feature
priority: 2
audit: media-routing-architecture
discovered_via: jordanh-gke-image-401
---
# Media credential model: per-provider vault secrets, distinct entitlements

## Why this exists

Image-gen 401'd on a freshly-deployed hub (jordanh-gke) even though chat worked,
because the deploy's modality escrow falls back to the **chat** key when the
image key isn't supplied:

```
# deploy-mac-fleet.sh modality escrow
("image", "MAC_ROUTER_IMAGE_KEY",
 os.environ.get("NVIDIA_IMAGE_API_KEY") or os.environ.get("NVIDIA_API_KEY") or "")
```

So a fresh hub got `secret:nvidia-image` = the chat key, which NVIDIA's
`ai.api.nvidia.com/v1/genai` rejects (`401 Authentication failed`). The chat
NIM key and the genai/image key are **distinct entitlements** at build.nvidia.com
— conflating them silently produces a working-chat / broken-image fleet.

(Resolved operationally for jordanh-gke by rotating `nvidia-image` to a real
build key via `DELETE`+`POST /secrets`; this ticket makes the model correct.)

## What to fix

- Keys are per-**provider/entitlement**, referenced by `secret:<name>` and
  resolved from `SecretsService` (the vault already exists; rotation is a vault
  op — no config edit, no redeploy).
- Stop the chat-key fallback for media. If `NVIDIA_IMAGE_API_KEY` (and audio/
  video) isn't supplied, escrow **nothing** for that modality and mark the
  corresponding `media` bindings *unavailable* (media-01's fallback then skips
  them gracefully) — never escrow the chat key under an image/audio/video name.
- Surface missing media entitlements at deploy time (a clear "image-gen
  disabled: no nvidia-image key" line) instead of a runtime 401.
- A small `mac secret rotate <name>` CLI (today there's create/delete/resolve
  but no HTTP rotate) so operators don't DELETE+POST by hand.

## Acceptance Criteria
- Per-provider `secret:` keys; no cross-entitlement fallback (chat key never
  escrowed as a media key).
- Deploy reports which media operations are enabled vs disabled (by key
  presence), no hard failure when a media key is absent.
- `mac secret rotate <name> --value-file <f>` (stdin `-` supported), audited.
