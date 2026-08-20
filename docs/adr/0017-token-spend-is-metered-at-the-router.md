# ADR 0017 - Token spend is metered at the router, not reported by the client

- Status: **Proposed**
- Date: 2026-08-19
- Decision owner: MAC fleet owner
- Related: ADR 0003 (tokenhub core into mac), ADR 0013 (one authoritative hub
  allocator)
- Prior art: `NVIDIA-dev/horde-claw-fleet` ADR-0020, ADR-0030, ADR-0053,
  ADR-0067

## Context

MAC records model usage as one observability event per request, `llm.route`,
whose token counts live inside `observability_events.detail` — a `text` column
holding JSON. There is no token table and no token column. Cost is not stored;
`estimate_route_cost()` in `src/mac/scientific_optimizer.py` prices
`resolved_model` against a models catalog at read time and returns
`(cost, was_priceable)`.

Measured over the seven days to 2026-08-19, on 28,352 `llm.route` events:

| Fact | Value |
| --- | --- |
| Input tokens recorded | 481.8M |
| Output tokens recorded | 5.05M |
| Streaming routes | 18,222 (64%) |
| Routes flagged `stream_no_usage` | 5,948 |
| Routes with `input_tokens = null` | 8,352 (**29.5%**) |
| Routes reporting cached tokens | **0** |
| Attributed to `agent` | 17,541 routes / 30.4M input tokens |
| Attributed to `task` | 8,333 routes / 431.1M input tokens |
| Attributed to nothing | 2,474 routes / 20.2M input tokens |

Three defects follow.

### 1. Metering is delegated to the caller, so a third of traffic is unmetered

`src/mac/router_app.py` captures streamed usage from the terminal SSE frame,
and its own comment states the assumption plainly:

> the client asked for include_usage, so the terminal SSE frame carries the
> token counts

That is a pass-through, not a policy. MAC injects
`stream_options: {"include_usage": true}` in exactly one place —
`src/mac/responses_adapter.py:145` — so a coding agent that streams without
requesting usage produces a route MAC cannot meter. The router then records
`input_tokens: null` and `stream_no_usage: true` and moves on. That is 8,352
requests in a week, and they book as **$0**.

This is not a provider limitation. `horde-claw-fleet`'s
`investigations/opus-4-7-telemetry.md` reproduced streaming requests against
`https://inference-api.nvidia.com/v1/chat/completions` — the same endpoint and
the same `provider: "nvidia"` MAC routes through — and confirmed the terminal
chunk carries a full `usage` object, including
`prompt_tokens_details.cached_tokens` and `cache_read_input_tokens`, whenever
`stream_options.include_usage` is set on the request.

MAC is not being denied the data. It is not asking for it.

### 2. Attribution is split across incompatible keys

A route is subject to an `agent`, or to a `task`, or to nothing. The three sets
do not reconcile: task-attributed routes carry 431M of the 482M input tokens,
while agent-attributed routes are 62% of the row count. No query can total
spend per project without either double-counting the overlap or silently
dropping the 2,474 unattributed routes.

`horde-claw-fleet` solved this with a `source` discriminator
(`task | gateway | cron | heartbeat | subagent`) over a nullable `task_id`, so
that non-task inference is explicitly classified rather than left null. Their
ADR-0020 names the same failure we have: *"we can answer 'how many tokens did
task X spend?' but not 'how much did the entire system spend today?'"*

### 3. There is no budget, and the one signal that exists is advisory

Nothing in MAC caps spend. The single guard is a KPI signal,
`high_token_work_without_publication`, which warns at 250,000 tokens with no
publication (`src/mac/services.py:8156`). During the same week that signal had
every reason to fire: 23 open pull requests covered 12 distinct pieces of work,
one task having emitted five divergent implementations. Nothing acted on it.

## Decision

### 1. The router meters; the client cannot opt out

The router injects `stream_options: {"include_usage": true}` on every
OpenAI-compatible upstream streaming request, regardless of what the client
sent. If the client did not ask for usage, the router strips the usage frame
from the response it returns downstream, so client behaviour is unchanged while
the hub still meters.

`stream_no_usage` remains, but changes meaning: it becomes a genuine provider
fault worth alerting on, rather than the expected outcome for two thirds of
traffic.

### 2. Usage is a column, not a substring

Token spend moves out of JSON-in-text into a first-class table with typed
columns: `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `estimated_cost_usd`, `api_calls`, `provider`, `model`,
`source`, `subject_id`, `latency_ms`, `outcome`.

Two properties are non-negotiable, both learned from `horde-claw-fleet`:

- **Cost is computed and stored at write time.** Pricing at read time, as
  `estimate_route_cost()` does now, silently re-prices historical spend
  whenever the catalog changes, so last month's bill moves. Store the cost that
  applied when the call was made.
- **Rows are events and are summed; they are not cumulative snapshots.**
  `horde-claw-fleet` ADR-0053 records a real bug where cumulative per-task
  snapshot rows were `SUM()`ed instead of `MAX()`ed, inflating spend. MAC's
  rows are per-request events, so summation is correct — but the table must
  state which it is, in a comment and in a test, or the same bug arrives later.

### 3. Every route carries exactly one attribution key

`source` classifies the surface (`task`, `agent`, `gateway`, `cron`,
`review`, `subagent`), and `subject_id` names the specific one. A route with no
attributable subject is recorded as `source = 'gateway'` with a null
`subject_id`, never as an unclassified row. "Unattributed" stops being a
silent category and becomes a countable one.

### 4. A daily budget that can refuse

Adopt `horde-claw-fleet`'s shape directly (`fleet-model/src/budget.rs`): a
`DailySpend {input_tokens, output_tokens, estimated_cost_usd}`, a
classification of `allowed | exhausted`, and a decision record carrying
`spent_usd`, `budget_usd` and the classification.

The budget is enforced at dispatch, not mid-stream: a task is not dispatched
once the day's spend is exhausted, and the refusal is recorded with the numbers
that produced it. Killing an in-flight request wastes what was already spent on
it; refusing the next one does not.

## Consequences

- The ~30% of spend currently invisible becomes visible. Reported cost will
  **rise sharply** on the day this lands. That is the correction, not a
  regression, and it should be announced before it lands so nobody reads the
  step change as a runaway.
- Cache accounting starts working. Given a 95:1 input:output ratio, cached
  input is likely a large share of real cost, and every one of those tokens is
  currently priced at full rate.
- `estimate_route_cost()` becomes a fallback for historical rows only. New rows
  carry their own cost.
- A budget that can refuse work can also stall the fleet. The ceiling must be
  operator-set, visible in `mac`, and overridable, or an exhausted budget
  becomes an outage with no obvious cause.

## Alternatives considered

**Keep pricing at read time.** Rejected: it makes historical spend a function
of the current catalog, so the same query answers differently over time.

**Ask clients to send `include_usage`.** Rejected: this is the status quo. It
depends on every coding agent, now and in future, opting into being metered.
Metering must not be the caller's choice.

**A separate gateway telemetry table.** Rejected for the reason
`horde-claw-fleet` rejected it in ADR-0020: it duplicates the schema and forces
every cost query to become a union.
