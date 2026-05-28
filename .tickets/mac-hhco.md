---
id: mac-hhco
status: closed
deps: []
links: []
created: 2026-05-27T20:52:14Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-hhco
---
# Fix TokenHub provider defaults for rocky

Rocky TokenHub was configured with unsupported OpenAI provider enabled and NVIDIA disabled. TokenHub should use NVIDIA as the default provider at https://inference-api.nvidia.com/v1, then agents should be rechecked against wildcard ladder support.

## Notes

Deployed to rocky from pushed commit cca93bb. Live TokenHub provider list now has only nvidia enabled at https://inference-api.nvidia.com/v1, wildcard list was narrowed to NVIDIA chat models, and OpenAI is no longer present as a rocky provider. Verification found a separate upstream NVIDIA 429: direct calls with the deployed key to https://inference-api.nvidia.com/v1/models return HTTP 429 with empty body, so agents still fail chat self-test until that key/quota issue is resolved.

## Close Reason

Provider bootstrap fixed, pushed, and deployed to rocky. Remaining agent degradation is tracked separately as mac-a72u because direct NVIDIA upstream calls with the deployed key return HTTP 429.
