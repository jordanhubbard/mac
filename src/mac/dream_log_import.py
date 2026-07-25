"""Merge orphaned gateway dream-cycle reports into MAC's durable memory store.

The gateway runs a host-side dream cycle (``~/.hermes/scripts/dream_cycle.py``,
not MAC code) that writes human-readable reports to ``$HERMES_HOME/dream_logs/``
(``dream_YYYYMMDD_HHMMSS.md``). No first-party MAC code reads that directory, so
the learning captured there — error patterns, human corrections, near-failure
skill correlations, action items — never reaches MAC's durable learning store,
which is ``memory_records`` (record_type ``dream:*``) in the ledger.

This importer parses those reports and writes each substantive one into
``memory_records`` as a ``dream:imported_report`` memory, so the stranded
learning is consolidated into the location MAC actually consumes. It is:

  * **idempotent** — dedupes by content hash, so re-running imports nothing new;
  * **noise-averse** — skips empty "No ... detected" reports (the vast majority),
    to avoid the kind of firehose that bloats the store;
  * **path-clean** — resolves the source directory via ``mac_paths`` only.

Same shape works for any other single-use-but-wrong-path metadata: point it at
the misplaced directory and it consolidates into the ledger.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from mac import mac_paths
from mac.models import MacMemoryTier, json_dumps

IMPORT_SCHEMA = "mac.dream_log_import.v1"
IMPORTED_RECORD_TYPE = "dream:imported_report"
IMPORTED_SUBJECT_TYPE = "dream"
DEFAULT_CREATED_BY = "dream-log-import"

_TITLE_RE = re.compile(r"^#\s*Dream Cycle Report\s*[—\-:]\s*(?P<when>.+?)\s*$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^Analyzed\b.*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
# A section body is "empty" if it only says nothing was found.
_EMPTY_BODY_RE = re.compile(
    r"^\s*No\b.*(detected|this cycle|found|correlations)\s*\.?\s*$",
    re.IGNORECASE,
)


def parse_dream_report(text: str) -> Dict[str, Any]:
    """Parse one dream-cycle markdown report into a structured dict.

    Returns keys: ``generated_at`` (str|None), ``summary`` (str|None), and
    ``sections`` (ordered dict of heading -> body text).
    """
    title = _TITLE_RE.search(text)
    summary = _SUMMARY_RE.search(text)
    sections: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections[match.group("name").strip()] = text[start:end].strip()
    return {
        "generated_at": title.group("when").strip() if title else None,
        "summary": summary.group(0).strip() if summary else None,
        "sections": sections,
    }


def report_is_empty(parsed: Dict[str, Any]) -> bool:
    """True when every section reports that nothing was found (no learning)."""
    sections = parsed.get("sections") or {}
    if not sections:
        return True
    for body in sections.values():
        stripped = (body or "").strip()
        if stripped and not _EMPTY_BODY_RE.match(stripped):
            return False
    return True


def _findings_digest(parsed: Dict[str, Any]) -> str:
    """Hash the FINDINGS (section bodies), not the raw file.

    The gateway writes a report every hour; most repeat the same findings with
    only a different ``# Dream Cycle Report — <time>`` header and "Analyzed N
    messages" line. Keying dedup on the normalized section content collapses
    those into a single durable memory (AMANALAP: merge the learning, not the
    hourly timestamp noise). Falls back to the whole body if no sections parse.
    """
    sections = parsed.get("sections") or {}
    basis = "\n".join(
        "%s\n%s" % (name.strip().lower(), (body or "").strip())
        for name, body in sorted(sections.items())
    ) if sections else ""
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _existing_hashes(cp: Any) -> set:
    """Content hashes already imported, so re-runs are idempotent."""
    hashes: set = set()
    try:
        rows = cp.store.query_all(
            "SELECT content FROM memory_records WHERE record_type = ?",
            (IMPORTED_RECORD_TYPE,),
        )
    except Exception:
        return hashes
    import json as _json

    for row in rows:
        try:
            payload = _json.loads(row["content"])
        except Exception:
            continue
        digest = payload.get("content_hash") if isinstance(payload, dict) else None
        if digest:
            hashes.add(digest)
    return hashes


def import_dream_logs(
    cp: Any,
    *,
    dream_logs_dir: Optional[Path] = None,
    agent_id: Optional[str] = None,
    created_by: str = DEFAULT_CREATED_BY,
    vector_writer: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Consolidate ``$HERMES_HOME/dream_logs/*.md`` into durable memory.

    ``cp`` is a ControlPlane. When ``vector_writer`` is provided, each imported
    memory is also embedded into the medium tier so it is retrievable via
    ``recall_dream_artifacts`` (dream memories that are not embedded never
    surface in vector recall — the whole point of the merge). Returns a stable
    report dict. When ``dry_run`` is set, nothing is written.
    """
    directory = Path(dream_logs_dir) if dream_logs_dir is not None else mac_paths.dream_logs_dir()
    report: Dict[str, Any] = {
        "schema": IMPORT_SCHEMA,
        "source_dir": str(directory),
        "scanned": 0,
        "imported": 0,
        "embedded": 0,
        "skipped_empty": 0,
        "skipped_duplicate": 0,
        "dry_run": dry_run,
        "errors": [],
        "imported_ids": [],
    }
    if not directory.is_dir():
        report["errors"].append({"error": "dream_logs directory not found", "path": str(directory)})
        return report

    seen = _existing_hashes(cp)
    files: List[Path] = sorted(p for p in directory.glob("*.md") if p.is_file())
    for path in files:
        report["scanned"] += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report["errors"].append({"file": path.name, "error": str(exc)})
            continue
        parsed = parse_dream_report(text)
        if report_is_empty(parsed):
            report["skipped_empty"] += 1
            continue
        digest = _findings_digest(parsed)
        if digest in seen:
            report["skipped_duplicate"] += 1
            continue
        seen.add(digest)
        payload = {
            "schema": IMPORT_SCHEMA,
            "source": "hermes_dream_logs",
            "source_file": path.name,
            "generated_at": parsed.get("generated_at"),
            "summary": parsed.get("summary"),
            "sections": parsed.get("sections"),
            "content_hash": digest,
        }
        if dry_run:
            report["imported"] += 1
            continue
        try:
            memory = cp.add_memory(
                task_id=None,
                subject_type=IMPORTED_SUBJECT_TYPE,
                subject_id=agent_id,
                record_type=IMPORTED_RECORD_TYPE,
                content=json_dumps(payload),
                evidence_id=None,
                created_by=created_by,
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the merge
            report["errors"].append({"file": path.name, "error": str(exc)})
            continue
        report["imported"] += 1
        report["imported_ids"].append(memory.id)
        if vector_writer is not None:
            try:
                vector_writer.embed_memory(
                    memory.id,
                    tier=MacMemoryTier.MEDIUM.value,
                    created_by=created_by,
                )
                report["embedded"] += 1
            except Exception as exc:  # noqa: BLE001 - memory persists even if embed fails
                report["errors"].append(
                    {"file": path.name, "phase": "embed", "error": str(exc)}
                )
    return report
