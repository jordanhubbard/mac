# c26 certifier phase-profile example

The c26 project was retired on 2026-09-14. This remains a reusable design
fixture for testing certifier profiles, not an active onboarding plan. It is deliberately not a
deployed c26 repository contract and does not authorize managed c26 work by
itself. An active project may adapt it only after its own certifier harness has been built,
published by CI as an immutable image digest, and proved to emit this exact
full-only receipt.

```yaml
phase_profile:
  schema: mac.certifier_phase_profile.v1
  version: 1
  checksum: sha256:8ff99ffaaf6feee9a0898f06083068b1d67bb4fc30012941a46189a6e816db3b
  full_targets:
    - Makefile
    - scripts/smoke.py
    - tests
  focused_required_tests: []
  selection_modes:
    c26_full_suite:
      authoritative:
        mode: full
        reason: c26_always_full
      supplemental:
        mode: skipped
        reason: c26_full_suite_is_authoritative
      expected_full_suite_count: 1
```

The checksum is SHA-256 over canonical JSON of the complete `phase_profile`
mapping after removing only `checksum`: sorted object keys, compact `,` and `:`
separators, and ASCII escaping enabled. Any content change requires a new
checksum and changes the certification job digest. An emitted selection mode,
phase mode, or reason that differs by even one character fails closed.

The executable test fixture is
`tests/certifier_phase_profile_fixtures.py::c26_phase_profile`; keep it and this
example semantically aligned while the fixture remains in the test suite.
The retired project has no pending contract-publication requirement.
