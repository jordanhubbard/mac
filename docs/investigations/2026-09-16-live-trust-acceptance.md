# Live request and recovery acceptance, 2026-09-16

The original ten-task cohort and all three workers' coding checks on the final deployed runtime are accepted. Conflict recovery required operator assistance. Slack provider configuration was repaired and directly verified; the next scheduled channel broadcast remains an independent pending observation, described below; this report does not claim hands-off fleet reliability.

## What was deployed

The three static workers ran MAC source `03fcd3db0a4ffbf5a28cc20ee0089719851cac35` with MAC and Hermes on Python 3.14.7. The Linux verifier image was `ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:1d4c5eb635c5fc53737b89949e22e04246870d57fa92f6b29547395716ea745d`. Verification stayed inside Linux OpenShell; no OpenShell runtime was deployed on macOS.

The final qualified deployment transaction finalized, its hub epoch committed, and all three workers returned healthy with their rollout holds cleared. The original code canaries and Rocky report ran after the earlier `3e498a6b` rollout; Bullwinkle's repaired report ran after `03fcd3db`. Five ordinary canaries completed and were separately accepted: LRU behavior, trie behavior, token end offsets, Rocky's read-only report, and Bullwinkle's read-only report. The last report passed 99 tests and mypy across 11 source files at canary revision `45f9ca7e6d45145bab881652f5571712291fd7cc`.

Eight project pauses owned by this rollout were restored after those five acceptances. The user's activation of nanolang and pre-existing pauses were preserved. Recovery experiments did not extend those project pauses.

The final-revision execution audit requires code execution after the last runtime rollout: Natasha's failed-test repair and Bullwinkle's corrected conflict canary supply it; Rocky's separate token-whitespace canary also completed and was accepted. Task `task_23eed90a65674b3b9cd9bf4f93b1c837`, executor `ev_6660f07d4b2041bc87c8f582f11efea3`, independent review `review_0edcb4a52d3c45e6889a72a1bc7e5805` and publication `pub_0353fd1ad4934bc6ad8654fa96a7be93` produced PR72 at `89fdc17110aa96e09e64364b3ed0ed03f627a5ee`. Independent Linux verification passed 104 tests, mypy across 11 files, exact token end offsets across surrounding whitespace and token immutability. Its task-owned authoring and verifier sandboxes were absent after completion. Earlier accepted results remain valid for their observed versions and are not relabelled as later execution.

## Live recovery evidence

| Case | Observed fault or boundary | Result |
|---|---|---|
| Missing input | Original task parked at zero attempts; operator supplied the exact expression ` 12.5 + 3` through the public answer command. | Accepted. Report preserved the leading space and returned offsets 1, 6, and 8. Full 99-test contract, signed review and report publication passed; independent Linux behavior check matched; sandbox removed. |
| Worker loss | One SIGKILL to Bullwinkle's worker service at 13:03:39 UTC during the exact task sandbox's observed 45-second pause. | Accepted. Systemd restarted after five seconds; a new execution resumed the still-valid original lease and attempt. Full 99-test contract and report publication passed, with one current executor result and one publication. No manual restart, forced expiry or task reopen was required. Both task sandboxes were removed. |
| Failed tests | Deliberate assertion at the start of LRUCache.get caused 6 failures and 93 passes. | Accepted. Initial rejection produced no push, PR or publication and left canonical main unchanged. After explicit repair authorization, the original task passed the unchanged contract and merged PR67 at `92419dcf5a0ef4e97b62b37f33d87d60d7006f7f`. Independent Linux verification passed 100 tests, mypy across 11 files and the missing-get eviction-order check; task sandbox and lease were cleared. |
| Publication conflict | Both workers prepared `92419dcf` and added different regressions to the same absent file. B merged; the gate refused A's actual add/add conflict. | Accepted with operator assistance. Independent-agent repair PR70 preserved both original bodies at `062e30ee`; original A was rejected for omitting the requested scenario and corrected in attempt 2. Its old PR69 initially stranded publication; the operator closed that superseded PR and requested normal gated publication of the current reviewed evidence. PR71 merged `aff777da2f981bf995fbacf68ea9479d3f248d4c`. Independent Linux verification passed 103 tests, mypy across 11 files and both exact requested scenarios. Both original results were accepted; leases and task sandboxes cleared. |

