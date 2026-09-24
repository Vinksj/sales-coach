"""Domain writes shared by every module.

Every mutation goes through the store engine (add_node/_emit), so the
events table stays the complete episodic history. Functions do not commit;
the caller owns the transaction.
"""
import json
import uuid
from typing import Optional

from . import identity, seller
from .store.stores import engine, now

ACTOR = "salescoach"

CALL_FIELDS = {
    "deal_id", "title", "started_at", "ended_at", "audio_dir", "lang_mode", "asr_live_model",
    "asr_final_model", "transcript_sha", "quality_score", "wf_state", "wf_error",
}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---- accounts, deals, people ------------------------------------------------

def create_account(conn, name, domains=(), actor=ACTOR, source_id=None) -> str:
    nid = new_id("acct")
    engine.add_node(conn, actor, id=nid, type="account", title=name, status="full", source_id=source_id)
    conn.execute("INSERT INTO accounts(node_id,name,domains) VALUES (?,?,?)",
                 (nid, name, json.dumps(sorted({d.lower().strip() for d in domains if d}))))
    return nid


def find_account_by_domain(conn, domain: str) -> Optional[str]:
    domain = (domain or "").lower().strip()
    if not domain:
        return None
    for row in conn.execute("SELECT node_id, domains FROM accounts"):
        if domain in json.loads(row["domains"] or "[]"):
            return row["node_id"]
    return None


def create_deal(conn, name, account_id=None, stage=None, actor=ACTOR, source_id=None) -> str:
    nid = new_id("deal")
    engine.add_node(conn, actor, id=nid, type="deal", title=name, status="full", source_id=source_id)
    conn.execute("INSERT INTO deals(node_id,account_id,name,stage,updated_at) VALUES (?,?,?,?,?)",
                 (nid, account_id, name, stage, now()))
    return nid


def create_person(conn, name, email=None, account_id=None, title=None, is_me=False,
                  contact_file=None, actor=ACTOR, source_id=None, user_id=None) -> str:
    """A person. `user_id` makes it a user's own row (the acting user's when is_me=True and no id is
    given); is_me is derived from it: 1 iff the person IS some user, never set on its own."""
    nid = new_id("person")
    if is_me and user_id is None:
        user_id = identity.actor_of(conn).user_id
    engine.add_node(conn, actor, id=nid, type="person", title=name, status="full", source_id=source_id)
    conn.execute(
        "INSERT INTO people(node_id,name,email,account_id,title,is_me,contact_file,user_id) VALUES (?,?,?,?,?,?,?,?)",
        (nid, name, (email or None) and email.lower().strip(), account_id, title, int(user_id is not None),
         contact_file, user_id))
    return nid


def find_person_by_email(conn, email: str) -> Optional[str]:
    if not email:
        return None
    row = conn.execute("SELECT node_id FROM people WHERE email=?", (email.lower().strip(),)).fetchone()
    return row["node_id"] if row else None


def me_row(conn):
    """The acting user's own people row (people.user_id = the actor), or None."""
    return conn.execute("SELECT * FROM people WHERE user_id=?", (identity.actor_of(conn).user_id,)).fetchone()


def ensure_me(conn, name=None, email=None) -> str:
    """The ACTING user's own people row, created on first use from their profile. Keyed on
    people.user_id, never on "the one is_me row": in a team every user has one."""
    row = me_row(conn)
    if row:
        return row["node_id"]
    name = name or seller.name() or "Me"
    email = email or seller.primary_email()
    if email and find_person_by_email(conn, email):
        email = None            # someone already holds that address; the unique index must not block the seller
    return create_person(conn, name, email=email, is_me=True)


def sync_me(conn) -> Optional[str]:
    """After a profile edit: the acting user's person row follows the profile's name and first address."""
    row = me_row(conn)
    if row is None:
        return None
    name, email = seller.name() or row["name"], seller.primary_email() or row["email"]
    holder = find_person_by_email(conn, email) if email else None
    if holder not in (None, row["node_id"]):
        email = row["email"]
    if (name, email) != (row["name"], row["email"]):
        conn.execute("UPDATE people SET name=?, email=? WHERE node_id=?", (name, email, row["node_id"]))
        conn.execute("UPDATE nodes SET title=? WHERE id=?", (name, row["node_id"]))
        engine._emit(conn, ACTOR, "person_updated", node_id=row["node_id"], after={"name": name, "email": email})
    return row["node_id"]


