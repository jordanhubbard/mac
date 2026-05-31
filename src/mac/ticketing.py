"""Ticketing connector abstraction — "meta tickets" for the rest of the system.

Why this exists
---------------
beads was painful to remove because it had **no abstraction boundary**: beads
specific reads, writes, polling, and dolt sync were scattered across ~100 call
sites in the control-plane lifecycle (claim / evidence / publish / review /
transition / heartbeat / dispatch). Every ticketing system is essentially the
same shape — issues with an id, title, body, state, priority, and dependencies —
so the rest of MAC should never see a *specific* system. It should see a
:class:`MetaTicket` behind a :class:`TicketingConnector`.

The model
---------
* :class:`MetaTicket` — the system-agnostic ticket the rest of MAC consumes.
* :class:`TicketingConnector` — a pluggable source. Two kinds:
    - **canonical** (``is_writeback=False``, ``is_canonical=True``): the native
      store. For MAC that's ``.tickets/<id>.md`` + the ``mac task`` ledger.
    - **import-only** (``is_writeback=False``, ``is_canonical=False``): a
      *one-way* importer, e.g. beads. It can *detect* and *convert* a foreign
      source into native tickets but is **never** read or written as a live
      source afterwards.
    - **writeback** (``is_writeback=True``): a connector that also mirrors MAC
      lifecycle events back into an external system (e.g. a future Jira/GitHub
      connector). The lifecycle hooks (``on_task_*``) exist for these; the
      canonical and import-only connectors no-op them.

beads is deliberately an **import-only** connector here — that is the whole
point of "remove beads as a read/write source, keep one-way conversion."

Detection / conversion flow
---------------------------
:func:`detect_ticketing` reports which sources are present and, crucially, sets
``needs_conversion`` when a repo has a foreign source (``.beads``) but no native
``.tickets``. The hub surfaces that to the user through hermes, which asks
whether to run the one-way conversion (:meth:`TicketingConnector.convert`).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "MetaTicket",
    "TicketSourceReport",
    "TicketingDetection",
    "TicketingConnector",
    "NativeTicketingConnector",
    "BeadsImportConnector",
    "available_connectors",
    "detect_ticketing",
]


# ---------------------------------------------------------------------------
# Meta-ticket: the system-agnostic shape the rest of MAC sees
# ---------------------------------------------------------------------------


@dataclass
class MetaTicket:
    """A ticket as the rest of MAC sees it — independent of the source system."""

    id: str
    title: str
    description: str = ""
    state: str = "open"
    priority: int = 0
    dependencies: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    source: str = "native"          # connector name that produced it
    external_id: Optional[str] = None  # the id in the source system, if foreign
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketSourceReport:
    """Result of a connector probing a repo path (read-only, no side effects)."""

    connector: str
    present: bool
    is_canonical: bool
    ticket_count: int = 0
    open_count: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketingDetection:
    """What ticketing sources a repo has + the recommended action."""

    repo_path: str
    sources: List[TicketSourceReport]
    needs_conversion: bool
    conversion_from: Optional[str] = None  # connector name to convert FROM
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "sources": [vars(s) for s in self.sources],
            "needs_conversion": self.needs_conversion,
            "conversion_from": self.conversion_from,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Connector interface
# ---------------------------------------------------------------------------


class TicketingConnector(abc.ABC):
    """A pluggable ticketing source. Hides all implementation details behind
    :class:`MetaTicket`."""

    #: Stable short identifier.
    name: str = "connector"
    #: True only for the native/default store.
    is_canonical: bool = False
    #: True only for connectors that mirror MAC lifecycle events into an
    #: external system. Import-only + canonical connectors leave this False —
    #: they are NOT a read/write source for live operation.
    is_writeback: bool = False

    @abc.abstractmethod
    def detect(self, repo_path: Path) -> TicketSourceReport:
        """Probe ``repo_path`` for this source. Read-only, no side effects."""

    def import_tickets(self, repo_path: Path) -> List[MetaTicket]:
        """One-way read of the source's tickets as :class:`MetaTicket`. Default:
        none (override for sources that support import)."""
        return []

    def convert(
        self,
        repo_path: Path,
        *,
        project: str,
        cp: Any = None,
        actor: str = "ticketing-connector",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """One-way conversion of this source into native ``.tickets/`` (+ the
        ledger when ``cp`` is given). Default: not supported."""
        raise NotImplementedError("%s does not support conversion" % self.name)

    # -- lifecycle mirror hooks (no-op unless is_writeback) ------------------
    # These are the single seam the control plane calls; a writeback connector
    # overrides them. Beads is import-only, so it never implements these — which
    # is exactly why beads is no longer a read/write source.
    def on_task_claimed(self, ticket: MetaTicket, agent_id: str) -> None:  # noqa: D401
        return None

    def on_task_closed(self, ticket: MetaTicket, reason: str) -> None:
        return None

    def on_task_transitioned(self, ticket: MetaTicket, from_state: str, to_state: str) -> None:
        return None

    def on_evidence_added(self, ticket: MetaTicket, evidence: Dict[str, Any]) -> None:
        return None

    def on_review_claimed(self, ticket: MetaTicket, reviewer_id: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Native connector — .tickets/ + the mac task ledger (canonical)
# ---------------------------------------------------------------------------

_TICKETS_DIR = ".tickets"


class NativeTicketingConnector(TicketingConnector):
    """MAC's own source: ``.tickets/<id>.md`` mirror + the ``mac task`` ledger.

    Canonical, so no writeback is needed — the ledger *is* the store. The
    lifecycle hooks stay no-ops."""

    name = "native"
    is_canonical = True
    is_writeback = False

    def detect(self, repo_path: Path) -> TicketSourceReport:
        repo_path = Path(repo_path).expanduser()
        tickets_dir = repo_path / _TICKETS_DIR
        files = sorted(tickets_dir.glob("*.md")) if tickets_dir.is_dir() else []
        return TicketSourceReport(
            connector=self.name,
            present=tickets_dir.is_dir(),
            is_canonical=True,
            ticket_count=len(files),
            detail={"tickets_dir": str(tickets_dir)},
        )

    def import_tickets(self, repo_path: Path) -> List[MetaTicket]:
        repo_path = Path(repo_path).expanduser()
        tickets_dir = repo_path / _TICKETS_DIR
        tickets: List[MetaTicket] = []
        if not tickets_dir.is_dir():
            return tickets
        for path in sorted(tickets_dir.glob("*.md")):
            fm = _read_frontmatter(path)
            tickets.append(
                MetaTicket(
                    id=str(fm.get("id") or path.stem),
                    title=str(fm.get("title") or path.stem),
                    state=str(fm.get("status") or "open"),
                    priority=int(fm.get("priority") or 0) if str(fm.get("priority") or "").strip().isdigit() else 0,
                    source=self.name,
                    metadata={k: v for k, v in fm.items() if k not in {"id", "title", "status", "priority"}},
                )
            )
        return tickets


def _read_frontmatter(path: Path) -> Dict[str, Any]:
    """Parse the leading ``--- ... ---`` YAML-ish frontmatter of a ticket md
    file. Intentionally dependency-free + forgiving (key: value lines)."""
    out: Dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Beads connector — IMPORT-ONLY (one-way conversion), never a live source
# ---------------------------------------------------------------------------


class BeadsImportConnector(TicketingConnector):
    """Read-only beads → tickets importer. beads is NOT a read/write source:
    this connector only *detects* a ``.beads`` directory and performs a one-way
    *conversion* into native ``.tickets/``. It implements no lifecycle hooks."""

    name = "beads"
    is_canonical = False
    is_writeback = False

    def detect(self, repo_path: Path) -> TicketSourceReport:
        from mac.beads_migrator import detect as _detect

        report = _detect(Path(repo_path).expanduser())
        return TicketSourceReport(
            connector=self.name,
            present=report.has_beads_dir,
            is_canonical=False,
            ticket_count=report.issue_count,
            open_count=report.open_count,
            detail={
                "has_issues_jsonl": report.has_issues_jsonl,
                "has_embeddeddolt": report.has_embeddeddolt,
                "closed_count": report.closed_count,
            },
        )

    def import_tickets(self, repo_path: Path) -> List[MetaTicket]:
        from mac.beads_migrator import _read_issues_jsonl, BEADS_DIR_NAME, ISSUES_JSONL

        jsonl = Path(repo_path).expanduser() / BEADS_DIR_NAME / ISSUES_JSONL
        tickets: List[MetaTicket] = []
        if not jsonl.is_file():
            return tickets
        for issue in _read_issues_jsonl(jsonl):
            tickets.append(
                MetaTicket(
                    id=str(issue.get("id") or ""),
                    title=str(issue.get("title") or issue.get("summary") or ""),
                    description=str(issue.get("description") or issue.get("body") or ""),
                    state=str(issue.get("status") or "open"),
                    source=self.name,
                    external_id=str(issue.get("id") or "") or None,
                    metadata={"original": issue},
                )
            )
        return tickets

    def convert(
        self,
        repo_path: Path,
        *,
        project: str,
        cp: Any = None,
        actor: str = "ticketing-connector",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """One-way beads → native ``.tickets/`` conversion (and the ledger when
        ``cp`` is supplied). Delegates to the migrator; never writes back to
        beads."""
        from mac.beads_migrator import migrate

        report = migrate(
            Path(repo_path).expanduser(),
            cp,
            project=project,
            actor=actor,
            dry_run=dry_run,
            emit_tickets=True,
            tickets_only=cp is None,
        )
        return report.to_dict()


# ---------------------------------------------------------------------------
# Registry + detection
# ---------------------------------------------------------------------------


def available_connectors() -> List[TicketingConnector]:
    """All known connectors. Native first (canonical), then importers.

    A future Jira/GitHub/Linear connector is added here and the rest of MAC is
    unchanged — that is the payoff of the abstraction."""
    return [NativeTicketingConnector(), BeadsImportConnector()]


def detect_ticketing(repo_path: Path) -> TicketingDetection:
    """Detect which ticketing sources a repo has and whether a one-way
    conversion should be offered.

    The rule the user asked for: if there is **no** native ``.tickets/`` but a
    foreign source (``.beads``) **is** present, flag ``needs_conversion`` so the
    hub's hermes agent can ask the user whether to convert."""
    repo_path = Path(repo_path).expanduser()
    reports = [c.detect(repo_path) for c in available_connectors()]
    by_name = {r.connector: r for r in reports}
    native = by_name.get("native")
    native_present = bool(native and native.present and native.ticket_count > 0)

    conversion_from = None
    if not native_present:
        for report in reports:
            if not report.is_canonical and report.present:
                conversion_from = report.connector
                break
    needs_conversion = conversion_from is not None

    if needs_conversion:
        message = (
            "%s has a '%s' source but no native .tickets/. Offer the user a "
            "one-way conversion to .tickets (mac task ledger)." % (repo_path, conversion_from)
        )
    elif native_present:
        message = "Native .tickets/ present; no conversion needed."
    else:
        message = "No ticketing source detected."

    return TicketingDetection(
        repo_path=str(repo_path),
        sources=reports,
        needs_conversion=needs_conversion,
        conversion_from=conversion_from,
        message=message,
    )


def connector_for(name: str) -> Optional[TicketingConnector]:
    for connector in available_connectors():
        if connector.name == name:
            return connector
    return None
