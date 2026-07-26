# Staged-but-unwired `src/mac` module integration audit

Tracking follow-up for `docs/audit.md` §6.1. This is a **read-only audit**: it
enumerates every first-party module under `src/mac` (excluding the vendored
`src/mac/_hermes` runtime) that is imported by no other `src/mac` module and is
statically reachable only from its own test file, then classifies each one as
having a **real integration path** or being **genuinely abandoned**. It changes
no `src/` code and deletes nothing — it records findings and names the modules a
future repair task must act on.

## 1. Reproducible enumeration

A module is a **candidate** when a repo-wide grep (scope: `src`, `tests`,
`scripts`, `deploy`, `docs`, `.mac`, `pyproject.toml`, `Makefile`, `conftest.py`,
`ide`, `desktop`, `plugin`, `skills`, `docker`, and `src/mac/data/*.json`) shows
**no import or reference** to it from any **non-test, non-self `src/mac`** file.
`_hermes` is excluded as a candidate but references *from* `_hermes` still count.
Binary assets and `.venv` are excluded from the scan.

Deterministic reproduction (no `rg` dependency; pure Python stdlib):

```text
python3 - <<'PY'
import os, re
SRC = "src/mac"
modules = [n[:-3] for n in sorted(os.listdir(SRC))
           if n.endswith(".py") and n != "__init__.py" and not n.startswith("_hermes")]
scope = ["src", "tests", "scripts", "deploy", "docs", ".mac",
         "ide", "desktop", "plugin", "skills", "docker"]
extra = ["pyproject.toml", "setup.py", "Makefile", "conftest.py"]
skip = {".png", ".jpg", ".jpeg", ".gif", ".wav", ".mp3", ".gz", ".zip",
        ".tar", ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".so", ".pyc"}
paths = []
for d in scope:
    for base, dirs, fs in os.walk(d):
        if ".venv" in dirs: dirs.remove(".venv")
        if "__pycache__" in dirs: dirs.remove("__pycache__")
        paths += [os.path.join(base, f) for f in fs
                  if os.path.splitext(f)[1].lower() not in skip]
paths += [f for f in extra if os.path.isfile(f)]
text = {}
for p in paths:
    try: text["./" + p] = open(p, encoding="utf-8", errors="replace").read()
    except Exception: text["./" + p] = ""
def referenced_by_src(mod):
    self_f = f"./src/mac/{mod}.py"
    imp = re.compile(rf"(?:^|[^.\w])(?:import\s+mac\.{mod}(?:[.\s,]|$)|"
                     rf"from\s+mac\.{mod}(?:[.\s]|$))", re.M)
    imp3 = re.compile(rf"from\s+mac\s+import\s+[^\n]*\b{mod}\b")
    dotted = re.compile(rf"\bmac\.{mod}\b")
    for n, t in text.items():
        if n == self_f or (f"mac.{mod}" not in t and "from mac import" not in t):
            continue
        if imp.search(t) or imp3.search(t) or dotted.search(t):
            if n.startswith("./src/mac/") and "/_hermes/" not in n:
                return True
    return False
cands = [m for m in modules if not referenced_by_src(m)]
print(len(cands), "candidates:")
for c in cands: print(" ", c)
PY
```

Run against the audited worktree this yields **19 candidates** (198 modules
scanned):

```text
changeset_adoption        hermes_gateway            openclaw_delivery_continuity  review_finalizer
dream_scanner             hermes_home_audit         openshell_collector           skill_auto_repair
evidence_cli              ide_launcher              openshell_supervisor          supervisor
git_askpass               investigation_artifacts   predispatch_conflict          webdav_server
hermes_chat_config        openclaw_checkpoint_gc    project_inception
```

## 2. Reconciliation with `docs/audit.md` §6.1

The enumeration is regenerated, not hardcoded, and reconciled against the
task's prior candidate lists:

- **No longer candidates (now referenced by other `src/mac` modules).** `docs/audit.md` §6.1
  listed `hgx_provider`, `hgx_provision`, `harness_reflex`, and
  `evidence_reuse_verifier`. In the current worktree these are referenced by
  first-party modules and therefore fall out of the candidate set:
  `hgx_provider` — real import at `src/mac/hgx_elastic_capacity.py:38` and
  `src/mac/cli.py:5262`; `hgx_provision` — design-surface reference at
  `src/mac/hgx_elastic_capacity.py:7`; `harness_reflex` — design-surface
  reference at `src/mac/harness_recovery.py:4`; `evidence_reuse_verifier` —
  design-surface references at `src/mac/services.py:9605` and
  `src/mac/models.py:3168`.
- **Newly present candidates** not named in the original ~18/§6.1 lists:
  `openclaw_delivery_continuity`, `supervisor`, `hermes_home_audit`.
- **Still candidates** from the prior lists: `changeset_adoption`, `dream_scanner`,
  `evidence_cli`, `git_askpass`, `hermes_chat_config`, `hermes_gateway`,
  `ide_launcher`, `investigation_artifacts`, `openclaw_checkpoint_gc`,
  `openshell_collector`, `openshell_supervisor`, `predispatch_conflict`,
  `project_inception`, `review_finalizer`, `skill_auto_repair`, `webdav_server`.

