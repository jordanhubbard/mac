# Repository CI/CD lifecycle monitoring

MAC treats CI/CD as a continuation of repository work. Publication still
completes when reviewed work is integrated and remotely verified; CI health is
checked afterward and can create linked maintenance work without rewriting the
historical publication result or turning CI into a delivery gate.

## Why this exists

A read-only audit of the authenticated GitHub notification feed on 2026-07-24
found 1,336 accumulated threads. Of those, 1,304 (97.6%) were CI activity and
1,302 were still unread. The alerts spanned 15 repositories from 2026-05-09
through 2026-07-24; 989 concerned a repository's main/default branch.

The largest backlogs were:

| Repository | CI notifications |
| --- | ---: |
| `jordanhubbard/ACC` | 616 |
| `jordanhubbard/mac` | 412 |
| `NVIDIA-dev/Sim-Slim` | 115 |
| `jordanhubbard/nanolang` | 73 |
| `NVIDIA-dev/oss-tracker` | 24 |
| `jordanhubbard/Aviation` | 13 |

This is not merely old inbox noise. There were 19 new CI notifications in the
last 24 hours. Representative failures included MAC's
[impact-selected coverage gate](https://github.com/jordanhubbard/mac/actions/runs/30078995918),
NanoLang's
[backend matrix](https://github.com/jordanhubbard/nanolang/actions/runs/30057187324),
and Aviation's
[application deployment workflow](https://github.com/jordanhubbard/Aviation/actions/runs/29937216960).

Notification read state is therefore not an execution ledger. The monitor keys
checks to immutable repository, ref, and commit SHA identities and records the
outcome in MAC.

## Cadence

The monitor intentionally uses only two coarse latency buckets:

- Recent average completed-run duration at or below two hours: check at least
  every four hours.
- Recent average duration above two hours: check at least every eight hours.

This matches observed repositories without trying to predict every workflow:
MAC and NanoLang generally finish in 10-20 minutes, while successful OrcaSlicer
builds average about four hours and can approach six. A run that is still
pending is re-polled; it is not treated as missing or failed.

Repositories may override the defaults with project metadata under
`cicd_monitor`. GitHub repositories are otherwise auto-detected, and
repositories with no Actions/check/status data are quietly classified as
having no CI.

## Post-publication checks

After a Git publication, MAC records a durable
`cicd.followup.scheduled` observability event containing:

- the publication and source task IDs;
- the registered project and GitHub repository identity;
- the exact canonical SHA; and
- the first due time.

It checks that exact canonical SHA after the settle delay, rechecks pending CI,
and records the outcome. A terminal failure is ordinary maintenance input:
MAC creates at most one unfinished, low-priority cleanup task per repository
and coalesces later failures into the same cleanup pressure. That task may fix
a bounded issue directly, classify a transient or intentionally accepted
failure, or split out narrower work only when that is genuinely useful.

The original repository task remains completed. A later CI failure is new,
linked lifecycle work rather than a retroactive claim that publication did not
happen. It does not hold dispatch, publication, deployment, or unrelated work.

## Operation

The authoritative hub enables the monitor by default. Operators can inspect or
trigger it through:

```text
GET  /cicd-monitor/status
POST /cicd-monitor/run
```

GitHub credentials are resolved through the existing secret-safe GitHub token
provider. Reports and task metadata contain repository/run URLs and classified
outcomes, never token values or authenticated URLs.
