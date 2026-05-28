---
id: mac-cil
status: closed
deps: []
links: []
created: 2026-05-24T09:05:38Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-cil
---
# Verify Hermes prompt bridge executes MAC runtime context

Why this issue exists: MAC now writes and verifies a rich runtime/session capability context, but the Hermes-side prompt bridge is only proven by static markers in prompt_builder.py and deploy patch application. That is too indirect for the goal that hermes-agent actually understands MAC tasks/projects as first-class objects. What needs to be done: add an executable test/verifier that applies the MAC runtime prompt patch to a representative Hermes prompt_builder module and proves build_context_files_prompt() loads the configured mac-runtime-context.md into the prompt without leaking absent or private state.

## Close Reason

Deploy now executes the patched Hermes prompt builder against the written MAC runtime context, and tests apply the prompt patch to a representative Hermes module and prove build_context_files_prompt includes the MAC runtime/session contract.
