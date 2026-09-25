"""Per-user export under row-level security (Phase 8, Postgres only, cloud mode): rep A's export holds none of
rep B's rows; manager M's export holds M's own work only, never the team's (which M may read); the operator's
`salescoach export --user` (the owner role) is scoped the same way."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from salescoach import cli, identity, users
from salescoach.store import tenancy
from salescoach.web import app as app_module

import factories
from conftest import seed_org_settings
from test_route_crawl import cloud  # noqa: F401

pytestmark = pytest.mark.postgres_only

A, B, M = "u-ea", "u-eb", "u-em"
ROLES = {A: "rep", B: "rep", M: "manager"}


@pytest.fixture
def org(db, cloud, pg_owner):  # noqa: F811
    users.create_team(db, "West", team_id="t-west")
    for uid, role in ROLES.items():
        users.create(db, f"{uid}@tessel.test", uid, role=role, team_id="t-west" if role == "rep" else None, user_id=uid)
    users.set_managers(db, "t-west", [M])
    db.commit()
    for uid in ROLES:                                      # a row of every OWNED table for each of them
        with identity.as_actor(db, identity.Actor(uid, identity.INTERACTIVE, ROLES[uid])):
            mine = factories.Owner(db, uid)
            for table in sorted(tenancy.tables_of(tenancy.OWNED)):
                factories.insert(db, table, uid, mine)
            db.commit()
    seed_org_settings(pg_owner)
    return db


def _owners(files: dict) -> set:
    found = set()
    for name, rows in files.items():
        if name == "README.json":
            continue
        for row in rows:
            for col in ("owner_id", "user_id", "owner_user_id"):
                if row.get(col):
                    found.add(row[col])
            if name == "users.json":
                found.add(row["id"])
    return found


def _get(client, who) -> dict:
    r = client.get("/me/export", headers={"x-test-user": who})
    assert r.status_code == 200, r.text[:200]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return {n: json.loads(zf.read(n)) for n in zf.namelist()}


def test_each_export_holds_its_users_rows_only(org):
    client = TestClient(app_module.create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False)
    for who in (A, B, M):
        files = _get(client, who)
        assert _owners(files) == {who}, who
        # every OWNED table is in it with the user's own row(s)
        for table in tenancy.tables_of(tenancy.OWNED):
            assert files[f"{table}.json"], (who, table)
    # the manager can read A's calls, and still exports only their own
    with identity.as_actor(org, identity.Actor(M, identity.INTERACTIVE, "manager")):
        assert org.execute("SELECT COUNT(*) FROM calls WHERE owner_id=?", (A,)).fetchone()[0] > 0


def test_the_operator_export_is_scoped_to_the_user(org, tmp_path):
    out = tmp_path / "a.zip"
    assert cli.main(["export", "--user", f"{A}@tessel.test", "--out", str(out)]) == 0
    with zipfile.ZipFile(out) as zf:
        files = {n: json.loads(zf.read(n)) for n in zf.namelist()}
    assert _owners(files) == {A}
    assert all("secret_enc" not in r for r in files["source_connections.json"])
