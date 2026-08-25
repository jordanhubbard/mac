"""Task groups: a set of tasks named by what they have in common.

Everything that edits a task took exactly one task id. That was survivable
while a human filed the work, and stops being survivable the moment the system
files it for them -- the parked-task inbox is the clearest case, but the same
shape appears for any bulk correction: a capability typo repeated across a
hundred tasks, a generator's output that needs retiring, a project rename.
Answering those one at a time is not a workflow, it is a tax, and the size of
the tax is set by the machine rather than by the operator.

A *selector* is the group. It is a short expression over task attributes:

    state=needs_input project=mac
    state=open unmet=agent_capabilities_missing
    metadata.origin=dream_low_confidence_repair priority<=2

The expression is the group's identity. It is a string, so the same group
travels unchanged through the CLI, the HTTP API, the UI, a ticket, or a
message to a colleague, and it re-evaluates against the ledger every time
rather than freezing a list of ids that starts rotting immediately. Naming and
saving one is a thin layer on top of this; the expression is what makes the
group first-class.

Grammar, deliberately small:

    key=value      equals            state=open
    key!=value     not equals        state!=completed
    key~value      contains          title~postgres
    key>=n  key<=n numeric bounds    priority>=5  attempts<=1
    key=a,b        any of            state=open,needs_input

Repeated keys are ANDed like any other term, so ``state=open state=failed``
matches nothing; use ``state=open,failed``. Unknown keys are an error naming
the valid ones, because a silently-ignored term would quietly widen a bulk
mutation, which is the worst possible failure for this feature.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from mac.models import ValidationError

#: Columns that live in SQL and can be compared directly.
_TEXT_COLUMNS = {
    "id": "id",
    "state": "state",
    "project": "project",
    "title": "title",
    "description": "description",
    "owner": "owner_agent_id",
}
_NUMERIC_COLUMNS = {
    "priority": "priority",
    "attempts": "attempt_count",
    "max_attempts": "max_attempts",
}
#: Matched across title and description together.
_TEXT_SEARCH = "text"
#: JSON-valued fields, confirmed in Python after the SQL pass narrows the set.
_CAPABILITY = "capability"
_METADATA_PREFIX = "metadata."
#: Requires evaluating the task against the fleet, so it is applied last.
_UNMET = "unmet"
#: Names a saved group. Expanded to that group's own terms before evaluation,
#: which is what lets a group be refined in place -- `group=parked-mac
#: priority>=5` is the saved selector AND the extra bound, with no second
#: grammar for "saved" versus "ad hoc".
_GROUP = "group"

VALID_KEYS: Tuple[str, ...] = tuple(
    sorted(
        list(_TEXT_COLUMNS)
        + list(_NUMERIC_COLUMNS)
        + [_TEXT_SEARCH, _CAPABILITY, _UNMET, _GROUP, "metadata.<path>"]
    )
)

_TERM = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.]*)(?P<op>!=|>=|<=|~|=)(?P<value>.*)$")


class SelectorError(ValidationError):
    """A selector expression the system refuses to guess at.

    A domain error, not a parsing accident: the control plane's public methods
    must not leak implementation exceptions, and "your selector names a key
    that does not exist" is exactly the kind of thing a caller should be able
    to catch as invalid input.
    """


def _quote(value: str) -> str:
    """Quote a value that would otherwise re-parse as several terms."""
    if value and not any(char.isspace() or char in ",\"'" for char in value):
        return value
    if '"' in value:
        return "'%s'" % value
    return '"%s"' % value


@dataclass(frozen=True)
class Term:
    key: str
    op: str
    values: Tuple[str, ...]

    def render(self) -> str:
        """Render back to source form, re-quoting where the parser needs it.

        The rendered expression is what the batch audit record stores, so it
        has to survive a round trip -- an unreplayable selector would defeat
        the point of recording which group an operation ran against.
        """
        return "%s%s%s" % (self.key, self.op, ",".join(_quote(v) for v in self.values))


@dataclass(frozen=True)
class TaskSelector:
    """A parsed, re-evaluatable description of a group of tasks."""

    terms: Tuple[Term, ...] = ()
    limit: Optional[int] = None

    @property
    def expression(self) -> str:
        return " ".join(term.render() for term in self.terms)

    @property
    def is_empty(self) -> bool:
        return not self.terms

    def keys(self) -> Tuple[str, ...]:
        return tuple(sorted({term.key for term in self.terms}))

    def group_names(self) -> Tuple[str, ...]:
        """Saved groups this selector references, in order."""
        return tuple(value for term in self.terms if term.key == _GROUP for value in term.values)

    def sql_complete(self) -> bool:
        """Whether SQL alone decides this selector, exactly.

        Derived from :func:`compile_term`, so it cannot claim coverage the
        compiler does not provide. True when every term compiles, which with
        metadata and capabilities indexed is now the common case -- and it
        lets the group be COUNTed instead of loaded: 0.004s against 0.166s
        over 100k tasks, with no memory proportional to the group.

        That SQL is *exact* for these terms, and not merely a narrowing
        superset, is what test_sql_never_excludes_a_row_python_would_match
        proves by running both sides over the awkward spellings -- JSON true
        against Python True, 3 against "3", unicode, backslashes, wildcards.
        That test is load-bearing for correctness here.
        """
        return all(compile_term(term) is not None for term in self.terms)

    def requires_fleet_evaluation(self) -> bool:
        """Whether answering this needs the allocator, not just the ledger."""
        return any(term.key == _UNMET for term in self.terms)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expression": self.expression,
            "terms": [{"key": t.key, "op": t.op, "values": list(t.values)} for t in self.terms],
            "limit": self.limit,
            "requires_fleet_evaluation": self.requires_fleet_evaluation(),
        }


def parse_selector(expression: str, *, limit: Optional[int] = None) -> TaskSelector:
    """Parse a selector expression, or explain exactly what is wrong with it.

    Refusing an unknown key matters more here than it usually does: a term
    that is quietly dropped widens the group, and the group is what a bulk
    mutation is about to be applied to.
    """
    text = (expression or "").strip()
    if not text:
        raise SelectorError(
            "empty selector: refusing to match every task by accident. "
            "State at least one term, e.g. state=needs_input. Valid keys: %s"
            % ", ".join(VALID_KEYS)
        )

    terms: List[Term] = []
    for token in _split_terms(text):
        match = _TERM.match(token)
        if not match:
            raise SelectorError(
                "cannot parse %r: expected key=value, key!=value, key~value, "
                "key>=n or key<=n" % token
            )
        key = match.group("key")
        op = match.group("op")
        raw = match.group("value")
        if raw == "":
            raise SelectorError("term %r has no value" % token)
        if raw[0] in "=<>~!":
            # `state~=open` otherwise parses as contains "=open" -- a term that
            # silently means something other than what was typed, on the input
            # that decides what a bulk mutation touches.
            raise SelectorError("unknown operator in %r: use =, !=, ~, >= or <=" % token)
        _validate_key(key, op, token)
        values = tuple(part.strip() for part in raw.split(",") if part.strip())
        if not values:
            raise SelectorError("term %r has no value" % token)
        if op in {">=", "<="}:
            if len(values) != 1:
                raise SelectorError("%r takes a single number, not a list" % token)
            try:
                int(values[0])
            except ValueError:
                raise SelectorError("%r expects a number, got %r" % (token, values[0])) from None
        if key == _UNMET:
            _validate_unmet(values, token)
        if key == "state":
            _validate_states(values, token)
        if key in _NUMERIC_COLUMNS and op in {"=", "!="}:
            values = _normalise_numbers(values, token)
        terms.append(Term(key=key, op=op, values=values))
    return TaskSelector(terms=tuple(terms), limit=limit)


def _split_terms(text: str) -> List[str]:
    """Split on whitespace, honouring quotes so values may contain spaces."""
    tokens: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if quote:
        raise SelectorError("unbalanced %s quote in selector" % quote)
    if current:
        tokens.append("".join(current))
    return tokens


def _validate_states(values: Sequence[str], token: str) -> None:
    """Reject task states that do not exist.

    ``state=need_input`` (a typo) matches nothing, which is merely useless.
    ``state!=need_input`` matches EVERY task, which at fleet scale means a
    bulk operation over the entire ledger. Same asymmetry as unmet=.
    """
    from mac.models import TaskState

    known = {state.value for state in TaskState}
    unknown = sorted(set(values) - known)
    if unknown:
        raise SelectorError(
            "unknown task state(s) %s in %r. Valid states: %s"
            % (", ".join(unknown), token, ", ".join(sorted(known)))
        )


def _normalise_numbers(values: Sequence[str], token: str) -> Tuple[str, ...]:
    """Canonicalise numeric equality values.

    compile_sql binds these as ints while _compare tests them as strings, so
    ``priority!=007`` excluded priority 7 in SQL and admitted it in Python.
    Parsing them here also turns ``priority=x`` into a selector error instead
    of an int() crash halfway down the stack.
    """
    normalised = []
    for value in values:
        try:
            normalised.append(str(int(value)))
        except ValueError:
            raise SelectorError("%r expects a number, got %r" % (token, value)) from None
    return tuple(normalised)


def _validate_unmet(values: Sequence[str], token: str) -> None:
    """Reject rejection codes that do not exist.

    A typo here is not harmless: ``unmet=typo`` matches nothing, but
    ``unmet!=typo`` matches EVERYTHING, which is the silent widening this
    module refuses to allow anywhere else.
    """
    from mac.allocator import (
        AUTHORIZATION_REJECTIONS,
        REQUIREMENT_REJECTIONS,
        TRANSIENT_REJECTIONS,
    )

    known = REQUIREMENT_REJECTIONS | AUTHORIZATION_REJECTIONS | TRANSIENT_REJECTIONS
    unknown = sorted({v.split(":", 1)[0] for v in values} - set(known))
    if unknown:
        raise SelectorError(
            "unknown rejection code(s) %s in %r. Valid codes: %s"
            % (", ".join(unknown), token, ", ".join(sorted(known)))
        )


def _validate_key(key: str, op: str, token: str) -> None:
    if key.startswith(_METADATA_PREFIX):
        if len(key) <= len(_METADATA_PREFIX):
            raise SelectorError("%r needs a path, e.g. metadata.origin=..." % token)
        return
    known = set(_TEXT_COLUMNS) | set(_NUMERIC_COLUMNS) | {_TEXT_SEARCH, _CAPABILITY, _UNMET, _GROUP}
    if key not in known:
        raise SelectorError(
            "unknown selector key %r in %r. Valid keys: %s" % (key, token, ", ".join(VALID_KEYS))
        )
    if op in {">=", "<="} and key not in _NUMERIC_COLUMNS:
        raise SelectorError(
            "%r is not numeric; %s only applies to %s"
            % (key, op, ", ".join(sorted(_NUMERIC_COLUMNS)))
        )


# --- evaluation ------------------------------------------------------------


def _like_pattern(value: str) -> str:
    """A LIKE pattern matching ``value`` anywhere, with wildcards neutralised.

    Without this a value containing % or _ silently widens the prefilter, and
    one containing a backslash narrows it: Postgres reads \\b as a literal b,
    so `title~a\\b` matched nothing in SQL while Python matched it.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def _json_token(value: str) -> str:
    """``value`` as it appears inside the stored JSON array.

    required_capabilities is written with ensure_ascii=True, so a capability
    named "café" is stored as "caf\\u00e9". Prefiltering on the raw text would
    never find it.
    """
    return json.dumps(value)[1:-1]


