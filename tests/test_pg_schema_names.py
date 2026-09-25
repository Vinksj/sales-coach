"""The per-test Postgres schema names (tests/pg_schemas.py): a starting session drops its own schemas and stale
ones, never those of another session that is running now. (Before, every session start dropped every
sc_test_% schema, so two suites sharing one database broke each other with "relation does not exist".)"""
from datetime import datetime, timedelta, timezone

import pg_schemas

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _prefix(at, pid):
    return pg_schemas.session_prefix(now=at, pid=pid)


def test_a_prefix_names_its_session_and_start_time_and_fits_a_postgres_identifier():
    a, b = _prefix(NOW, 41), _prefix(NOW, 41)
    assert a != b                                              # same pid, same second: still two sessions
    name = a + "0123456789ab"
    assert len(name) <= 63 and pg_schemas.created_at(name) == NOW
    assert pg_schemas.created_at("sc_test_0123456789ab") is None
    assert pg_schemas.created_at("sc_test_99999999999999_1_abcd_x") is None       # not a date


def test_a_session_drops_its_own_and_stale_schemas_but_not_a_running_sessions():
    mine = _prefix(NOW, 10)
    running = _prefix(NOW - timedelta(minutes=20), 11)         # another suite, started 20 minutes ago
    recent = _prefix(NOW - timedelta(hours=5, minutes=59), 12)
    stale = _prefix(NOW - timedelta(hours=6, minutes=1), 13)   # its session is long gone
    names = [mine + "aaaaaaaaaaaa", running + "bbbbbbbbbbbb", recent + "cccccccccccc", stale + "dddddddddddd",
             "sc_test_0123456789ab", "sc_testing_other", "public"]
    dropped = pg_schemas.to_drop(names, mine, now=NOW, others_connected=True)
    assert dropped == [mine + "aaaaaaaaaaaa", stale + "dddddddddddd"]


def test_old_style_names_go_only_when_nobody_else_is_connected():
    mine = _prefix(NOW, 10)
    legacy = "sc_test_0123456789ab"
    assert legacy not in pg_schemas.to_drop([legacy], mine, now=NOW, others_connected=True)
    assert pg_schemas.to_drop([legacy], mine, now=NOW, others_connected=False) == [legacy]
    running = _prefix(NOW - timedelta(minutes=1), 11) + "bbbbbbbbbbbb"
    assert pg_schemas.to_drop([running], mine, now=NOW, others_connected=False) == []    # timestamped: age decides
