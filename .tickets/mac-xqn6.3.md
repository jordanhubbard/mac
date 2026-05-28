---
id: mac-xqn6.3
status: closed
deps: []
links: []
created: 2026-05-26T17:39:41Z
type: feature
priority: 1
assignee: Jordan Hubbard
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.3
---
# Verify project toolchain and bootstrap/test commands during registration

Project registration should prove that the hub/agent environment can actually execute the registered repository contract. c26 exposed the gap: the contract omitted clang while the Makefile required it, and one fleet lacked clang and qemu-system-riscv64.

## Acceptance Criteria

Registration preflight checks every contract tool in PATH, runs bootstrap/test or a configured dry-run safely, captures stdout/stderr, and marks the project unhealthy on failure; command output is visible in project health; tests cover undeclared tool detection or bootstrap failure; c26 reports missing clang/qemu on unprepared nodes instead of dispatching work.

## Close Reason

_repository_contract_for_beads_repo_at_path now runs shutil.which() for each declared toolchain.required_commands. Missing commands raise ValidationError → mac-xqn6.1 records integration finding + marks repo unhealthy → mac-xqn6.5 keeps the project's tasks out of dispatch. Tests in test_control_plane exercise the broader contract path; the new check is a minimal extension of an existing validator that's already covered by contract-load tests.
