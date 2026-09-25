"""`salescoach import-sqlite` (Phase 8): a single-user SQLite install round-trips into an empty Postgres org.

The source is built by the ordinary fixtures on a real SQLite file (DATABASE_URL is lifted while it is built):
a processed call with its loops and email, a second paste, learned patterns (one merged into another), an
open proposal, speaker labels and user state, a recorder-native call, a raw payload and bus events whose
dedupe keys embed the owner. Then it is imported as a new rep and read back as that rep through the app role.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from salescoach import identity, users
from salescoach.learning import patterns
from salescoach.lifecycle import importer
from salescoach.orchestrator import worker
from salescoach.sources import paste
from salescoach.store import stores, tenancy
from salescoach.store.stores import now
from test_core_pipeline import CALL1, CALL2, _script, _setup

pytestmark = pytest.mark.postgres_only

EMAIL = "maya.rep@tessel.test"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def install(tmp_path, monkeypatch, fake_llm):
    """A local install's sales.db, built on SQLite by the same code a laptop runs."""
    path = tmp_path / "laptop" / "sales.db"
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "SALES_DB")}
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("SALES_DB", str(path))
    conn = stores.sales(path)
    try:
        deal, people = _setup(conn)
        _script(fake_llm)
        call = paste.import_text(conn, CALL1, "NWP weekly", deal_id=deal, participants=people)
        worker.drain(conn)
        paste.import_text(conn, CALL2, "NWP second", deal_id=deal, participants=people)
        worker.drain(conn)
        # a call a recorder delivered on the laptop (org-level poller: no owner in the ref) and its payload
        conn.execute("INSERT INTO nodes(id,type,title) VALUES ('call-ff-1','call','Fireflies call')")
        conn.execute("INSERT INTO calls(node_id,source,source_ref,title,started_at,wf_state) "
                     "VALUES ('call-ff-1','fireflies','fireflies:abc123','Fireflies call','2026-09-01T10:00:00+05:30','done')")
        conn.execute("INSERT INTO sources(node_id,uri,capture) VALUES ('call-ff-1','fireflies:abc123','fireflies')")
        conn.execute("INSERT INTO raw_payloads(source_kind,source_ref,body,sha256,created_at) "
                     "VALUES ('fireflies','fireflies:abc123','{}','sha-ff',?)", (now(),))
        patterns.recompute(conn)                          # the counted patterns, before the hand-made ones below
        conn.commit()
        # learned patterns: one merged into another, both in a proposal and behind the memory gate
        for key in ("new:asked_budget", "budget_discovery"):
            conn.execute("INSERT INTO learned_patterns(id,family,key,status) VALUES (?,?,?,'active')",
                         (f"lp:seller:u:local:{key}", "seller", key))
        conn.execute("UPDATE learned_patterns SET merged_into='lp:seller:u:local:budget_discovery' "
                     "WHERE key='new:asked_budget'")
        conn.execute("INSERT INTO learning_proposals(kind,subject,pattern_id,target_id,summary,payload,status,created_at) "
                     "VALUES ('merge','new:asked_budget','lp:seller:u:local:new:asked_budget',"
                     "'lp:seller:u:local:budget_discovery','Merge?','{\"n\": 3}','open',?)", (now(),))
        conn.execute("INSERT INTO field_provenance(entity_id,field,value,confidence,provenance,updated_at) "
                     "VALUES ('lp:seller:u:local:budget_discovery','user_state','confirmed','user_input','{}',?)", (now(),))
        stores.set_user_state(conn, "setup:card_dismissed", "1")
        stores.set_state(conn, "automation:followups:ran_for", "2026-09-24")     # an older install's per-user key
        users.remember_label(conn, None, "Maya I.")
        conn.execute("INSERT INTO wf_events(event_id,type,entity_id,payload,dedupe_key,status,created_at,owner) "
                     "VALUES ('ev-run','FOLLOW_UP_RUN','user:local','{\"owner_id\": \"local\"}',"
                     "'FOLLOW_UP_RUN:local:2026-09-20T10:00:00','done',?,'local')", (now(),))
        conn.commit()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in (*tenancy.tables_of(tenancy.OWNED), *importer.EXTRA_TABLES) if conn.table_exists(t)}
        paste_ref = conn.execute("SELECT source_ref FROM calls WHERE node_id=?", (call,)).fetchone()[0]
    finally:
        conn.close()
    for k, v in saved.items():
        monkeypatch.setenv(k, v) if v is not None else monkeypatch.delenv(k, raising=False)
    assert paste_ref.startswith("paste:local:")
    return {"path": path, "counts": counts, "call": call, "deal": deal, "paste_ref": paste_ref}


