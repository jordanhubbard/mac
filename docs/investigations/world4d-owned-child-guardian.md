# World4D Linux owned-child guardian

This candidate is inert source awaiting independent review and approved Linux
OpenShell execution. It is not a qualified or deployed process-safety repair.
The two original lifecycle cases and their deadlines remain unchanged. The
clean FIFO adapter is a separate delta; it does not implement another queue.

## Ownership and launch

All four Linux owned launches (storage `psql`, world daemon, snapshot renderer,
and management RPC) use `OwnedChild`. Other platforms retain their original
subprocess/group/watchdog paths. Service preparation copies the standalone
stdlib guardian beside service/presence; non-Linux caller imports do not require
the helper, and the helper itself remains importable without fcntl for mocked
controls. Actual guardian launch requires Linux descriptor control. Management
installation resolves its
payload relative to its manifest and copies client/helper there. Same-file
reinstallation is idempotent. Validation precedes configuration changes.

The observing parent owns one control writer and one status reader, protected
by a per-handle lifecycle lock. Parent poll/wait/communicate may reap the keeper
without changing stop authority. Parent stop, including failure recovery,
never signals a numeric PID or process group. The caller-visible PID remains
the **payload** PID so existing World4D monitoring/restart behavior is retained;
it is diagnostic, never the handle's stop authority.

The keeper is a separate isolated (`-ISu`) interpreter executing this standalone
stdlib file. Environment values remain actual `Popen(env=...)` values and are
inherited by payload exec; no environment mapping or payload arguments appear
in the helper's command line. Original arguments remain parent-local so
TimeoutExpired retains the original command, requested timeout and partial
stdout/stderr. Bounded communicate slices observe keeper failure promptly even
with an unlimited/long operation timeout, without shortening healthy operation.
Configuration uses a private pipe with a bounded
64MiB record and one shared startup deadline. Helper argv contains only its
fixed mode and owned FD numbers. Payload stdin is never the configuration
channel. The keeper verifies single-threaded execution, SIGCHLD default,
ordinary libc subreaper admission, initially empty own-child membership and
ECHILD before forking anything. Failure of prerequisites launches no payload.

Before payload exec, every non-stdio FD except the CLOEXEC exec-error pipe is
closed, including control/liveness/status/telemetry. The keeper closes its own
stdio copies after fork. The original supervisor-liveness reader belongs to
the keeper; the Linux payload uses a private entrypoint that omits the competing
watchdog thread. That entrypoint is not a permission, identity or custody proof;
the keeper's successful kernel admission/fork establishes actual custody.
The standalone/non-Linux watchdog remains unchanged.

## Stop, adoption and final drain

The first stop latches one global monotonic time. TERM ends at t0+5s; KILL ends
at t0+10s, across repeated requests and every adopted generation. FORCE or
supervisor/control EOF advances to KILL without extending either deadline.
There is no tighter FORCE+5s cap. A saturated initial stop write escalates by
closing the sole owned writer; FORCE cannot be silently dropped on EAGAIN.
EOF descriptors are removed from the selector to avoid a ready-FD busy loop.

The single-thread keeper reads only its own kernel child list and verifies each
PID with waitid WNOWAIT and Linux __WALL. A fresh phase/deadline/reap-state
callback runs after each potentially blocking ownership observation immediately
before the actual positive/group send; crossing t0+5 selects KILL and crossing
t0+10 prohibits a send. Final-reap completion also cannot emit late DRAINED. No child is reaped while signaling is
possible. Successful group signaling, EPERM and ESRCH all require fresh
adopted-child accounting. Positive signals are sent only to still-waitable own
direct children, checked again immediately before each send. Retaining their
waitability pins PID identities; UID/start-time guesses, peer scans, pidfds,
raw syscalls and policy changes are not used. Group authority is anchored in
the unreaped setsid payload, never a caller-supplied group identifier. A private
fork-stub acknowledgement after successful setsid establishes the group proof;
exec-pipe EOF alone does not. Interrupted admission without that proof uses only
verified direct-child cleanup, never numeric group recovery.

