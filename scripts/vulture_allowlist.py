# Vulture allowlist — false positives for the dead-code contract gate.
# Regenerate ONLY for genuine false positives (framework hooks, interface
# no-op params, dynamic dispatch). Genuine dead code must be DELETED, not
# added here. Regenerate: scripts/dead-code-check.sh --make-whitelist
# Gate: scripts/dead-code-check.sh (min-confidence 90; *_hermes* exclude inert
# after PR #377 removed the vendored tree).

ticket  # unused variable (src/mac/ticketing.py:157)
ticket  # unused variable (src/mac/ticketing.py:160)
ticket  # unused variable (src/mac/ticketing.py:163)
ticket  # unused variable (src/mac/ticketing.py:166)
ticket  # unused variable (src/mac/ticketing.py:169)
