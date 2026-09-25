"""Retention (Phase 8), on both backends: an expired call goes with everything that hangs off it; deals,
accounts and people stay; open loops and a 'sending' email stay without their call; a dry run deletes nothing;
no setting keeps everything. The Postgres-only rules (one rep's duty never touches another's calls, a manager's
comment goes with the rep's call, only a service session may forget annotations) are in
tests/isolation/test_retention_rls.py."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from salescoach import cli, identity, repo
from salescoach.automation import scheduler
from salescoach.lifecycle import retention, settings
from salescoach.manager import comments
from salescoach.sources import paste
from salescoach.store.stores import now
from test_core_pipeline import CALL2
from test_web import processed  # noqa: F401

OLD = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")


def _service(db):
    """Retention runs as a background duty: the owner's SERVICE session (Postgres lets only that forget the
    comments and views about a deleted call)."""
    return identity.as_actor(db, identity.LOCAL_ACTOR.as_service())


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


@pytest.fixture
def aged(db, processed):  # noqa: F811
    """The processed call made 400 days old, with a closed and an open loop, a drafted and a 'sending' email,
    a comment, a view and its raw payload; plus a second, recent call on the same deal."""
    call, deal = processed["call"], processed["deal"]
    db.execute("UPDATE calls SET started_at=? WHERE node_id=?", (OLD, call))
    loops = list(processed["loops"].values())
    assert len(loops) >= 2
    db.execute("UPDATE loops SET status='done' WHERE node_id=?", (loops[0],))
    db.execute("UPDATE loops SET status='open' WHERE node_id=?", (loops[1],))
    db.execute("INSERT INTO emails(call_id,deal_id,status,subject,body,created_at) VALUES (?,?,'sending','s','b',?)",
               (call, deal, now()))
    sending = _count(db, "SELECT MAX(id) FROM emails")
    ref = _count(db, "SELECT source_ref FROM calls WHERE node_id=?", (call,))
    db.execute("INSERT INTO raw_payloads(source_kind,source_ref,body,sha256,created_at) VALUES ('paste',?,'x','sha-old',?)",
               (ref, now()))
    comments.add(db, "call", call, "Push for the CFO date")
    comments.add(db, "loop", loops[0], "Done, thanks")
    db.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
               "VALUES ('local','local','call',?,?)", (call, now()))
    db.commit()
    recent = paste.import_text(db, CALL2, "NWP second", deal_id=deal, participants=processed["people"])
    db.commit()
    return {**processed, "closed": loops[0], "open": loops[1], "sending": sending, "recent": recent}


def test_an_expired_call_goes_with_what_hangs_off_it(db, aged):
    call, deal = aged["call"], aged["deal"]
    drafted = aged["email"]["id"]
    people_before = _count(db, "SELECT COUNT(*) FROM people")
    with _service(db):
        result = retention.purge(db, days=365)
    assert result["calls"] == 1 and result["cutoff"] < now()[:10]
    for table, col in (("calls", "node_id"), ("turns", "call_id"), ("artifacts", "call_id"), ("claims", "call_id"),
                       ("agent_runs", "call_id"), ("call_participants", "call_id"), ("sources", "node_id"),
                       ("nodes", "id"), ("events", "node_id"), ("seller_observations", "call_id")):
        assert _count(db, f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (call,)) == 0, table
    assert _count(db, "SELECT COUNT(*) FROM loops WHERE node_id=?", (aged["closed"],)) == 0
    assert _count(db, "SELECT COUNT(*) FROM nodes WHERE id=?", (aged["closed"],)) == 0
    open_loop = db.execute("SELECT call_id, status FROM loops WHERE node_id=?", (aged["open"],)).fetchone()
    assert tuple(open_loop) == (None, "open")                           # open loops stay, without their call
    assert _count(db, "SELECT COUNT(*) FROM emails WHERE id=?", (drafted,)) == 0
    sending = db.execute("SELECT call_id, status FROM emails WHERE id=?", (aged["sending"],)).fetchone()
    assert tuple(sending) == (None, "sending")
    assert _count(db, "SELECT COUNT(*) FROM comments") == 0
    assert _count(db, "SELECT COUNT(*) FROM access_log") == 0
    assert _count(db, "SELECT COUNT(*) FROM raw_payloads WHERE sha256='sha-old'") == 0
    # what stays: the deal, the directory, the recent call
    assert _count(db, "SELECT COUNT(*) FROM deals WHERE node_id=?", (deal,)) == 1
    assert _count(db, "SELECT COUNT(*) FROM people") == people_before
    assert _count(db, "SELECT COUNT(*) FROM calls WHERE node_id=?", (aged["recent"],)) == 1
    assert _count(db, "SELECT COUNT(*) FROM turns WHERE call_id=?", (aged["recent"],)) > 0
    audit = db.execute("SELECT after FROM events WHERE kind='retention.purge'").fetchall()
    assert len(audit) == 1 and json.loads(audit[0][0])["call_ids"] == [call]
    with _service(db):
        assert retention.purge(db, days=365)["calls"] == 0              # idempotent


def test_a_dry_run_counts_and_deletes_nothing(db, aged, capsys):
    before = _count(db, "SELECT COUNT(*) FROM turns")
    assert retention.purge(db, days=365, dry_run=True)["calls"] == 1
    assert cli.main(["retention", "--dry-run", "--days", "365"]) == 0
    assert "would delete 1 call(s)" in capsys.readouterr().out
    assert _count(db, "SELECT COUNT(*) FROM turns") == before
    assert _count(db, "SELECT COUNT(*) FROM calls WHERE node_id=?", (aged["call"],)) == 1


def test_no_setting_keeps_everything_and_the_duty_says_so(db, aged):
    assert settings.retention_days() is None
    duty = next(d for d in scheduler.default_duties() if d.name == "retention")
    assert not duty.needs_profile and duty.per_user
    assert duty.run(db) == {"skipped": "retention.days is not set: everything is kept"}
    assert _count(db, "SELECT COUNT(*) FROM calls") == 2


def test_the_setting_drives_the_duty_and_a_typo_deletes_nothing(db, aged, seller_settings):
    (seller_settings / "org.yaml").write_text("retention:\n  days: '365 days'\n")
    assert settings.retention_days() is None
    settings.save("365", "Calls are recorded for coaching.")
    assert settings.retention_days() == 365
    duty = next(d for d in scheduler.default_duties() if d.name == "retention")
    with _service(db):
        assert duty.run(db)["calls"] == 1
    db.commit()
    assert repo.get_call(db, aged["call"]) is None
    with pytest.raises(ValueError):
        settings.save("-3", "")
    with pytest.raises(ValueError):
        settings.save("", "x" * (settings.MAX_NOTICE + 1))
