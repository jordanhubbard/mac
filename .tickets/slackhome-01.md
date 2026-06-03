---
id: slackhome-01
status: open
deps: []
links: [gketun-03]
created: 2026-06-03T00:00:00Z
type: enhancement
priority: 3
audit: rocky-home-channel-regression
discovered_via: rocky-home-channel-fix
---
# Slack home-channel delivery is single-workspace (only homes[0] is applied)

## Context

#60 fixed the *false* "No home channel is set" prompt — it now respects a
config-set home channel rather than the retired `SLACK_HOME_CHANNEL` env var. But
the underlying home-channel **application** is still single-workspace:
`src/mac/_hermes/gateway/config.py` `_slack_home_from_resolved_file` returns only
`homes[0]` and sets a single `config.platforms[Platform.SLACK].home_channel`, and
`get_home_channel(SLACK)` returns that one channel.

The deploy resolves a home channel **per workspace** into
`slack_home_channels.json` (e.g. rocky has two: `offtera` team `THJ9A47K3` and
`omgjkh` team `TE0V8MBEJ`, both `#rockyandfriends`). With a single applied home
channel, cron-job results and cross-platform messages are delivered to the first
workspace's channel only; the second workspace's resolved home channel is never
used for delivery.

## Proposed fix

Make Slack home-channel resolution per-workspace (keyed by `team_id`): the
gateway should consult `slack_home_channels.json` (which already carries
`team_id` per entry) for the relevant workspace when delivering, instead of a
single global `home_channel`. Likely needs per-account home channels on the Slack
`PlatformConfig` (or a `team_id -> channel` map consulted at send time).

## Acceptance

Cron / cross-platform delivery reaches the configured home channel in **each**
connected Slack workspace, not just the first one in `slack_home_channels.json`.
