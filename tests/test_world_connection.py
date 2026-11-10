"""An explicit World database must never fall back to a different authority."""

from unittest.mock import patch

import psycopg
import pytest

from mac import world


def test_explicit_world_dsn_fails_closed():
    dsn = "dbname=unavailable_explicit_world"
    with patch.object(
        psycopg, "connect", side_effect=psycopg.OperationalError("unavailable")
    ) as connect:
        with pytest.raises(RuntimeError):
            world._connect(dsn)
    assert connect.call_count == 1
    assert connect.call_args.args == (dsn,)
