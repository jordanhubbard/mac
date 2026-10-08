"""`mac task say` and `mac task messages`: a person talking to a running task."""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>`; return (rc, stdout text)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    return rc, out.getvalue().strip()


def test_say_posts_a_directive_and_messages_reads_it_back(tmp_path):
    rc, created = _run(tmp_path, "task", "create", "Fix the parser")
    assert rc == 0
    task_id = json.loads(created)["id"]

    rc, posted = _run(
        tmp_path, "task", "say", task_id, "only touch src/parser.c", "--directive", "--as", "jkh"
    )
    assert rc == 0
    posted = json.loads(posted)
    assert (posted["kind"], posted["author_kind"], posted["author"]) == ("directive", "human", "jkh")

    rc, listed = _run(tmp_path, "task", "messages", task_id, "--jsonl")
    assert rc == 0
    assert [json.loads(line)["body"] for line in listed.splitlines()] == ["only touch src/parser.c"]

    rc, text = _run(tmp_path, "task", "messages", task_id)
    assert rc == 0
    assert "jkh: [directive] only touch src/parser.c" in text

    rc, after = _run(tmp_path, "task", "messages", task_id, "--after", str(posted["id"]))
    assert rc == 0 and after == ""
