# HTTP API reference

This route index is generated from the current FastAPI OpenAPI schema.
Use the schema exposed by a running hub at `/openapi.json` for complete
request and response definitions.

| Method | Path | Operation |
|---|---|---|
| `GET` | `/.well-known/acp` | Acp Manifest Route |
| `GET` | `/.well-known/agent-card.json` | A2A Agent Card Route |
| `GET` | `/.well-known/agent.json` | A2A Agent Card Route |
| `POST` | `/a2a` | A2A Rpc Route |
| `GET` | `/action-events` | List Action Events |
| `POST` | `/action-events` | Record Action Event |
| `GET` | `/action-events/export/otlp` | Export Action Events Otlp |
| `GET` | `/action-events/stream` | Action Events Stream |
| `POST` | `/agentbus` | Publish Agentbus Content |
| `POST` | `/agentbus/artifact-publish` | Publish Agentbus Artifact |
| `POST` | `/agentbus/human-directive` | Publish Human Directive Route |
| `POST` | `/agentbus/repo-update` | Publish Agentbus Repo Update |
| `POST` | `/agentbus/request` | Agentbus Request |
| `GET` | `/agentbus/streams` | List Agentbus Streams |
| `POST` | `/agentbus/streams` | Open Agentbus Stream |
| `GET` | `/agentbus/streams/{stream_id}/chunks` | Read Agentbus Chunks |
| `POST` | `/agentbus/streams/{stream_id}/chunks` | Append Agentbus Chunk |
| `POST` | `/agentbus/streams/{stream_id}/close` | Close Agentbus Stream |
| `GET` | `/agentbus/streams/{stream_id}/directive-verification` | Verify Human Directive Route |
| `GET` | `/agentbus/streams/{stream_id}/events` | Agentbus Stream Events |
| `GET` | `/agents` | List Agents |
| `POST` | `/agents` | Register Agent |
| `POST` | `/agents/bulk` | Bulk Update Agents |
| `GET` | `/agents/dispatch-hold/authority` | Dispatch Hold Authority |
| `POST` | `/agents/dispatch-hold/epochs/open` | Open Fleet Release Epoch |
| `GET` | `/agents/dispatch-hold/epochs/{epoch_id}` | Dispatch Hold Epoch Status |
| `POST` | `/agents/dispatch-hold/epochs/{epoch_id}/abort` | Abort Fleet Release Epoch |
| `POST` | `/agents/dispatch-hold/epochs/{epoch_id}/commit` | Commit Fleet Release Epoch |
| `POST` | `/agents/dispatch-hold/epochs/{epoch_id}/prove` | Prove Fleet Release Epoch |
| `GET` | `/agents/dispatch-hold/epochs/{epoch_id}/readiness` | Dispatch Hold Epoch Pre Prove Readiness |
| `POST` | `/agents/dispatch-hold/release-batch` | Release Dispatch Holds Batch |
| `POST` | `/agents/dispatch-hold/transition-batch` | Transition Dispatch Holds Batch |
| `DELETE` | `/agents/{agent_id}` | Delete Agent |
| `GET` | `/agents/{agent_id}` | Get Agent |
| `PUT` | `/agents/{agent_id}` | Update Agent |
| `GET` | `/agents/{agent_id}/agentbus/inbox` | Agentbus Inbox Events |
| `POST` | `/agents/{agent_id}/attestation-key/recover` | Recover Agent Attestation Key |
| `POST` | `/agents/{agent_id}/attestation-key/rotate` | Rotate Agent Attestation Key |
| `POST` | `/agents/{agent_id}/attestation-key/verify` | Verify Agent Attestation Key |
| `POST` | `/agents/{agent_id}/claim-next` | Claim Next For Agent |
| `GET` | `/agents/{agent_id}/command-audit` | List Agent Command Audit |
| `POST` | `/agents/{agent_id}/command-audit` | Record Agent Command Audit |
| `POST` | `/agents/{agent_id}/crash-reports` | Report Agent Crash |
| `POST` | `/agents/{agent_id}/directive-activations/{activation_id}/ack` | Acknowledge Directive Activation |
| `GET` | `/agents/{agent_id}/directives/effective` | Effective Directives For Agent |
| `POST` | `/agents/{agent_id}/disable` | Disable Agent |
| `DELETE` | `/agents/{agent_id}/dispatch-hold` | Clear Dispatch Hold |
| `POST` | `/agents/{agent_id}/dispatch-hold` | Set Dispatch Hold |
| `POST` | `/agents/{agent_id}/dispatch-hold/acquire` | Acquire Dispatch Hold |
| `POST` | `/agents/{agent_id}/dispatch-hold/release` | Release Dispatch Hold |
| `POST` | `/agents/{agent_id}/heartbeat` | Heartbeat Agent |
| `GET` | `/agents/{agent_id}/identity` | Get Agent Identity |
| `POST` | `/agents/{agent_id}/installed-packages` | Update Agent Installed Packages |
| `POST` | `/agents/{agent_id}/messages/deliver` | Deliver Messages |
| `DELETE` | `/agents/{agent_id}/mood` | Clear Mood |
| `GET` | `/agents/{agent_id}/mood` | Get Mood |
| `POST` | `/agents/{agent_id}/mood` | Set Mood |
| `PUT` | `/agents/{agent_id}/mood` | Set Mood |
| `GET` | `/agents/{agent_id}/mood/history` | List Mood History |
| `POST` | `/agents/{agent_id}/nap-consolidate` | Consolidate Nap |
| `POST` | `/agents/{agent_id}/nap-cycle` | Run Nap Cycle |
| `POST` | `/agents/{agent_id}/nap-runs` | Begin Nap |
| `GET` | `/agents/{agent_id}/nap-schedule` | Get Nap Schedule |
| `POST` | `/agents/{agent_id}/nap-schedule` | Configure Nap |
| `PUT` | `/agents/{agent_id}/nap-schedule` | Configure Nap |
| `GET` | `/agents/{agent_id}/nap-schedule/next` | Next Nap Window |
| `GET` | `/agents/{agent_id}/openshell/policy` | Get Agent Openshell Policy |
| `GET` | `/agents/{agent_id}/openshell/status` | Get Agent Openshell Status |
| `POST` | `/agents/{agent_id}/openshell/status` | Report Agent Openshell Status |
| `POST` | `/agents/{agent_id}/reflect` | Reflect Agent |
| `POST` | `/agents/{agent_id}/report-repository-executor/approve` | Approve Agent Report Repository Executor |
| `POST` | `/agents/{agent_id}/report-repository-executor/revoke` | Revoke Agent Report Repository Executor |
| `GET` | `/agents/{agent_id}/representation` | Resolve Agent Representation |
| `DELETE` | `/agents/{agent_id}/role` | Unassign Role |
| `POST` | `/agents/{agent_id}/role` | Assign Role |
| `POST` | `/agents/{agent_id}/service-claims/sync` | Sync Agent Service Claims |
| `GET` | `/artifacts` | List Artifacts |
| `POST` | `/artifacts` | Register Artifact |
| `DELETE` | `/artifacts/{artifact_id_or_digest}` | Delete Artifact |
| `GET` | `/artifacts/{artifact_id_or_digest}` | Get Artifact |
| `POST` | `/backlog-groom/run` | Backlog Groom Run |
| `GET` | `/backlog-groom/status` | Backlog Groom Status |
| `POST` | `/break-glass-authorizations/{authorization_id}/revoke` | Revoke Break Glass |
| `GET` | `/bridge/items` | List Project Items |
| `POST` | `/bridge/items` | Import Project Item |
| `GET` | `/bridge/repositories` | List Project Repositories |
| `POST` | `/bridge/repositories` | Register Project Repository |
| `POST` | `/cicd-monitor/run` | Cicd Monitor Run |
| `GET` | `/cicd-monitor/status` | Cicd Monitor Status |
| `GET` | `/command-audit` | List Command Audit |
| `GET` | `/communication/accounts` | List Communication Accounts |
| `POST` | `/communication/accounts` | Configure Communication Account |
| `DELETE` | `/communication/accounts/{account_id}` | Delete Communication Account |
| `GET` | `/communication/accounts/{account_id}` | Get Communication Account |
| `GET` | `/communication/deliveries` | List Human Messages |
| `POST` | `/communication/deliveries` | Enqueue Human Message |
| `POST` | `/communication/deliveries/claim` | Claim Human Messages |
| `POST` | `/communication/deliveries/{delivery_id}/ack` | Acknowledge Human Message |
| `POST` | `/communication/deliveries/{delivery_id}/fail` | Fail Human Message |
| `GET` | `/communication/gateway-leases` | List Gateway Identity Leases |
| `POST` | `/communication/gateway-leases/acquire` | Acquire Gateway Identity Lease |
| `POST` | `/communication/gateway-leases/{lease_id}/release` | Release Gateway Identity Lease |
| `POST` | `/communication/gateway-leases/{lease_id}/renew` | Renew Gateway Identity Lease |
| `GET` | `/communication/identities` | List Communication Identities |
| `POST` | `/communication/identities` | Configure Communication Identity |
| `DELETE` | `/communication/identities/{identity_id_or_name}` | Delete Communication Identity |
| `GET` | `/communication/identities/{identity_id_or_name}` | Get Communication Identity |
| `GET` | `/communication/representations` | List Representation Bindings |
| `POST` | `/communication/representations` | Configure Representation Binding |
| `DELETE` | `/communication/representations/{binding_id}` | Delete Representation Binding |
| `GET` | `/conversation-threads` | List Conversation Threads |
| `POST` | `/conversation-threads` | Track Conversation |
| `GET` | `/conversation-threads/{thread_id}` | Get Conversation Thread |
| `GET` | `/crash-reports` | List Crash Reports |
| `GET` | `/crash-reports/{report_id}` | Get Crash Report |
| `POST` | `/crash-reports/{report_id}/resolve` | Resolve Crash Report |
| `POST` | `/curiosity-review/run` | Curiosity Review Run |
| `GET` | `/curiosity-review/status` | Curiosity Review Status |
| `GET` | `/curiosity/candidates` | List Curiosity Candidates |
| `POST` | `/curiosity/candidates/{candidate_id}/{decision}` | Decide Curiosity Candidate |
| `GET` | `/dashboard/agents/{agent_id}` | Dashboard Agent |
| `POST` | `/dashboard/agents/{agent_id}/terminal-sessions` | Dashboard Terminal Session Open |
| `GET` | `/dashboard/dispatch/explain` | Dashboard Dispatch Explain |
| `PUT` | `/dashboard/hermes/fleets/{fleet_id_or_name}/config-surface` | Dashboard Hermes Config Surface Update |
| `POST` | `/dashboard/hermes/fleets/{fleet_id_or_name}/config-surface/apply` | Dashboard Hermes Config Surface Apply |
| `GET` | `/dashboard/hermes/{instance_id}/activity` | Dashboard Hermes Activity |
| `GET` | `/dashboard/rollouts/{rollout_id}/status` | Dashboard Rollout Status |
| `GET` | `/dashboard/state` | Dashboard State |
| `GET` | `/dashboard/stream` | Dashboard Stream |
| `GET` | `/dashboard/tasks/{task_id}/timeline` | Dashboard Task Timeline |
| `GET` | `/dashboard/terminal-sessions` | Dashboard Terminal Sessions |
| `POST` | `/dashboard/terminal-sessions/{session_id}/close` | Dashboard Terminal Session Close |
| `GET` | `/dashboard/terminal-sessions/{session_id}/events` | Dashboard Terminal Session Events |
| `POST` | `/dashboard/terminal-sessions/{session_id}/input` | Dashboard Terminal Session Input |
| `POST` | `/dashboard/terminal-sessions/{session_id}/resize` | Dashboard Terminal Session Resize |
| `POST` | `/dashboard/workflow-plan/accept` | Dashboard Workflow Plan Accept |
| `POST` | `/dashboard/workflow-plan/preview` | Dashboard Workflow Plan Preview |
| `GET` | `/diagnostics` | Diagnostics |
| `GET` | `/directive-bindings` | List Directive Bindings |
| `POST` | `/directive-bindings` | Set Directive Binding |
| `GET` | `/directive-waivers` | List Directive Waivers |
| `POST` | `/directive-waivers/{waiver_id}/revoke` | Revoke Directive Waiver |
| `GET` | `/directives` | List Directives |
| `POST` | `/directives` | Propose Directive |
| `GET` | `/directives/effective` | Effective Directives |
| `GET` | `/directives/{directive_id}` | Get Directive |
| `POST` | `/directives/{directive_id}/activate` | Activate Directive |
| `POST` | `/directives/{directive_id}/approve` | Approve Directive |
| `POST` | `/directives/{directive_id}/check` | Check Directive |
| `POST` | `/directives/{directive_id}/deactivate` | Deactivate Directive |
| `GET` | `/directives/{directive_id}/impact` | Directive Impact |
| `GET` | `/directives/{directive_id}/versions` | List Directive Versions |
| `POST` | `/directives/{directive_id}/waivers` | Create Directive Waiver |
| `POST` | `/dispatch/assign` | Dispatch Once |
| `GET` | `/dispatch/dead-letters` | Dead Letters |
| `GET` | `/dispatch/dead-letters/page` | Dead Letters Page |
| `POST` | `/dispatch/tick` | Dispatch Tick |
| `POST` | `/dream/import-logs` | Import Dream Logs |
| `GET` | `/environments` | List Environments |
| `POST` | `/environments` | Register Environment |
| `GET` | `/environments/{env_id}` | Get Environment |
| `GET` | `/environments/{env_id}/current` | Current Deployment |
| `POST` | `/environments/{env_id}/deploy` | Deploy Artifact |
| `GET` | `/environments/{env_id}/deployments` | List Deployments |
| `GET` | `/eval-runs` | List Eval Runs |
| `POST` | `/eval-runs` | Record Eval Run |
| `GET` | `/eval-sets` | List Eval Sets |
| `POST` | `/eval-sets` | Create Eval Set |
| `GET` | `/eval-sets/{eval_set_id}` | Get Eval Set |
| `POST` | `/eval-sets/{eval_set_id}/baseline` | Update Eval Set Baseline |
| `GET` | `/eval-sets/{eval_set_id}/events` | List Eval Set Events |
| `GET` | `/events` | List Events |
| `GET` | `/events/stream` | Stream Events |
| `GET` | `/evidence/{evidence_id}/artifacts` | List Evidence Artifacts |
| `GET` | `/evidence/{evidence_id}/artifacts/{artifact_id}` | Get Evidence Artifact |
| `GET` | `/fleet/build-distribution` | Fleet Build Distribution |
| `GET` | `/fleet/snapshot` | Fleet Snapshot |
| `GET` | `/fleets` | List Fleets |
| `POST` | `/fleets` | Create Fleet |
| `DELETE` | `/fleets/{fleet_id_or_name}` | Delete Fleet |
| `GET` | `/fleets/{fleet_id_or_name}` | Get Fleet |
| `PUT` | `/fleets/{fleet_id_or_name}` | Update Fleet |
| `POST` | `/fleets/{fleet_id_or_name}/observed-agents` | Observe Fleet Agent |
| `POST` | `/github-ingest/run` | Github Ingest Run |
| `GET` | `/github-ingest/status` | Github Ingest Status |
| `GET` | `/health` | Health |
| `GET` | `/humans` | List Humans |
| `POST` | `/humans` | Register Human |
| `GET` | `/humans/resolve` | Resolve Human |
| `DELETE` | `/humans/{human_id}` | Delete Human |
| `GET` | `/humans/{human_id}` | Get Human |
| `GET` | `/integrations/findings` | List Integration Findings |
| `POST` | `/integrations/findings` | Record Integration Finding Endpoint |
| `GET` | `/integrations/observations` | List Integration Observations |
| `POST` | `/leases/{lease_id}/delegate` | Delegate Lease |
| `POST` | `/leases/{lease_id}/renew` | Renew Lease |
| `GET` | `/machines` | List Machines |
| `POST` | `/machines` | Register Machine |
| `GET` | `/machines/{machine_id}` | Get Machine |
| `GET` | `/memory` | Search Memory |
| `POST` | `/memory` | Add Memory |
| `GET` | `/memory/remembered` | List Remembered Memory |
| `POST` | `/memory/remembered` | Remember Memory |
| `DELETE` | `/memory/remembered/{key}` | Forget Memory |
| `POST` | `/memory/summarize-actions` | Memory Summarize Actions |
| `GET` | `/messages` | List Messages |
| `POST` | `/messages` | Send Message |
| `POST` | `/model-selection/promote` | Model Selection Promote |
| `POST` | `/model-selection/refresh` | Model Selection Refresh |
| `GET` | `/model-selection/status` | Model Selection Status |
| `GET` | `/nap-due` | List Due Nap Agents |
| `GET` | `/nap-runs` | List Nap Runs |
| `GET` | `/nap-runs/{run_id}` | Get Nap Run |
| `POST` | `/nap-runs/{run_id}/complete` | Complete Nap |
| `POST` | `/nap-runs/{run_id}/fail` | Fail Nap |
| `GET` | `/nap-schedules` | List Nap Schedules |
| `POST` | `/nap-tick/run` | Nap Tick Run |
| `GET` | `/nap-tick/status` | Nap Tick Status |
| `GET` | `/notifications` | List Notifications |
| `POST` | `/notifications/{notification_id}/delivered` | Mark Notification Delivered |
| `GET` | `/notifier/channels` | List Notifier Channels |
| `POST` | `/notifier/channels` | Configure Notifier Channel |
| `DELETE` | `/notifier/channels/{channel_id_or_name}` | Delete Notifier Channel |
| `GET` | `/notifier/channels/{channel_id_or_name}` | Get Notifier Channel |
| `POST` | `/notifier/deliver` | Deliver Notifications |
| `GET` | `/observability` | List Observability |
| `GET` | `/observability/logs` | List Observability Logs |
| `POST` | `/observability/logs` | Record Observability Log |
| `GET` | `/observability/metrics` | List Observability Metrics |
| `POST` | `/observability/metrics` | Record Observability Metric |
| `POST` | `/observability/prune` | Prune Observability |
| `GET` | `/observability/stream` | Observability Stream |
| `GET` | `/observability/summary` | Observability Summary |
| `GET` | `/openclaw-executions/{execution_id}` | Get Openclaw Execution |
| `GET` | `/openshell/policies` | List Openshell Policies |
| `POST` | `/openshell/policies` | Create Openshell Policy |
| `DELETE` | `/openshell/policies/{policy_id}` | Delete Openshell Policy |
| `GET` | `/openshell/policies/{policy_id}` | Get Openshell Policy |
| `PUT` | `/openshell/policies/{policy_id}` | Update Openshell Policy |
| `GET` | `/openshell/policies/{policy_id}/assignments` | List Openshell Policy Assignments |
| `POST` | `/openshell/policies/{policy_id}/assignments` | Assign Openshell Policy |
| `POST` | `/openshell/policies/{policy_id}/render` | Render Openshell Policy |
| `GET` | `/openshell/policies/{policy_id}/versions` | List Openshell Policy Versions |
| `GET` | `/optimizer/experiments` | List Scientific Experiments |
| `POST` | `/optimizer/experiments` | Create Scientific Experiment |
| `GET` | `/optimizer/experiments/{experiment_id}` | Get Scientific Experiment |
| `POST` | `/optimizer/experiments/{experiment_id}/analyze` | Analyze Scientific Experiment |
| `GET` | `/optimizer/experiments/{experiment_id}/evidence` | Get Scientific Experiment Evidence |
| `POST` | `/optimizer/experiments/{experiment_id}/observe/{task_id}` | Observe Scientific Task |
| `POST` | `/optimizer/experiments/{experiment_id}/pause` | Pause Scientific Experiment |
| `POST` | `/optimizer/experiments/{experiment_id}/promote` | Promote Scientific Experiment |
| `POST` | `/optimizer/experiments/{experiment_id}/start` | Start Scientific Experiment |
| `GET` | `/optimizer/policies` | List Scientific Policies |
| `POST` | `/optimizer/policies` | Create Scientific Policy |
| `GET` | `/optimizer/policies/{policy_id}` | Get Scientific Policy |
| `POST` | `/optimizer/policies/{policy_id}/promote` | Promote Scientific Policy |
| `POST` | `/optimizer/projects/{project}/rollback/{policy_id}` | Rollback Scientific Policy |
| `GET` | `/optimizer/status` | Scientific Optimizer Status |
| `POST` | `/optimizer/tick` | Scientific Optimizer Tick |
| `GET` | `/persona-instances` | List Persona Instances |
| `POST` | `/persona-instances` | Register Persona Instance |
| `GET` | `/persona-instances/{instance_id}/context` | Persona Context |
| `POST` | `/persona-instances/{instance_id}/openclaw-executions` | Begin Openclaw Execution |
| `GET` | `/persona-instances/{instance_id}/runtime-proof` | Persona Runtime Proof |
| `POST` | `/persona-instances/{instance_id}/runtime-proof` | Persona Runtime Proof With Startup |
| `POST` | `/persona-instances/{instance_id}/tasks` | Create Interaction Task |
| `GET` | `/persona-instances/{instance_id}/work-context` | Persona Work Context |
| `GET` | `/personas` | List Personas |
| `POST` | `/personas` | Register Persona |
| `GET` | `/platform-bindings` | List Platform Bindings |
| `POST` | `/platform-bindings` | Register Platform Binding |
| `GET` | `/projects` | List Projects |
| `POST` | `/projects` | Create Project |
| `POST` | `/projects/register` | Register Project |
| `DELETE` | `/projects/{project}` | Delete Project |
| `GET` | `/projects/{project}` | Get Project |
| `PUT` | `/projects/{project}` | Update Project |
| `POST` | `/projects/{project}/dispatch` | Set Project Dispatch |
| `GET` | `/provisioning/requests` | List Provisioning Requests |
| `POST` | `/provisioning/requests` | Create Provisioning Request |
| `GET` | `/provisioning/requests/{request_id}` | Get Provisioning Request |
| `POST` | `/provisioning/requests/{request_id}/cancel` | Cancel Provisioning Request |
| `POST` | `/provisioning/requests/{request_id}/fulfill` | Fulfill Provisioning Request |
| `POST` | `/publications` | Publish |
| `POST` | `/repository-refs/reconcile` | Reconcile Repository Refs |
| `GET` | `/repository-refs/reconciler` | Repository Ref Reconciler Status |
| `GET` | `/review-experiments/{experiment_id}` | Review Experiment Report |
| `POST` | `/reviews/default/tick` | Default Review Tick |
| `POST` | `/reviews/{review_id}/claim` | Claim Review |
| `POST` | `/reviews/{review_id}/decision` | Submit Review |
| `GET` | `/roles` | List Roles |
| `POST` | `/roles` | Create Role |
| `POST` | `/roles/seed` | Seed Roles |
| `GET` | `/roles/{role_id_or_slug}` | Get Role |
| `DELETE` | `/roles/{role_id}` | Delete Role |
| `PUT` | `/roles/{role_id}` | Update Role |
| `GET` | `/rollouts` | List Rollouts |
| `POST` | `/rollouts` | Create Rollout |
| `POST` | `/rollouts/{rollout_id}/advance` | Advance Rollout |
| `POST` | `/rollouts/{rollout_id}/artifact` | Verify Rollout Artifact |
| `POST` | `/rollouts/{rollout_id}/health` | Evaluate Rollout Health |
| `POST` | `/rollouts/{rollout_id}/rescue` | Rescue Rollout |
| `GET` | `/runtime-deltas` | List Runtime Deltas |
| `POST` | `/runtime-deltas` | Propose Runtime Delta |
| `GET` | `/runtime-deltas/{delta_id}` | Get Runtime Delta |
| `POST` | `/runtime-deltas/{delta_id}/promote` | Promote Runtime Delta |
| `POST` | `/runtime-deltas/{delta_id}/reject` | Reject Runtime Delta |
| `POST` | `/runtime-deltas/{delta_id}/validate` | Validate Runtime Delta |
| `POST` | `/runtime-runs` | Create Runtime Run |
| `POST` | `/runtime-runs/{run_id}/complete` | Complete Runtime Run |
| `GET` | `/runtimes` | List Runtimes |
| `POST` | `/runtimes` | Create Runtime |
| `POST` | `/sandbox/rollout` | Roll Out Sandbox Image |
| `GET` | `/secret-audits` | List Secret Audits |
| `GET` | `/secrets` | List Secrets |
| `POST` | `/secrets` | Create Secret |
| `DELETE` | `/secrets/{name}` | Delete Secret |
| `POST` | `/secrets/{name}/resolve` | Resolve Secret |
| `POST` | `/secrets/{name}/rotate` | Rotate Secret |
| `POST` | `/secrets/{secret_id}/access` | Request Secret |
| `POST` | `/secrets/{secret_id}/reveal` | Reveal Secret |
| `POST` | `/self-heal/run` | Self Heal Run |
| `GET` | `/self-heal/status` | Self Heal Status |
| `GET` | `/service-claims` | List Service Claims |
| `GET` | `/service-roles` | List Service Roles |
| `GET` | `/source-convergence` | Source Convergence Status |
| `POST` | `/source-convergence/tick` | Tick Source Convergence |
| `GET` | `/startup/hermes` | Hermes Startup |
| `GET` | `/task-groups` | List Task Groups |
| `POST` | `/task-groups` | Save Task Group |
| `DELETE` | `/task-groups/{name}` | Delete Task Group |
| `GET` | `/task-groups/{name}` | Get Task Group |
| `GET` | `/tasks` | List Tasks |
| `POST` | `/tasks` | Create Task |
| `GET` | `/tasks/audit` | Audit Tasks |
| `POST` | `/tasks/batch` | Apply Task Batch |
| `GET` | `/tasks/generator-yield` | Task Generator Yield |
| `POST` | `/tasks/preflight` | Dispatch Preflight |
| `GET` | `/tasks/ready` | Ready Tasks |
| `GET` | `/tasks/ready/explain` | Ready Task Explanations |
| `POST` | `/tasks/recover-stranded` | Task Recover Stranded |
| `GET` | `/tasks/search` | Search Tasks |
| `POST` | `/tasks/select` | Select Tasks |
| `GET` | `/tasks/stats` | Task Stats |
| `GET` | `/tasks/throughput` | Task Throughput |
| `DELETE` | `/tasks/{task_id}` | Delete Task |
| `GET` | `/tasks/{task_id}` | Get Task |
| `PUT` | `/tasks/{task_id}` | Update Task |
| `POST` | `/tasks/{task_id}/activity` | Append Task Activity |
| `POST` | `/tasks/{task_id}/answer` | Answer Task |
| `POST` | `/tasks/{task_id}/ask` | Ask Task |
| `GET` | `/tasks/{task_id}/break-glass-authorizations` | List Break Glass Authorizations |
| `POST` | `/tasks/{task_id}/break-glass-authorizations` | Authorize Break Glass |
| `POST` | `/tasks/{task_id}/children` | Add Child Tasks |
| `POST` | `/tasks/{task_id}/claim` | Claim Task |
| `GET` | `/tasks/{task_id}/dispatch-explain` | Task Dispatch Explain |
| `POST` | `/tasks/{task_id}/evidence` | Add Evidence |
| `GET` | `/tasks/{task_id}/export` | Export Task |
| `POST` | `/tasks/{task_id}/force-complete` | Force Complete Task |
| `GET` | `/tasks/{task_id}/publication-route` | Task Publication Route |
| `POST` | `/tasks/{task_id}/release` | Release Task |
| `POST` | `/tasks/{task_id}/reopen` | Reopen Task |
| `POST` | `/tasks/{task_id}/review-experiment` | Assign Review Experiment |
| `GET` | `/tasks/{task_id}/review-observation` | Review Observation |
| `POST` | `/tasks/{task_id}/review-outcomes` | Record Review Outcome |
| `POST` | `/tasks/{task_id}/reviews` | Request Review |
| `POST` | `/tasks/{task_id}/start` | Start Task |
| `POST` | `/tasks/{task_id}/submit-for-review` | Submit For Review |
| `GET` | `/tasks/{task_id}/summary` | Task Summary |
| `GET` | `/tasks/{task_id}/transcript` | Get Task Transcript |
| `POST` | `/tasks/{task_id}/transcript` | Record Task Transcript |
| `POST` | `/tasks/{task_id}/transition` | Transition Task |
| `GET` | `/tenants` | List Tenants |
| `POST` | `/tenants` | Register Tenant |
| `GET` | `/users` | List Users |
| `POST` | `/users` | Register User |
| `GET` | `/v1/agents/{agent_id}/agentbus-cursor` | Get Agentbus Cursor |
| `PUT` | `/v1/agents/{agent_id}/agentbus-cursor` | Set Agentbus Cursor |
| `GET` | `/v1/agents/{agent_id}/config-flags` | List Agent Config Flags |
| `DELETE` | `/v1/agents/{agent_id}/config-flags/{flag}` | Clear Agent Config Flag |
| `PUT` | `/v1/agents/{agent_id}/config-flags/{flag}` | Set Agent Config Flag |
| `GET` | `/v1/agents/{agent_id}/continuity` | Get Openclaw Continuity Context |
| `PUT` | `/v1/agents/{agent_id}/deploy-config` | Report Agent Deploy Config |
| `POST` | `/v1/agents/{agent_id}/deregister` | Deregister Agent Route |
| `GET` | `/v1/agents/{agent_id}/effective-config` | Get Agent Effective Config |
| `POST` | `/v1/agents/{agent_id}/memory` | Store Agent Memory |
| `DELETE` | `/v1/agents/{agent_id}/mood` | Clear Openclaw Agent Mood |
| `POST` | `/v1/agents/{agent_id}/mood` | Set Openclaw Agent Mood |
| `GET` | `/v1/memory/dreams/recall` | Recall Dream Artifacts |
| `GET` | `/v1/memory/health` | Memory Health |
| `GET` | `/v1/memory/recall` | Recall Memory |
| `GET` | `/vector-refs` | List Vector Refs |
| `POST` | `/vector-refs` | Record Vector Ref |
| `GET` | `/work-package-certification-jobs/{job_id}` | Work Package Certification Status |
| `POST` | `/work-package-certification-jobs/{job_id}/claim` | Claim Work Package Certification Job |
| `POST` | `/work-package-certification-jobs/{job_id}/ingest` | Ingest Work Package Certification Result |
| `POST` | `/work-package-certification-jobs/{job_id}/run` | Run Work Package Certification Job |
| `POST` | `/work-package-finalizations/{finalization_id}/outcomes` | Record Work Package Finalization Outcome |
| `GET` | `/work-package-integration-batches/{batch_id}` | Work Package Integration Status |
| `POST` | `/work-package-integration-batches/{batch_id}/accept-certification` | Accept Work Package Certification |
| `POST` | `/work-package-integration-batches/{batch_id}/assemble` | Assemble Work Package Integration Batch |
| `POST` | `/work-package-integration-batches/{batch_id}/certification-jobs` | Prepare Work Package Certification Job |
| `POST` | `/work-package-integration-batches/{batch_id}/claim` | Claim Work Package Integration Batch |
| `POST` | `/work-package-integration-batches/{batch_id}/finalize-publication` | Finalize Work Package Publication |
| `POST` | `/work-package-integration-batches/{batch_id}/land` | Land Work Package |
| `POST` | `/work-package-integration-batches/{batch_id}/reject-failed-certification` | Reject Failed Work Package Certification |
| `POST` | `/work-package-outputs/{evidence_id}/verify` | Verify Work Package Output |
| `GET` | `/work-package-pipeline/status` | Work Package Pipeline Status |
| `POST` | `/work-package-pipeline/trigger` | Trigger Work Package Pipeline |
| `GET` | `/work-package-telemetry` | Export Work Package Telemetry |
| `GET` | `/work-package-telemetry/comparable-atomic-outcomes` | Comparable Atomic Execution Outcomes |
| `GET` | `/work-packages` | List Work Packages |
| `POST` | `/work-packages` | Admit Work Package |
| `POST` | `/work-packages/candidates/{candidate_id}/accept` | Accept Work Package Candidate |
| `POST` | `/work-packages/candidates/{candidate_id}/reject` | Reject Work Package Candidate |
| `DELETE` | `/work-packages/{package_id}` | Cancel Work Package |
| `GET` | `/work-packages/{package_id}` | Describe Work Package |
| `PUT` | `/work-packages/{package_id}` | Update Work Package |
| `POST` | `/work-packages/{package_id}/activate` | Activate Work Package |
| `GET` | `/work-packages/{package_id}/activation-readiness` | Work Package Activation Readiness |
| `POST` | `/work-packages/{package_id}/assemble` | Assemble Work Package |
| `POST` | `/work-packages/{package_id}/integration-batches` | Create Work Package Integration Batch |
| `POST` | `/work-packages/{package_id}/pause` | Pause Work Package |
| `POST` | `/work-packages/{package_id}/replan` | Replan Work Package |
| `POST` | `/work-packages/{package_id}/replan-preview` | Preview Work Package Replan |
| `GET` | `/work-packages/{package_id}/telemetry` | Describe Work Package Telemetry |
| `GET` | `/workflows` | List Workflows |
| `POST` | `/workflows` | Create Workflow |
| `GET` | `/workflows/drafts` | List Workflow Drafts |
| `POST` | `/workflows/drafts` | Create Workflow Draft |
| `GET` | `/workflows/drafts/{draft_id}` | Get Workflow Draft |
| `PUT` | `/workflows/drafts/{draft_id}` | Update Workflow Draft |
| `POST` | `/workflows/drafts/{draft_id}/approve` | Approve Workflow Draft |
| `POST` | `/workflows/drafts/{draft_id}/preview` | Preview Workflow Draft |
| `POST` | `/workflows/import-yaml` | Import Workflow Yaml |
| `POST` | `/workflows/preview` | Preview Workflow Definition |
| `GET` | `/workflows/runs` | List Workflow Runs |
| `POST` | `/workflows/runs/tick` | Tick Workflow Runs |
| `GET` | `/workflows/runs/{run_id}` | Get Workflow Run |
| `POST` | `/workflows/runs/{run_id}/cancel` | Cancel Workflow Run |
| `GET` | `/workflows/runs/{run_id}/decisions` | Workflow Run Decisions |
| `POST` | `/workflows/seed` | Seed Workflows |
| `GET` | `/workflows/{workflow_id_or_slug}` | Get Workflow |
| `GET` | `/workflows/{workflow_id_or_slug}/decisions` | Workflow Decisions |
| `POST` | `/workflows/{workflow_id_or_slug}/preview` | Preview Workflow |
| `POST` | `/workflows/{workflow_id_or_slug}/start` | Start Workflow Run |
| `DELETE` | `/workflows/{workflow_id}` | Delete Workflow |
| `PUT` | `/workflows/{workflow_id}` | Update Workflow |