Conflict recovery was assisted, not hands-off: the operator rejected A, staged a clarification of the original requirements with a dependency on the integration child, reopened the original task, accepted the independently checked child, released A after the child completed, closed superseded PR69, and invoked the public publication command for the current independently reviewed evidence after its recorded backoff expired. No task was force-completed and no publication gate was bypassed. The child also recovered a transient `merge_queue_slot_lost` through its ordinary bounded retry.

The resumed worker did not itself observe the earlier process loss. Operator signal receipts, the service journal, replacement process identity and the later execution transcript establish the interruption and recovery. A model's narrative is not a substitute for those observations.

Current executor evidence accepted for missing input: `ev_101719e6189f49e6b066ff71b81292e7`. Worker-loss evidence: `ev_5c8c4f7e683a4ccf9897b50b7bb73491`. Accepted repaired-test evidence: `ev_222dbd2486524fba95aae17c5450dcdd`. Deliberately failed test evidence retained: `ev_63c9af124b7f4f94a577e8fc2a098940`. The operational parent is `task_de8195a73b6d448bb8bf286abaf4576e`; its ledger holds the deployment, independent checks, fault receipts and acceptance checkpoints.

## What the evidence says about the design

The strongest decisions are worth preserving: PostgreSQL holds durable task and lease history; an actual failed repository gate prevents publication; review and verification remain independent of authoring; and operator acceptance is attached to the current evidence rather than inferred from a green test. The live conflict gate also preserved canonical main and assigned repair to an agent other than the original author. Those are useful boundaries, not obstacles to bypass.

The remaining weakness is repeated interpretation of the same execution facts. The recovery task's human-readable brief substituted publication-time main for the reviewed attempt base and named a MAC-specific test script, while its structured repository contract still held the correct command. The flow view likewise used a resettable attempt counter where history derivation counted successive claims. Both defects create incompatible views without losing the underlying authoritative records. Repair should consume those existing records consistently; neither a new ledger nor a replacement runtime is justified by these observations.

The next correction should bind PR reuse to the actual reviewed branch: it directly blocked a valid result in this run. Then make generated repair context and attempt analytics consume the existing evidence and history consistently. Use the demonstrated multi-lease retry as the acceptance scenario. Adding another retry loop or publication owner would preserve the ambiguity that caused the failure.

The user-facing acceptance work also covers the shipped console: obsolete request responses cannot overwrite a newer task selection, response-body reads have deadlines, and CLI recovery handoffs remain explicit. The exact-source UI suite and live asset byte checks support those claims; they do not establish a rendered browser walkthrough. Live capacity reporting correctly showed zero executable idle workers while virtual operator/reviewer records were idle, matching the allocator's rejection of the queued integration task.

## Scope and limits

Automated exact-source UI tests and byte comparisons established that the shipped console assets matched the qualified source; a rendered browser walkthrough is not claimed. Tests, operator acceptance, publication and linked deployment remain separate outcomes. Report publication does not imply application deployment.

At the September 16 15:06 UTC checkpoint, the fixed original ten-task cohort had 10 accepted completions out of 10. Median creation-to-completion time was 46,215 seconds (12.84 hours); the ledger reported 15 interventions under its narrow definition. The integration child and additional final-revision canary are tracked separately, without changing that original denominator. Creation-to-completion time includes deliberate staging holds, rollout waits and controlled recovery interventions; this experiment does not measure ordinary dispatch latency or establish a production service level. Costs without observed priced routes remain unknown. Public intervention counts cover only their documented ledger events; the deliberate service signal and other external operator actions are separately recorded and must not disappear from the interpretation.

