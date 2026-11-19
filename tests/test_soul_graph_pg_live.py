"""A loaded soul graph must not pin vacuum while a model keeps it in memory."""

import uuid
import time

import psycopg
from psycopg.pq import TransactionStatus
import pytest

from mac.soul_graph_pg import SoulGraphPG
from mac.test_support import test_dsn as pg_test_dsn
from tests.pg_worker_databases import WorkerDatabases


@pytest.fixture
def soul_database(request):
    databases = WorkerDatabases(pg_test_dsn())
    request.addfinalizer(databases.close)
    dsn = databases.create()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE soul_nodes (
                id text PRIMARY KEY, content text, tags text[], parents text[],
                pinned boolean, score float8 DEFAULT 0,
                created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
                last_accessed timestamptz DEFAULT now(), access_count integer DEFAULT 0,
                metadata jsonb DEFAULT '{}', agent text);
            CREATE TABLE soul_edges (
                parent_id text, child_id text, relation text,
                PRIMARY KEY (parent_id,child_id,relation));
            INSERT INTO soul_nodes(id,content,tags,parents,pinned,agent) VALUES
                ('root','Root observation',ARRAY['test'],ARRAY[]::text[],true,'agent-a'),
                ('child','Child observation',ARRAY['test'],ARRAY['root'],false,'agent-a'),
                ('peer','Peer observation',ARRAY['test'],ARRAY[]::text[],false,'agent-b');
            INSERT INTO soul_edges VALUES ('root','child','supports');
        """)
        yield conn, dsn


@pytest.mark.parametrize("cross_agent", [False, True])
def test_loaded_graph_releases_snapshot_and_remains_writable(soul_database, cross_agent):
    observer, dsn = soul_database
    graph = SoulGraphPG.from_pg(dsn, "agent-a", cross_agent=cross_agent)
    try:
        assert set(graph.nodes) == ({"root", "child", "peer"} if cross_agent else {"root", "child"})
        assert graph.nodes["child"].metadata["parent_rels"]["root"] == "supports"
        assert graph._pg_conn.info.transaction_status == TransactionStatus.IDLE
        state, xmin, started = observer.execute(
            "SELECT state, backend_xmin, xact_start FROM pg_stat_activity WHERE pid=%s",
            (graph._pg_conn.info.backend_pid,),
        ).fetchone()
        assert (state, xmin, started) == ("idle", None, None)
        # The retained connection still groups node and edge writes and commits
        # them together; the fix must not turn each save statement into autocommit.
        assert not graph._pg_conn.autocommit
        node = graph.add("New observation", parents=["root"], node_id="new-node")
        assert graph.save_pg() == 2
        assert graph._pg_conn.info.transaction_status == TransactionStatus.IDLE
        assert observer.execute(
            "SELECT content FROM soul_nodes WHERE id=%s", (node.id,)
        ).fetchone() == ("New observation",)
        assert observer.execute(
            "SELECT relation FROM soul_edges WHERE child_id='new-node'"
        ).fetchone() == ("child",)
    finally:
        graph._pg_conn.close()


def test_failed_load_closes_its_database_connection(soul_database):
    observer, dsn = soul_database
    observer.execute("DROP TABLE soul_edges")
    application = "soul-load-failure-" + uuid.uuid4().hex
    dsn += "&application_name=" + application
    # Keep the exception traceback alive so GC cannot accidentally hide a leaked
    # connection from an incomplete load.
    with pytest.raises(psycopg.errors.UndefinedTable) as failure:
        SoulGraphPG.from_pg(dsn, "agent-a")
    assert failure.value
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        count = observer.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name=%s", (application,)
        ).fetchone()[0]
        if count == 0:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("failed load retained its database connection")
