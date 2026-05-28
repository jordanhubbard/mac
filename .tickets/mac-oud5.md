---
id: mac-oud5
status: closed
deps: []
links: []
created: 2026-05-27T00:53:58Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-oud5
---
# Agent-reported running_digest is accepted without proof

src/mac/services.py:5450-5475 — heartbeat checks the digest exists in runtime_environments.digest, but does not verify the agent actually runs that build. An agent can claim any registered digest and fleet_build_distribution (services.py:5615-5638) tallies the lie. Fix: server-issued challenge/response over the running binary, or signed attestation from the agent's runtime.

## Close Reason

Closed
