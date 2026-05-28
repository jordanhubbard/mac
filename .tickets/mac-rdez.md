---
id: mac-rdez
status: closed
deps: []
links: []
created: 2026-05-27T17:58:59Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-rdez
---
# Restore a usable Hermes LLM provider

The Rocky fleet is deployed and TokenHub now returns first-class budget_exceeded errors, but Hermes cannot complete chat/work while the configured OpenAI account is quota-exhausted and the NVIDIA provider registry/credentials do not provide a working fallback. Add or repair at least one usable TokenHub provider/model, then rerun Hermes smoke tests on rocky, natasha, and bullwinkle.

## Notes

2026-05-27 verification: TokenHub on rocky is healthy and serving 302 models with the nvidia provider enabled. Hermes chat self-test passes on all three agents:
- rocky: status=passed, all 6 checks (firecrawl_web_search, hermes_chat, identity_env, qdrant_shared_memory, runtime_context, tokenhub_runtime), chat_returncode=0
- natasha: status=passed, all 6 checks
- bullwinkle: status=passed, all 6 checks
Chat completions through TokenHub return HTTP 200 routed via the nvidia provider. Earlier "NVIDIA disabled / 401" notes are stale.

Follow-up filed for orphan tokenhub vs disabled systemd unit on rocky.

## Close Reason

Smoke tests pass on rocky/natasha/bullwinkle; TokenHub nvidia provider live with 302 models, chat self-test returncode=0 on all three. Follow-up mac-l6o0 covers reconciling the orphan tokenhub process with the disabled systemd unit on rocky.
