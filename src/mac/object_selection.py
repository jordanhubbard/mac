"""One selector grammar, for every first-class object.

``task_selection`` gave tasks a selector -- ``state=open project=mac``,
``state!=completed``, ``priority>=5`` -- and it is the right shape. It was also
task-only and reachable only from ``mac task select`` and ``mac task batch``,
so the obvious spelling of the obvious question did not work::

    mac task list --selector 'state!=cancelled'      # no such flag
    mac project list --selector 'dispatch=paused'    # no such concept

This module generalises the SAME grammar to project and agent,
and to the CRUD verbs, without introducing a second syntax. Two selector
languages would be worse than one incomplete one: the whole value of the
expression is that it means the same thing in the CLI, the API, a ticket and a
message to a colleague.

What is shared and what is not:

* The GRAMMAR and the parser come from ``task_selection`` unchanged. A term is
  ``key op value``, values are comma-separated for any-of, and ``!=`` negates.
* The ATTRIBUTES are per object. ``state`` means something for a task and
  nothing for a project, and an unknown key is an ERROR naming the valid ones
  for that object -- never a silently-dropped term. A dropped term widens the
  group, and the group is what a bulk mutation is about to run against.
* The EVALUATION here is in Python over records the caller already has. Tasks
  keep their SQL compiler for scale; projects, agents and work packages are
  small collections that ``list`` has already loaded, so filtering them in
  Python costs nothing and avoids four new SQL paths that could each be subtly
  wrong.

On applying a selector to a MUTATION. The operator's reason for wanting it is
exact: "the only way to clean what may be thousands of bad tasks". So it is
supported, and every mutating path is DRY BY DEFAULT -- it reports what it
would touch and changes nothing until ``--apply``. A selector typo that
narrows costs a re-run; one that widens costs the ledger.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mac.models import ValidationError
from mac.task_selection import SelectorError, Term, _TERM, _split_terms


class ObjectAttributes:
    """The keys a selector may use against one kind of object.

    ``text`` keys compare as strings, ``numeric`` keys as numbers, and
    ``boolean`` keys accept true/false spellings so ``paused=true`` reads the
    way an operator would write it.
    """

    def __init__(
        self,
        name: str,
        text: Mapping[str, str],
        numeric: Mapping[str, str] = (),
        boolean: Mapping[str, str] = (),
        list_keys: Mapping[str, str] = (),
    ) -> None:
        self.name = name
        self.text = dict(text)
        self.numeric = dict(numeric or {})
        self.boolean = dict(boolean or {})
        self.list_keys = dict(list_keys or {})

    @property
    def valid_keys(self) -> Tuple[str, ...]:
        return tuple(
            sorted(
                list(self.text)
                + list(self.numeric)
                + list(self.boolean)
                + list(self.list_keys)
                + ["metadata.<path>"]
            )
        )

    def field_for(self, key: str) -> Optional[Tuple[str, str]]:
        """(kind, record field) for a selector key, or None if unknown."""
        if key.startswith("metadata."):
            return ("metadata", key[len("metadata.") :])
        for kind, table in (
            ("text", self.text),
            ("numeric", self.numeric),
            ("boolean", self.boolean),
            ("list", self.list_keys),
        ):
            if key in table:
                return (kind, table[key])
        return None


#: Per-object attribute registries.
#:
#: Only attributes an operator would plausibly select on. A registry that
#: mirrored every column would be a worse answer than a small one: every key
#: here is a promise that filtering on it behaves sensibly.
OBJECTS: Dict[str, ObjectAttributes] = {
    "task": ObjectAttributes(
        "task",
        text={
            "id": "id",
            "state": "state",
            "project": "project",
            "title": "title",
            "description": "description",
            "owner": "owner_agent_id",
        },
        numeric={
            "priority": "priority",
            "attempts": "attempt_count",
            "max_attempts": "max_attempts",
        },
        list_keys={"capability": "required_capabilities", "dependency": "dependencies"},
    ),
    "project": ObjectAttributes(
        "project",
        text={"name": "name", "id": "name", "description": "description"},
        boolean={"paused": "dispatch_paused"},
    ),
    "agent": ObjectAttributes(
        "agent",
        text={
            "id": "id",
            "name": "name",
            "status": "status",
            "machine": "machine_id",
            "task": "current_task_id",
        },
        numeric={"capacity": "capacity", "active_leases": "active_leases"},
        list_keys={"capability": "capabilities"},
    ),
}


def valid_keys(object_name: str) -> Tuple[str, ...]:
    attributes = OBJECTS.get(object_name)
    return attributes.valid_keys if attributes else ()


def parse(expression: str, object_name: str) -> Tuple[Term, ...]:
    """Parse a selector for one object kind, refusing keys it does not have.

    An empty selector is refused rather than treated as "everything". Matching
    every record by accident is precisely the failure mode that makes this
    feature dangerous on a mutating verb.
    """
    attributes = OBJECTS.get(object_name)
    if attributes is None:
        raise SelectorError(
            "no selector attributes are defined for %r; known objects: %s"
            % (object_name, ", ".join(sorted(OBJECTS)))
        )
    text = (expression or "").strip()
    if not text:
        raise SelectorError(
            "empty selector: refusing to match every %s by accident. State at "
            "least one term. Valid keys: %s" % (object_name, ", ".join(attributes.valid_keys))
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
        if attributes.field_for(key) is None:
            raise SelectorError(
                "%r is not a selectable attribute of %s. Valid keys: %s"
                % (key, object_name, ", ".join(attributes.valid_keys))
            )
        raw = match.group("value")
        values = tuple(v.strip() for v in raw.split(",") if v.strip()) or ("",)
        terms.append(Term(key=key, op=match.group("op"), values=values))
    return tuple(terms)


def _record_value(record: Any, field: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


def _metadata_value(record: Any, path: str) -> Any:
    node = _record_value(record, "metadata")
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _term_matches(term: Term, record: Any, attributes: ObjectAttributes) -> bool:
    kind, field = attributes.field_for(term.key)  # type: ignore[misc]
    if kind == "metadata":
        actual = _metadata_value(record, field)
    else:
        actual = _record_value(record, field)

    if kind == "boolean":
        wanted = _as_bool(term.values[0])
        if wanted is None:
            raise SelectorError("%s takes true or false, got %r" % (term.key, term.values[0]))
        hit = bool(actual) is wanted
        return hit if term.op in {"=", "~"} else not hit

    if kind == "numeric":
        try:
            current = float(actual)
        except (TypeError, ValueError):
            # A record with no value cannot satisfy a bound. It also must not
            # crash the scan: one odd row should not stop a bulk cleanup.
            return term.op == "!="
        try:
            bounds = [float(v) for v in term.values]
        except ValueError:
            raise SelectorError("%s expects a number, got %r" % (term.key, ",".join(term.values)))
        if term.op == ">=":
            return current >= bounds[0]
        if term.op == "<=":
            return current <= bounds[0]
        if term.op == "!=":
            return all(current != b for b in bounds)
        return any(current == b for b in bounds)

    if kind == "list":
        have = {str(item) for item in (actual or [])}
        if term.op == "~":
            hit = any(any(v in item for item in have) for v in term.values)
        else:
            hit = bool(have & set(term.values))
        return hit if term.op in {"=", "~"} else not hit

    current_text = "" if actual is None else str(actual)
    if term.op == "~":
        return any(v.lower() in current_text.lower() for v in term.values)
    if term.op in {">=", "<="}:
        raise SelectorError("%s is not numeric; %s needs a numeric attribute" % (term.key, term.op))
    hit = current_text in set(term.values)
    return hit if term.op == "=" else not hit


def matches(record: Any, terms: Sequence[Term], object_name: str) -> bool:
    attributes = OBJECTS[object_name]
    return all(_term_matches(term, record, attributes) for term in terms)


def filter_records(records: Iterable[Any], expression: str, object_name: str) -> List[Any]:
    """Every record the expression names, in the order given."""
    terms = parse(expression, object_name)
    return [r for r in records if matches(r, terms, object_name)]


def render(terms: Sequence[Term]) -> str:
    return " ".join(term.render() for term in terms)
