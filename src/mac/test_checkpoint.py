"""Resume a failed test-gate run instead of paying for the whole suite again.

CORRECTNESS BEATS SPEED, AND THAT ORDERING IS NOT NEGOTIABLE. A checkpoint that
lets the gate skip a test which would now fail defeats the entire purpose of the
gate, so every rule below is written to fail OPEN (run everything) whenever the
answer is not provable. A missing, stale, unreadable, or ambiguous checkpoint is
not an error: it is a full run.

WHY THIS EXISTS. CI's `sanity` job runs ~11,400 tests in 50 minutes to 2 hours.
Real failures observed in one afternoon: a stale ``mkdocs.yml`` nav entry that
failed 48 minutes in, two assertions out of 11,187, and six tests in one file.
Each cost a complete second run of the suite to re-verify a one-line fix.

--------------------------------------------------------------------------
1. WHAT IS A CHECKPOINT KEYED ON?
--------------------------------------------------------------------------
NOT the commit sha. Keying on the sha would make every checkpoint worthless the
moment the one-line fix lands, which is precisely the case worth optimizing.

A checkpoint is keyed on two things:

  * a RUNNER FINGERPRINT -- content hashes of the files that decide what the
    gate runs and what it asserts (the runner script, both conftests, the
    pytest/`pyproject` configuration, the selection policy, the lockfile, the
    committed impact map) plus the interpreter version and the gate-shaping
    environment. Any difference discards the whole checkpoint, no exceptions.

  * a TREE MANIFEST -- the git blob hash of every tracked and untracked-but-not-
    ignored file. This is not compared for equality; it is DIFFERENCED against
    the current tree so the checkpoint survives an edit and we can reason about
    exactly which files moved.

INVALIDATION RULE, stated plainly. A recorded result for test file ``F`` is
honored on a later run only when ALL of the following hold; otherwise ``F`` runs
again, and if any of them cannot even be *evaluated* the entire checkpoint is
discarded and the complete suite runs:

  (a) the checkpoint's schema and runner fingerprint match this run exactly;
  (b) every test recorded for ``F`` passed -- one failure, error, or unknown
      outcome in ``F`` re-runs all of ``F``;
  (c) ``F`` is in neither the policy's nor the impact map's ``always_run``;
  (d) ``F`` is not itself in the delta (a changed test file re-runs);
  (e) no file in the delta charges any test to ``F`` under the impact map,
      the reviewed path contracts, or the ``always_run`` sets;
  (f) every file in the delta is classifiable -- an unmapped source file, an
      opaque non-code file, a globally invalidating file, or a source file whose
      map entry was not built from the bytes present in the checkpointed tree
      all force a full run.

--------------------------------------------------------------------------
2. WHAT IS SAFE TO SKIP ON RESUME?
--------------------------------------------------------------------------
A test that passed against tree A says nothing about tree B unless the delta
A->B can be bounded. The committed impact map is exactly that bound, and this
module deliberately does NOT invent a second bounding rule: it charges the delta
to tests with the same layers ``scripts/resolve-impacted-tests.py`` uses, at
FILE granularity, which is the map's own documented safe superset.

Two extra conservatisms on top of the resolver:

  * WHOLE TEST FILES, NEVER INDIVIDUAL TESTS. Skipping half of a module leaves
    the other half running against different module-scoped fixture state than
    the run that recorded the result. A file is skipped in full or not at all.

  * ``always_run`` IS ABSOLUTE. The map's docstring says unattributed tests go
    into ``always_run`` "so the selector can never silently drop them"; a
    checkpoint that carried them forward would silently drop them one release
    later. They are never carried forward, on any tree, for any reason.

Skipping is done by DESELECTION at collection time, not by narrowing the pytest
argv. Collection still imports every test module, so an import error or a
collection-time failure in a carried-forward file is still caught.

--------------------------------------------------------------------------
3. WHERE DOES THE CHECKPOINT LIVE?
--------------------------------------------------------------------------
A JSON document under ``MAC_TEST_CHECKPOINT_DIR`` (default
``.mac-test-checkpoint/``, gitignored). That covers the two places the gate
actually re-runs in the same workspace: an operator or agent iterating locally,
and ``auto_land.run_contract_gate`` re-invoked in a task workspace. CI is
ephemeral, so ``.github/workflows/ci.yml`` restores the directory with
``actions/cache`` keyed per branch.

The document is self-describing and self-validating rather than trusted, so the
same bytes can later be carried as ledger attempt evidence (``add_evidence`` +
``evidence_attempt_links`` already store per-attempt artifacts) without changing
any rule here. That hub-side hop is deliberately NOT implemented yet -- see the
module's follow-up task -- because a durable store adds nothing until the
file-backed rules have run in anger.

--------------------------------------------------------------------------
4. COVERAGE FLOORS
--------------------------------------------------------------------------
A partial re-run produces partial coverage data, and the repository's floors sit
within 0.35pp of failing. Combining a previous run's coverage with a resumed
run's would over-state coverage for any file whose executing tests changed, and
an over-stated total can turn a red gate green. That is the exact failure this
module exists to prevent, so IT IS NEVER DONE.

The rule instead is: THE CHECKPOINT SHORT-CIRCUITS FAILURE, NEVER SUCCESS.

  * On a coverage-enforcing gate, a valid checkpoint buys a TRIAGE PASS: the
    not-carried-forward tests run first, without coverage. If they fail, the
    gate exits red in minutes instead of an hour. If they pass, the runner falls
    through and executes the complete, unresumed, coverage-measured gate exactly
    as it does today. Whole-repo floors are therefore only ever computed from a
    complete run -- there is no code path by which a resumed run reports green
    without one.

  * On a gate that measures no coverage at all (the PR ``sanity`` path, which
    passes explicit test paths, and ``MAC_TEST_COVERAGE=0``), a green resumed
    run is terminal, because there is no coverage number to protect.

For the motivating example this turns 50 + 50 minutes into 50 + 3.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SCHEMA = "mac.test_checkpoint.v1"
PLAN_SCHEMA = "mac.test_checkpoint_plan.v1"
DEFAULT_DIR = ".mac-test-checkpoint"
CHECKPOINT_NAME = "checkpoint.json"
RESULTS_DIRNAME = "results"

# Files that decide WHAT the gate runs and WHAT it asserts. A byte change in any
# of them discards the checkpoint outright rather than trying to reason about
# the consequences.
FINGERPRINT_FILES = (
    "scripts/run-contract-tests.sh",
    "scripts/resolve-impacted-tests.py",
    "conftest.py",
    "tests/conftest.py",
    "pyproject.toml",
    "test-policy.toml",
    "uv.lock",
    "src/mac/data/test_impact_map.json",
    "src/mac/test_checkpoint.py",
)

# Environment that changes the gate's meaning. MAC_TEST_JOBS is deliberately
# absent: worker count changes scheduling, not whether a test passed.
FINGERPRINT_ENV = (
    "MAC_TEST_COVERAGE",
    "MAC_TEST_DISABLE_GROUPS",
    "MAC_TEST_PORTFOLIO",
    "MAC_TEST_SELECT_BASE",
)


# --------------------------------------------------------------------------
# Plan document
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """The decision, plus enough detail for a human to audit it in the log."""

    mode: str  # "full" | "resume"
    reason: str
    skip_files: tuple[str, ...] = ()
    skip_tests: int = 0
    recorded_files: int = 0
    recorded_tests: int = 0
    delta: tuple[str, ...] = ()
    previously_failed: tuple[str, ...] = ()
    coverage_authoritative: bool = True
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PLAN_SCHEMA,
            "mode": self.mode,
            "reason": self.reason,
            "skip_files": list(self.skip_files),
            "skip_tests": self.skip_tests,
            "recorded_files": self.recorded_files,
            "recorded_tests": self.recorded_tests,
            "delta": list(self.delta),
            "previously_failed": list(self.previously_failed),
            "coverage_authoritative": self.coverage_authoritative,
            "notes": list(self.notes),
        }


def _full(reason: str, **kwargs: object) -> Plan:
    return Plan(mode="full", reason=reason, **kwargs)  # type: ignore[arg-type]


def render_plan(plan: Plan) -> str:
    """Human-auditable one-screen explanation.

    A silent optimization nobody can read is how a CI job once reported green
    while running zero tests, so every decision prints WHY and HOW MUCH.
    """
    lines = ["test checkpoint: %s (%s)" % (plan.mode, plan.reason)]
    if plan.delta:
        shown = list(plan.delta[:20])
        for path in shown:
            lines.append("  changed since checkpoint: " + path)
        if len(plan.delta) > len(shown):
            lines.append("  ... and %d more changed files" % (len(plan.delta) - len(shown)))
    if plan.previously_failed:
        shown_f = list(plan.previously_failed[:20])
        for nodeid in shown_f:
            lines.append("  previously failed: " + nodeid)
        if len(plan.previously_failed) > len(shown_f):
            lines.append(
                "  ... and %d more previously-failing tests"
                % (len(plan.previously_failed) - len(shown_f))
            )
    for note in plan.notes:
        lines.append("  note: " + note)
    if plan.mode == "resume":
        lines.append(
            "  carrying forward %d test files (%d tests) that passed and that no "
            "changed file charges a test to" % (len(plan.skip_files), plan.skip_tests)
        )
        lines.append(
            "  re-running %d of %d recorded test files"
            % (plan.recorded_files - len(plan.skip_files), plan.recorded_files)
        )
        if not plan.coverage_authoritative:
            lines.append(
                "  coverage: this is a TRIAGE pass only -- whole-repo floors are "
                "NOT evaluated here; a green triage falls through to the complete gate"
            )
    else:
        lines.append("  running the complete suite (checkpointing declined)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Tree manifest + fingerprint
# --------------------------------------------------------------------------


def _git(argv: list[str], repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argv],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )


def _hash_paths(paths: Iterable[str], repo_root: Path) -> dict[str, str]:
    """git blob hashes for worktree contents, batched through hash-object."""
    listed = [p for p in paths if p]
    if not listed:
        return {}
    proc = subprocess.run(
        ["git", "hash-object", "--stdin-paths"],
        cwd=str(repo_root),
        input="\n".join(listed) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    digests = proc.stdout.split()
    if len(digests) != len(listed):
        return {}
    return dict(zip(listed, digests))


def tree_manifest(repo_root: Path) -> dict[str, str] | None:
    """path -> git blob hash for every tracked + untracked-not-ignored file.

    None on any git failure, which the caller must treat as "run everything":
    a manifest we cannot build is a delta we cannot bound.
    """
    staged = _git(["ls-files", "-s"], repo_root)
    if staged.returncode != 0:
        return None
    manifest: dict[str, str] = {}
    for line in staged.stdout.splitlines():
        # "<mode> <sha> <stage>\t<path>"
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if not path or len(parts) < 2:
            return None
        manifest[path] = parts[1]

    # The index hash is stale for anything edited but not staged, and the whole
    # point of this module is to survive exactly that edit.
    dirty = _git(["diff-files", "--name-only"], repo_root)
    if dirty.returncode != 0:
        return None
    changed = [p for p in dirty.stdout.splitlines() if p]

    untracked = _git(["ls-files", "--others", "--exclude-standard"], repo_root)
    if untracked.returncode != 0:
        return None
    changed.extend(p for p in untracked.stdout.splitlines() if p)

    if changed:
        rehashed = _hash_paths(changed, repo_root)
        if len(rehashed) != len(set(changed)):
            return None
        manifest.update(rehashed)

    # A file deleted from the worktree but still in the index is not part of
    # this tree. Dropping it makes it show up in the delta, which is correct.
    for path in list(manifest):
        if not (repo_root / path).is_file():
            manifest.pop(path, None)
    return manifest


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def map_source_hashes(repo_root: Path, impact_map: dict | None) -> dict[str, str]:
    """sha256 of each mapped source file, in the impact map's own hash scheme.

    Recorded at checkpoint-write time so a later run can ask the question
    ``_fresh_map_files`` asks of git -- "was the map built from these bytes?" --
    without needing the checkpointed tree to still exist as a commit.
    """
    if not impact_map:
        return {}
    result: dict[str, str] = {}
    for name in impact_map.get("file_tests", {}):
        digest = _sha256_file(repo_root / name)
        if digest is not None:
            result[name] = digest
    return result


def runner_fingerprint(repo_root: Path, env: dict[str, str] | None = None) -> str:
    env = dict(os.environ if env is None else env)
    parts: list[str] = [
        "python=%s" % platform.python_version(),
        "impl=%s" % platform.python_implementation(),
        "platform=%s" % sys.platform,
    ]
    for name in FINGERPRINT_FILES:
        digest = _sha256_file(repo_root / name) or "absent"
        parts.append("%s=%s" % (name, digest))
    for name in FINGERPRINT_ENV:
        parts.append("%s=%s" % (name, env.get(name, "")))
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Result ingestion (written by the conftest hooks)
# --------------------------------------------------------------------------


def _test_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def ingest_results(results_dir: Path) -> dict[str, str]:
    """nodeid -> strongest outcome, merged across every xdist worker's file.

    "Strongest" means failure wins: a test that failed in any phase is failed,
    never carried forward. An unparsable line is dropped rather than guessed at
    (the enclosing run still records everything it could read; a file whose
    results are incomplete simply has fewer passes to carry forward).
    """
    outcomes: dict[str, str] = {}
    if not results_dir.is_dir():
        return outcomes
    for path in sorted(results_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            nodeid = str(record.get("nodeid") or "")
            outcome = str(record.get("outcome") or "")
            if not nodeid or outcome not in {"passed", "failed", "skipped"}:
                continue
            previous = outcomes.get(nodeid)
            if previous == "failed":
                continue
            if outcome == "failed" or previous is None:
                outcomes[nodeid] = outcome
            elif previous == "skipped" and outcome == "passed":
                # A skip report followed by a pass cannot happen, but if the
                # data says both, the weaker claim wins.
                outcomes[nodeid] = "skipped"
    return outcomes


# --------------------------------------------------------------------------
# Checkpoint document
# --------------------------------------------------------------------------


def build_checkpoint(
    *,
    repo_root: Path,
    outcomes: dict[str, str],
    gate: str,
    impact_map: dict | None,
    env: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """Fold a completed run's outcomes into a fresh checkpoint document.

    Returns None when the tree manifest cannot be built, because a checkpoint
    without a manifest can never be safely resumed from and writing one would
    only invite a future reader to trust it.
    """
    manifest = tree_manifest(repo_root)
    if manifest is None:
        return None
    files: dict[str, dict[str, int]] = {}
    failed: list[str] = []
    for nodeid, outcome in outcomes.items():
        entry = files.setdefault(_test_file(nodeid), {"passed": 0, "failed": 0, "other": 0})
        if outcome == "passed":
            entry["passed"] += 1
        elif outcome == "failed":
            entry["failed"] += 1
            failed.append(nodeid)
        else:
            entry["other"] += 1
    head = _git(["rev-parse", "HEAD"], repo_root)
    return {
        "schema": SCHEMA,
        "gate": gate,
        "head_sha": head.stdout.strip() if head.returncode == 0 else None,
        "runner_fingerprint": runner_fingerprint(repo_root, env),
        "tree": manifest,
        "map_source_hashes": map_source_hashes(repo_root, impact_map),
        "files": files,
        "failed_tests": sorted(failed),
        "stats": {
            "recorded_tests": len(outcomes),
            "recorded_files": len(files),
            "failed_tests": len(failed),
        },
    }


def read_carried_forward(path: Path) -> set[str]:
    try:
        return {
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    except OSError:
        return set()


def merge_carried_forward(
    document: dict[str, object],
    previous: dict | None,
    carried: set[str],
    *,
    repo_root: Path,
) -> dict[str, object] | None:
    """Fold a resumed run's carried-forward files back into the new checkpoint.

    A resumed run only executes part of the suite, so on its own it records only
    that part -- and the NEXT run would then have nothing to carry forward,
    making every resume a one-shot. Folding the previous entries forward keeps
    the chain alive.

    This is sound because the charging rule is file-level and content-based, so
    it composes: if the A->B delta charges nothing to F and the B->C delta
    charges nothing to F, then neither does A->C, because every file that
    differs between A and C differs in at least one of the two hops and was
    therefore charged in that hop. Returns None when the previous checkpoint is
    not the one this run resumed from (different fingerprint), in which case the
    caller must record only what actually ran.
    """
    if previous is None or not carried:
        return None
    if previous.get("runner_fingerprint") != document.get("runner_fingerprint"):
        return None
    previous_files: dict[str, dict[str, int]] = previous.get("files") or {}
    files: dict[str, dict[str, int]] = dict(document.get("files") or {})  # type: ignore[arg-type]
    merged = 0
    for name in sorted(carried):
        entry = previous_files.get(name)
        if not entry or name in files:
            continue
        if int(entry.get("failed") or 0) or int(entry.get("other") or 0):
            # Never promote a non-passing entry across a resume.
            continue
        files[name] = dict(entry)
        merged += 1
    document = dict(document)
    document["files"] = files
    document["carried_forward_files"] = merged
    stats = dict(document.get("stats") or {})  # type: ignore[arg-type]
    stats["recorded_files"] = len(files)
    stats["recorded_tests"] = sum(
        int(entry.get("passed") or 0) + int(entry.get("failed") or 0) + int(entry.get("other") or 0)
        for entry in files.values()
    )
    document["stats"] = stats
    return document


def checkpoint_path(directory: Path) -> Path:
    return directory / CHECKPOINT_NAME


def results_dir(directory: Path) -> Path:
    return directory / RESULTS_DIRNAME


def write_checkpoint(directory: Path, document: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = checkpoint_path(directory)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


def load_checkpoint(directory: Path) -> dict | None:
    """The stored document, or None for anything we would have to guess about."""
    try:
        document = json.loads(checkpoint_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return None
    if not isinstance(document.get("tree"), dict) or not isinstance(document.get("files"), dict):
        return None
    return document


# --------------------------------------------------------------------------
# The resolver (loaded from its hyphenated script, as select-sanity-tests does)
# --------------------------------------------------------------------------

_RESOLVER_NAME = "mac_resolve_impacted_tests"


def resolver_module_name(repo_root: Path) -> str:
    """The `sys.modules` key for the resolver loaded from ``repo_root``.

    KEYED BY ROOT, deliberately. The resolver reads `test-policy.toml` and the
    impact map relative to a root captured at import, so a module loaded from
    one repository answers questions about THAT repository forever. Caching it
    under a single fixed name -- which is what this did -- makes the first
    caller in the process decide the answers for every later one.

    That is not hypothetical. `tests/test_test_checkpoint.py` builds a synthetic
    repository whose policy declares `Makefile` global and
    `always_run = ["tests/test_guard.py"]`, loads the resolver against it, and
    leaves it in `sys.modules`. Any later caller in the same pytest worker then
    resolved against a temp directory that had already been deleted:

        tests/test_select_sanity_tests.py::test_opaque_non_code_forces_full
            assert 'global_infrastructure_changed' == 'unmappable_non_code_change'
        tests/test_select_sanity_tests.py::test_source_change_uses_impact_map_and_canaries
            assert 'tests/.../public_contract.py' in ['tests/test_guard.py', ...]

    Both files pass in isolation, so this only appears when `-n 8` happens to
    put them in one worker -- and it surfaced as two red tests only by luck.
    The same contaminated module is what CHOOSES WHICH TESTS CI RUNS, and a
    wrong policy there under-selects silently: fewer tests run, the gate is
    green, and nothing reports it.
    """
    return "%s__%s" % (
        _RESOLVER_NAME,
        hashlib.sha256(str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:16],
    )


def load_resolver(repo_root: Path):
    """Import scripts/resolve-impacted-tests.py, or None if it is unavailable.

    The charging rules live there and are unit-tested there; duplicating them
    here would let the two drift, and a drifted copy is a copy that skips a test
    the resolver would have run.
    """
    name = resolver_module_name(repo_root)
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = repo_root / "scripts" / "resolve-impacted-tests.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


@dataclass
class Charge:
    """Which test files the delta forces back onto the run list."""

    files: set[str] = field(default_factory=set)
    full_reason: str | None = None
    notes: list[str] = field(default_factory=list)


def charge_delta(
    delta: Iterable[str],
    *,
    repo_root: Path,
    resolver,
    policy,
    impact_map: dict | None,
    checkpoint_map_hashes: dict[str, str],
) -> Charge:
    """File-level charging of a delta to test files, fail-open on anything odd.

    This is the resolver's layering (global paths -> reviewed path contracts ->
    test files -> impact map -> full) collapsed to file granularity, which the
    map's builder documents as its safe superset. It is deliberately COARSER
    than ``resolve()``: no line-level or scope-level narrowing, because those
    need two git revisions of the file and a checkpointed tree is not a commit.
    Coarser means it charges MORE tests back onto the run list, never fewer.
    """
    charge = Charge()
    contracts = getattr(resolver, "PATH_TEST_CONTRACTS", {})
    file_tests = (impact_map or {}).get("file_tests", {})
    nodeids = (impact_map or {}).get("nodeids", [])
    map_hashes = (impact_map or {}).get("file_hashes", {})

    for path in sorted({p for p in delta if p}):
        if path in getattr(policy, "global_full_paths", frozenset()):
            charge.full_reason = "global_infrastructure_changed"
            return charge
        if path in contracts:
            charge.files.update(contracts[path])
            continue
        # A path can be BOTH (plugin/test_*.py is a test file AND source), so
        # these are unioned rather than treated as alternatives -- the resolver
        # does the same, and charging twice is always the safe direction.
        is_test = resolver._is_test_file(path)
        is_source = resolver._is_source_code(path)
        if is_test:
            charge.files.add(path)
        if not is_source:
            if is_test or resolver._is_documentation(path):
                # Documentation selects no tests, exactly as the resolver
                # decides. The executable-documentation guards are in
                # always_run, which is never carried forward, so they still run.
                continue
            # Opaque shell/CI/data/config: nothing can tie it to the tests that
            # exercise it. The resolver runs everything here and so do we.
            charge.full_reason = "unmappable_non_code_change"
            return charge
        if path not in file_tests:
            # A source file the map never saw (new, or renamed into place).
            # Without a trustworthy mapping to tests, this must be a full run.
            charge.full_reason = "source_file_absent_from_impact_map"
            return charge
        if map_hashes.get(path) != checkpoint_map_hashes.get(path):
            # The map's knowledge of this file was not built from the bytes that
            # were present when the checkpoint was taken, so the tests it
            # attributes are not the tests that ran. Content-identity is the
            # same question ``_fresh_map_files`` asks git about ancestry, asked
            # of a tree that may never have been committed.
            charge.full_reason = "impact_map_stale_for_changed_file"
            return charge
        for index in file_tests.get(path, []):
            if 0 <= index < len(nodeids):
                charge.files.add(_test_file(nodeids[index]))
            else:
                charge.full_reason = "impact_map_index_out_of_range"
                return charge
    return charge


def always_run_files(policy, impact_map: dict | None) -> set[str]:
    """Test files that must never be carried forward, from both sources."""
    always = set(getattr(policy, "always_run", ()) or ())
    if impact_map:
        always.update(impact_map.get("always_run", []) or [])
    return always


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


def plan(
    *,
    repo_root: Path,
    directory: Path,
    require_whole_coverage: bool,
    env: dict[str, str] | None = None,
    resolver=None,
    policy=None,
    impact_map=None,
) -> Plan:
    """Decide whether to resume, and from where. Fail open at every step."""
    document = load_checkpoint(directory)
    if document is None:
        return _full("no_usable_checkpoint")

    fingerprint = runner_fingerprint(repo_root, env)
    if document.get("runner_fingerprint") != fingerprint:
        return _full(
            "runner_fingerprint_changed",
            notes=(
                "the runner, its configuration, the impact map, or the gate "
                "environment changed since the checkpoint",
            ),
        )

    recorded_files: dict[str, dict[str, int]] = document.get("files") or {}
    previously_failed = tuple(document.get("failed_tests") or [])
    recorded_tests = int((document.get("stats") or {}).get("recorded_tests") or 0)
    if not recorded_files:
        return _full("checkpoint_recorded_no_tests")

    manifest = tree_manifest(repo_root)
    if manifest is None:
        return _full("tree_manifest_unavailable")
    stored: dict[str, str] = document.get("tree") or {}
    delta = tuple(
        sorted(
            {p for p in stored if stored.get(p) != manifest.get(p)}
            | {p for p in manifest if manifest.get(p) != stored.get(p)}
        )
    )

    resolver = resolver if resolver is not None else load_resolver(repo_root)
    if resolver is None:
        return _full("resolver_unavailable", delta=delta)
    try:
        policy = policy if policy is not None else resolver.load_policy()
        if impact_map is None:
            impact_map = resolver.load_map(policy.map_path)
    except Exception:
        return _full("selection_policy_unreadable", delta=delta)
    if not impact_map:
        return _full("impact_map_unavailable", delta=delta)

    charge = charge_delta(
        delta,
        repo_root=repo_root,
        resolver=resolver,
        policy=policy,
        impact_map=impact_map,
        checkpoint_map_hashes=document.get("map_source_hashes") or {},
    )
    if charge.full_reason:
        return _full(charge.full_reason, delta=delta, previously_failed=previously_failed)

    never_skip = always_run_files(policy, impact_map) | charge.files | set(delta)

    skip_files: list[str] = []
    skip_tests = 0
    for name, counts in sorted(recorded_files.items()):
        if name in never_skip:
            continue
        if int(counts.get("failed") or 0) or int(counts.get("other") or 0):
            # A failure re-runs, obviously. A skip/xfail also re-runs: its
            # outcome depended on runtime conditions we did not record, so
            # "it did not fail last time" is not a result to carry forward.
            continue
        passed = int(counts.get("passed") or 0)
        if passed <= 0:
            continue
        if not (repo_root / name).is_file():
            # Deleted or renamed since the checkpoint. Nothing to skip, and the
            # rename's new path is in the delta and therefore in never_skip.
            continue
        skip_files.append(name)
        skip_tests += passed

    if not skip_files:
        return _full(
            "nothing_can_be_carried_forward",
            delta=delta,
            previously_failed=previously_failed,
            recorded_files=len(recorded_files),
            recorded_tests=recorded_tests,
        )

    if require_whole_coverage:
        # Whole-repo statement/branch floors cannot be measured from a subset,
        # and combining old coverage with new would over-state any file whose
        # executing tests changed. So the caller is told the resume is a triage
        # pass: fast red, and a green falls through to the complete gate.
        return Plan(
            mode="resume",
            reason="triage_pass_only",
            skip_files=tuple(skip_files),
            skip_tests=skip_tests,
            recorded_files=len(recorded_files),
            recorded_tests=recorded_tests,
            delta=delta,
            previously_failed=previously_failed,
            coverage_authoritative=False,
            notes=tuple(charge.notes)
            + (
                "whole-repo coverage floors are never evaluated on a resumed "
                "subset; a green triage pass runs the complete gate afterwards",
            ),
        )

    return Plan(
        mode="resume",
        reason="carried_forward_from_checkpoint",
        skip_files=tuple(skip_files),
        skip_tests=skip_tests,
        recorded_files=len(recorded_files),
        recorded_tests=recorded_tests,
        delta=delta,
        previously_failed=previously_failed,
        coverage_authoritative=True,
        notes=tuple(charge.notes),
    )