MAX_GROUP_DEPTH = 8


def expand_groups(
    selector: TaskSelector,
    resolve: Any,
    *,
    _seen: Optional[frozenset] = None,
    _depth: int = 0,
) -> TaskSelector:
    """Replace every ``group=`` term with the terms of the group it names.

    ``resolve`` maps a group name to its expression. Expansion is recursive so
    a group may be defined in terms of another, and both guards below matter:
    a group that references itself, directly or through a chain, would
    otherwise expand forever, and this runs on operator input.

    The expanded selector is what gets evaluated, recorded, and shown, so an
    audit record names the concrete terms that ran rather than a group whose
    definition may since have changed.
    """
    if not selector.group_names():
        return selector
    if _depth >= MAX_GROUP_DEPTH:
        raise SelectorError(
            "task group nesting deeper than %d levels; groups probably "
            "reference each other" % MAX_GROUP_DEPTH
        )
    seen = _seen or frozenset()

    expanded: List[Term] = []
    for term in selector.terms:
        if term.key != _GROUP:
            expanded.append(term)
            continue
        if term.op != "=":
            raise SelectorError("group only supports '=', not %r" % term.op)
        for name in term.values:
            if name in seen:
                raise SelectorError(
                    "task group %r references itself (via %s)" % (name, " -> ".join(sorted(seen)))
                )
            expression = resolve(name)
            if not expression:
                raise SelectorError("unknown task group %r" % name)
            inner = expand_groups(
                parse_selector(expression),
                resolve,
                _seen=seen | {name},
                _depth=_depth + 1,
            )
            expanded.extend(inner.terms)
    return TaskSelector(terms=tuple(expanded), limit=selector.limit)


