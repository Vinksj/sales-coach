"""Phase 4 auto-send executor. It refuses under the shipped config, the default
policy, when disabled, in dry run, off hours, over the daily cap, for a contact
not approved, for a weak or risky email and for an email already 'sending';
when every condition holds it sends exactly once, through
policy.approve_and_send, as policy:<name>."""
from datetime import date, timedelta, timezone
from types import SimpleNamespace

import pytest

from salescoach import config
from salescoach.automation import autosend, followup
from salescoach.execution import policy
from salescoach.orchestrator import worker
from test_p4_support import (ANITA, ARJUN, NUDGE_BODY, TODAY, TUESDAY, FakeGmail, at, cfg, clock,  # noqa: F401
                             link_nudge, loop_row, make_email, make_loop, world)


def no_gmail():
    raise AssertionError("auto-send reached for Gmail")


def arm(cfg, policy_name="APPROVED_CONTACTS_AUTO_SEND", contacts=(ARJUN,), **auto):
    cfg["policy"] = {"email_policy": policy_name, "approved_contacts": list(contacts)}
    cfg["automation"] = {"auto_send": {"enabled": True, "dry_run": False, **auto}}


@pytest.fixture
def ready(db, world, clock):
    """A clean nudge for a confirmed loop, to a contact who has had mail from us, drafted at 07:30 IST."""
    prior = make_email(db, world.deal, kind="followup", status="sent", sent_at="2026-09-03T06:00:00+00:00",
                       subject="NWP next steps", body="Hi Arjun,\n\nGood speaking today.\n\nThanks,\nMaya")
    loop = make_loop(db, world.deal)
    email = make_email(db, world.deal)
    link_nudge(db, loop, email, world.deal, eval_date=TODAY.isoformat())
    return SimpleNamespace(loop=loop, email=email, prior=prior)


def _row(db, email_id):
    return db.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()


def _log(db):
    return [dict(r) for r in db.execute("SELECT * FROM autosend_log ORDER BY id")]


# ---- refusals ---------------------------------------------------------------------------------

def test_the_shipped_config_never_sends(db, ready):
    assert config.load("automation")["auto_send"]["enabled"] is False
    assert config.load("automation")["auto_send"]["dry_run"] is True
    assert config.load("policy")["email_policy"] == "ALL_EMAILS_REQUIRE_APPROVAL"
    assert autosend.run_once(db, no_gmail) == {"skipped": "auto_send.enabled is false"}
    v = autosend.check(db, _row(db, ready.email))
    assert not v.ok and "email_policy is ALL_EMAILS_REQUIRE_APPROVAL; nothing auto-sends under it" in v.fails
    assert "auto_send.enabled is false in config/automation.yaml" in v.fails
    assert autosend.status(db)["armed"] is False
    assert _row(db, ready.email)["status"] == "drafted"


def test_the_default_policy_refuses_even_when_auto_send_is_on(db, ready, cfg):
    cfg["automation"] = {"auto_send": {"enabled": True, "dry_run": False}}
    assert autosend.run_once(db, no_gmail) == {"skipped": "email_policy is ALL_EMAILS_REQUIRE_APPROVAL"}
    assert autosend.status(db)["armed"] is False


def test_disabled_means_nothing_runs(db, ready, cfg):
    arm(cfg, enabled=False)
    assert autosend.run_once(db, no_gmail) == {"skipped": "auto_send.enabled is false"}
    assert _log(db) == [] and _row(db, ready.email)["status"] == "drafted"


def test_dry_run_only_logs_what_it_would_do(db, ready, cfg):
    arm(cfg, dry_run=True)
    first = autosend.run_once(db, no_gmail)
    assert first["dry_run"] is True and [e["outcome"] for e in first["emails"]] == ["would_send"]
    autosend.run_once(db, no_gmail)
    log = _log(db)
    assert len(log) == 1 and log[0]["outcome"] == "would_send" and log[0]["dry_run"] == 1   # not repeated
    assert "would auto-send because" in log[0]["reasons"]
    assert _row(db, ready.email)["status"] == "drafted"


@pytest.mark.parametrize("when", [at(TODAY, "20:00"), at(TODAY, "09:00"), at(date(2026, 9, 19), "11:00")],
                         ids=["evening", "before-0930", "saturday"])
def test_off_hours_refused(db, ready, cfg, clock, when):
    arm(cfg)
    clock.set(when)
    [out] = autosend.run_once(db, no_gmail)["emails"]
    assert out["outcome"] == "refused"
    assert "outside sending hours (weekdays 09:30 to 19:00 IST)" in out["reasons"]


def test_over_the_daily_cap_refused(db, world, ready, cfg):
    arm(cfg, daily_cap=1)
    second_loop = make_loop(db, world.deal, "Arjun to share the freight data")
    second = make_email(db, world.deal, subject="Freight data")
    link_nudge(db, second_loop, second, world.deal, eval_date=TODAY.isoformat())
    gmail = FakeGmail()
    outcomes = autosend.run_once(db, lambda: gmail)["emails"]
    assert [o["outcome"] for o in outcomes] == ["sent", "refused"]
    assert "today's cap of 1 auto-sends is used up" in outcomes[1]["reasons"]
    assert len(gmail.sent) == 1 and _row(db, second)["status"] == "drafted"
    assert autosend.status(db)["sent_today"] == 1


def test_a_contact_not_approved_is_refused(db, ready, cfg):
    arm(cfg, contacts=(ANITA,))
    [out] = autosend.run_once(db, no_gmail)["emails"]
    assert out["outcome"] == "refused" and f"not on approved_contacts: {ARJUN}" in out["reasons"]


