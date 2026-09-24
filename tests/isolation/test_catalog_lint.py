"""Every table is classified OWNED / ORG / SYSTEM (store/tenancy.py), on both backends, and every
OWNED table carries owner_id with an index on it.

Phase 2 puts row-level security on every OWNED table; this lint is what stops a table nobody
thought about from reaching a multi-user database. It fails on a table the schema creates that
tenancy.TABLE_CLASS does not name, on a name in TABLE_CLASS that no schema creates, on any class
outside the three, on an OWNED table without owner_id (NOT NULL unless tenancy.OWNER_NULLABLE says
why), without an index whose first column is owner_id, or with a default other than 'local' (the
Postgres default, the session setting, is set by store/pg/0002_owner.sql and checked live below).
"""
import pytest

from salescoach.store import catalog, tenancy

CLASSES = {tenancy.OWNED, tenancy.ORG, tenancy.SYSTEM}


def test_every_sqlite_table_is_classified():
    unclassified = set(catalog.tables()) - set(tenancy.TABLE_CLASS) - set(tenancy.SQLITE_ONLY)
    assert not unclassified, f"add these to tenancy.TABLE_CLASS: {sorted(unclassified)}"


def test_every_classified_table_exists_somewhere():
    ghosts = set(tenancy.TABLE_CLASS) - set(catalog.tables()) - set(tenancy.POSTGRES_ONLY)
    assert not ghosts, f"tenancy.TABLE_CLASS names tables no schema creates: {sorted(ghosts)}"


def test_classes_are_known_disjoint_and_complete():
    assert set(tenancy.TABLE_CLASS.values()) <= CLASSES
    owned, org, system = (tenancy.tables_of(k) for k in (tenancy.OWNED, tenancy.ORG, tenancy.SYSTEM))
    assert not (owned & org) and not (owned & system) and not (org & system)
    assert owned | org | system == set(tenancy.TABLE_CLASS)
    assert org == {"accounts", "people", "users", "teams", "team_managers"}
    assert {"wf_events", "state", "user_state", "user_speaker_labels", "schema_migrations"} <= system


def test_every_owned_table_has_owner_id_and_an_index():
    problems = []
    for table in sorted(tenancy.tables_of(tenancy.OWNED)):
        cols = {c.name: c for c in catalog.tables()[table]}
        col = cols.get("owner_id")
        if col is None:
            problems.append(f"{table}: no owner_id column")
            continue
        if col.type != "TEXT":
            problems.append(f"{table}.owner_id: type {col.type}, not TEXT")
        nullable = table in tenancy.OWNER_NULLABLE
        if (col.notnull or bool(col.pk)) == nullable:
            problems.append(f"{table}.owner_id: NOT NULL={col.notnull or bool(col.pk)}, expected {not nullable}")
        if col.default != "'local'":
            problems.append(f"{table}.owner_id: default {col.default!r}, expected 'local'")
        if not any(ix.table == table and ix.columns[:1] == ("owner_id",) for ix in catalog.indexes()):
            problems.append(f"{table}: no index starting with owner_id")
    assert not problems, "\n".join(problems)


def test_no_unowned_table_has_owner_id():
    stray = [t for t, cols in catalog.tables().items()
             if t not in tenancy.tables_of(tenancy.OWNED) and any(c.name == "owner_id" for c in cols)]
    assert not stray, f"owner_id on a table that is not OWNED: {stray}"


def test_owner_parents_name_real_tables_and_columns():
    tables = catalog.tables()
    for child, parents in tenancy.OWNER_PARENTS.items():
        assert tenancy.TABLE_CLASS.get(child) == tenancy.OWNED, child
        child_cols = {c.name for c in tables[child]}
        for parent, key, column in parents:
            assert tenancy.TABLE_CLASS.get(parent) == tenancy.OWNED, (child, parent)
            assert key in {c.name for c in tables[parent]}, (child, parent, key)
            assert column in child_cols, (child, column)


@pytest.mark.postgres_only
def test_every_live_postgres_table_is_classified(db):
    live = {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
        "AND table_type = 'BASE TABLE'")}
    unclassified = live - set(tenancy.TABLE_CLASS)
    assert not unclassified, f"add these to tenancy.TABLE_CLASS: {sorted(unclassified)}"
    assert "schema_migrations" in live


@pytest.mark.postgres_only
def test_live_postgres_owner_defaults_and_triggers(db):
    """Every OWNED table defaults owner_id to the session setting; every child table listed in
    tenancy.OWNER_PARENTS has its BEFORE INSERT trigger."""
    defaults = {r[0]: r[1] for r in db.execute(
        "SELECT table_name, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND column_name = 'owner_id'").fetchall()}
    wrong = {t: d for t, d in defaults.items() if "current_setting('app.user_id'" not in (d or "")}
    assert set(defaults) == set(tenancy.tables_of(tenancy.OWNED)) and not wrong, wrong
    triggers = {r[0] for r in db.execute(
        "SELECT event_object_table FROM information_schema.triggers WHERE trigger_schema = current_schema() "
        "AND event_manipulation = 'INSERT' AND action_timing = 'BEFORE'").fetchall()}
    assert triggers == set(tenancy.OWNER_PARENTS)
