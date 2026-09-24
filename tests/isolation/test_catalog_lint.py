"""Every table is classified OWNED / ORG / SYSTEM (store/tenancy.py), on both backends.

Phase 2 puts row-level security on every OWNED table; this lint is what stops a table nobody
thought about from reaching a multi-user database. It fails on a table the schema creates that
tenancy.TABLE_CLASS does not name, on a name in TABLE_CLASS that no schema creates, and on any
class outside the three.
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
    assert org == {"accounts", "people"}
    assert {"wf_events", "state", "schema_migrations"} <= system


@pytest.mark.postgres_only
def test_every_live_postgres_table_is_classified(db):
    live = {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
        "AND table_type = 'BASE TABLE'")}
    unclassified = live - set(tenancy.TABLE_CLASS)
    assert not unclassified, f"add these to tenancy.TABLE_CLASS: {sorted(unclassified)}"
    assert "schema_migrations" in live
