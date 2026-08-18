!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground Truth: dream finding `dreamrepair:71b00e8122761c2caeacd04c7ed3f49c` (slack display-label trace)

**Task**: Read-only investigation tracing the dream-repair candidate
`dreamrepair:71b00e8122761c2caeacd04c7ed3f49c` through the MAC hub. Determine
what the `slack` provider label actually names, what evidence backs the finding,
and whether it points at a concrete, reproducible Slack failure worth repairing.

**Prepared by**: fleet worker (investigation node; no production code, test,
skill, config, or deploy edits).

## Verdict: NOT ACTIONABLE — `slack` is a display label, the sole record is a planning success, and the chain is self-referential

The candidate carries the provider label `slack` only because a plain
word-boundary regex (`\bslack\b`, confidence `0.35`, evidence_count `1`) matched
the UI advertisement text
`openclaw · gateway · delegate for MAC Hive · openshell · slack + telegram · verified`
composed in `ide/src/components/agentFacts.ts`. That text is a rendered channel
projection, not a Slack transport, chat-gateway runtime, or persona-binding
failure. The single supporting record is a `plan_decomposed` artifact — a
*planning success*, not a failure — and the provenance chain loops back through
prior read-only investigations without ever reaching a reproduced defect. There
is nothing to fix; the correct disposition is to close the finding as NOT
ACTIONABLE.

No files under `src/mac/`, `tests/`, `skills/`, `.mac/`, or `deploy/` were
modified by this investigation.

## The candidate -> evidence chain

The trace runs from the candidate finding back through two ancestor tasks to a
static UI string:

1. **Candidate** `dreamrepair:71b00e8122761c2caeacd04c7ed3f49c` — a low-confidence
   `slack` finding (confidence `0.35`, evidence_count `1`).
2. **Parent evidence** = the `plan_decomposed` output of
   task `task_39cc8eaa9caf427a878e66d439355c29` — itself a `slack` dream
   investigation. `plan_decomposed` records the successful decomposition of a
   plan into child nodes; it is a *planning success* signal, not a failure
   observation.
3. That plan-decomposition **points to**
   task `task_02d09d23d68940e8a545d8261f3afa53` — a completed, read-only MAC IDE
   frontend defect investigation (already terminal, no live defect to carry
   forward).
4. The `slack` provider label itself originates **only** from the regex
   `\bslack\b` matching the advertisement text
   `openshell · slack + telegram · verified` in
   `ide/src/components/agentFacts.ts`.

Every hop in the chain is either a planning-success artifact or a completed
read-only investigation. No hop introduces an independent, reproduced Slack
failure.

## `slack` is a display label, not a provider failure

The matched string is assembled by `chatGatewayLabel` in
`ide/src/components/agentFacts.ts:52`. The word `slack` enters that label as an
*enabled channel key*, not as a failing subsystem:

- Enabled chat-gateway channels are read from `gateway.channels` and filtered to
  the ones with `enabled === true` (`ide/src/components/agentFacts.ts:62`).
- Those channel *names* are joined with `" + "`
  (`ide/src/components/agentFacts.ts:77`), producing `slack + telegram` for a
  fixture whose gateway advertises both channels enabled.
- The channel segment is then joined with the other advertisement fields using
  `" · "` (`ide/src/components/agentFacts.ts:79`), yielding
  `openclaw · gateway · delegate for MAC Hive · openshell · slack + telegram · verified`.

So `slack` is the *name of an advertised channel* rendered into a verified
service advertisement string. The IDE frontend investigation that this chain
descends from already reached the same conclusion: its `AGENT_MESH_FINDINGS.md`
"Slack advertisement projection" note statically traces `chatGatewayLabel`
against the same fixture and records the verdict "already-correct (not a real
defect)" — the projected string is byte-for-byte identical to the contract
expectation exercised by
`ide/tests/workbench-project-tree.spec.ts`. There is no Slack transport or
provider execution path implicated anywhere in the match.

## The sole record is a planning success, not a failure

The finding's single supporting artifact is a `plan_decomposed` record. That
record type marks a plan node being successfully broken into child work — it is
emitted on the *happy path* of decomposition. Treating a planning-success
artifact as failure evidence for a `slack` provider defect is a category error:
the record contains no error signature, no failing test id, no stack trace, and
no Slack runtime observation. Its presence says only that a plan was decomposed,
not that anything broke.

## Self-referential loop

The chain never escapes its own investigation lineage:

- The candidate's parent is a `slack` dream investigation
  (`task_39cc8eaa9caf427a878e66d439355c29`), whose `plan_decomposed` output is
  the "evidence".
- That output points to another completed read-only investigation
  (`task_02d09d23d68940e8a545d8261f3afa53`).
- The `slack` label itself is re-derived each time from the *same* static UI
  string via the *same* bare-token regex.

The pattern therefore re-derives itself from prior dismissals and a single
static advertisement string. It adds zero net-new signal about any real defect,
and its `0.35` confidence is the classifier's single-record structural floor (an
evidence-volume signal), not a corroborated fault.

## Evidence gap (state in the disposition)

- **One low-confidence record.** evidence_count `1`, confidence `0.35`; the lone
  artifact is a `plan_decomposed` (planning success), not a failure record.
- **No reproduced Slack failure.** The signal is a `\bslack\b` word-boundary
  match against a rendered UI advertisement string, not an error signature,
  failing assertion, or reproducer. `slack` names an enabled *channel* in a
  verified advertisement, not a provider that failed.
- **Self-referential loop.** The provenance chain runs candidate -> `slack`
  investigation `plan_decomposed` -> completed read-only IDE-frontend
  investigation -> the same static `agentFacts.ts` string, with the label
  re-derived from that string on each pass.

## Actionability decision and reopen criteria

- **Decision**: NOT ACTIONABLE — close
  `dreamrepair:71b00e8122761c2caeacd04c7ed3f49c`.
- **No repo change warranted** in the source: the advertisement projection is a
  healthy, contract-covered display string; the worktree is clean; no Slack
  runtime is named beyond the channel token in the rendered label.
- **Reopen criteria**: reopen only if a *named* Slack transport, chat-gateway
  runtime, or persona binding acquires a reproducible failure signature (a real
  error, stack trace, or failing test) backed by at least two independent,
  non-self-referential evidence records that are not `plan_decomposed`/planning
  artifacts.

## Verification performed (read-only)

- Traced the candidate -> parent `plan_decomposed` -> pointed-to completed
  investigation chain from the task description's provenance fields.
- Confirmed the `slack` label originates from `\bslack\b` matching the
  advertisement text in `ide/src/components/agentFacts.ts` and statically
  followed `chatGatewayLabel` (`ide/src/components/agentFacts.ts:52`,
  `ide/src/components/agentFacts.ts:62`, `ide/src/components/agentFacts.ts:77`,
  `ide/src/components/agentFacts.ts:79`) to reproduce
  `openclaw · gateway · delegate for MAC Hive · openshell · slack + telegram · verified`.
- Cross-checked the existing IDE-frontend note
  (`ide/AGENT_MESH_FINDINGS.md`) which independently reaches the "already-correct
  (not a real defect)" verdict for the same projection.
- Confirmed `git status --porcelain` on the worktree is clean; no `src/`,
  `tests/`, `skills/`, `.mac/`, or `deploy/` file was edited by this audit.