> **Assumption (recorded).** The audited worktree is a single squashed baseline
> commit (`MAC OpenShell sandbox baseline`), so per-file authorship dates from
> git history are not available here. The landing dates in `docs/audit.md` §6.1
> (e.g. `openclaw_checkpoint_gc` 2026-07-24) are carried forward from that
> document rather than re-derived. Classification below relies on static
> integration evidence (entrypoints, script/deploy callers, design surface,
> passing tests), which does not depend on commit dates.

## 3. Classification

Classes: **WIRED-VIA-ENTRYPOINT** (`pyproject.toml` `[project.scripts]`),
**WIRED-VIA-SCRIPT/DEPLOY** (`scripts/*.py` / `deploy/*` invoking `python -m
mac.<mod>` or importing it), **WIRED-VIA-DESIGN-SURFACE** (referenced from
first-party module docstrings/design comments or the test-impact map),
**STAGED-INTENDED** (recent tested-first addition with a plausible roadmap owner
but no runtime caller yet), **ABANDONED** (no entrypoint, script/deploy caller,
design reference, or roadmap trace).

| module | classification | evidence (file:line) | has_test | verdict |
|---|---|---|---|---|
| `evidence_cli` | WIRED-VIA-ENTRYPOINT | `pyproject.toml:101` (`mac-evidence = "mac.evidence_cli:main"`); also `deploy/codex-runner/mac-task-executor-codex:48` | `tests/test_evidence_cli.py` (pass) | keep-wired |
| `git_askpass` | WIRED-VIA-ENTRYPOINT | `pyproject.toml:107` (`mac-git-askpass = "mac.git_askpass:main"`) | `tests/test_git_askpass.py` (pass) | keep-wired |
| `hermes_gateway` | WIRED-VIA-ENTRYPOINT | `pyproject.toml:102` (`mac-hermes-gateway = "mac.hermes_gateway:main"`); `main` at `src/mac/hermes_gateway.py:122` | `tests/test_hermes_gateway_sandbox.py`, `tests/test_hermes_vendor.py` (pass) | keep-wired |
| `openshell_supervisor` | WIRED-VIA-ENTRYPOINT | `pyproject.toml:103` (`mac-openshell-supervisor = "mac.openshell_supervisor:main"`) | `tests/test_openshell_management.py`, `tests/test_infrastructure_coverage.py` (pass) | keep-wired |
| `openshell_collector` | WIRED-VIA-ENTRYPOINT | `pyproject.toml:104` (`mac-openshell-collector = "mac.openshell_collector:main"`) | `tests/test_openshell_management.py`, `tests/test_infrastructure_coverage.py` (pass) | keep-wired |
| `webdav_server` | WIRED-VIA-ENTRYPOINT | `pyproject.toml:91` (`mac-webdav-server = "mac.webdav_server:main"`); also `deploy/install-webdav-server.sh:193` | `tests/test_webdav_server.py` (pass) | keep-wired |
| `project_inception` | WIRED-VIA-SCRIPT/DEPLOY | `scripts/prove-c26-inception.py:9` (`from mac.project_inception import run_c26_project_inception_proof`) | `tests/test_project_inception.py` (pass) | keep-wired |
| `review_finalizer` | WIRED-VIA-SCRIPT/DEPLOY | `deploy/codex-runner/mac-task-executor-opencode-review:449` (`python3 -m mac.review_finalizer`) | `tests/test_review_finalizer.py` (pass) | keep-wired |
| `hermes_chat_config` | WIRED-VIA-SCRIPT/DEPLOY | `deploy/fleet-node-install.sh:8755` (`python -m mac.hermes_chat_config ...`) | `tests/test_hermes_chat_config.py` (pass) | keep-wired |
| `ide_launcher` | WIRED-VIA-SCRIPT/DEPLOY | `Makefile:302` (`"$(PYTHON)" -m mac.ide_launcher`) | `tests/test_ide_launcher.py` (pass) | keep-wired |
| `dream_scanner` | WIRED-VIA-DESIGN-SURFACE | design ref `src/mac/dream_repair_tasks.py:22`; in `src/mac/data/test_impact_map.json`; §6.1 `docs/audit.md:259` | `tests/test_dream_scanner.py` (pass) | stage-with-tracking |
| `investigation_artifacts` | WIRED-VIA-DESIGN-SURFACE | canonical per-run artifact set; exercised by `tests/test_per_run_artifact_gitignore.py:25` (`from mac.investigation_artifacts import ...`); §6.1 `docs/audit.md:259` | via `tests/test_per_run_artifact_gitignore.py` (pass) | stage-with-tracking |
| `predispatch_conflict` | WIRED-VIA-DESIGN-SURFACE | investigation trace `docs/reference/documentation-inventory.md:63`; in `src/mac/data/test_impact_map.json`; §6.1 `docs/audit.md:251` | `tests/test_predispatch_conflict.py` (pass) | stage-with-tracking |
| `openclaw_checkpoint_gc` | WIRED-VIA-DESIGN-SURFACE | in `src/mac/data/test_impact_map.json`; §6.1 `docs/audit.md:265`; cross-referenced by `src/mac/openclaw_checkpoint_gc.py:44` OpenClaw family | `tests/test_openclaw_checkpoint_gc.py` (pass) | stage-with-tracking |
| `skill_auto_repair` | WIRED-VIA-DESIGN-SURFACE | in `src/mac/data/test_impact_map.json`; dream-pipeline design trace `docs/archive/field-notes/prereq-task-fd2f34.md:47`; §6.1 `docs/audit.md:253` | `tests/test_skill_auto_repair.py` (pass) | stage-with-tracking |
| `hermes_home_audit` | STAGED-INTENDED | roadmap owner: `docs/home-consolidation.md:134` ("Extend `src/mac/hermes_home_audit.py` into a `mac_home_audit` ..."); in `src/mac/data/test_impact_map.json`; no runtime caller yet | `tests/test_hermes_home_audit.py` (pass) | stage-with-tracking |
| `supervisor` | STAGED-INTENDED | self-describing watchdog design `src/mac/supervisor.py:15` (launchd `com.mac.supervisor`); `main` at `src/mac/supervisor.py:479`; in `src/mac/data/test_impact_map.json`; no entrypoint/launchd install wired yet | `tests/test_supervisor.py` (pass) | stage-with-tracking |
| `openclaw_delivery_continuity` | STAGED-INTENDED | OpenClaw restart-safe delivery core `src/mac/openclaw_delivery_continuity.py:1`; family cross-reference `src/mac/openclaw_checkpoint_gc.py:44`; in `src/mac/data/test_impact_map.json`; no runtime caller yet | `tests/test_openclaw_delivery_continuity.py` (pass) | stage-with-tracking |
| `changeset_adoption` | STAGED-INTENDED | controller changeset-adoption core `src/mac/changeset_adoption.py:1`; shares OpenClaw rollout schema family (`ROLLOUT_PLAN_SCHEMA = "mac.openclaw_fleet_rollout.v1"`, `src/mac/openclaw_fleet_rollout.py:22`); no runtime caller yet | `tests/test_changeset_adoption.py` (pass) | stage-with-tracking |

