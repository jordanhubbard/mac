---
id: mac-l6o0
status: in_progress
deps: []
links: []
created: 2026-05-28T00:55:42Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-l6o0
---
# install-tokenhub-service.sh: clean up prior-mode unit on supervisor-mode drift

Original premise (orphan tokenhub) was wrong. TokenHub on rocky is actually running under /etc/systemd/system/mac-tokenhub.service (enabled, active, healthy). PID's PPid=1 is normal for Type=simple systemd services, not an orphan signal.

Real problem: install-tokenhub-service.sh's detect_supervisor() picks mode dynamically:
  1. systemd-user if "systemctl --user is-active tokenhub.service" returns true
  2. else systemd if /run/systemd/system exists
  3. else launchd, supervisord

On rocky:
- May 10 install: chose systemd-user, wrote ~/.config/systemd/user/tokenhub.service
- May 27 install: chose systemd, wrote /etc/systemd/system/mac-tokenhub.service

The May 27 pass took over TokenHub correctly but left the May 10 user-level unit behind in a disabled state. deploy-state.json was also not updated; it still says mode=systemd-user even though the system unit is now authoritative.

Why it only shows on rocky: natasha runs mode=docker-compose, bullwinkle runs mode=client. Neither path produces multiple coexisting unit files.

Action:
1. When install_service() chooses a mode, disable+remove artifacts from the OTHER supervisor modes:
   - If choosing systemd: stop+disable any ~/.config/systemd/user/tokenhub.service and remove the file (or at least disable).
   - If choosing systemd-user: stop+disable any /etc/systemd/system/${FLEET_NAME}-tokenhub.service via sudo.
   - Similar for launchd<->supervisord if those paths can collide.
2. Rewrite ~/.tokenhub/deploy-state.json to reflect the chosen mode every run.
3. Optionally: log a warning when prior-mode artifacts were found and reaped so operators can see the transition.
4. Clean up rocky's leftover ~/.config/systemd/user/tokenhub.service.

Cosmetic only today (only one unit is bound to the port) but a future install pass could re-flip to systemd-user mode if the system unit ever goes inactive at the moment of install, which would surprise operators.

## Acceptance Criteria

- install-tokenhub-service.sh removes/disables prior-mode unit files before installing under the new mode
- deploy-state.json reflects the chosen mode after every install run
- rocky's leftover ~/.config/systemd/user/tokenhub.service is removed
- Re-running setup-fleet on rocky with the new install logic results in exactly one tokenhub unit file (the system one) and no stale user unit
