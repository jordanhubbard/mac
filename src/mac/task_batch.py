"""Apply one change to a group of tasks, once, with the blast radius visible.

The single-task verbs are correct and stay correct; this is the layer above
them. It exists because the number of tasks needing the same human decision is
set by the machine, not by the person answering, and any workflow that costs
one command per task loses that race by construction.

Three properties do the safety work, because a bulk mutation over a selector
is exactly the shape of change that is easy to get catastrophically wrong:

* **Dry run is the default.** Every operation reports what it would touch and
  changes nothing until asked twice. Option validation and the state
  preconditions run identically in both modes, so a preview cannot promise
  changes the apply will refuse outright. It is honest rather than perfect:
  guards that can only be evaluated during the write (lease fences, package
  authority) surface at apply time, so the preview understates refusals and
  never invents them.
* **The group is identified, not just counted.** ``expect_count`` catches a
  group that changed size; ``expect_token`` catches one that changed
  membership without changing size, which is the case a count cannot see.
* **Per-task isolation.** One task that refuses its transition does not abort
  the batch or roll back its siblings; it is reported by id with its reason.
  A batch that dies halfway leaves the operator worse off than one that
  finishes and tells the truth about what it could not do.
* **One audit identity.** Every task touched records the same ``batch_id`` and
  the selector that chose it, so the whole operation is one reviewable unit
  afterwards rather than N unrelated edits that happen to share a timestamp.

Operations delegate to the ordinary single-task methods rather than writing
rows directly, so every state-machine rule, lease guard, and audit hook that
protects a single task protects it here too.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from mac.models import (
    JsonDict,
    NotFoundError,
    StrEnum,
    TaskState,
    ValidationError,
    new_id,
    utcnow,
)
from mac.task_selection import (
    SelectorError,
    TaskSelector,
    compile_sql,
    expand_groups,
    matches,
    parse_selector,
)

#: Metadata keys the control plane itself relies on. Removing one is legal --
#: an operator may genuinely need to -- but it is never something to discover
#: afterwards, so a preview names them separately from ordinary keys.
LOAD_BEARING_METADATA_KEYS: FrozenSet[str] = frozenset(
    {
        "needs_input",
        "needs_input_history",
        "no_dispatch",
        "execution_contract",
        "publication_lane",
        "publication_route",
        "work_package",
        "work_package_id",
        "target_agent_id",
        "target_agent_name",
        "repository_ref_lifecycle",
    }
)

#: Keys the control plane regenerates on every write, so they are never
#: actually lost. Reporting them as removed would be a false alarm, and a
#: preview that cries wolf gets skimmed -- which costs exactly the attention
#: the real warnings need.
_DERIVED_METADATA_KEYS: FrozenSet[str] = frozenset({"execution_contract"})

#: The metadata options, in the order they are applied to one task.
_METADATA_OPTIONS: Tuple[str, ...] = (
    "metadata_replace",
    "metadata_merge",
    "metadata_set",
    "metadata_unset",
)


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge ``overlay`` into ``base``, recursing into nested objects.

    A shallow dict.update replaces a nested object wholesale, so merging
    ``{"origin": {"kind": "x"}}`` would silently drop ``origin.tenant_id``.
    That is the same class of invisible loss as replacing metadata outright,
    just harder to notice.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _set_path(payload: Dict[str, Any], path: str, value: Any) -> Dict[str, Any]:
    """Assign a dotted path, creating intermediate objects."""
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise BatchOperationError("metadata_set needs a key path")
    result = dict(payload)
    cursor = result
    for part in parts[:-1]:
        nxt = cursor.get(part)
        cursor[part] = dict(nxt) if isinstance(nxt, Mapping) else {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return result


def _unset_path(payload: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Remove a dotted path. A path that is already absent is not an error."""
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise BatchOperationError("metadata_unset needs a key path")
    result = dict(payload)
    cursor = result
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, Mapping):
            return result
        cursor[part] = dict(nxt)
        cursor = cursor[part]
    cursor.pop(parts[-1], None)
    return result


