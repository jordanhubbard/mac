---
name: nvidia-inference-multimodal
description: "Go beyond text: understand images (vision) and generate images via the NVIDIA inference hub through the in-mac router. Use when a task involves looking at an image/screenshot or producing an image — no local GPU needed (routes to hosted models)."
version: "0.1.0"
license: Apache-2.0
platforms: [linux, macos, windows]
tools:
  - Read
  - Shell
metadata:
  author: mac fleet (jordanh)
  tags: [nvidia, inference, multimodal, vision, vlm, image-generation, sdxl, flux, router]
  hermes:
    tags: [vision, image, multimodal, image-gen, nvidia, router]
    related_skills: [omniverse-realtime-viewer]
---

# NVIDIA inference: vision + image generation (via the in-mac router)

The fleet's chat already routes through the in-mac router (`/v1/chat/completions`)
to the NVIDIA inference hub, and the hub hosts many model *types* (chat, vision,
text-to-image, embeddings, …). This is a **routing** capability — you do **not**
need a local GPU or a local Stable Diffusion install; you call the router and it
forwards to NVIDIA's hosted models.

Call the router at the hub URL with the mac bearer:
- URL base: `${MAC_HUB_URL:-http://127.0.0.1:8789}`  (spokes reach the hub via this)
- Auth header: `Authorization: Bearer $MAC_API_TOKEN`

## 1) Understand an image (vision / VLM) — works today
The routed chat model is multimodal, so send the image as message content. Use a
`data:` URL (base64) for local files, or an `http(s)` URL the model can fetch.

```bash
B64=$(base64 -w0 path/to/image.png)
curl -s -X POST "$MAC_HUB_URL/v1/chat/completions" \
  -H "Authorization: Bearer $MAC_API_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"model\":\"*\",\"max_tokens\":300,\"messages\":[{\"role\":\"user\",\"content\":[
        {\"type\":\"text\",\"text\":\"Describe this image.\"},
        {\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,$B64\"}}]}]}"
```
`"model":"*"` lets the router pick the configured chat model (it's vision-capable).
Verified: the chat path returns 200 and analyzes the image.

## 2) Generate an image (text-to-image) — verified working via FLUX
POST to the router's image proxy `POST /v1/genai/<model>`; it forwards to NVIDIA's
hosted image models. Use **FLUX** (verified on this fleet — HTTP 200 → base64 image):

```bash
# FLUX.1-schnell — fast (steps 4). Recommended default.
curl -s -X POST "$MAC_HUB_URL/v1/genai/black-forest-labs/flux.1-schnell" \
  -H "Authorization: Bearer $MAC_API_TOKEN" -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube on a white table","steps":4,"seed":0}' -o out.json

# FLUX.1-dev — higher quality (steps MUST be >= 5, else HTTP 422)
curl -s -X POST "$MAC_HUB_URL/v1/genai/black-forest-labs/flux.1-dev" \
  -H "Authorization: Bearer $MAC_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube on a white table","steps":8,"seed":0}' -o out.json
```
The response carries the image as base64 in `artifacts[0].base64`; decode it:
```bash
python3 -c "import json,base64,sys; d=json.load(open('out.json'));
b=(d.get('artifacts') or [{}])[0].get('base64') or d.get('image') or d.get('b64_json');
open('out.png','wb').write(base64.b64decode(b)) if b else sys.exit('no image: '+str(d)[:200])"
```
Notes:
- Older `stabilityai/sdxl-turbo` / `stable-diffusion-xl` return **404** on this
  account — prefer the FLUX models above.
- `HTTP 401 "Authentication failed"` means the hub's `nvidia-image` vault key isn't
  a `build.nvidia.com` image key. It is set on this fleet; if it regresses,
  re-escrow a `build.nvidia.com` `nvapi-…` key as the `nvidia-image` secret.

## When you'd run a model locally instead
Only for models not on the hub, data that can't leave (privacy), very high
throughput, or custom/fine-tuned models — then a GPU node can host a NIM / vLLM /
diffusion service. For the standard set above, the router + hub cover it.
