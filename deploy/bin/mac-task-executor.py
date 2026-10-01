# Autonomous task executor shim. The real, unit-tested logic lives in the
# mac.task_executor module (extracted from this heredoc per loop-01): it
# builds the prompt, runs a verified coding agent inside OpenShell, writes deterministic
# evidence, emits executor telemetry, and feeds deployment lessons into
# memory so the fleet gets smarter over time.
from mac.task_executor import main

raise SystemExit(main())
