---
id: mac-wjy3
status: open
deps: []
links: []
created: 2026-05-28T17:32:35Z
type: bug
priority: 1
mac-task-id: pending:mac-wjy3
---
# Worker submits evidence with tests:null after task execution

The mac-wsny e2e v2 task (task_e5043d35a817498db46e14b09286f21b) had its description explicitly say "Run scripts/run-contract-tests.sh and ensure it passes." The worker (bullwinkle) ran the file edit, committed locally, but the resulting verification manifest has verification.tests=null — no test invocation recorded at all.

By contrast, v1 task_56c6aeea51584f638814d0d7b052034e on the same worker DID record tests (546 passed, 5 failed, command="scripts/run-contract-tests.sh"). The difference between the two runs suggests the worker's executor/Hermes path is non-deterministic about whether tests get run.

Possible causes:
- The executor's task prompt may be truncating the "Run tests" instruction
- The LLM (NVIDIA via TokenHub) may sometimes decide tests aren't necessary for a docs-only change
- The evidence collector may be losing the test results if they're emitted out-of-band

Action:
1. Inspect mac-hermes-task-executor stdout/stderr for v2 (workspace at /home/jkh/.mac/agent-workspaces/task_e5043d35a817498db46e14b09286f21b/ on bullwinkle) to see what Hermes actually did.
2. Decide: should the contract test command be invoked by the worker framework directly (deterministic) rather than left to Hermes prompt-following (non-deterministic)?
3. Either way, the verification contract should reject tests:null as it does test failures; today it accepts the manifest, and the workflow only fails later for "no passing tests".

Discovered during mac-wsny e2e proof attempt.
