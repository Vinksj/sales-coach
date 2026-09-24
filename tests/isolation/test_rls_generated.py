"""store/pg/0003_rls.sql is exactly what store/rls.py generates from tenancy.py (both backends), and,
live on Postgres, every table has the security the generator says it has and the app runs as the
app role, which bypasses nothing."""
import pytest

from salescoach.store import rls, tenancy

CLASSES = (tenancy.OWNED, tenancy.ORG, tenancy.SYSTEM)


def test_committed_rls_file_is_the_generator_output():
    assert rls.OUT.read_text() == rls.generate(), "run scripts/gen_pg_rls.py --write"


def test_every_table_has_a_policy_set():
    for table in tenancy.TABLE_CLASS:
        select, insert, update, delete = rls.policies_for(table)
        assert select, table
    assert set(rls.ORG_POLICIES) == tenancy.tables_of(tenancy.ORG)
    assert set(rls.SYSTEM_POLICIES) == tenancy.tables_of(tenancy.SYSTEM)


def test_owned_policies_fail_closed_by_construction():
    """Every OWNED policy goes through app_visible_owners() (reads) or app_can_write() (writes), both of
    which are empty / false for an unset app.user_id; nothing OWNED is ever `true`."""
    for table in tenancy.tables_of(tenancy.OWNED):
        select, insert, update, delete = rls.policies_for(table)
        assert "app_visible_owners()" in select
        for expr in (insert, update, delete):
            assert "app_can_write(owner_id)" in expr and "true" not in expr.split()


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
               "app_owner_of", "app_child_owner", "app_users_guard"):
        assert definer.get(fn) is True, fn
    for fn in ("app_actor_id", "app_mode_ok"):
        assert definer.get(fn) is False, fn
