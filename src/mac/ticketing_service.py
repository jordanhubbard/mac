"""Ticketing detection + one-way conversion, extracted from ``ControlPlane``.

``ControlPlane`` is a ~13.9k-line, 400+-method god-object. It already composes
~22 focused sub-services; the debt the architecture review flagged is that
cohesive domain clusters kept accreting as methods on the god-object instead of
being delegated to services. This module extracts the ticketing cluster
(``detect_ticketing`` / ``convert_ticketing_source``) as the first of that
follow-on delegation, establishing the pattern (a small service holding a
control-plane reference; the god-object keeps thin delegation shims for API
compatibility). See ``task_bf0d1f01`` for the full extraction plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mac.models import JsonDict


class TicketingCoordinator:
    """Detects foreign ticket sources in a repo and runs the one-way import
    into the MAC ledger. Never writes back to the foreign source."""

    def __init__(self, control_plane: Any) -> None:
        # Held for record_log telemetry and to hand the connector a control
        # plane for ledger writes during conversion.
        self._cp = control_plane

    def detect_ticketing(self, repo_path: str) -> JsonDict:
        """Report which ticketing sources a repo has + whether a one-way ledger
        import should be offered (foreign source present, no local .tickets
        compatibility mirror). Read-only. Emits a
        ``ticketing.conversion_available`` observation the hub's hermes agent
        can surface to the user."""
        from mac.ticketing import detect_ticketing as _detect

        detection = _detect(Path(repo_path))
        if detection.needs_conversion:
            self._cp.record_log(
                "ticketing.conversion_available",
                layer="control_plane",
                source="ticketing",
                level="info",
                subject_type="environment",
                subject_id=str(repo_path),
                detail={
                    "schema": "mac.ticketing_conversion.v1",
                    "conversion_from": detection.conversion_from,
                    "message": detection.message,
                    "prompt": (
                        "Repo %s has a '%s' ticket source but no local .tickets "
                        "compatibility mirror. Import it one-way into the MAC "
                        "task ledger?" % (repo_path, detection.conversion_from)
                    ),
                },
            )
        return detection.to_dict()

    def convert_ticketing_source(
        self,
        repo_path: str,
        *,
        project: str,
        actor: str = "hermes",
        dry_run: bool = False,
    ) -> JsonDict:
        """Run the one-way conversion of a detected foreign source (e.g. beads)
        into MAC ledger tasks plus optional local compatibility files. Hermes
        calls this only after the user agrees. Never writes back to the foreign
        source."""
        from mac.ticketing import connector_for, detect_ticketing as _detect

        detection = _detect(Path(repo_path))
        if not detection.needs_conversion or not detection.conversion_from:
            return {"status": "no_conversion_needed", "detection": detection.to_dict()}
        connector = connector_for(detection.conversion_from)
        if connector is None:
            return {"status": "unknown_connector", "detection": detection.to_dict()}
        report = connector.convert(
            Path(repo_path),
            project=project,
            cp=None if dry_run else self._cp,
            actor=actor,
            dry_run=dry_run,
        )
        self._cp.record_log(
            "ticketing.converted",
            layer="control_plane",
            source="ticketing",
            level="info",
            subject_type="environment",
            subject_id=str(repo_path),
            detail={
                "schema": "mac.ticketing_conversion.v1",
                "from": detection.conversion_from,
                "report": report,
            },
        )
        return {"status": "converted", "from": detection.conversion_from, "report": report}


__all__ = ["TicketingCoordinator"]
