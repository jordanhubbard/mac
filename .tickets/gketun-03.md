---
id: gketun-03
status: open
deps: [gketun-02]
links: [gketun-01, gketun-02]
created: 2026-06-03T00:00:00Z
type: bug
priority: 1
audit: jordanh-GKE-supervisord-validation
discovered_via: fresh-fleet-deploy
---
# network=none spokes don't register with the hub even with the tunnel up

## Symptom

After the reverse tunnel is established (spoke reaches `127.0.0.1:18789` → 200)
and the spoke agent is RUNNING with `hermes_chat: True`, the spoke still does not
appear in the hub's `/agents` list (only the hub is registered). The self-test
logs `failed to report heartbeat: HTTP Error 404` (the agent id isn't registered,
so `/agents/<id>/heartbeat` 404s).

## Hypothesis / likely link to gketun-02

The startup self-test still has blocking problems (Qdrant + Firecrawl unreachable
— see [[gketun-02]]), which appears to prevent the agent from completing
registration with the hub even though the process stays up and chat works. If so,
fixing the spoke shared-service URLs (gketun-02) should clear the self-test and
let registration proceed. Needs confirmation that registration is gated on a
clean self-test (vs. a separate auth/endpoint problem in the register call).

## Acceptance

A fresh `network: none` spoke, after deploy, appears in the hub `/agents` list
with a healthy status, without manual intervention.
