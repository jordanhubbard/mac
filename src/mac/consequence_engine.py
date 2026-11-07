"""
consequence_engine.py — Fleet event observer that auto-writes soul nodes.

Design
------
The consequence engine watches fleet events (GitHub PR merges, MAC task
closures, etc.) and writes soul nodes automatically. It answers the question:
"what did we learn when that PR merged?" — without requiring any agent to
manually journal the event.

This is distinct from:
  - github_ingest.py:        GitHub *issues* → MAC tasks (backlog manufacturing)
  - dream_candidate_entries: Session-log distillation (periodic, async)
  - soul_add via MCP:        Manual agent writes during active sessions

The engine writes *consequence nodes* — nodes tagged [fleet, consequence] that
capture the operational meaning of an event, not just its occurrence. Each node
is append-only; the watermark is a soul_node tagged [watermark, consequence_engine]
that records the last PR number processed.

Phase 1: GitHub PR merges → soul nodes.
Phase 2 (future): MAC task closures, failed-then-fixed patterns.

CLI
---
  python -m mac.consequence_engine --repo jordanhubbard/mac \\
    --agent bullwinkle \\
    --db-url postgresql://fleet_agent@100.72.16.110:5432/agent_brain \\
    [--dry-run]

Returns 0 on success, 1 on error. Prints a JSON summary to stdout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("mac.consequence_engine")

WATERMARK_NODE_ID = "consequence_engine_watermark"
WATERMARK_TAG = "consequence_engine"
CONSEQUENCE_TAG = "consequence"
SCHEMA_VERSION = "mac.consequence_engine.v1"


# ---------------------------------------------------------------------------
# GitHub helpers (stdlib only — no extra deps)
# ---------------------------------------------------------------------------


def _gh_token() -> str:
    """Read GitHub token from environment (same vars as gitops.token_for_host)."""
    return os.environ.get("GH_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()


def _gh_api(path: str, token: str, params: Optional[Dict[str, str]] = None) -> Any:
    """Call GitHub REST API and return parsed JSON."""
    import urllib.request
    import urllib.parse

    base = f"https://api.github.com{path}"
    if params:
        base = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(base)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_merged_prs_since(
    owner: str,
    repo: str,
    since_pr_number: int,
    token: str,
    max_prs: int = 20,
) -> List[Dict[str, Any]]:
    """Return merged PRs with number > since_pr_number, oldest first.

    Uses the GitHub search API to find recently merged PRs. Returns up to
    max_prs PRs (bounded to prevent run-away on a busy repo).
    """
    # GitHub search: merged, sorted by created desc, then we filter and invert
    query = f"repo:{owner}/{repo} is:pr is:merged"
    results = _gh_api(
        "/search/issues",
        token,
        params={"q": query, "sort": "created", "order": "desc", "per_page": "50"},
    )
    items = results.get("items", [])
    # Keep only those with number > watermark
    new_prs = [pr for pr in items if pr.get("number", 0) > since_pr_number]
    # Oldest first so we process in order
    new_prs.sort(key=lambda p: p.get("number", 0))
    return new_prs[:max_prs]


def fetch_pr_detail(owner: str, repo: str, number: int, token: str) -> Dict[str, Any]:
    """Fetch full PR detail including files changed."""
    pr = _gh_api(f"/repos/{owner}/{repo}/pulls/{number}", token)
    # Fetch changed files (first page — 30 items covers most PRs)
    try:
        files_data = _gh_api(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            token,
            params={"per_page": "30"},
        )
        pr["_files"] = files_data
    except Exception:
        pr["_files"] = []
    return pr


# ---------------------------------------------------------------------------
# Consequence node synthesis
# ---------------------------------------------------------------------------


def _categorize_files(files: List[Dict[str, Any]]) -> Dict[str, int]:
    """Group changed files into coarse categories: src, tests, docs, scripts, config."""
    cats: Dict[str, int] = {}
    for f in files:
        path = f.get("filename", "")
        if path.startswith("tests/") or path.startswith("test_"):
            cats["tests"] = cats.get("tests", 0) + 1
        elif path.startswith("src/"):
            cats["src"] = cats.get("src", 0) + 1
        elif path.startswith("docs/") or path.endswith(".md"):
            cats["docs"] = cats.get("docs", 0) + 1
        elif path.startswith("scripts/"):
            cats["scripts"] = cats.get("scripts", 0) + 1
        else:
            cats["config"] = cats.get("config", 0) + 1
    return cats


def _infer_kind(title: str, files: List[Dict[str, Any]]) -> str:
    """Infer PR kind: feature, fix, docs, refactor, test, chore."""
    t = title.lower()
    if re.search(r"\bfix\b|bug|patch|repair|resolve\b", t):
        return "fix"
    if re.search(r"\bdocs?\b|readme|changelog", t):
        return "docs"
    if re.search(r"\brefactor\b|cleanup|clean.up|simplif", t):
        return "refactor"
    if re.search(r"\btest\b|spec\b", t):
        return "test"
    if re.search(r"\bchore\b|bump\b|update deps|lint\b", t):
        return "chore"
    return "feature"


def _impact_summary(title: str, files: List[Dict[str, Any]], body: str) -> str:
    """Generate a concise impact statement for the soul node."""
    cats = _categorize_files(files)
    kind = _infer_kind(title, files)
    n_files = len(files)

    # Pull first non-empty line of body as context, if present
    body_lead = ""
    if body:
        for line in body.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and len(line) > 10:
                body_lead = line[:200]
                break

    cat_summary = ", ".join(f"{v} {k}" for k, v in sorted(cats.items()))
    impact = f"Kind: {kind}. Changed {n_files} file(s) ({cat_summary or 'misc'}). "
    if body_lead:
        impact += f"Author note: {body_lead}"
    return impact.strip()


def synthesize_consequence_node(
    pr: Dict[str, Any],
    owner: str,
    repo: str,
    agent: str,
) -> Dict[str, Any]:
    """Build the soul node dict for a merged PR.

    Returns a dict ready for direct Postgres insert into soul_nodes.
    """
    number = pr["number"]
    title = pr.get("title", "(no title)")
    merged_at = pr.get("pull_request", {}).get("merged_at") or pr.get("merged_at", "")
    html_url = pr.get("html_url", f"https://github.com/{owner}/{repo}/pull/{number}")
    files = pr.get("_files", [])
    body = pr.get("body") or ""

    impact = _impact_summary(title, files, body)
    merge_ts = merged_at[:10] if merged_at else "unknown"

    content = (
        f"## PR #{number} merged ({merge_ts}): {title}\n\n"
        f"Repo: {owner}/{repo}  URL: {html_url}\n\n"
        f"{impact}\n\n"
        f"Schema: {SCHEMA_VERSION}"
    )

    node_id = f"pr_merge_{owner.replace('/', '_')}_{repo}_{number}"

    return {
        "id": node_id,
        "content": content,
        "tags": ["fleet", CONSEQUENCE_TAG, "pr_merge"],
        "parents": [],
        "pinned": False,
        "score": 1.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_accessed": datetime.now(timezone.utc).isoformat(),
        "access_count": 0,
        "metadata": {
            "visibility": "fleet",
            "consequence": True,
            "pr_number": number,
            "repo": f"{owner}/{repo}",
            "merged_at": merged_at,
            "kind": _infer_kind(title, files),
            "schema": SCHEMA_VERSION,
        },
        "agent": agent,
    }


# ---------------------------------------------------------------------------
# Postgres watermark helpers
# ---------------------------------------------------------------------------


def _get_watermark(conn: Any, agent: str) -> int:
    """Return the last processed PR number (0 if none recorded)."""
    cur = conn.execute(
        "SELECT metadata->>'last_pr_number' FROM soul_nodes WHERE id = %s AND agent = %s",
        (WATERMARK_NODE_ID, agent),
    )
    row = cur.fetchone()
    if row and row[0]:
        try:
            return int(row[0])
        except (ValueError, TypeError):
            return 0
    return 0


def _upsert_watermark(conn: Any, agent: str, last_pr_number: int) -> None:
    """Update or create the watermark node."""
    now = datetime.now(timezone.utc).isoformat()
    meta = json.dumps({"last_pr_number": last_pr_number, "schema": SCHEMA_VERSION})
    # Try update first
    cur = conn.execute(
        "UPDATE soul_nodes SET metadata = %s::jsonb, updated_at = %s WHERE id = %s AND agent = %s",
        (meta, now, WATERMARK_NODE_ID, agent),
    )
    if cur.rowcount == 0:
        # Insert
        content = f"## Consequence engine watermark\nLast processed PR: #{last_pr_number}"
        conn.execute(
            """
            INSERT INTO soul_nodes
              (id, content, tags, parents, pinned, score,
               created_at, updated_at, last_accessed, access_count, metadata, agent)
            VALUES (%s, %s, %s::text[], %s::text[], %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                WATERMARK_NODE_ID,
                content,
                [WATERMARK_TAG, "watermark"],
                [],
                False,
                1.0,
                now,
                now,
                now,
                0,
                meta,
                agent,
            ),
        )