@pytest.fixture
def target(db, pg_owner):
    """An empty cloud org: the bootstrap admin exists, nothing else."""
    users.create(db, "admin@tessel.test", "Admin", role="admin", user_id="u-admin")
    db.commit()
    return pg_owner


def test_the_install_round_trips_as_a_new_rep(install, target, monkeypatch):
    before = _sha(install["path"])
    lines = []
    report = importer.run(install["path"], EMAIL, dry_run=False, log=lines.append)
    assert _sha(install["path"]) == before, "the source file was written"
    assert report.committed and report.user_created
    uid = report.user_id
    pg = target
    user = pg.execute("SELECT role, status FROM users WHERE id=?", (uid,)).fetchone()
    assert tuple(user) == ("rep", "active")
    for table, n in install["counts"].items():
        have = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        extra = {"events": 1, "user_state": 1}.get(table, 0)          # the import's audit row; the moved state key
        assert have == n + extra, (table, have, n)
        if tenancy.TABLE_CLASS.get(table) == tenancy.OWNED:
            owners = {r[0] for r in pg.execute(f"SELECT DISTINCT owner_id FROM {table}").fetchall()}
            assert owners <= {uid, None}, (table, owners)
    assert install["counts"]["calls"] == 3 and install["counts"]["loops"] > 0 and install["counts"]["emails"] > 0
    # owner-embedding strings
    ref = pg.execute("SELECT source_ref FROM calls WHERE node_id=?", (install["call"],)).fetchone()[0]
    assert ref == install["paste_ref"].replace("paste:local:", f"paste:{uid}:")
    assert pg.execute("SELECT source_ref FROM calls WHERE node_id='call-ff-1'").fetchone()[0] == f"fireflies:{uid}:abc123"
    assert pg.execute("SELECT uri FROM sources WHERE node_id='call-ff-1'").fetchone()[0] == f"fireflies:{uid}:abc123"
    assert pg.execute("SELECT source_ref FROM raw_payloads").fetchone()[0] == f"fireflies:{uid}:abc123"
    lp = dict(pg.execute("SELECT id, merged_into FROM learned_patterns WHERE key='new:asked_budget'").fetchone())
    assert lp == {"id": f"lp:seller:u:{uid}:new:asked_budget", "merged_into": f"lp:seller:u:{uid}:budget_discovery"}
    prop = pg.execute("SELECT pattern_id, target_id FROM learning_proposals").fetchone()
    assert tuple(prop) == (f"lp:seller:u:{uid}:new:asked_budget", f"lp:seller:u:{uid}:budget_discovery")
    assert pg.execute("SELECT COUNT(*) FROM field_provenance WHERE entity_id=?",
                      (f"lp:seller:u:{uid}:budget_discovery",)).fetchone()[0] == 1
    assert pg.execute("SELECT COUNT(*) FROM people WHERE user_id=? AND is_me=1", (uid,)).fetchone()[0] == 1
    assert pg.execute("SELECT value FROM user_state WHERE user_id=? AND key='setup:card_dismissed'", (uid,)).fetchone()
    assert pg.execute("SELECT COUNT(*) FROM user_speaker_labels WHERE user_id=?", (uid,)).fetchone()[0] == 1
    assert pg.execute("SELECT value FROM user_state WHERE user_id=? AND key='automation:followups:ran_for'",
                      (uid,)).fetchone()[0] == "2026-09-24"
    assert pg.execute("SELECT COUNT(*) FROM state WHERE key='automation:followups:ran_for'").fetchone()[0] == 0
    ev = pg.execute("SELECT entity_id, dedupe_key, owner, payload FROM wf_events WHERE event_id='ev-run'").fetchone()
    assert (ev["entity_id"], ev["dedupe_key"], ev["owner"]) == (f"user:{uid}", f"FOLLOW_UP_RUN:{uid}:2026-09-20T10:00:00", uid)
    assert json.loads(ev["payload"]) == {"owner_id": uid}
    for key in (f"CALL_ENDED:{install['call']}",):
        assert pg.execute("SELECT COUNT(*) FROM wf_events WHERE dedupe_key=?", (key,)).fetchone()[0] == 1
    # ids verbatim, sequences past the max
    top = pg.execute("SELECT MAX(id) FROM agent_runs").fetchone()[0]
    src = sqlite3.connect(install["path"])
    assert top == src.execute("SELECT MAX(id) FROM agent_runs").fetchone()[0]
    src.close()
    audit = pg.execute("SELECT owner_id, after FROM events WHERE kind='admin.import_sqlite'").fetchone()
    assert audit["owner_id"] == uid and json.loads(audit["after"])["email"] == EMAIL
    # the rep reads their work through the app role, in cloud mode; a new row gets a fresh id
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with identity.session(uid) as conn:
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 3
        conn.execute("INSERT INTO agent_runs(agent,status,started_at) VALUES ('probe','ok',?)", (now(),))
        conn.commit()
        assert conn.execute("SELECT MAX(id) FROM agent_runs").fetchone()[0] == top + 1
    with identity.session("u-admin") as conn:                  # an admin who manages no team reads no content
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert any("verification passed" in line for line in lines)
    # a second import into the now non-empty org is refused, and changes nothing
    with pytest.raises(importer.ImportRefused, match="not empty"):
        importer.run(install["path"], "other@tessel.test", log=lambda *_: None)
    assert pg.execute("SELECT COUNT(*) FROM users WHERE email='other@tessel.test'").fetchone()[0] == 0


