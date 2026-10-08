# Staged-but-unwired `src/mac` module integration audit

> **Verdict overturned (2026-08-18).** This audit's original §4/§5 conclusion —
> "no module is genuinely abandoned; no deletion is warranted; preserve all 19
> modules" — has been **retired**. It was falsified within four days of the
> 2026-07-24 pass: `dream_scanner.py`, listed there as stage-with-tracking with
> "0 abandoned", was deleted on 2026-07-28 (`084c43cf`, "dreaming: rewrite as
> memory curation, not defect scanning"). A design-surface *mention* or a
> `test_impact_map.json` entry is **not** an integration path. §0 below is the
> current-tree resolution and supersedes the reversed pass, whose §2–§6 have
> been removed.
>
> The reversal has since been carried out in stages. Ten of the twelve modules
> called out by the follow-up dead-code task (task_251b3796) — `changeset_adoption`,
> `evidence_reuse_verifier`, `harness_reflex`, `hermes_home_audit`, two
> modules of the since-removed chat-gateway runtime,
> `openshell_static_runtime_refresh`, `remote_session`, `reported_version`,
> `skill_auto_repair` — and their tests are **deleted** in the current tree; git
> history retains them if any is wanted for real. Of the two survivors,
> `investigation_artifacts` is **not** abandoned: it is wired to a real,
> behaviour-exercising test and carries a dated owner and a concrete wiring plan
> in §0. The other, `predispatch_conflict`, passed its 2026-09-30 re-audit date
> still unwired and was **deleted** with its test on 2026-10-01 (§0.2).

## 0. Current-tree resolution (2026-08-18) — supersedes §4/§5

This section decides each still-present candidate from the twelve-module
dead-code task (task_251b3796) *per module*, as the task requires: each is either
**deleted with its test**, or kept with a **named, dated owner and a concrete
wiring plan** so a genuinely-abandoned module can no longer hide among the
merely-not-yet-wired. It replaces the reversed "preserve all 19 / no deletion"
verdict in §4/§5.

### 0.1 Deleted (10 of 12)

The following modules had no non-test importer, no entrypoint, and no
script/deploy caller — only a design-surface mention or a `test_impact_map.json`
entry, which the `dream_scanner` precedent proves is not an integration path.
Each has been removed together with its test file:

`changeset_adoption`, `evidence_reuse_verifier`, `harness_reflex`,
`hermes_home_audit`, two modules of the since-removed chat-gateway runtime,
`openshell_static_runtime_refresh`, `remote_session`, `reported_version`,
`skill_auto_repair`.

Nothing imported them at runtime, so nothing breaks. Their docstring/design
mentions in other modules are prose, not calls, and are left in place as history;
git retains the code.

### 0.2 Kept with a dated owner and wiring plan (2 of 12)

Both survivors are kept because each is exercised by a real test that runs real
behaviour (not a self-referential manifest), and each has a concrete, named
integration point. The prior "evidence of wiring" for `predispatch_conflict` was
circular — it cited only `src/mac/data/test_impact_map.json` (a test-selection
manifest) and this audit itself; that citation is corrected below to the actual
caller and design contract.

| module | owner (role, dated) | real test today | wiring plan: named integration point + trigger | re-audit by |
|---|---|---|---|---|
| `predispatch_conflict` | **DELETED 2026-10-01** | — | Never wired by its 2026-09-30 re-audit date. Deleted with `tests/test_predispatch_conflict.py` when the review/merge pipeline was simplified; git history retains it. | — |
| `investigation_artifacts` | fleet dead-code steward, recorded 2026-08-18 | `tests/test_per_run_artifact_gitignore.py` imports `PER_RUN_INVESTIGATION_ARTIFACTS` and asserts the checked-in `.gitignore` root-anchors every name and masks no nested product file (real `git check-ignore`) | Single source of truth for the per-run artifact filename set the `.gitignore` publication-merge guard depends on. The module derives `PER_RUN_INVESTIGATION_ARTIFACT_GITIGNORE_PATTERNS` from `PER_RUN_INVESTIGATION_ARTIFACTS`; `tests/test_per_run_artifact_gitignore.py` enforces `.gitignore` against it so the two cannot drift. Trigger to fully wire in `src/`: replace the second, hand-maintained copy of the list in `tests/test_gitignore_investigation_artifacts.py` (and any `.gitignore` generator) with an import of this module, collapsing to one SSOT. | 2026-09-30 |

Re-audit rule: on each subsequent dead-code pass, re-run the §1 enumeration and
diff against this table. Investigate any survivor that becomes a candidate **and**
loses its passing test or its named integration point — that is the abandonment
signal to act on. If a survivor is still unwired past its re-audit date with no
progress on its trigger, delete it with its test on the `dream_scanner`
precedent.

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

## 2–6. The reversed 2026-07-24 pass

That pass's candidate list, reconciliation, classification, verdict and action
list (§2–§6) described a tree that no longer exists and were removed; git
history retains them. §0 above is the resolution.
