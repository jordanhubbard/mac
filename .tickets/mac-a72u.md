---
id: mac-a72u
status: closed
deps: []
links: []
created: 2026-05-27T21:10:11Z
type: bug
priority: 0
mac-task-id: pending:mac-a72u
---
# Resolve NVIDIA upstream 429 on rocky TokenHub key

After deploying NVIDIA_API_KEY__ROCKY, rocky TokenHub has only the nvidia provider enabled at https://inference-api.nvidia.com/v1 and /v1/models lists NVIDIA-owned models. Direct calls from rocky to NVIDIA with the deployed key return HTTP 429 with an empty body, and TokenHub wildcard chat returns 502 after upstream rate_limited failures. Agents remain degraded until the upstream key/quota/rate-limit issue is fixed or a usable NVIDIA key is supplied.

## Close Reason

After NVIDIA provider cooldown cleared and the wildcard ladder was narrowed to NVIDIA chat-capable models, TokenHub wildcard chat succeeds and all three agents report healthy. No remaining upstream 429 blocker observed in live verification.
