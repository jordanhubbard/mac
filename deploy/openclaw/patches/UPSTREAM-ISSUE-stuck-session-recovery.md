# Draft upstream issue for github.com/openclaw/openclaw

Status: FILED 2026-07-12 as https://github.com/openclaw/openclaw/issues/105586
(by jordanhubbard). Companion to MAC task task_b6315ed0 and
patch-stuck-session-recovery.py in this directory. Drop the local image patch
once an upstream release ships the fix and the pinned base image is bumped.

---

Title: [Bug]: Stuck-session recovery loops `keep_lane reason=active_reply_work` forever on busy channels — `isActiveRunProgressStale` ignores the terminal progress reasons the detector itself flags (`terminalProgressStale=true`)

## Version

OpenClaw 2026.6.11 (official image, digest `sha256:3814fb1f...a90a61ce7`),
Slack plugin 2026.6.11, Linux + macOS gateways. Still present in current
`diagnostic-stuck-session-recovery.runtime.ts`.

## Summary

When a session's embedded-run marker leaks (the run ended but
`isEmbeddedAgentRunActive()` still reports it active), the stuck-session
watchdog correctly detects the wedge — but recovery declines to reclaim the
lane, forever, on any channel with regular traffic. The agent can still add
reactions but never posts a reply on the affected channel until the gateway
process is restarted.

This is a gap in the recovery machinery added after #71127, sibling to the
`release_lane` no-op in #95248.

## Observed behavior (production logs)

Detector — correctly identifies the contradiction (last progress is a
*terminal* reason, yet work is queued and the run marker claims active):

```
[diagnostic] stuck session: sessionId=06d655d5-... sessionKey=agent:main:slack:channel:c0amsbeu7cj:thread:1783848010.717979
  state=processing age=144s queueDepth=4 reason=queued_work_without_active_run
  classification=stale_session_state lastProgress=run:completed lastProgressAge=252s
  terminalProgressStale=true recovery=checking
```

Recovery — skips with `keep_lane`, 11 consecutive times over ~10 minutes,
until a manual gateway restart:

```
[diagnostic] stuck session recovery outcome: status=skipped action=keep_lane
  sessionId=06d655d5-... activeSessionId=06d655d5-... activeWorkKind=embedded_run
  reason=active_reply_work
```

Meanwhile every queued message runs `REPLY_SKIP` and the channel receives
only ⚠️ reactions, no text.

## Root cause

In `src/logging/diagnostic-stuck-session-recovery.runtime.ts`, the
`active_reply_work` branch gates reclaim on `isActiveRunProgressStale()`:

```ts
function isActiveRunProgressStale(params) {
    if ((params.queueDepth ?? 0) <= 0) return false;
    const lastProgressAgeMs = getDiagnosticSessionActivitySnapshot({
        sessionId: params.sessionId,
        sessionKey: params.sessionKey
    }).lastProgressAgeMs;
    return typeof lastProgressAgeMs === "number" && lastProgressAgeMs >= params.staleAbortMs;
}
```

Two problems compound:

1. It only looks at `lastProgressAgeMs` vs `staleAbortMs` (default
   `STUCK_SESSION_PROGRESS_STALE_MS` = 5 min). It never consults
   `lastProgressReason`, even though the *detector*
   (`classifySessionAttention` / `sessionAttentionFields`) already treats a
   terminal reason (`isTerminalDiagnosticProgressReason`: `run:completed`,
   `embedded_run:ended`, `response.completed` variants) as proof of
   staleness and logs `terminalProgressStale=true`. Detector and recovery
   disagree about the same evidence.
2. `lastProgressAt` is touched by ordinary session activity (queued message
   progress, merges via `resolveSessionActivity`), so on any channel with
   regular traffic the 5-minute idle clock keeps resetting and the reclaim
   threshold is never crossed. The busier the channel, the more permanently
   it stays wedged — inverted from the heuristic's assumption. Quiet
   deployments self-heal after 5 minutes and likely never notice; busy
   multi-session deployments (multiple channels/threads/agent-to-agent
   traffic) hard-wedge.

## Proposed fix

Align recovery with the detector's own contract: in
`isActiveRunProgressStale`, treat a terminal `lastProgressReason` (the same
`isTerminalDiagnosticProgressReason` list) with `queueDepth > 0` as stale,
keeping the idle-age comparison as the fallback for non-terminal wedges:

```ts
function isActiveRunProgressStale(params) {
    if ((params.queueDepth ?? 0) <= 0) return false;
    const snapshot = getDiagnosticSessionActivitySnapshot({
        sessionId: params.sessionId,
        sessionKey: params.sessionKey
    });
    // A terminal last-progress reason with work queued behind the lane means
    // the "active" embedded-run marker is a leftover from a run that already
    // ended — the detector already logs terminalProgressStale=true for this.
    if (isTerminalDiagnosticProgressReason(snapshot.lastProgressReason)) return true;
    const lastProgressAgeMs = snapshot.lastProgressAgeMs;
    return typeof lastProgressAgeMs === "number" && lastProgressAgeMs >= params.staleAbortMs;
}
```

Safety: recovery is already gated by the detector's age threshold, the
`isDiagnosticSessionStateCurrent` generation check (a genuinely advancing
session bumps its generation and recovery skips as `stale_session_state`),
and `queueDepth > 0`. A live run emits non-terminal progress reasons
(`model_call:started`, `tool:*:started`, `embedded_run:started`) within its
first event, so the terminal-reason window cannot describe a healthy run
that is minutes old.

We are running this fix in production (applied as an image-build patch to
the dist bundle) across a 3-gateway fleet; happy to send it as a PR.

## Reproduction sketch

1. Cause an embedded-run marker leak on a session lane (abort/timeout churn
   under concurrent sessions makes this easiest to hit; we see it under
   multi-channel Slack + programmatic agent-turn load).
2. Keep the channel active (a message every <5 min).
3. Watch the detector log `terminalProgressStale=true` and recovery answer
   `keep_lane reason=active_reply_work` indefinitely; all queued messages
   REPLY_SKIP until gateway restart.

## Related

- #71127 (stuck sessions detected but never aborted — the machinery this
  gap lives in)
- #95248 / #95299 (`release_lane` no-op when claim held by live worker —
  sibling gap in the same recovery path)