def _metadata_after(current: Mapping[str, Any], options: Mapping[str, Any]) -> Dict[str, Any]:
    """The metadata one task would end up with, given the batch's options.

    Shared by the preview and the write, so what is shown is what happens.
    """
    result: Dict[str, Any] = dict(current or {})
    if options.get("metadata_replace") is not None:
        result = dict(options["metadata_replace"])
    if options.get("metadata_merge"):
        result = _deep_merge(result, options["metadata_merge"])
    for path, value in (options.get("metadata_set") or {}).items():
        result = _set_path(result, path, value)
    for path in options.get("metadata_unset") or ():
        result = _unset_path(result, path)
    return result


def _metadata_impact(tasks: Sequence[Any], options: Mapping[str, Any]) -> JsonDict:
    """What a metadata change would remove, across the whole group.

    The gap this closes: a preview that lists ids and titles cannot show that
    an operation is about to erase `needs_input` from four hundred tasks. The
    operation is not forbidden -- it is reported, per key, with the
    load-bearing ones called out, before anything is written.
    """
    removed: Dict[str, int] = {}
    changed_tasks = 0
    for task in tasks:
        before = dict(getattr(task, "metadata", None) or {})
        after = _metadata_after(before, options)
        if after != before:
            changed_tasks += 1
        for key in (set(before) - set(after)) - _DERIVED_METADATA_KEYS:
            removed[key] = removed.get(key, 0) + 1
    load_bearing = {
        key: count
        for key, count in removed.items()
        if key in LOAD_BEARING_METADATA_KEYS
    }
    return {
        "tasks_changed": changed_tasks,
        "removed_keys": dict(sorted(removed.items())),
        # Named separately because losing one of these changes how the control
        # plane treats the task, not just what it records about it.
        "load_bearing_keys_removed": dict(sorted(load_bearing.items())),
    }


class BatchOperation(StrEnum):
    """What a batch does to each task in the group.

    A closed set rather than bare strings, matching how the rest of the
    codebase models task states and agent statuses, so a typo is a lookup
    failure rather than a silent no-match.
    """

    ANSWER = "answer"
    SET = "set"
    CLOSE = "close"
    CANCEL = "cancel"
    REOPEN = "reopen"
    RELEASE = "release"


OPERATIONS: Tuple[str, ...] = tuple(op.value for op in BatchOperation)

#: The options each operation accepts. Anything else is refused rather than
#: dropped: the selector side already refuses unknown keys because a silently
#: ignored term widens a mutation, and the write side must be no laxer -- a
#: swallowed `reasons=` typo would cancel a hundred tasks with no reason at all.
#: States where "no agent can run this" is still a live question. A terminal
#: task is not waiting on the fleet, so it is never unmet.
_DISPATCHABLE_STATES = frozenset(
    {
        TaskState.OPEN.value,
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.NEEDS_INPUT.value,
    }
)

_OPTION_KEYS: Mapping[str, frozenset] = {
    BatchOperation.ANSWER: frozenset({"answer"}),
    BatchOperation.SET: frozenset(
        {
            "title",
            "description",
            "project",
            "priority",
            "required_capabilities",
            "max_attempts",
            "metadata_replace",
            "metadata_merge",
            "metadata_set",
            "metadata_unset",
        }
    ),
    BatchOperation.CLOSE: frozenset({"target_state", "reason"}),
    BatchOperation.CANCEL: frozenset({"reason", "disposition", "replacement_task"}),
    BatchOperation.REOPEN: frozenset({"reason"}),
    BatchOperation.RELEASE: frozenset(),
}


