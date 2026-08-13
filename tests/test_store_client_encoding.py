"""Non-ASCII text must survive the trip to Postgres.

Observed live on the hub, failing a review whose evidence contained a section
sign:

    File ".../psycopg/_queries.py", line 167, in _ensure_bytes
        return query.encode(self._tx.encoding)
    UnicodeEncodeError: 'ascii' codec can't encode character '\\xa7'
        in position 17789: ordinal not in range(128)

Nothing was wrong with the data or the database: every database in that
cluster is UTF8 with en_US.UTF-8 collation. libpq derives client_encoding from
the process locale when the connection does not state one, and a LaunchDaemon
has no LANG -- so the hub negotiated SQL_ASCII and psycopg encoded every
statement as ASCII.

That makes the failure both severe and easy to miss: agent output routinely
carries non-ASCII (box drawing, check marks, em dashes, ordinary prose), so it
rejects real work at random, and only on a host whose service manager strips
the locale. A developer machine has a UTF-8 locale and never sees it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pathlib
import pytest

from mac.test_support import ephemeral_store


@pytest.fixture()
def store():
    return ephemeral_store()


def test_the_connection_states_utf8_rather_than_inheriting_it(store):
    """The property that matters is the negotiated encoding, not the setting
    we passed: a pool that accepted the keyword but did not apply it would
    still fail on the hub."""
    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("show client_encoding")
            encoding = cur.fetchone()[0]

    assert encoding.upper() in {"UTF8", "UTF-8"}


def test_a_statement_carrying_non_ascii_reaches_the_server(store):
    """The live failure reproduced end to end. The section sign is the exact
    character that broke the hub; the rest are what agent output actually
    contains."""
    sample = "§ evidence — ✓ done │ 100% café"

    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select %s::text", (sample,))
            assert cur.fetchone()[0] == sample
            # Inline, not parameterised: the hub's failure was encoding the
            # QUERY, so a bound parameter alone would not exercise it.
            cur.execute("select '%s'::text" % sample.replace("'", "''"))
            assert cur.fetchone()[0] == sample


def test_the_store_states_the_encoding_instead_of_inheriting_it():
    """The only assertion here that can actually fail.

    The two above pass on any machine with a UTF-8 locale, fix or no fix --
    which is precisely why this went unnoticed. Reproducing the hub in a test
    was attempted and abandoned: a subprocess with LANG and LC_* stripped still
    negotiates UTF8 on macOS, so it could not fail either.

    What is left is the difference itself: the connection must SAY UTF8 rather
    than take whatever the process locale implies. Removing the keyword fails
    this and nothing else.
    """
    import inspect

    from mac.store_postgres import PostgresStore

    source = inspect.getsource(PostgresStore.__init__)

    assert "client_encoding" in source, (
        "PostgresStore must state client_encoding on the pool; libpq otherwise "
        "derives it from the process locale, and a service manager that strips "
        "the locale yields SQL_ASCII"
    )