def test_a_young_draft_waits(db, ready, cfg, clock):
    arm(cfg)
    ten_minutes_ago = (TUESDAY - timedelta(minutes=10)).astimezone(timezone.utc).isoformat(timespec="seconds")
    db.execute("UPDATE emails SET updated_at=? WHERE id=?", (ten_minutes_ago, ready.email))
    db.commit()
    [out] = autosend.run_once(db, no_gmail)["emails"]
    assert "the draft is younger than 60 minutes" in out["reasons"]


def test_an_email_in_sending_is_never_touched(db, ready, cfg):
    arm(cfg)
    db.execute("UPDATE emails SET status='sending', error=? WHERE id=?",
               ("delivery unknown: TimeoutError: read timed out. Check Gmail Sent before doing anything.", ready.email))
    db.commit()
    assert autosend.run_once(db, no_gmail)["emails"] == []          # only drafts are read
    v = autosend.check(db, _row(db, ready.email))
    assert not v.ok and "the email is sending, not a draft" in v.fails
    with pytest.raises(policy.SendRefused):
        policy.approve_and_send(db, ready.email, FakeGmail(), approved_by="policy:APPROVED_CONTACTS_AUTO_SEND")


def test_unknown_delivery_is_not_retried(db, ready, cfg):
    arm(cfg)
    gmail = FakeGmail(fail=TimeoutError("read timed out"))
    [out] = autosend.run_once(db, lambda: gmail)["emails"]
    assert out["outcome"] == "failed"
    row = _row(db, ready.email)
    assert row["status"] == "sending" and row["error"].startswith("delivery unknown")
    assert autosend.run_once(db, lambda: gmail)["emails"] == [] and gmail.attempts == 1


def test_a_send_refused_by_the_policy_path_is_logged(db, ready, cfg, monkeypatch):
    arm(cfg)

    def refuse(*a, **k):
        raise policy.SendRefused("someone else is sending it")
    monkeypatch.setattr(policy, "approve_and_send", refuse)
    [out] = autosend.run_once(db, FakeGmail)["emails"]
    assert out["outcome"] == "refused" and _log(db)[-1]["outcome"] == "refused"
    assert "send refused: someone else is sending it" in _log(db)[-1]["reasons"]


# ---- sending --------------------------------------------------------------------------------

def test_sends_exactly_once_when_every_condition_holds(db, ready, cfg):
    arm(cfg)
    gmail = FakeGmail()
    result = autosend.run_once(db, lambda: gmail)
    assert result["dry_run"] is False and [e["outcome"] for e in result["emails"]] == ["sent"]
    assert [m.to for m in gmail.sent] == [[ARJUN]] and gmail.sent[0].message_id.startswith("<sc-")
    row = _row(db, ready.email)
    assert (row["status"], row["approved_by"]) == ("sent", "policy:APPROVED_CONTACTS_AUTO_SEND")

    assert autosend.run_once(db, lambda: gmail)["emails"] == []     # nothing left to send
    assert len(gmail.sent) == 1 and [e["outcome"] for e in _log(db)] == ["sent"]
    worker.drain(db)                                                # EMAIL_SENT -> the nudge counts
    assert loop_row(db, ready.loop)["follow_up_count"] == 1
    assert "policy:APPROVED_CONTACTS_AUTO_SEND approved it" in followup.explain(followup.why(db, ready.email))


def test_low_risk_policy_sends_a_clean_nudge(db, ready, cfg):
    arm(cfg, "LOW_RISK_FOLLOWUPS_AUTO_SEND", contacts=())
    gmail = FakeGmail()
    assert [e["outcome"] for e in autosend.run_once(db, lambda: gmail)["emails"]] == ["sent"]
    assert _row(db, ready.email)["approved_by"] == "policy:LOW_RISK_FOLLOWUPS_AUTO_SEND"


def _body(extra):
    def change(db, r):
        db.execute("UPDATE emails SET body=? WHERE id=?", (NUDGE_BODY.replace("\n\nThanks", f" {extra}\n\nThanks"), r.email))
    return change


def _first_email(db, r):
    db.execute("DELETE FROM emails WHERE id=?", (r.prior,))


def _weak_loop(db, r):
    db.execute("UPDATE loops SET review_state='proposed', confidence='low', source='implied_commitment' "
               "WHERE node_id=?", (r.loop,))


def _two_people(db, r):
    db.execute("UPDATE emails SET cc_addrs=? WHERE id=?", (f'["{ANITA}"]', r.email))


@pytest.mark.parametrize("change,reason", [
    (_body("The pricing note is below."), 'mentions commercial terms ("pricing")'),
    (_body("It covers 3 plants."), "contains a number other than a date or time"),
    (_body("I will send the revised deck tomorrow."), "makes a commitment that is not a confirmed loop"),
    (_first_email, f"first email to {ARJUN}"),
    (_weak_loop, "the commitment behind it is too weak for an external action"),
    (_two_people, "more than one recipient"),
], ids=["commercial", "numbers", "new-commitment", "first-email", "weak-loop", "two-recipients"])
def test_low_risk_policy_refuses_anything_that_is_not(db, ready, cfg, change, reason):
    arm(cfg, "LOW_RISK_FOLLOWUPS_AUTO_SEND", contacts=())
    change(db, ready)
    db.commit()
    [out] = autosend.run_once(db, no_gmail)["emails"]
    assert out["outcome"] == "refused" and any(reason in r for r in out["reasons"]), out["reasons"]
    assert _row(db, ready.email)["status"] == "drafted"


def test_cli_status(db, ready, capsys):
    from test_p4_followup import run_cli
    run_cli("autosend", "status")
    out = capsys.readouterr().out
    assert '"armed": false' in out and '"email_policy": "ALL_EMAILS_REQUIRE_APPROVAL"' in out