@dataclass(frozen=True)
class TaskSelection:
    """The tasks a selector resolves to right now."""

    selector: TaskSelector
    tasks: Tuple[Any, ...] = ()
    matched: int = 0
    truncated: bool = False

    @property
    def token(self) -> str:
        """A fingerprint of exactly which tasks this is, not how many.

        expect_count only guards cardinality: if one task is cancelled and
        another created between preview and apply, the count is unchanged and
        the batch silently acts on a task nobody previewed.
        """
        digest = hashlib.sha256(
            "\n".join(sorted(task.id for task in self.tasks)).encode("utf-8")
        )
        return digest.hexdigest()[:16]

    def to_dict(self, *, sample: int = 20) -> JsonDict:
        return {
            "selector": self.selector.to_dict(),
            "matched": self.matched,
            "token": self.token,
            "returned": len(self.tasks),
            "truncated": self.truncated,
            "tasks": [_task_summary(task) for task in self.tasks[:sample]],
            "sample_size": min(sample, len(self.tasks)),
        }


@dataclass(frozen=True)
class BatchOutcome:
    """What a batch did, or would do."""

    batch_id: str
    selection_token: str
    operation: str
    selector: str
    applied: bool
    matched: int
    changed: Tuple[str, ...] = ()
    failed: Tuple[JsonDict, ...] = ()
    truncated: bool = False
    #: Present when the operation touches metadata: which keys the group would
    #: lose, and how many tasks lose each. The preview shows this so that
    #: erasing a load-bearing key is a decision rather than a discovery.
    metadata_impact: Optional[JsonDict] = None

    def to_dict(self) -> JsonDict:
        return {
            "batch_id": self.batch_id,
            "selection_token": self.selection_token,
            "operation": self.operation,
            "selector": self.selector,
            "applied": self.applied,
            "matched": self.matched,
            "changed": list(self.changed),
            "changed_count": len(self.changed),
            "failed": list(self.failed),
            "failed_count": len(self.failed),
            "truncated": self.truncated,
            "metadata_impact": self.metadata_impact,
        }


def _task_summary(task: Any) -> JsonDict:
    record = task.to_dict() if hasattr(task, "to_dict") else dict(task)
    metadata = record.get("metadata") or {}
    parked = metadata.get("needs_input") or {}
    return {
        "id": record.get("id"),
        "title": record.get("title"),
        "project": record.get("project"),
        "state": record.get("state"),
        "priority": record.get("priority"),
        "questions": [q.get("question") for q in (parked.get("questions") or [])],
    }


#: Which states each operation can legally act on. Used to predict refusals
#: in a dry run so the preview and the apply agree, rather than discovering
#: them one at a time during the write.
_APPLICABLE_STATES: Mapping[str, frozenset] = {
    BatchOperation.ANSWER: frozenset({TaskState.NEEDS_INPUT.value}),
    BatchOperation.REOPEN: frozenset(
        {TaskState.FAILED.value, TaskState.CANCELLED.value, TaskState.BLOCKED.value}
    ),
}


def _predicted_refusal(task: Any, operation: "BatchOperation") -> Optional[str]:
    """Why this task would refuse the operation, if it obviously would.

    Deliberately partial: it covers the state preconditions a preview can
    check cheaply and without side effects. The apply still catches whatever
    this cannot foresee -- lease fences, package guards -- and reports it the
    same way, so the preview understates refusals rather than inventing them.
    """
    allowed = _APPLICABLE_STATES.get(operation)
    if allowed is not None and task.state not in allowed:
        return "ValidationError: task is %s; %s applies to %s" % (
            task.state,
            operation.value,
            ", ".join(sorted(allowed)),
        )
    return None


class BatchCountMismatch(ValidationError):
    """The group is no longer the one that was previewed."""


class BatchOperationError(ValidationError):
    """The operation or its options are wrong.

    Distinct from SelectorError, which is about the expression that chose the
    group: a caller reporting "your selector is malformed" must not also
    catch "you forgot the answer text".
    """


