# Hermes deployment profile and readiness

Tracked by `task_e49af13e0f4849f9962999eb59114503`.

The September 2026 hub rollout exposed two independent failures. Deployment
selected `~/.mac/openclaw`, which lacked the Slack credentials and model route
held in the original `~/.hermes` profile. Its readiness checks also disagreed:
the node exempted Hermes from process verification, while the outer attestation
looked for a MAC-owned service name instead of the upstream service.

Restoring the original profile through Hermes' supported CLI restored Slack
authentication and Socket Mode. A separate model request succeeded. These
observations established chat recovery; they did not establish worker health,
complete the fleet rollout, or demonstrate an end-to-end task outcome.

Deployment now preserves the upstream service's configured `HERMES_HOME` when
reconciling an old mac.env. Prerequisite context receipts and the generated
environment use the same profile. A new conflicting profile override fails
instead of silently relocating credentials. The existing Python path resolver's
legacy fallback remains compatible; deployed services receive an explicit home.

Service replacement stops Hermes first and checks that its previous runtime
process actually exited. Readiness matches the current service summary rather
than historical log text, then requires the selected profile's runtime writer
to belong to that supervised process and report Slack connected. This checks
transport readiness without sending a message. A successful model completion
and an operator-visible reply remain separate acceptance evidence.

Supervisor proofs inspect `ai.hermes.gateway` in both launchd user domains, or
`hermes-gateway.service` in the systemd user manager. Competing legacy units,
duplicate launchd owners, and unstable process/domain observations fail the
proof. The node receipt and independent outer attestation both inspect those
real identities; Hermes no longer has a readiness exemption.

Operational completion still requires the normal sequence: source verification,
independent review and publication, supported fleet deployment, one completed
canary, then gradual backlog release. Do not infer completion from a merged PR
or a supervised chat process.