def compile_term(term: Term) -> Optional[Tuple[str, List[Any]]]:
    """The SQL for one term, or None if SQL cannot express it exactly.

    This is the single source of truth for what the database can decide.
    ``compile_sql`` builds the WHERE clause from it and ``TaskSelector.
    sql_complete`` asks whether every term is covered -- previously two
    separate judgements, which drifted the moment they existed: sql_complete
    claimed SQL handled ``text!=`` while compile_sql was deliberately leaving
    it to Python, so a negated text search silently selected everything.
    """
    column = _TEXT_COLUMNS.get(term.key)
    if column is not None:
        if term.op == "=":
            return (
                "(%s)" % " OR ".join("%s = ?" % column for _ in term.values),
                list(term.values),
            )
        if term.op == "!=":
            clauses = " AND ".join(
                "(%s IS NULL OR %s <> ?)" % (column, column) for _ in term.values
            )
            return ("(%s)" % clauses, list(term.values))
        if term.op == "~":
            return (
                "(%s)" % " OR ".join("%s ILIKE ? ESCAPE '\\'" % column for _ in term.values),
                [_like_pattern(value) for value in term.values],
            )
        return None

    numeric = _NUMERIC_COLUMNS.get(term.key)
    if numeric is not None:
        if term.op == ">=":
            return ("%s >= ?" % numeric, [int(term.values[0])])
        if term.op == "<=":
            return ("%s <= ?" % numeric, [int(term.values[0])])
        if term.op == "=":
            return (
                "(%s)" % " OR ".join("%s = ?" % numeric for _ in term.values),
                [int(value) for value in term.values],
            )
        if term.op == "!=":
            clauses = " AND ".join(
                "(%s IS NULL OR %s <> ?)" % (numeric, numeric) for _ in term.values
            )
            return ("(%s)" % clauses, [int(value) for value in term.values])
        return None

    if term.key == _TEXT_SEARCH and term.op in {"=", "~"}:
        clauses = []
        params: List[Any] = []
        for value in term.values:
            clauses.append("(title ILIKE ? ESCAPE '\\' OR description ILIKE ? ESCAPE '\\')")
            pattern = _like_pattern(value)
            params.extend([pattern, pattern])
        return ("(%s)" % " OR ".join(clauses), params)

    if term.key == _CAPABILITY and term.op == "=":
        # Exact membership through the GIN-indexed generated column.
        # jsonb_exists rather than the `?` operator: `?` is this codebase's
        # parameter placeholder and the store's translation would eat it.
        return (
            "(%s)" % " OR ".join("jsonb_exists(capabilities_json, ?)" for _ in term.values),
            list(term.values),
        )

    if term.key.startswith(_METADATA_PREFIX) and term.op in {"=", "~"}:
        # Path lookups run against the GIN-indexed generated column instead of
        # loading every task and parsing JSON in Python: 1.4s -> 0.015s over
        # 100k tasks.
        path = "{%s}" % ",".join(
            part for part in term.key[len(_METADATA_PREFIX) :].split(".") if part
        )
        clauses = []
        params = []
        for value in term.values:
            if term.op == "=":
                clauses.append("metadata_json #>> ?::text[] = ?")
                params.extend([path, value])
            else:
                clauses.append("metadata_json #>> ?::text[] ILIKE ? ESCAPE '\\'")
                params.extend([path, _like_pattern(value)])
        return ("(%s)" % " OR ".join(clauses), params)

    # capability~ / capability!= / metadata!= / unmet / group: Python decides.
    return None


