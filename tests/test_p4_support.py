"""Builders and fixtures shared by the Phase 4 tests (no tests in this module).

Everything is offline: FakeProvider for the agents, FakeGmail for sending and
reading replies, FakeCalendar for free/busy, and the IST clock pinned by the
`clock` fixture. Config files are never edited: `cfg` overlays them in memory.
"""
import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from salescoach import config, repo
from salescoach.automation import common
from salescoach.automation.calendar import CalEvent, CalendarUnavailable
from salescoach.memory import gate
from salescoach.orchestrator import bus, workflow
from salescoach.store.stores import engine, now
from salescoach.store.db import insert_id

IST = common.IST
ORIGIN = {"origin": "http://127.0.0.1:8140"}
ARJUN, ANITA, OUTSIDER = "arjun@northwind.test", "anita@northwind.test", "someone@elsewhere.test"
TUESDAY = datetime(2026, 9, 15, 11, 0, tzinfo=IST)          # Tue 15 Sep 2026, 11:00 IST
TODAY = TUESDAY.date()
NUDGE_BODY = ("Hi Arjun,\n\nOn our 2 Sep call you mentioned you'd set up a meeting with your CFO. "
              "Is that still the plan for this month?\n\nThanks,\nMaya")


# ---- config and clock -----------------------------------------------------------

def _merge(base, extra):
    out = dict(base or {})
    for key, value in (extra or {}).items():
        out[key] = _merge(out.get(key), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


@pytest.fixture
def cfg(monkeypatch):
    """cfg['automation'] = {...} overlays config/automation.yaml for one test (deep-merged, files untouched)."""
    overrides = {}
    real = config.load
    monkeypatch.setattr(config, "load",
                        lambda name: _merge(real(name), overrides[name]) if name in overrides else real(name))
    return overrides


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def set(self, at):
        self.at = at


@pytest.fixture
def clock(monkeypatch):
    c = Clock(TUESDAY)
    monkeypatch.setattr(common, "now_ist", c)          # today_ist() reads it too
    return c


def at(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)), IST)


# ---- the world: one deal, two buyers, Maya -----------------------------------------

@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(bus, "RETRY_BACKOFF_S", 0)
    from salescoach.plugins import execution
    workflow.ensure_plugins()
    execution.register(workflow)                       # idempotent; guards against a plugin reset elsewhere
    acct = repo.create_account(db, "Northwind", ["northwind.test"])
    deal = repo.create_deal(db, "NWP pilot", account_id=acct, stage="pilot")
    arjun = repo.create_person(db, "Arjun Kumar", email=ARJUN, account_id=acct, title="Sourcing head")
    anita = repo.create_person(db, "Anita Rao", email=ANITA, account_id=acct, title="CFO")
    repo.link_deal_person(db, deal, arjun, role="champion")
    repo.link_deal_person(db, deal, anita, role="economic_buyer")
    me = repo.ensure_me(db)
    repo.link_deal_person(db, deal, me)
    db.commit()
    return SimpleNamespace(acct=acct, deal=deal, arjun=arjun, anita=anita, me=me)


def make_loop(db, deal, description="Arjun to set up the meeting with the CFO", *, owner="prospect",
              owner_name="Arjun Kumar", type_="prospect_action", review_state="confirmed",
              source="explicit_commitment", confidence="explicit", priority="medium", status="open",
              due_date=None, next_check_at=TODAY.isoformat(), created_at="2026-09-02T12:00:00+05:30",
              last_activity_at=None, follow_up_count=0, call_id=None, **extra) -> str:
    lid = repo.new_id("loop")
    engine.add_node(db, "test", id=lid, type="loop", kind=type_, title=description, status="full")
    row = {"node_id": lid, "deal_id": deal, "call_id": call_id, "type": type_, "description": description,
           "owner": owner, "owner_name": owner_name, "source": source, "confidence": confidence,
           "evidence_quote": "I'll set up a meeting with our CFO", "priority": priority, "due_date": due_date,
           "due_date_confidence": "explicit" if due_date else "unknown", "status": status,
           "next_check_at": next_check_at, "follow_up_count": follow_up_count, "review_state": review_state,
           "created_at": created_at, "last_activity_at": last_activity_at or created_at}
    row.update(extra)
    db.execute(f"INSERT INTO loops({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    # A confirmed loop carries Maya's word on its status, exactly as review.confirm_loop records it.
    gate.set_initial(db, lid, "loops", "status", status,
                     "user_input" if review_state == "confirmed" else confidence, {"kind": "test"})
    db.commit()
    return lid


def make_email(db, deal, *, to=(ARJUN,), cc=(), subject="The CFO meeting", body=NUDGE_BODY, kind="nudge",
               status="drafted", call_id=None, sent_at=None, thread_id=None, error=None,
               created_at="2026-09-15T02:00:00+00:00") -> int:
    cur = db.execute(
        "INSERT INTO emails(call_id,deal_id,kind,to_addrs,cc_addrs,subject,body,draft_body,status,sent_at,"
        "gmail_thread_id,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (call_id, deal, kind, json.dumps(list(to)), json.dumps(list(cc)), subject, body, body, status, sent_at,
         thread_id, error, created_at, created_at))
    db.commit()
    return insert_id(cur)


