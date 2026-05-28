---
id: mac-xqn6
status: deferred
deps: []
links: []
created: 2026-05-26T17:39:35Z
type: epic
priority: 1
mac-task-id: pending:mac-xqn6
---
# Harden project registration and import preflight

MAC accepted c26 work even though the effective execution and publication environment was not proven. c26 had a runtime contract, but one fleet checkout lacked declared/actual build tools and GitHub write access; review then stalled on missing remote refs and failed credentials. Project registration must prove contract, toolchain, bootstrap/test, and canonical remote access before tasks are imported or dispatched.

## Acceptance Criteria

Project registration/import refuses unhealthy repositories with clear user-facing findings; missing contracts can be explicitly bootstrapped and pushed; declared and discovered toolchain requirements are verified; canonical Git read/write access is proven non-interactively; c26 can be registered on the intended fleet without stalled review or publication failures.

## Notes

2026-05-26: Audit-pass deferral. Three of seven children closed in this session:
- mac-xqn6.1: contract gate + integration finding + unhealthy stamp
- mac-xqn6.3: toolchain.required_commands presence check
- mac-xqn6.5: dispatch fence skips OPEN tasks from unhealthy projects

Remaining four deferred until 2026-07-01 because they need real
external infrastructure or substantial architectural work that's
beyond an audit-fix session:
- .2 (Bootstrap missing contract): CLI UX + git push back; needs design + live credentials
- .4 (Live git remote access): network ops; needs per-project credential handling
- .6 (Review workflow for unpublished branches): workflow-state redesign
- .7 (c26 operational remediation): not code — operational work against real fleet
