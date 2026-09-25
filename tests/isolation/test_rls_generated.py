"""store/pg/rls.sql is exactly what store/rls.py generates from tenancy.py (both backends), and,
live on Postgres, every table has the security the generator says it has and the app runs as the
app role, which bypasses nothing.

rls.sql is a REPEATABLE step (store/pgmigrate.py): applied after the numbered migrations whenever its
checksum changes, so a table a LATER numbered migration creates is policed too (the case a numbered
0003 could never cover), a policy removed from the generator does not linger, and the app refuses a
database whose applied policies are not this build's until `salescoach migrate` runs.
"""
import os
import uuid

import pytest

from salescoach import cli
from salescoach.store import db as dbmod
from salescoach.store import pgmigrate, rls, stores, tenancy

CLASSES = (tenancy.OWNED, tenancy.ORG, tenancy.SYSTEM)


def test_committed_rls_file_is_the_generator_output():
    assert rls.OUT.read_text() == rls.generate(), "run scripts/gen_pg_rls.py --write"
    assert rls.OUT.name == "rls.sql" and pgmigrate.REPEATABLES == {rls.NAME: rls.OUT}
    assert not list(rls.OUT.parent.glob("0003_*.sql")), "the policies are the repeatable rls.sql, not a numbered step"


def test_rls_sql_is_written_to_be_applied_again():
    """Idempotent end to end: every policy of the schema is dropped first, functions are replaced,
    triggers are dropped before they are made, and every table states FORCE or NO FORCE."""
    text = rls.generate()
    first_policy = text.index("CREATE POLICY")
    assert text.index("FROM pg_policies WHERE schemaname = current_schema()") < first_policy
    assert "DROP POLICY %I ON %I.%I" in text
    assert "CREATE FUNCTION" not in text.replace("CREATE OR REPLACE FUNCTION", "")
    for line in text.splitlines():
        if line.startswith("CREATE TRIGGER"):
            name, table = line.split()[2], line.split(" ON ")[1].split()[0]
            assert f"DROP TRIGGER IF EXISTS {name} ON {table};" in text, line
    for table, kind in tenancy.TABLE_CLASS.items():
        want = "FORCE" if kind == tenancy.OWNED else "NO FORCE"
        assert f"ALTER TABLE {table} {want} ROW LEVEL SECURITY;" in text, table


def test_every_table_has_a_policy_set():
    for table in tenancy.TABLE_CLASS:
        select, insert, update, delete = rls.policies_for(table)
        assert select, table
    assert set(rls.ORG_POLICIES) == tenancy.tables_of(tenancy.ORG)
    assert set(rls.SYSTEM_POLICIES) == tenancy.tables_of(tenancy.SYSTEM)


def test_owned_policies_fail_closed_by_construction():
    """Every OWNED policy goes through app_visible_owners() (reads) or app_can_write() (writes), both of
    which are empty / false for an unset app.user_id; nothing OWNED is ever `true`. The documented
    exceptions (rls.OWNED_EXCEPTIONS: comments, written by their author) still bind every write to the
    acting user and to the owners that user may read."""
    for table in tenancy.tables_of(tenancy.OWNED):
        select, insert, update, delete = rls.policies_for(table)
        assert "app_visible_owners()" in select
        for expr in (insert, update, delete):
            assert "true" not in expr.split()
            if table in rls.OWNED_EXCEPTIONS:
                assert "app_actor_id()" in expr and "app_visible_owners()" in expr, (table, expr)
            else:
                assert "app_can_write(owner_id)" in expr
    assert set(rls.OWNED_EXCEPTIONS) == {"comments"}
    assert all(len(spec[4]) > 100 for spec in rls.OWNED_EXCEPTIONS.values())      # the reason is written down


@pytest.mark.postgres_only
def test_live_tables_have_the_generated_security(db):
    rows = db.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND c.relkind = 'r'").fetchall()
    live = {r[0]: (r[1], r[2]) for r in rows}
    assert set(live) == set(tenancy.TABLE_CLASS)
    for table, kind in tenancy.TABLE_CLASS.items():
        enabled, forced = live[table]
        assert enabled, f"{table}: row security not enabled"
        assert forced == (kind == tenancy.OWNED), f"{table}: FORCE should be {kind == tenancy.OWNED}"
    policies = {}
    for name, table, cmd in db.execute(
            "SELECT policyname, tablename, cmd FROM pg_policies WHERE schemaname = current_schema()").fetchall():
        policies.setdefault(table, set()).add(cmd)
    for table in tenancy.TABLE_CLASS:
        select, insert, update, delete = rls.policies_for(table)
        want = {"SELECT"} | ({"INSERT"} if insert else set()) | ({"UPDATE"} if update else set()) | (
            {"DELETE"} if delete else set())
        assert policies.get(table, set()) == want, table


