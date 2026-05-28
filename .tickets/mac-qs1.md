---
id: mac-qs1
status: closed
deps: []
links: []
created: 2026-05-25T06:00:48Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-qs1
---
# Allow repo-less review verdicts for operator-result evidence

The c26 e2e workflow produced signed operator_result evidence and Bullwinkle produced signed approved review_verdict evidence, but default review stayed pending because review verdict validation always required repo.head_sha/dirty/pushed fields. Repo-less operator_result tasks should be reviewable without a Git repo anchor; repo-backed evidence should still require matching pushed repo metadata.

## Close Reason

Fixed repo-less operator_result review handling and linked review verdict publication validation; added regression tests and deployed fixes to the fleet.