def link_deal_person(conn, deal_id, person_id, role=None, actor=ACTOR):
    conn.execute(
        "INSERT INTO deal_people(deal_id,person_id,role_in_deal) VALUES (?,?,?) "
        "ON CONFLICT(deal_id,person_id) DO UPDATE SET role_in_deal=COALESCE(excluded.role_in_deal, deal_people.role_in_deal)",
        (deal_id, person_id, role))
    engine._emit(conn, actor, "deal_person_linked", node_id=deal_id, after={"person_id": person_id, "role": role})


def deal_people(conn, deal_id):
    return conn.execute(
        "SELECT p.*, dp.role_in_deal FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
        "WHERE dp.deal_id=? ORDER BY p.name", (deal_id,)).fetchall()


# ---- calls ------------------------------------------------------------------

def create_call(conn, *, source, title=None, deal_id=None, lang_mode="auto", audio_dir=None,
                started_at=None, source_ref=None, wf_state="live", history=False, actor=ACTOR) -> str:
    """history=True marks backfilled history: analysed and remembered, but no follow-up is drafted for
    it and Jarvis is not told it belongs to sales. `source` is only a name; it carries no policy."""
    seller.require_configured()         # the one door every capture and import goes through
    started_at = started_at or now()
    nid = "call-" + started_at[:10].replace("-", "") + "-" + uuid.uuid4().hex[:8]
    engine.add_node(conn, actor, id=nid, type="call", kind=source, title=title, status="full",
                    created_at=started_at)
    conn.execute(
        "INSERT INTO calls(node_id,deal_id,source,source_ref,title,started_at,audio_dir,lang_mode,wf_state,"
        "history,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (nid, deal_id, source, source_ref, title, started_at, audio_dir, lang_mode, wf_state, int(bool(history)),
         now()))
    engine.set_source(conn, actor, nid, uri=source_ref or f"{source}:{nid}", capture=source,
                      raw_path=audio_dir)
    return nid


def owner_of(conn, node_id: str) -> Optional[str]:
    """Who owns a call, deal or loop (nodes.owner_id); None for an unknown id or a directory node."""
    row = conn.execute("SELECT owner_id FROM nodes WHERE id=?", (node_id,)).fetchone()
    return row["owner_id"] if row else None


def user_source_ref(prefix: str, digest: str, conn=None) -> str:
    """A source_ref for something a USER brought in (a paste, an upload): '<prefix>:<owner>:<digest>',
    so two users importing the same text get two calls. Recorder-native refs (fireflies:<id>) are
    unchanged until Phase 4 makes the connections per user."""
    owner = identity.actor_of(conn).user_id if conn is not None else identity.current_user_id()
    return f"{prefix}:{owner}:{digest}"


def get_call(conn, call_id):
    return conn.execute("SELECT * FROM calls WHERE node_id=?", (call_id,)).fetchone()


def update_call(conn, call_id, actor=ACTOR, **fields):
    unknown = set(fields) - CALL_FIELDS
    if unknown:
        raise ValueError(f"unknown call fields: {sorted(unknown)}")
    if not fields:
        return
    before_row = get_call(conn, call_id)
    if before_row is None:
        raise KeyError(call_id)
    before = {k: before_row[k] for k in fields}
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE calls SET {sets}, updated_at=? WHERE node_id=?",
                 (*fields.values(), now(), call_id))
    engine._emit(conn, actor, "call_updated", node_id=call_id, before=before, after=fields)


def set_call_state(conn, call_id, state, error=None, actor=ACTOR):
    update_call(conn, call_id, actor=actor, wf_state=state, wf_error=error)


def add_participant(conn, call_id, person_id):
    conn.execute("INSERT OR IGNORE INTO call_participants(call_id,person_id) VALUES (?,?)",
                 (call_id, person_id))


def call_participants(conn, call_id):
    return conn.execute(
        "SELECT p.* FROM call_participants cp JOIN people p ON p.node_id=cp.person_id "
        "WHERE cp.call_id=? ORDER BY p.is_me DESC, p.name", (call_id,)).fetchall()


def turns(conn, call_id, tier="final"):
    return conn.execute(
        "SELECT * FROM turns WHERE call_id=? AND tier=? ORDER BY idx", (call_id, tier)).fetchall()