@pytest.mark.postgres_only
def test_the_app_runs_as_the_app_role(db):
    who, superuser, bypass = db.execute(
        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert who == rls.APP_ROLE and not superuser and not bypass
    owner = db.execute("SELECT tableowner FROM pg_tables WHERE schemaname = current_schema() AND tablename = 'calls'"
                       ).fetchone()[0]
    assert owner != rls.APP_ROLE                                                 # the owner role is another role
    with pytest.raises(Exception):
        db.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES (999,'x','t')")
    db.rollback()
    with pytest.raises(Exception):
        db.execute("CREATE TABLE should_not_work (x INTEGER)")
    db.rollback()
    definer = {r[0]: r[1] for r in db.execute(
        "SELECT p.proname, p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema()").fetchall()}
    for fn in ("app_actor_active", "app_actor_role", "app_visible_owners", "app_can_write", "app_users_empty",
               "app_owner_of", "app_child_owner"):
        assert definer.get(fn) is True, fn
    # The guards run as the invoker: row_security_active() must ask about the caller, not the function's owner.
    for fn in ("app_actor_id", "app_mode_ok", "app_users_guard", "app_comments_guard", "app_oauth_tokens_guard"):
        assert definer.get(fn) is False, fn


# ---- the repeatable step, live ---------------------------------------------------------------------------

def _owner():
    conn = dbmod.connect(os.environ["DATABASE_MIGRATE_URL"])       # the owner role, as nobody
    conn.system = True
    return conn


@pytest.fixture
def scratch():
    """A schema of its own, migrated by the owner role the way `salescoach migrate` does."""
    name = f"sc_test_rep_{uuid.uuid4().hex[:8]}"
    conn = _owner()
    conn.execute(f'CREATE SCHEMA "{name}"')
    conn.execute(f'SET search_path TO "{name}"')
    try:
        yield conn, name
    finally:
        conn.execute("RESET search_path")
        conn.execute(f'DROP SCHEMA "{name}" CASCADE')
        conn.close()


def _security(conn, table):
    row = conn.execute("SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n "
                       "ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND c.relname = ?", (table,)).fetchone()
    policies = {r[0] for r in conn.execute(
        "SELECT cmd FROM pg_policies WHERE schemaname = current_schema() AND tablename = ?", (table,)).fetchall()}
    return (bool(row[0]), bool(row[1])) if row else None, policies


@pytest.mark.postgres_only
def test_tables_created_after_the_policies_are_policed(scratch, monkeypatch, tmp_path):
    """The tables 0004 (sessions, invites, oauth_tokens) and 0005 (org_settings, raw_payloads) create are
    policed; and a table a FUTURE numbered migration adds is policed by the next `migrate` once tenancy.py
    classifies it, with no edit to any earlier step."""
    conn, _name = scratch
    applied = pgmigrate.apply(conn)
    assert applied[-1] == "rls" and max(v for v in applied if isinstance(v, int)) == pgmigrate.expected_version()
    for table in ("sessions", "invites", "oauth_tokens", "org_settings"):
        assert _security(conn, table) == ((True, False), {"SELECT", "INSERT", "UPDATE", "DELETE"}), table
    assert _security(conn, "raw_payloads") == ((True, True), {"SELECT", "INSERT", "UPDATE", "DELETE"})
    assert pgmigrate.apply(conn) == []                                          # nothing changed: nothing applied

    # a later migration adds an OWNED table; tenancy.py classifies it; rls.sql is regenerated
    later = pgmigrate.expected_version() + 1
    monkeypatch.setitem(pgmigrate.PY_STEPS, later, lambda c: c.execute(
        "CREATE TABLE later_things (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL "
        "DEFAULT NULLIF(current_setting('app.user_id', true), ''), body TEXT)"))
    monkeypatch.setitem(tenancy.TABLE_CLASS, "later_things", tenancy.OWNED)
    regenerated = tmp_path / "rls.sql"
    regenerated.write_text(rls.generate())
    monkeypatch.setitem(pgmigrate.REPEATABLES, rls.NAME, regenerated)
    assert pgmigrate.stale_repeatables(conn) == ["rls"]
    assert pgmigrate.apply(conn) == [later, "rls"]
    assert _security(conn, "later_things") == ((True, True), {"SELECT", "INSERT", "UPDATE", "DELETE"})
    assert pgmigrate.stale_repeatables(conn) == []


@pytest.mark.postgres_only
def test_a_policy_the_generator_no_longer_makes_does_not_linger(scratch):
    conn, _name = scratch
    pgmigrate.apply(conn)
    conn.execute("CREATE POLICY calls_leftover ON calls FOR SELECT USING (true)")      # an old build's policy
    conn.execute("UPDATE schema_repeatables SET checksum='an older build' WHERE name='rls'")
    assert pgmigrate.stale_repeatables(conn) == ["rls"]
    assert pgmigrate.apply(conn) == ["rls"]
    names = {r[0] for r in conn.execute(
        "SELECT policyname FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'calls'").fetchall()}
    assert names == {"calls_select", "calls_insert", "calls_update", "calls_delete"}


@pytest.mark.postgres_only
def test_the_app_refuses_policies_that_are_not_this_builds_until_migrate(db, pg_owner, capsys):
    """assert_current (every process, at its first connection) checks the recorded checksum as it checks
    the version; `migrate --check` reports it; `migrate` re-applies it and the app starts again."""
    schema = stores._pg_schema
    pg_owner.execute("UPDATE schema_repeatables SET checksum='an older build' WHERE name='rls'")
    pg_owner.commit()
    stores.forget_verified()
    with pytest.raises(pgmigrate.SchemaOutOfDate, match="rls.sql .*salescoach migrate"):
        stores.sales()
    url = f"{os.environ['DATABASE_MIGRATE_URL']}?options=-c%20search_path%3D{schema}"
    assert cli.main(["migrate", "--check", "--url", url]) == 1
    assert "rls.sql (row-level security) differs" in capsys.readouterr().out
    assert cli.main(["migrate", "--url", url]) == 0
    assert "applied rls.sql" in capsys.readouterr().out
    assert cli.main(["migrate", "--check", "--url", url]) == 0
    stores.forget_verified()
    stores.sales().close()                                                       # starts again