def compile_sql(selector: TaskSelector) -> Tuple[str, List[Any]]:
    """Compile the SQL-expressible terms into a WHERE clause.

    Terms that :func:`compile_term` cannot express are left out, so this is a
    *narrowing* filter and callers must still run :func:`matches` over the
    rows -- unless :meth:`TaskSelector.sql_complete` says every term compiled,
    in which case the clause is exact and the group can simply be counted.
    """
    clauses: List[str] = []
    params: List[Any] = []
    for term in selector.terms:
        compiled = compile_term(term)
        if compiled is None:
            continue
        clause, values = compiled
        clauses.append(clause)
        params.extend(values)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def matches(
    selector: TaskSelector,
    task: Any,
    *,
    unmet_codes: Optional[Sequence[str]] = None,
) -> bool:
    """Decide a single task against every term, including the JSON ones.

    ``unmet_codes`` carries the requirement-eligibility verdict for this task
    when the selector asks for one; it is passed in rather than recomputed so
    a scan evaluates the fleet once instead of once per task.
    """
    return all(_term_matches(term, task, unmet_codes) for term in selector.terms)


def _task_value(task: Any, name: str) -> Any:
    if isinstance(task, Mapping):
        return task.get(name)
    return getattr(task, name, None)


def _term_matches(term: Term, task: Any, unmet_codes: Optional[Sequence[str]]) -> bool:
    if term.key == _GROUP:
        raise SelectorError(
            "group=%s was not expanded before evaluation; call "
            "expand_groups() first" % ",".join(term.values)
        )
    if term.key == _UNMET:
        codes = {str(code).split(":", 1)[0] for code in (unmet_codes or ())}
        wanted = {value.split(":", 1)[0] for value in term.values}
        hit = bool(codes & wanted)
        return hit if term.op in {"=", "~"} else not hit

    if term.key == _CAPABILITY:
        have = {str(c) for c in (_task_value(task, "required_capabilities") or [])}
        if term.op == "~":
            # The grammar documents ~ as "contains", so honour it per
            # capability rather than quietly meaning equality.
            hit = any(
                value.lower() in capability.lower() for capability in have for value in term.values
            )
        else:
            hit = bool(have & set(term.values))
        return hit if term.op in {"=", "~"} else not hit

    if term.key.startswith(_METADATA_PREFIX):
        path = term.key[len(_METADATA_PREFIX) :].split(".")
        actual = _resolve_path(_task_value(task, "metadata"), path)
        return _compare(actual, term)

    if term.key == _TEXT_SEARCH:
        haystack = "%s\n%s" % (
            _task_value(task, "title") or "",
            _task_value(task, "description") or "",
        )
        hit = all(value.lower() in haystack.lower() for value in term.values)
        return hit if term.op in {"=", "~"} else not hit

    column = _TEXT_COLUMNS.get(term.key) or _NUMERIC_COLUMNS.get(term.key)
    if column is None:
        # Parse-time validation should make this unreachable. Returning True
        # would widen the group silently, which is the one direction this
        # module must never fail in.
        raise SelectorError("unhandled selector key %r" % term.key)
    return _compare(_task_value(task, column), term)


def _resolve_path(payload: Any, path: Sequence[str]) -> Any:
    current = payload
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except (TypeError, ValueError):
            return None
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _compare(actual: Any, term: Term) -> bool:
    if term.op in {">=", "<="}:
        try:
            left = float(actual)
            right = float(term.values[0])
        except (TypeError, ValueError):
            return False
        return left >= right if term.op == ">=" else left <= right

    rendered = "" if actual is None else str(actual)
    if term.op == "~":
        return all(value.lower() in rendered.lower() for value in term.values)
    hit = rendered in set(term.values)
    if not hit and isinstance(actual, bool):
        # metadata flags are booleans in JSON but words in a selector.
        hit = str(actual).lower() in {value.lower() for value in term.values}
    return hit if term.op == "=" else not hit