| Original case | Task | Creation to completion | Accepted |
|---|---|---|---|
| Bullwinkle LRU code | `task_883bd8474136bcf15b83fb258c00ad55` | 12.4 h | Yes |
| Natasha trie code | `task_62896378399a0e2a8086962919e01907` | 12.52 h | Yes |
| Rocky token-end code | `task_f569f8a6f6372704d432921ff98cc14f` | 12.66 h | Yes |
| Rocky report | `task_00e09b814320a53699e442aa2debcbdd` | 12.79 h | Yes |
| Bullwinkle report | `task_e7953b07d4dbef7d229e2330fb3a4cd1` | 17.28 h | Yes |
| Missing input | `task_7b3fd89cace7eae478d489aa9b06ab80` | 13.13 h | Yes |
| Failed-test repair | `task_5ca714452342d39347c07baa47d4419d` | 12.88 h | Yes |
| Worker loss | `task_576efd8fec045301627f53b4ffc75c36` | 12.24 h | Yes |
| Conflict A correction | `task_e0fa0a06f7b20256f346d9530f7031c9` | 14.14 h | Yes |
| Conflict B | `task_210c56508f490a2ceb5bc4d157a0639e` | 13.18 h | Yes |

The hourly generator yield gate remains in place. These results do not justify removing PostgreSQL authority, lease fencing, signed evidence, independent verification or bounded task generation.

## Known limitations and follow-up work

The deploy controller's initial drain/compensation failure remains recorded under `task_f06257ed57ae4611a2b272447fe836dd`. The successful retry waited for actual task and verifier quiescence; it did not lower readiness checks.

Native authoring needs a complete handoff of the approved Linux image, writable workspace and task-owned cleanup (`task_4fd62a58b37143a49c2796cd9e6da196`). Failed exploratory sandboxes were removed only after owner and workspace checks.

A valid nested report can still acquire a bootstrap line as its short summary (`task_bafad98f86fad5886c245523b01b6cb8`). Substantive report evidence was inspected directly for acceptance; the short-summary presentation defect remains distinct.

The final live audit found provider configuration drift despite connected Slack gateways. Natasha and Bullwinkle retained rejected custom-provider credentials; Rocky also retained endpoint/provider environment differences. The installer correction landed separately as MAC PR856 (`5439b5e7`). Under operational task `task_a777c53459f34314a82862dbd874e5d5`, the operator used the existing `mac.hermes_chat_config` owner to synchronize each installed Hermes profile from its deploy-authoritative environment, remove one stale custom credential-pool entry per host, and restart only its existing chat gateway. Protected backups were retained on each host; persona files were preserved and code workers were not restarted. All three selected Hermes runtimes then resolved credentials without explicit key/base overrides and returned the expected model-generation response with HTTP 200. Each new gateway writer reported Slack connected. This verifies provider generation and gateway reconnection, not a human-authored Slack round trip. Deployed MAC source remains the qualified `03fcd3db`; this configuration repair is not presented as a rollout of PR856's new installer code.

Rocky's explicit channel-only cron routing fix is deployed. The September 16 18:00 Pacific scheduled delivery has not yet been observed; no unsolicited test broadcast was sent.

Flow analytics disagrees with raw history after a public reopen resets the current attempt counter (`task_99c6db349bc94cdf807acc7042a7e015`). The failed-test case has a correctly retained failure followed by a successful new lease, while the flow view incorrectly shows completed attempt 1 and pending attempt 2. Acceptance uses actual history, current signed evidence, review and canonical proof; the derived attempt labels are not reliable evidence of ongoing work.

Conflict repair context needs to consume the reviewed base and declared project test command (`task_b169d5b48fcb449ca0e980c877facaf4`). The live repair task carries the correct structured contract but a contradictory brief and misleading landed-since provenance. The integration child nevertheless completed and was independently accepted; context defects are recorded separately from that successful result.

PR reuse also conflates stable task identity with lease-specific branch identity (`task_d5f50ff10c604c32bbe60b0c2afd677e`). The worker attached the old PR69 to its corrected result; the hub detected a branch mismatch but its fallback performed the same lookup and reused PR69 again. Closing the superseded PR is a recorded operator intervention. It does not establish that retries reconcile PR identity automatically.
