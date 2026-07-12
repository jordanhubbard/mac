#!/usr/bin/env python3
"""Patch OpenClaw's stuck-session recovery to honor terminal progress reasons.

Upstream bug (MAC task task_b6315ed0): the stuck-session DETECTOR classifies a
session whose last progress reason is terminal (run:completed /
embedded_run:ended / response.completed variants) as stale and logs
``terminalProgressStale=true`` — but the RECOVERY module's
``isActiveRunProgressStale`` ignores the reason and only compares
``lastProgressAgeMs`` against a 5-minute idle threshold. Channel chatter and
abort-settle events keep resetting that clock, so on a busy channel a wedged
lane hits ``keep_lane reason=active_reply_work`` forever and the agent can
react (emoji) but never reply until the gateway is restarted.

Fix: teach ``isActiveRunProgressStale`` the same terminal-reason contract the
detector uses (``isTerminalDiagnosticProgressReason``): a terminal last
progress reason with work queued behind the lane means the "active" embedded
run marker is a leftover from a run that already ended — reclaim it.

The patch is an exact-match replacement and FAILS THE IMAGE BUILD when the
expected source is missing (e.g. after an upstream base-image bump), so drift
surfaces loudly instead of silently dropping the fix. Idempotent: re-running
on a patched tree is a no-op success.
"""

from __future__ import annotations

import glob
import sys

TARGET_GLOB = "/app/dist/diagnostic-stuck-session-recovery.runtime-*.js"

ORIGINAL = """function isActiveRunProgressStale(params) {
\tif ((params.queueDepth ?? 0) <= 0) return false;
\tconst lastProgressAgeMs = getDiagnosticSessionActivitySnapshot({
\t\tsessionId: params.sessionId,
\t\tsessionKey: params.sessionKey
\t}).lastProgressAgeMs;
\treturn typeof lastProgressAgeMs === "number" && lastProgressAgeMs >= params.staleAbortMs;
}"""

PATCHED = """function isActiveRunProgressStale(params) {
\tif ((params.queueDepth ?? 0) <= 0) return false;
\tconst snapshot = getDiagnosticSessionActivitySnapshot({
\t\tsessionId: params.sessionId,
\t\tsessionKey: params.sessionKey
\t});
\t/* MAC patch (task_b6315ed0): a terminal last-progress reason with work
\tqueued behind the lane means the active embedded-run marker is a leftover
\tfrom a run that already ended (the detector logs terminalProgressStale=true
\tfor exactly this). Without this check, chatter keeps resetting
\tlastProgressAgeMs and a wedged lane on a busy channel never recovers. */
\tconst reason = snapshot.lastProgressReason;
\tif (typeof reason === "string" && (reason === "run:completed" || reason === "embedded_run:ended" || reason.includes("response.completed") || reason.includes("rawResponseItem/completed") || reason.includes("raw_response_item.completed") || reason.includes("output_item.done"))) return true;
\tconst lastProgressAgeMs = snapshot.lastProgressAgeMs;
\treturn typeof lastProgressAgeMs === "number" && lastProgressAgeMs >= params.staleAbortMs;
}"""

PATCH_MARKER = "MAC patch (task_b6315ed0)"


def patch_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if PATCH_MARKER in text:
        return "already-patched"
    count = text.count(ORIGINAL)
    if count != 1:
        raise SystemExit(
            "patch-stuck-session-recovery: expected exactly 1 match of "
            "isActiveRunProgressStale in %s, found %d — upstream changed; "
            "re-derive the patch before shipping this image" % (path, count)
        )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(ORIGINAL, PATCHED))
    return "patched"


def main() -> None:
    pattern = sys.argv[1] if len(sys.argv) > 1 else TARGET_GLOB
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise SystemExit(
            "patch-stuck-session-recovery: no files match %s — upstream "
            "layout changed; re-derive the patch" % pattern
        )
    for path in paths:
        print("%s: %s" % (path, patch_file(path)))


if __name__ == "__main__":
    main()