class UnsatisfiableTaskParker:
    """Park work the fleet provably cannot run, so a person decides its fate.

    A task whose requirements no agent can meet is not blocked and not failed:
    it is ready, and the allocator rejects every candidate forever. Nothing
    surfaces it, so it sits next to an idle fleet looking healthy. That is the
    exact shape of the 2,900-task backlog the assessment measured.

    Parking it states the question -- "no agent can satisfy these
    requirements" -- and puts it in the operator's inbox, where the answer is
    to change the requirement, provision an agent, or cancel the work. All
    three are decisions only a person can make, which is the definition of
    needs_input.

    This is OFF by default and bounded by design. Turning it on can park a
    great many tasks at once, which is only humane because they can be
    answered as a group; the parked-task inbox and this sweep were built for
    each other.
    """

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane

    def sweep(
        self,
        *,
        actor: str = "unsatisfiable-requirements-sweep",
        apply: bool = False,
        limit: Optional[int] = None,
    ) -> JsonDict:
        """Park every open task no agent can satisfy. Dry by default."""
        # Every structural rejection, not only the capability one: hardware,
        # resources, role, and the execution boundary strand a task just as
        # permanently, and the old capability-only check was blind to all four.
        from mac.allocator import AUTHORIZATION_REJECTIONS, REQUIREMENT_REJECTIONS

        codes = ",".join(sorted(REQUIREMENT_REJECTIONS | AUTHORIZATION_REJECTIONS))
        selection = self.control_plane.task_batches.select(
            "state=open unmet=%s" % codes, limit=limit
        )

        parked: List[str] = []
        failed: List[JsonDict] = []
        for task in selection.tasks:
            if not apply:
                parked.append(task.id)
                continue
            try:
                self.control_plane.request_task_input(
                    task.id,
                    [
                        {
                            "question": (
                                "No agent can satisfy this task's requirements. "
                                "Change what it asks for, provision an agent "
                                "that fits, or cancel it."
                            ),
                            "why": "every candidate was rejected for a "
                            "requirement it cannot meet, not for being busy",
                        }
                    ],
                    actor,
                )
            except Exception as exc:  # noqa: BLE001 - per task, never fatal
                failed.append({"id": task.id, "error": "%s: %s" % (type(exc).__name__, exc)})
            else:
                parked.append(task.id)

        return {
            "schema": "mac.unsatisfiable_task_sweep.v1",
            "applied": bool(apply),
            "matched": selection.matched,
            "parked": parked,
            "parked_count": len(parked),
            "failed": failed,
            "truncated": selection.truncated,
            # The group that answers them, ready to paste.
            "inbox_selector": "state=needs_input metadata.needs_input.asked_by=%s" % actor,
        }


class _TaskRow:
    """A task as a selector sees it: the columns that decide and display.

    Building a full Task for every candidate costs about three times the query
    that fetched it, and selection discards most of what it builds -- parsed
    dependency lists, lease fences, publication routes. This view parses the
    two JSON columns lazily and nothing else.
    """

    __slots__ = ("_row", "_metadata", "_capabilities")

    def __init__(self, row: Any) -> None:
        self._row = row
        self._metadata: Any = _UNSET
        self._capabilities: Any = _UNSET

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[name]
        except (KeyError, IndexError, TypeError):
            raise AttributeError(name) from None

    @property
    def metadata(self) -> Dict[str, Any]:
        if self._metadata is _UNSET:
            self._metadata = _load_json(self._row["metadata"], {})
        return self._metadata

    @property
    def required_capabilities(self) -> List[str]:
        if self._capabilities is _UNSET:
            self._capabilities = _load_json(self._row["required_capabilities"], [])
        return self._capabilities

    def to_dict(self) -> JsonDict:
        record = dict(self._row)
        record["metadata"] = self.metadata
        record["required_capabilities"] = self.required_capabilities
        return record


_UNSET = object()