Final reap requires two coherent membership/terminal observations. Its state
transition is permanent: no further numeric signal is possible, even after a
reap error. Every reaped identity/status must match; final ECHILD and an actual
integer payload exit status are required before DRAINED. Ready/drained/status
messages travel on a separate bounded channel; payload stdout/stderr contain
only payload data. Parent stream/stop waits use remaining budget. Unexpected
EOF or an unproven drain is explicit failure and prevents blind restart.

Postlaunch failure is sticky. The keeper reports failure, closes input watches,
and can finish validated cleanup only inside the already-latched original
budget. A denied positive signal disables unchanged retries. After deadline it
only re-observes at a bounded rate. It neither self-exits abandoning potentially
live children nor fabricates DRAINED. Kernel-stuck/unobservable jobs can retain
the keeper indefinitely; no bounded parent return can guarantee kernel progress.
A partial/unproven final reap retains failed custody rather than reusing PID
identities. Proven full drain followed by a telemetry/status error may exit as
failure, never DRAINED success. Keeper crash itself remains an explicit safety
limit: parent has no numeric recovery authority and must not blindly restart.

This implementation is for bounded controlled jobs. Membership is capped at
4096 retained children; normal observation forces controlled-job shutdown at
half that count, before continued long-lived churn can grow without bound.
Exceeding the readable bound fails closed; a hostile forking workload can still
outrun observation and remains an explicit failed-custody/resource limit. No
hidden background reaper defeats identity pinning. Ordinary fork/exec/adoption is the
runtime qualification target. __WALL flag selection and ownership refusal have
mocked source tests; custom-clone/CLONE_PARENT/thread exit modes are **not**
claimed as live-qualified by the earlier ordinary-fork capability report.

## Private diagnostics and verification

World and snapshot callers preopen a unique mode0600 telemetry sink in their
private runtime directory. Payloads inherit no sink copy. Fields are fixed:
phase, operation enum, errno number, retained/observed child and terminal
counts, original deadline and payload-status-present. No arguments, environment,
paths, arbitrary exception strings or credential values are recorded. Counts
refer to the keeper's retained custody snapshot, not an assertion about peers.
Repeated primitive/errno failures are deduplicated; membership/terminal changes
and bounded signal transitions supply the useful causal observations without
an unchanged-error log flood. SQL/RPC use the same control machinery and can
be given an explicit telemetry directory by callers; no credential-bearing
connection diagnostics are added.

New tests include portable mocked ownership/FD/protocol/deadline controls, real
Linux forced group EPERM/ESRCH in the **executed helper copy**, detached nested
TERM-ignoring listeners, already-exited payload, foreign sibling survival,
supervisor SIGKILL/liveness EOF, stdio/exit/timeout, configuration beyond pipe
capacity, and real standalone install/reinstall. They have NOT run at source
handoff. The original tests are byte-identical; neither skips nor deadlines
were changed. Both generators, lint, real pinned WebMux CLI and FULL prescribed
contract in the approved Linux OpenShell runtime remain mandatory after exact
source approval. Source writing is not acceptance of primitive safety or FIFO
admission; deployment/canary/GPU admission remain separate ROOT decisions.

The earlier crash artifacts lacked the failed primitive/errno and therefore do
not prove a particular crash cause. This candidate removes the competing Linux
watchdog path and supplies safe causal telemetry without claiming an unmeasured
failure mechanism. Raw historical stderr/ps/provider outputs remain private and
are not included in source artifacts.

## Canonical versus uploaded baseline

A normal OpenShell source upload creates a fresh Git baseline commit. Its HEAD
SHA can differ from the canonical commit while the complete tree is identical.
Compare the actual uploaded baseline tree to the prepared canonical **tree**,
retain both commit identities and verify worktree cleanliness. A different HEAD
alone is not a source mismatch. The canonical source artifact retains real
canonical ancestry; a verifier applying its reviewed patch to an uploaded
baseline must verify the base tree/preimages and resulting manifest, not invent
ancestry or downgrade current main. This is a source-identity rule, not a waiver
for dirty files, changed dependencies, a different actual tree or mandatory gates.