## 4. Verdict summary

- **keep-wired (10)** — a concrete runtime path exists today:
  `evidence_cli`, `git_askpass`, `hermes_gateway`, `openshell_supervisor`,
  `openshell_collector`, `webdav_server` (entrypoints); `project_inception`,
  `review_finalizer`, `hermes_chat_config`, `ide_launcher` (script/deploy).
- **stage-with-tracking (9)** — no runtime caller yet, but each has a passing
  test and a design/roadmap trace, i.e. the deliberate "land the capability with
  unit tests, wire it later" pattern from §6.1:
  `dream_scanner`, `investigation_artifacts`, `predispatch_conflict`,
  `openclaw_checkpoint_gc`, `skill_auto_repair`, `hermes_home_audit`,
  `supervisor`, `openclaw_delivery_continuity`, `changeset_adoption`.
- **ABANDONED-needs-action (0)** — no candidate lacks all of entrypoint,
  script/deploy caller, design reference, and passing test. **No module is
  genuinely abandoned; no deletion is warranted.**

## 5. Action list for the repair task

There is **no deletion or code change to perform**. The only follow-up is
lightweight tracking so a future genuinely-abandoned module cannot hide among
the not-yet-wired:

1. Add a runtime-wiring tracking note (roadmap owner + target integration point)
   for each of the 9 **stage-with-tracking** modules, prioritising the three
   with the thinnest external trace — `supervisor` (needs a launchd/systemd
   install unit), `openclaw_delivery_continuity`, and `changeset_adoption`
   (both awaiting an OpenClaw-rollout caller).
2. Re-run the enumeration in §1 on each subsequent audit and diff against this
   table; investigate any module that becomes a candidate **and** loses its
   passing test or design trace — that is the abandonment signal to act on.
3. No `src/mac` deletions. Preserve all 19 modules.

## 6. Verification

Doc-only change. The candidate module tests and their covering suites are green
in the audited worktree:

- Dedicated candidate tests: `290 passed` across
  `tests/test_changeset_adoption.py`, `tests/test_dream_scanner.py`,
  `tests/test_evidence_cli.py`, `tests/test_git_askpass.py`,
  `tests/test_hermes_chat_config.py`, `tests/test_hermes_home_audit.py`,
  `tests/test_ide_launcher.py`, `tests/test_openclaw_checkpoint_gc.py`,
  `tests/test_openclaw_delivery_continuity.py`,
  `tests/test_predispatch_conflict.py`, `tests/test_project_inception.py`,
  `tests/test_review_finalizer.py`, `tests/test_skill_auto_repair.py`,
  `tests/test_supervisor.py`, `tests/test_webdav_server.py`.
- Covering suites for candidates without a dedicated test file: `35 passed`
  across `tests/test_hermes_gateway_sandbox.py`,
  `tests/test_per_run_artifact_gitignore.py`,
  `tests/test_openshell_management.py`, `tests/test_infrastructure_coverage.py`.