def link_nudge(db, loop_id, email_id, deal=None, eval_date="2026-09-10"):
    """The follow-up decision that drafted a nudge (as followup._apply records it)."""
    db.execute("INSERT INTO followup_decisions(loop_id,deal_id,eval_date,stage,check_name,decision,rationale,facts,"
               "email_id,created_at) VALUES (?,?,?,'agent','agent','send_nudge','test','{}',?,?)",
               (loop_id, deal, eval_date, email_id, now()))
    db.commit()


def loop_row(db, loop_id):
    return db.execute("SELECT * FROM loops WHERE node_id=?", (loop_id,)).fetchone()


def events_of(db, type_):
    return [dict(r, payload=json.loads(r["payload"])) for r in
            db.execute("SELECT * FROM wf_events WHERE type=? ORDER BY id", (type_,))]


# ---- agent answers ------------------------------------------------------------------

def decision(decision="send_nudge", **kw):
    out = {"still_relevant": True, "decision": decision, "wait_until": None,
           "rationale": "The date he gave has passed and he has been responsive, so a short nudge is right now.",
           "relationship_risk": "low", "relationship_note": "Arjun is an engaged champion."}
    out.update(kw)
    return out


def nudge(**kw):
    out = {"to": [ARJUN], "cc": [], "subject": "The CFO meeting", "body": NUDGE_BODY,
           "rationale": "Short and specific, in his own words."}
    out.update(kw)
    return out


# ---- Gmail and calendar fakes -------------------------------------------------------------

class FakeGmail:
    """Send, Drafts, Sent search and thread reads, all in memory."""
    address = "maya@tessel.test"

    def __init__(self, threads=None, fail=None):
        self.sent, self.drafts, self.read = [], [], []
        self.threads = dict(threads or {})
        self.fail = fail                    # an exception instance raised by send()
        self.broken_threads = set()
        self.attempts = 0                   # every send() call, including ones that raise

    def profile(self):
        return {"emailAddress": self.address}

    def send(self, msg):
        self.attempts += 1
        if self.fail is not None:
            raise self.fail
        self.sent.append(msg)
        return {"message_id": f"m{len(self.sent)}", "thread_id": f"t{len(self.sent)}"}

    def save_draft(self, msg):
        self.drafts.append(msg)
        return {"draft_id": f"d{len(self.drafts)}", "message_id": "m-draft", "thread_id": "t-draft"}

    def find_sent(self, rfc822_message_id):
        return None

    def read_replies(self, thread_id, since):
        self.read.append((thread_id, since))
        if thread_id in self.broken_threads:
            raise RuntimeError("thread unreadable")
        return list(self.threads.get(thread_id, []))


def gmail_msg(mid, body, frm=ARJUN, name="Arjun Kumar", received="2026-09-15T05:00:00+00:00",
              labels=("INBOX",), thread="t-1"):
    return {"message_id": mid, "thread_id": thread, "from_addr": frm, "from_name": name,
            "subject": "Re: The CFO meeting", "received_at": received, "label_ids": list(labels), "body": body}


class FakeCalendar:
    """Read-only by construction: it can only list events."""
    name = "fake_calendar"

    def __init__(self, events=(), error=None):
        self._events, self.error, self.calls = list(events), error, []

    def events(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise CalendarUnavailable(self.error)
        return [e for e in self._events if e.start < end and e.end > start]


def ev(start, minutes=30, **kw) -> CalEvent:
    kw.setdefault("id", f"e-{start:%m%d%H%M}")
    kw.setdefault("title", "Busy")
    return CalEvent(start=start, end=start + timedelta(minutes=minutes), **kw)