def _insert_node(conn: Any, node: Dict[str, Any]) -> bool:
    """Insert a consequence node. Returns True if inserted, False if already exists."""
    # Check existence first
    cur = conn.execute(
        "SELECT 1 FROM soul_nodes WHERE id = %s",
        (node["id"],),
    )
    if cur.fetchone():
        return False  # already exists (idempotent)

    conn.execute(
        """
        INSERT INTO soul_nodes
          (id, content, tags, parents, pinned, score,
           created_at, updated_at, last_accessed, access_count, metadata, agent)
        VALUES (%s, %s, %s::text[], %s::text[], %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            node["id"],
            node["content"],
            node["tags"],
            node["parents"],
            node["pinned"],
            node["score"],
            node["created_at"],
            node["updated_at"],
            node["last_accessed"],
            node["access_count"],
            json.dumps(node["metadata"]),
            node["agent"],
        ),
    )
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


class ConsequenceEngine:
    """Watch fleet events and write soul nodes for significant state changes."""

    def __init__(
        self,
        db_url: str,
        agent: str,
        repo: str = "jordanhubbard/mac",
        dry_run: bool = False,
        max_prs: int = 20,
    ):
        self.db_url = db_url
        self.agent = agent
        owner_repo = repo.split("/", 1)
        if len(owner_repo) != 2:
            raise ValueError(f"repo must be owner/repo, got: {repo!r}")
        self.owner, self.repo_name = owner_repo
        self.dry_run = dry_run
        self.max_prs = max_prs

    def _connect(self) -> Any:
        import psycopg  # type: ignore[import]

        return psycopg.connect(self.db_url, autocommit=False)

    def run(self) -> Dict[str, Any]:
        """Run one consequence engine pass. Returns a summary dict."""
        token = _gh_token()
        if not token:
            return {"error": "No GH_TOKEN or GITHUB_TOKEN in environment", "written": 0}

        conn = self._connect()
        try:
            watermark = _get_watermark(conn, self.agent)
            _log.info(
                "Consequence engine: watermark=%d repo=%s/%s",
                watermark,
                self.owner,
                self.repo_name,
            )

            merged_prs = fetch_merged_prs_since(
                self.owner, self.repo_name, watermark, token, max_prs=self.max_prs
            )
            _log.info("Found %d new merged PRs since #%d", len(merged_prs), watermark)

            written = []
            skipped = []
            errors = []
            highest_number = watermark

            for pr_stub in merged_prs:
                number = pr_stub["number"]
                try:
                    pr = fetch_pr_detail(self.owner, self.repo_name, number, token)
                    # Merge stub metadata (search API has pull_request.merged_at)
                    if "pull_request" in pr_stub:
                        pr.setdefault("merged_at", pr_stub["pull_request"].get("merged_at"))
                    pr["_files"] = pr.get("_files", [])

                    node = synthesize_consequence_node(pr, self.owner, self.repo_name, self.agent)

                    if self.dry_run:
                        written.append({"pr": number, "node_id": node["id"], "dry_run": True})
                    else:
                        inserted = _insert_node(conn, node)
                        if inserted:
                            written.append({"pr": number, "node_id": node["id"]})
                        else:
                            skipped.append(number)

                    highest_number = max(highest_number, number)

                except Exception as exc:
                    _log.warning("Error processing PR #%d: %s", number, exc)
                    errors.append({"pr": number, "error": str(exc)[:200]})

            if not self.dry_run and highest_number > watermark:
                _upsert_watermark(conn, self.agent, highest_number)

            conn.commit()

            summary = {
                "written": len(written),
                "skipped": len(skipped),
                "errors": len(errors),
                "watermark_before": watermark,
                "watermark_after": highest_number,
                "nodes": written,
                "error_detail": errors,
            }
            return summary

        except Exception as exc:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Consequence engine: watch fleet events, write soul nodes."
    )
    parser.add_argument("--repo", default="jordanhubbard/mac", help="owner/repo")
    parser.add_argument("--agent", default="bullwinkle", help="Agent name (soul graph owner)")
    parser.add_argument(
        "--db-url",
        default=os.environ.get(
            "MAC_SOUL_PG_URL",
            "postgresql://fleet_agent@100.72.16.110:5432/agent_brain",
        ),
        help="Postgres connection string for agent_brain",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute nodes but don't write to Postgres",
    )
    parser.add_argument("--max-prs", type=int, default=20, help="Max PRs to process per run")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    engine = ConsequenceEngine(
        db_url=args.db_url,
        agent=args.agent,
        repo=args.repo,
        dry_run=args.dry_run,
        max_prs=args.max_prs,
    )
    try:
        result = engine.run()
        print(json.dumps(result, indent=2))
        return 0 if "error" not in result else 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        _log.exception("Consequence engine failed")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(_main())