def test_a_dry_run_writes_nothing_and_counts_everything(install, target):
    report = importer.run(install["path"], EMAIL, dry_run=True, log=lambda *_: None)
    assert report.dry_run and not report.committed
    assert report.counts["calls"] == install["counts"]["calls"]
    assert target.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert target.execute("SELECT COUNT(*) FROM users WHERE email=?", (EMAIL,)).fetchone()[0] == 0


def test_an_existing_user_is_used_and_an_older_source_is_migrated_on_a_copy(install, target):
    users.create_team  # noqa: B018 (the fixture's admin created the org)
    target.execute("INSERT INTO users(id,email,name,role,status,created_at) VALUES ('u-maya',?,'Maya','rep','invited',?)",
                   (EMAIL, now()))
    target.commit()
    # rewind the source to schema version 11 (pattern_observations keyed without the owner)
    raw = sqlite3.connect(install["path"])
    raw.executescript("""
        DROP INDEX IF EXISTS idx_lprop_open;
        CREATE UNIQUE INDEX idx_lprop_open ON learning_proposals(kind, subject) WHERE status='open';
        PRAGMA user_version = 11;
    """)
    raw.close()
    before = _sha(install["path"])
    report = importer.run(install["path"], EMAIL.upper(), log=lambda *_: None)
    assert report.user_id == "u-maya" and not report.user_created and report.source_version == 11
    assert _sha(install["path"]) == before
    assert sqlite3.connect(install["path"]).execute("PRAGMA user_version").fetchone()[0] == 11
    assert target.execute("SELECT COUNT(*) FROM calls WHERE owner_id='u-maya'").fetchone()[0] == 3


def test_a_multi_user_or_foreign_source_is_refused(install, target, tmp_path):
    bad = tmp_path / "not-a-store.db"
    sqlite3.connect(bad).execute("CREATE TABLE x(a)").connection.close()
    with pytest.raises(importer.ImportRefused, match="no calls table"):
        importer.run(bad, EMAIL, log=lambda *_: None)
    raw = sqlite3.connect(install["path"])
    raw.execute("UPDATE loops SET owner_id='u-someone' WHERE rowid = (SELECT MIN(rowid) FROM loops)")
    raw.commit()
    raw.close()
    with pytest.raises(importer.ImportRefused, match="not a single-user install"):
        importer.run(install["path"], EMAIL, log=lambda *_: None)
    assert target.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0          # all or nothing


def test_the_command_line(install, target, monkeypatch, capsys):
    from salescoach import cli
    assert cli.main(["import-sqlite", str(install["path"]), "--as", EMAIL, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "verification passed" in out and "calls" in out
    assert target.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    monkeypatch.delenv("DATABASE_MIGRATE_URL")
    assert cli.main(["import-sqlite", str(install["path"]), "--as", EMAIL]) == 2
    assert "DATABASE_MIGRATE_URL" in capsys.readouterr().err
