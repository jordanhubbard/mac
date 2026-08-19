"""The DEPENDENCIES column must not price out the title.

REPORTED: "mac task list is severely truncated and I don't see project names or
titles now at all."

Every column in the task table was capped except one. `project_width` is
`min(18, ...)`, ids are a fixed 13, the state cell is bounded -- but
`dependencies_width` was sized to the longest dependency cell in the WHOLE
table. So a single task with six blockers took 49 columns and squeezed every
title in the listing to 23 characters. On a real blocked backlog the field grew
past 70 and the titles were effectively gone.

That is the wrong trade. A listing is scanned for titles; the full dependency
list is one `mac task show` away. What the listing does owe the reader is the
COUNT -- how many things are holding this task -- which is why the overflow
marker carries a number rather than trailing off.
"""

from __future__ import annotations

import pytest

from mac.cli import MAX_DEPENDENCIES_WIDTH, _elide_dependencies, _render_task_table


def _rows(dep_counts, title="A task title long enough to be worth reading in full"):
    return [
        {
            "id": "task_%012d" % i,
            "state": "blocked",
            "dependencies": ["task_%08d" % j for j in range(n)],
            "title": title,
        }
        for i, n in enumerate(dep_counts)
    ]


def _column_starts(rendered):
    """Where each column begins, read off the rule line."""
    rule = rendered.splitlines()[1]
    starts, in_run = [], False
    for i, ch in enumerate(rule):
        if ch == "─" and not in_run:
            starts.append(i)
            in_run = True
        elif ch != "─":
            in_run = False
    return starts


def test_one_task_with_many_blockers_does_not_starve_every_title():
    """The reported bug, reduced."""
    wide = _render_task_table(_rows([1, 6]), show_project=False, width=100)

    title_start = _column_starts(wide)[-1]
    title_width = 100 - title_start

    assert title_width >= 40, (
        "titles were squeezed to %d columns by one task's dependency list"
        % title_width
    )


def test_the_dependencies_column_is_capped():
    rendered = _render_task_table(_rows([12]), show_project=False, width=200)
    starts = _column_starts(rendered)
    dependencies_width = starts[-1] - starts[-2] - 2

    assert dependencies_width <= MAX_DEPENDENCIES_WIDTH


def test_a_short_dependency_list_is_not_padded_out():
    """The cap is a ceiling, not a fixed width: the common case is 0 or 1."""
    rendered = _render_task_table(_rows([0, 1]), show_project=False, width=120)
    starts = _column_starts(rendered)

    assert starts[-1] - starts[-2] - 2 < MAX_DEPENDENCIES_WIDTH


# --------------------------------------------------------------------------
# the overflow marker
# --------------------------------------------------------------------------


def test_elision_reports_how_many_were_dropped():
    """A bare truncation tells the reader neither the count nor that anything
    is missing -- and the count is what a listing is scanned for."""
    out = _elide_dependencies("[task_a,task_b,task_c,task_d,task_e,task_f]", 20)

    assert out.endswith("]")
    assert "+" in out
    assert len(out) <= 20
    kept = out[1:-1].split(",")
    dropped = int(kept[-1].lstrip("+"))
    assert len(kept) - 1 + dropped == 6, "kept + dropped must equal the real total"


def test_a_cell_that_already_fits_is_untouched():
    assert _elide_dependencies("[task_a]", 20) == "[task_a]"
    assert _elide_dependencies("[]", 20) == "[]"


def test_a_single_id_too_wide_reports_the_count_not_a_fragment():
    """A truncated id looks like a real id and resolves to nothing."""
    out = _elide_dependencies("[task_averylongidentifier,task_b]", 8)

    assert out == "[+2]"
    assert "task_" not in out


@pytest.mark.parametrize("count", [2, 3, 7, 25])
def test_the_total_is_always_recoverable(count):
    cell = "[" + ",".join("task_%08d" % i for i in range(count)) + "]"
    out = _elide_dependencies(cell, MAX_DEPENDENCIES_WIDTH)

    inner = out[1:-1]
    parts = inner.split(",")
    dropped = int(parts[-1].lstrip("+")) if parts[-1].startswith("+") else 0
    kept = len(parts) - (1 if dropped else 0)
    assert kept + dropped == count


def test_the_table_never_exceeds_the_terminal():
    rendered = _render_task_table(_rows([9, 0, 3]), show_project=False, width=90)

    assert max(len(line) for line in rendered.splitlines()) <= 90