def _load_json(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback


class TaskGroupService:
    """Named, saved task groups.

    A group stores its *expression*, never its members. "Everything parked in
    mac" has to keep meaning that as tasks enter and leave the state; a
    materialised id list would be stale before it was read, and would quietly
    act on the wrong tasks later.
    """

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane

    def resolve(self, name: str) -> Optional[str]:
        row = self.control_plane.store.query_one(
            "SELECT expression FROM task_groups WHERE name = ?", (name,)
        )
        return row["expression"] if row is not None else None

    def get(self, name: str) -> JsonDict:
        row = self.control_plane.store.query_one(
            "SELECT * FROM task_groups WHERE name = ?", (name,)
        )
        if row is None:
            raise NotFoundError("task group %r not found" % name)
        return dict(row)

    def list(self) -> List[JsonDict]:
        return [
            dict(row)
            for row in self.control_plane.store.query_all(
                "SELECT * FROM task_groups ORDER BY name"
            )
        ]

    def save(
        self,
        name: str,
        expression: str,
        *,
        description: str = "",
        actor: str = "human",
    ) -> JsonDict:
        """Create or update a group, refusing one that does not resolve.

        The expression is parsed AND expanded before it is stored, so a group
        that references a missing or self-referential group fails here rather
        than at the moment someone runs a bulk operation against it.
        """
        clean = str(name or "").strip()
        if not clean:
            raise SelectorError("a task group needs a name")
        parsed = parse_selector(expression)
        expand_groups(parsed, self._resolve_excluding(clean))

        now = utcnow()
        self.control_plane.store.execute(
            """
            INSERT INTO task_groups (
                id, name, expression, description, created_by, metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                expression = excluded.expression,
                description = excluded.description,
                updated_at = excluded.updated_at
            """,
            (
                new_id("taskgroup"),
                clean,
                parsed.expression,
                str(description or ""),
                actor,
                now,
                now,
            ),
        )
        return self.get(clean)

    def delete(self, name: str) -> None:
        result = self.control_plane.store.execute(
            "DELETE FROM task_groups WHERE name = ?", (name,)
        )
        if not getattr(result, "rowcount", 1):
            raise NotFoundError("task group %r not found" % name)

    def _resolve_excluding(self, name: str) -> Any:
        """Resolver that hides the group being saved.

        Saving `a` as `group=a` must fail as a self-reference rather than
        silently resolving to whatever `a` used to be.
        """

        def resolve(other: str) -> Optional[str]:
            if other == name:
                raise SelectorError("task group %r references itself" % name)
            return self.resolve(other)

        return resolve


class TaskBatchService:
    """Selector-scoped reads and audited bulk writes over tasks."""

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane

    # -- reading ----------------------------------------------------------

    def select(
        self,
        selector: Any,
        *,
        limit: Optional[int] = None,
    ) -> TaskSelection:
        """Resolve a selector to the tasks it currently names.

        SQL narrows on the indexed columns; JSON-valued and fleet-derived
        terms are confirmed in Python. The fleet is evaluated at most once for
        the whole scan rather than once per task.
        """
        parsed = (
            selector
            if isinstance(selector, TaskSelector)
            else parse_selector(str(selector))
        )
        parsed = expand_groups(parsed, self.control_plane.task_groups.resolve)
        if parsed.is_empty:
            # parse_selector refuses an empty expression, but TaskSelector() is
            # constructible directly and would match every task in the ledger.
            raise SelectorError(
                "empty selector: refusing to match every task by accident"
            )
        where, params = compile_sql(parsed)
        store = self.control_plane.store
        effective = limit if limit is not None else parsed.limit
        order = " ORDER BY priority DESC, created_at"

        if parsed.sql_complete():
            # SQL decides, so the size of the group is a COUNT rather than a
            # scan: the preview no longer pays for rows it will not show, and
            # `matched` stays exact however large the group is. This is what
            # makes a limit a display choice instead of a correctness one.
            total = store.query_one(
                "SELECT COUNT(*) AS n FROM tasks" + where, tuple(params)
            )
            matched = int(total["n"]) if total else 0
            fetch = "SELECT * FROM tasks" + where + order
            fetch_params = list(params)
            if effective is not None:
                fetch += " LIMIT ?"
                fetch_params.append(int(effective))
            selected = [
                _TaskRow(row) for row in store.query_all(fetch, tuple(fetch_params))
            ]
            truncated = effective is not None and matched > len(selected)
        else:
            # A term SQL cannot express (unmet, or a negated JSON path) still
            # needs every candidate judged in Python. The rows are wrapped in a
            # light view rather than built into full Task objects -- measured
            # at 3x the cost of the query itself for rows that are then mostly
            # discarded.
            rows = store.query_all("SELECT * FROM tasks" + where + order, tuple(params))
            candidates = [_TaskRow(row) for row in rows]
            unmet_by_task: Dict[str, Sequence[str]] = {}
            if parsed.requires_fleet_evaluation():
                unmet_by_task = self._unmet_requirements(candidates)
            selected = [
                task
                for task in candidates
                if matches(parsed, task, unmet_codes=unmet_by_task.get(task.id, ()))
            ]
            matched = len(selected)
            truncated = effective is not None and matched > int(effective)
            if truncated:
                selected = selected[: int(effective)]
        return TaskSelection(
            selector=parsed,
            tasks=tuple(selected),
            matched=matched,
            truncated=truncated,
        )

    def _unmet_requirements(self, tasks: Sequence[Any]) -> Dict[str, Sequence[str]]:
        """Requirement verdicts for a whole scan, sharing one fleet snapshot."""
        from mac.allocator import classify_requirement_eligibility

        dispatch = self.control_plane.dispatch
        agents = list(self.control_plane.list_agents())
        agent_snapshots = [dispatch._v2_snapshot_agent(agent) for agent in agents]
        projects = {
            record.name: record for record in self.control_plane.list_project_records()
        }
        agent_ids_by_name: Dict[str, List[str]] = {}
        for agent in agents:
            agent_ids_by_name.setdefault(agent.name, []).append(agent.id)

        # classify_requirement_eligibility deliberately neutralises the task
        # gates, which is right for "can the fleet ever run this?" but wrong as
        # a selector: without a state bound, `unmet=... cancel` re-touches every
        # already-cancelled task in the ledger. Only states where dispatch is
        # still conceivable can be unmet.
        verdicts: Dict[str, Sequence[str]] = {}
        for task in tasks:
            if task.state not in _DISPATCHABLE_STATES:
                continue
            snapshot = dispatch._v2_snapshot_task(
                task,
                projects=projects,
                agent_ids_by_name=agent_ids_by_name,
                dependencies_satisfied_override=True,
                package_ready_override=True,
            )
            verdict = classify_requirement_eligibility(snapshot, agent_snapshots)
            verdicts[task.id] = tuple(verdict.unmet_requirements)
        return verdicts

    # -- writing ----------------------------------------------------------

    def apply(
        self,
        selector: Any,
        operation: str,
        *,
        actor: str = "human",
        apply: bool = False,
        expect_count: Optional[int] = None,
        expect_token: Optional[str] = None,
        limit: Optional[int] = None,
        **options: Any,
    ) -> BatchOutcome:
        """Run ``operation`` over the selected group.

        Defaults to a dry run: the returned outcome lists exactly the tasks
        that would change, and nothing is written. ``expect_count`` is the
        guard for scripted use -- it refuses when the group is not the size
        the caller last saw, so a batch written against a preview cannot
        silently act on a larger set later.
        """
        # Validate BEFORE selecting, and in both modes. This used to live
        # inside the per-task loop, which the dry run skipped entirely -- so a
        # preview of `answer` with no answer text happily reported "40 tasks
        # would change" and then failed all 40 on apply. The preview has to be
        # the same code path as the apply or it is not a preview.
        resolved = self._validate_operation(operation, options)
        selection = self.select(selector, limit=limit)
        if expect_count is not None and selection.matched != int(expect_count):
            raise BatchCountMismatch(
                "selector now matches %d task(s), not the %d expected; "
                "re-run the preview before applying"
                % (selection.matched, int(expect_count))
            )
        if expect_token is not None and selection.token != expect_token:
            raise BatchCountMismatch(
                "the selected group is no longer the one previewed (token %s, "
                "expected %s); re-run the preview before applying"
                % (selection.token, expect_token)
            )
        if apply and selection.truncated:
            # expect_count compares the FULL match count while the loop walks
            # the truncated slice, so a limited apply could pass the guard and
            # then silently touch a different set than the operator approved.
            raise BatchCountMismatch(
                "selector matches %d task(s) but the batch is limited to %d; "
                "refusing to apply a silently truncated group -- raise the "
                "limit or narrow the selector"
                % (selection.matched, len(selection.tasks))
            )

        batch_id = new_id("batch")
        changed: List[str] = []
        failed: List[JsonDict] = []

        for task in selection.tasks:
            predicted = _predicted_refusal(task, resolved)
            if predicted is not None:
                # Reported in BOTH modes. The dry run used to list every
                # selected task as "would change" without consulting the state
                # machine, so a preview could promise 40 changes and the apply
                # deliver none.
                failed.append({"id": task.id, "error": predicted})
                continue
            if not apply:
                changed.append(task.id)
                continue
            try:
                self._apply_one(task, resolved, actor=actor, batch_id=batch_id, **options)
            except Exception as exc:  # noqa: BLE001 - reported per task, never fatal
                # One refused transition must not cost the rest of the batch.
                failed.append(
                    {"id": task.id, "error": "%s: %s" % (type(exc).__name__, exc)[:400]}
                )
            else:
                changed.append(task.id)

        outcome = BatchOutcome(
            batch_id=batch_id,
            selection_token=selection.token,
            operation=str(resolved.value),
            selector=selection.selector.expression,
            applied=bool(apply),
            matched=selection.matched,
            changed=tuple(changed),
            failed=tuple(failed),
            truncated=selection.truncated,
            metadata_impact=(
                _metadata_impact(selection.tasks, options)
                if any(options.get(key) is not None for key in _METADATA_OPTIONS)
                else None
            ),
        )
        if apply:
            self._record(outcome, actor=actor)
        return outcome

    def _validate_operation(self, operation: str, options: Mapping[str, Any]) -> "BatchOperation":
        """Resolve the operation and check its options, before touching anything."""
        try:
            resolved = BatchOperation(operation)
        except ValueError:
            raise BatchOperationError(
                "unknown batch operation %r; valid operations: %s"
                % (operation, ", ".join(OPERATIONS))
            ) from None

        allowed = _OPTION_KEYS[resolved]
        supplied = {key for key, value in options.items() if value is not None}
        unknown = sorted(supplied - allowed)
        if unknown:
            raise BatchOperationError(
                "%s does not take %s; it accepts: %s"
                % (
                    resolved.value,
                    ", ".join(unknown),
                    ", ".join(sorted(allowed)) or "no options",
                )
            )
        if resolved is BatchOperation.ANSWER and not str(options.get("answer") or "").strip():
            raise BatchOperationError("answer requires the answer text")
        if resolved is BatchOperation.SET and not supplied:
            raise BatchOperationError(
                "set requires at least one field to change: %s"
                % ", ".join(sorted(allowed))
            )
        if resolved is BatchOperation.CLOSE:
            target = str(options.get("target_state") or TaskState.COMPLETED.value)
            valid = {TaskState.COMPLETED.value, TaskState.CANCELLED.value, TaskState.FAILED.value}
            if target not in valid:
                raise BatchOperationError(
                    "close target_state %r must be one of %s"
                    % (target, ", ".join(sorted(valid)))
                )
        return resolved

    def _apply_one(
        self,
        task: Any,
        operation: "BatchOperation",
        *,
        actor: str,
        batch_id: str,
        **options: Any,
    ) -> None:
        cp = self.control_plane
        if operation is BatchOperation.ANSWER:
            # `disposition` became REQUIRED on answer_task_input while this
            # branch was open: answering is a judgement about whether the
            # answer releases the task or closes it, and it deliberately has
            # no default at the service layer. A batch answer means "these
            # tasks can go back to the queue", which is exactly RESUME, and
            # this branch's own test says so -- "answering a group returns all
            # of them to the queue". An explicit override is honoured for
            # callers that mean the other thing.
            cp.answer_task_input(
                task.id,
                str(options["answer"]).strip(),
                actor,
                disposition=str(
                    options.get("disposition") or cp.ANSWER_RESUME
                ).strip().lower(),
            )
            return
        if operation is BatchOperation.SET:
            fields = {
                key: value
                for key, value in options.items()
                if key in _OPTION_KEYS[BatchOperation.SET]
                and key not in _METADATA_OPTIONS
                and value is not None
            }
            if any(options.get(key) is not None for key in _METADATA_OPTIONS):
                # Re-read rather than using the snapshot taken before the loop:
                # a long batch would otherwise revert whatever a worker wrote
                # to the task while it was running.
                current = dict(cp.get_task(task.id).metadata or {})
                fields["metadata"] = _metadata_after(current, options)
            cp.update_task(task.id, actor=actor, **fields)
            return
        if operation is BatchOperation.CLOSE:
            cp.close_task(
                task.id,
                str(options.get("target_state") or TaskState.COMPLETED.value),
                actor,
                _batch_detail(batch_id, options.get("reason")),
            )
            return
        if operation is BatchOperation.CANCEL:
            # Cancellation is close_task to CANCELLED with a disposition, the
            # same shape `mac task cancel` uses -- so the disposition rules
            # (superseded needs a replacement, and so on) apply unchanged.
            from mac.models import TaskState

            detail = _batch_detail(
                batch_id, options.get("reason") or "batch cancellation"
            )
            detail["disposition"] = options.get("disposition") or "preserve"
            if options.get("replacement_task"):
                detail["replacement_task_id"] = options["replacement_task"]
            cp.close_task(task.id, TaskState.CANCELLED.value, actor, detail)
            return
        if operation is BatchOperation.REOPEN:
            cp.reopen_task(task.id, actor, options.get("reason"))
            return
        if operation is BatchOperation.RELEASE:
            cp.release_task(task.id, actor=actor)
            return
        raise BatchOperationError("unhandled batch operation %r" % operation)

    def _record(self, outcome: BatchOutcome, *, actor: str) -> None:
        """One observation for the whole batch, so it reviews as one act."""
        try:
            self.control_plane.record_log(
                "task.batch.applied",
                layer="control_plane",
                source="operator",
                detail={
                    "batch_id": outcome.batch_id,
                    "operation": outcome.operation,
                    "selector": outcome.selector,
                    "actor": actor,
                    "matched": outcome.matched,
                    "changed_count": len(outcome.changed),
                    # The ids, not just the count: "which tasks did batch X
                    # touch?" is the first question asked after a bad batch,
                    # and four of the six operations do not stamp the batch id
                    # onto the task itself.
                    "changed": list(outcome.changed),
                    "failed": list(outcome.failed),
                    "failed_count": len(outcome.failed),
                    "truncated": outcome.truncated,
                },
            )
        except Exception:  # noqa: BLE001 - telemetry must never fail the batch
            pass


def _batch_detail(batch_id: str, reason: Optional[str]) -> JsonDict:
    detail: JsonDict = {"batch_id": batch_id}
    if reason:
        detail["reason"] = str(reason)
    return detail
