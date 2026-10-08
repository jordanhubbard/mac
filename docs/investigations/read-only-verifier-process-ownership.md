# Read-only verifier process ownership

The Linux report canary `task_e7953b07d4dbef7d229e2330fb3a4cd1`
failed on its first attempt on 2026-09-16. The report executor completed,
but the independent verifier's connection closed before it wrote its receipt.
The controller rejected the result rather than treating the agent's report
as verification. That behavior should be preserved.

The failure was reproduced in a disposable OpenShell sandbox using the same
qualified runtime image. Before repository execution, the verifier had PID 44
and UID 999; its ancestors were PIDs 1 and 44. The runtime's keepalive was a
separate process, PID 34, with the same UID and cgroup. The cleanup algorithm
excluded ancestors and treated every other same-UID cgroup member as untrusted.
Calling cleanup killed the keepalive and restarted the container. Docker
reported a restart without an OOM kill.

The design error was inferring ownership from process ancestry and cgroup
membership alone. The runtime can legitimately own a sibling process. Process
names would also be an inadequate ownership rule: repository code can launch
another process named `sleep`.

The repair captures runtime process lifetimes when the trusted verifier enters
its freshly provisioned sandbox, before any repository command. Cleanup
preserves those exact PID/start-time pairs and still removes new same-UID
cgroup processes, including detached and double-forked children. A recycled
PID does not inherit the exemption. The snapshot must never be captured in
the coding agent's sandbox or refreshed after bootstrap.

The Git-control digest, protected-input watches, independently provisioned
verification sandbox, fresh result writer, and signed receipt requirements
remain in place. Source tests cover runtime siblings, misleading process names,
PID reuse, unreadable identities, and capture ordering. A disposable live check
also proved that the candidate preserves the real OpenShell keepalive while
removing a detached child.

Exercising the complete verifier then exposed a second cleanup assumption:
`git clean -fdX` had already removed the ignored `.venv` directory, but cleanup
of the declared `.venv/bin/python` output treated its missing parent as a
verification failure. Missing parents now mean that the output is already
absent; existing symlink and non-directory parents still fail closed. The
regression uses a real Git checkout with ignored, nested declared outputs.

These checks are development evidence; completion
still requires the full gate, deployment of the qualified runtime, and a
successful retry of the original report canary.
