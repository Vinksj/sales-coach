"""Facts in the store -> pattern_observations, one family at a time.

Each collector returns the rows the store should hold for its family; sync()
upserts them on UNIQUE(family, key, subject), removes rows whose source is
gone, and never touches `excluded` (the user's "Wrong"). Evidence is ids, turn
numbers and counts only, never transcript or email text.

  seller         mirror of seller_observations, one row per (tag, call)
  seller_series  talk share, questions asked, discovery slots filled, from the FINAL
                 coach_state snapshot; live captures and stereo imports only
  email_voice    one rule per sent edit (voice.derive_rule)
  followup       per sent nudge: sequence number, weekday, days since the last touch,
                 recipient role; outcome = the derived email_replied
  nudge_trigger  per shown live-coach nudge; replay rows are stored flagged and never counted
  persona        stakeholder title -> bucket, per deal          (observation-only)
  objection      objection type per call, from the live coach and analyst claims (observation-only)
"""
import json
import re

from .. import identity
from ..automation import common
from ..store.stores import now
from . import cfg, seller_id, voice
from .outcomes import _own_domains

FAMILIES = ("seller", "seller_series", "email_voice", "followup", "nudge_trigger", "persona", "objection")
CONF_RANK = {"low": 0, "medium": 1, "high": 2, "explicit": 3}
COLUMNS = ("polarity", "call_id", "deal_id", "email_id", "nudge_id", "evidence", "confidence", "value",
           "outcome_kind", "outcome_value", "source_is_replay", "observed_at")


def _has(conn, table: str) -> bool:
    return conn.table_exists(table)


def _me(conn) -> str:
    """Whose observations these are: the ACTING user. Every collector reads only their rows: on Postgres a
    manager's interactive session can read the team's rows too, and sync() would otherwise store a rep's
    facts as the manager's own observations."""
    return identity.actor_of(conn).user_id


def _row(family, key, subject, **kw) -> dict:
    row = {c: None for c in COLUMNS}
    row.update(family=family, key=key, subject=subject, source_is_replay=0, evidence={})
    row.update(kw)
    return row


# ---- seller ---------------------------------------------------------------------------

def seller(conn) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for o in conn.execute("SELECT o.*, c.deal_id, c.started_at FROM seller_observations o "
                          "JOIN calls c ON c.node_id=o.call_id WHERE o.owner_id=? AND c.owner_id=? ORDER BY o.id",
                          (_me(conn), _me(conn))):
        k = (o["tag"], o["call_id"])
        turns = json.loads(o["evidence_turns"] or "[]")
        if k in rows:
            r = rows[k]
            r["evidence"]["observation_ids"].append(o["id"])
            if CONF_RANK.get(o["confidence"], 1) > CONF_RANK.get(r["confidence"], 1):
                r.update(confidence=o["confidence"], polarity=o["polarity"])
                r["evidence"].update(turns=turns[:5], severity=o["severity"])
            continue
        rows[k] = _row("seller", o["tag"], f"call:{o['call_id']}", polarity=o["polarity"], call_id=o["call_id"],
                       deal_id=o["deal_id"], confidence=o["confidence"], observed_at=o["started_at"],
                       evidence={"observation_ids": [o["id"]], "turns": turns[:5], "severity": o["severity"]})
    return list(rows.values())


def final_snapshots(conn) -> dict:
    """{call_id: {"snap", "replay", "session", "t"}}: the live session's final snapshot when there is
    one, else the newest replay's (a replay reads the same transcript, so its numbers are the same facts)."""
    if not _has(conn, "coach_state"):
        return {}
    modes, me = {}, _me(conn)
    if _has(conn, "nudges"):
        modes = {(r["call_id"], r["session"]): r["mode"] for r in conn.execute(
            "SELECT DISTINCT call_id, session, mode FROM nudges WHERE owner_id=?", (me,))}
    best: dict[str, dict] = {}
    for r in conn.execute("SELECT id, call_id, session, t_call, json FROM coach_state WHERE owner_id=? "
                          "AND json LIKE '%\"final\"%' ORDER BY id", (me,)):
        try:
            snap = json.loads(r["json"])
        except ValueError:
            continue
        if not snap.get("final"):
            continue
        mode = modes.get((r["call_id"], r["session"])) or ("replay" if str(r["session"]).startswith("replay") else "live")
        replay = mode != "live"
        held = best.get(r["call_id"])
        if held is None or (held["replay"] and not replay) or held["replay"] == replay:
            best[r["call_id"]] = {"snap": snap, "replay": replay, "session": r["session"], "t": r["t_call"]}
    return best


def _two_channel_calls(conn) -> dict:
    """Calls whose me/them split is real: live captures and stereo (me left) audio imports."""
    out = {}
    for c in conn.execute("SELECT c.node_id, c.deal_id, c.source, c.started_at, s.lineage FROM calls c "
                          "LEFT JOIN sources s ON s.node_id=c.node_id WHERE c.owner_id=? "
                          "AND c.source IN ('capture','audio_file')", (_me(conn),)):
        if c["source"] == "audio_file":
            try:
                lineage = json.loads(c["lineage"] or "[]")
            except ValueError:
                lineage = []
            if not any(isinstance(x, dict) and x.get("layout") == "stereo_me_left" for x in lineage):
                continue
        out[c["node_id"]] = c
    return out


def seller_series(conn, snapshots=None) -> list[dict]:
    snapshots = final_snapshots(conn) if snapshots is None else snapshots
    rows = []
    for call_id, call in _two_channel_calls(conn).items():
        held = snapshots.get(call_id)
        if held is None:
            continue
        snap, slots = held["snap"], held["snap"].get("slots") or {}
        known = sum(1 for s in slots.values() if (s or {}).get("status") == "known")
        partial = sum(1 for s in slots.values() if (s or {}).get("status") == "partial")
        values = {"talk_share": snap.get("me_share"), "questions_asked": snap.get("me_questions"),
                  "slots_filled": (known + partial) if slots else None}
        for key, value in values.items():
            if value is None:
                continue
            ev = {"session": held["session"], "source": call["source"]}
            if key == "slots_filled":
                ev.update(known=known, partial=partial, slots=len(slots))
            rows.append(_row("seller_series", key, f"call:{call_id}", call_id=call_id, deal_id=call["deal_id"],
                             value=float(value), source_is_replay=int(held["replay"]), observed_at=call["started_at"],
                             evidence=ev))
    return rows


# ---- email voice ------------------------------------------------------------------------

def email_voice(conn) -> list[dict]:
    settings, rows, seen = cfg("email_voice"), [], set()
    for e in conn.execute("SELECT ee.id, ee.email_id, ee.draft_body, ee.final_body, ee.created_at, m.deal_id, m.call_id, "
                          "m.sent_at FROM email_edits ee JOIN emails m ON m.id=ee.email_id WHERE ee.owner_id=? "
                          "AND m.owner_id=? AND m.status='sent' ORDER BY ee.id DESC", (_me(conn), _me(conn))):
        if e["email_id"] in seen:                     # one edit per sent email: the last one
            continue
        seen.add(e["email_id"])
        rule = voice.derive_rule(e["draft_body"] or "", e["final_body"] or "", settings)
        if rule is None:
            continue
        rows.append(_row("email_voice", rule["key"], f"edit:{e['id']}", email_id=e["email_id"], deal_id=e["deal_id"],
                         call_id=e["call_id"], observed_at=e["sent_at"] or e["created_at"],
                         evidence={"edit_id": e["id"], **rule["evidence"]}))
    return rows


# ---- follow-up effectiveness --------------------------------------------------------------

def gap_bucket(days: int, buckets) -> str | None:
    for lo, hi in buckets or []:
        if days >= lo and (hi is None or days <= hi):
            return f"{lo}+" if hi is None else f"{lo}-{hi}"
    return None


def followup(conn) -> list[dict]:
    if not _has(conn, "followup_decisions"):
        return []
    buckets, me = cfg("followup").get("gap_buckets"), _me(conn)
    replied = {r["subject_id"]: r["value"] for r in conn.execute(
        "SELECT subject_id, value FROM derived_outcomes WHERE owner_id=? AND kind='email_replied'", (me,))}
    nudges = conn.execute("SELECT * FROM emails WHERE owner_id=? AND kind='nudge' AND status='sent' "
                          "AND sent_at IS NOT NULL ORDER BY sent_at, id", (me,)).fetchall()
    loop_of = {r["email_id"]: r["loop_id"] for r in conn.execute(
        "SELECT email_id, loop_id FROM followup_decisions WHERE owner_id=? AND email_id IS NOT NULL ORDER BY id", (me,))}
    per_loop: dict[str, int] = {}
    rows = []
    for e in nudges:
        sent = common.ts(e["sent_at"])
        facts = {"weekday": common.ist_date(e["sent_at"]).strftime("%a").lower()}
        loop_id = loop_of.get(e["id"])
        if loop_id:
            per_loop[loop_id] = per_loop.get(loop_id, 0) + 1
            facts["seq"] = str(min(per_loop[loop_id], 4)) + ("+" if per_loop[loop_id] >= 4 else "")
        if e["deal_id"]:
            touches = [r[0] for r in conn.execute(
                "SELECT sent_at FROM emails WHERE owner_id=? AND deal_id=? AND status='sent' AND id!=? AND sent_at<? "
                "UNION ALL SELECT started_at FROM calls WHERE owner_id=? AND deal_id=? AND started_at<?",
                (me, e["deal_id"], e["id"], e["sent_at"], me, e["deal_id"], e["sent_at"]))]
            if _has(conn, "email_replies"):
                touches += [r[0] for r in conn.execute("SELECT received_at FROM email_replies WHERE owner_id=? "
                                                       "AND deal_id=?", (me, e["deal_id"]))]
            before = [t for t in (common.ts(x) for x in touches) if t is not None and t < sent]
            if before:
                gap = gap_bucket((common.ist_date(sent) - common.ist_date(max(before))).days, buckets)
                if gap:
                    facts["gap"] = gap
            to = [a.lower() for a in json.loads(e["to_addrs"] or "[]")]
            if to:
                role = conn.execute("SELECT dp.role_in_deal FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
                                    "WHERE dp.owner_id=? AND dp.deal_id=? AND lower(p.email)=?",
                                    (me, e["deal_id"], to[0])).fetchone()
                slug = re.sub(r"[^a-z0-9]+", "_", (role[0] or "").lower()).strip("_") if role else ""
                if slug:
                    facts["role"] = slug
        verdict = replied.get(str(e["id"]), "absent")
        outcome = {1: "replied", 0: "no_reply", None: "pending"}.get(verdict, "pending")
        for dim, bucket in facts.items():
            rows.append(_row("followup", f"{dim}:{bucket}", f"email:{e['id']}", email_id=e["id"], deal_id=e["deal_id"],
                             outcome_kind="email_replied", outcome_value=outcome, observed_at=e["sent_at"],
                             evidence={"loop_id": loop_id}))
    return rows


# ---- live-nudge usefulness -------------------------------------------------------------------

def nudge_trigger(conn) -> list[dict]:
    if not _has(conn, "nudges"):
        return []
    rows = []
    for n in conn.execute("SELECT n.id, n.call_id, n.session, n.mode, n.trigger, n.outcome, n.dismissed, n.t_call, "
                          "n.shown_wall, n.created_at, c.deal_id FROM nudges n LEFT JOIN calls c ON c.node_id=n.call_id "
                          "WHERE n.owner_id=? AND n.shown=1 ORDER BY n.id", (_me(conn),)):
        outcome = "dismissed" if n["dismissed"] else (n["outcome"] or "unknown")
        rows.append(_row("nudge_trigger", n["trigger"], f"nudge:{n['id']}", nudge_id=n["id"], call_id=n["call_id"],
                         deal_id=n["deal_id"], outcome_kind="nudge_outcome", outcome_value=outcome,
                         source_is_replay=int(n["mode"] != "live"), observed_at=n["shown_wall"] or n["created_at"],
                         evidence={"session": n["session"], "t_call": n["t_call"]}))
    return rows


# ---- personas and objections (observation-only) --------------------------------------------------

def persona_bucket(title: str | None, buckets: dict | None = None) -> str | None:
    words = set(re.findall(r"[a-z]+", (title or "").lower()))
    for bucket, keys in (buckets if buckets is not None else cfg("persona_buckets")).items():
        if words & {str(k).lower() for k in keys or []}:
            return bucket
    return None


def persona(conn) -> list[dict]:
    own, buckets, rows = _own_domains(conn), cfg("persona_buckets"), []
    for p in conn.execute("SELECT dp.deal_id, dp.role_in_deal, p.node_id, p.title, p.email FROM deal_people dp "
                          "JOIN people p ON p.node_id=dp.person_id WHERE dp.owner_id=? AND p.is_me=0", (_me(conn),)):
        domain = (p["email"] or "").rsplit("@", 1)[-1].lower()
        if domain and domain in own:
            continue
        bucket = persona_bucket(p["title"], buckets) or persona_bucket(p["role_in_deal"], buckets)
        if bucket:
            rows.append(_row("persona", bucket, f"person:{p['deal_id']}:{p['node_id']}", deal_id=p["deal_id"],
                             evidence={"person_id": p["node_id"]}))
    return rows


def objection(conn, snapshots=None) -> list[dict]:
    snapshots = final_snapshots(conn) if snapshots is None else snapshots
    rows: dict[tuple, dict] = {}
    me = _me(conn)
    calls = {c["node_id"]: c for c in conn.execute("SELECT node_id, deal_id, started_at FROM calls WHERE owner_id=?", (me,))}
    for call_id, held in snapshots.items():
        call = calls.get(call_id)
        if call is None:
            continue
        for ob in held["snap"].get("open_objections") or []:
            category = re.sub(r"[^a-z0-9]+", "_", str((ob or {}).get("category") or "").lower()).strip("_")
            if not category:
                continue
            r = rows.setdefault((category, call_id), _row(
                "objection", category, f"call:{call_id}", call_id=call_id, deal_id=call["deal_id"], confidence="medium",
                source_is_replay=int(held["replay"]), observed_at=call["started_at"],
                evidence={"sources": [], "at_s": [], "claim_ids": []}))
            if "live_coach" not in r["evidence"]["sources"]:
                r["evidence"]["sources"].append("live_coach")
            r["evidence"]["at_s"].append((ob or {}).get("t"))
    subjects = cfg("objection").get("claim_subjects") or {}
    if subjects:
        marks = ",".join("?" * len(subjects))
        for c in conn.execute(f"SELECT id, call_id, deal_id, subject, confidence FROM claims WHERE owner_id=? "
                              f"AND call_id IS NOT NULL AND subject IN ({marks}) ORDER BY id", (me, *subjects)):
            call = calls.get(c["call_id"])
            if call is None:
                continue
            key = subjects[c["subject"]]
            r = rows.get((key, c["call_id"]))
            if r is None:
                r = rows[(key, c["call_id"])] = _row(
                    "objection", key, f"call:{c['call_id']}", call_id=c["call_id"], deal_id=c["deal_id"] or call["deal_id"],
                    confidence=c["confidence"], observed_at=call["started_at"],
                    evidence={"sources": [], "at_s": [], "claim_ids": []})
            elif r["source_is_replay"]:
                # An analyst claim comes from the call itself: the row no longer rests on a replay alone.
                r.update(source_is_replay=0, confidence=c["confidence"])
            elif CONF_RANK.get(c["confidence"], 1) > CONF_RANK.get(r["confidence"], 1):
                r["confidence"] = c["confidence"]
            if "claims" not in r["evidence"]["sources"]:
                r["evidence"]["sources"].append("claims")
            r["evidence"]["claim_ids"].append(c["id"])
    return list(rows.values())


# ---- sync -------------------------------------------------------------------------------------------

def sync(conn) -> dict:
    """Make pattern_observations match the store. Idempotent; does not commit."""
    snapshots = final_snapshots(conn)
    wanted = {}
    for rows in (seller(conn), seller_series(conn, snapshots), email_voice(conn), followup(conn), nudge_trigger(conn),
                 persona(conn), objection(conn, snapshots)):
        for r in rows:
            wanted[(r["family"], r["key"], r["subject"])] = r
    # The acting user's own rows only: UNIQUE(owner_id, family, key, subject), and on Postgres the read policy
    # also shows a manager their team's rows, which this sync must neither update nor delete (and which the
    # collectors above never read: every one of them is limited to _me(conn)).
    owner = _me(conn)
    have = {(r["family"], r["key"], r["subject"]): r
            for r in conn.execute("SELECT * FROM pattern_observations WHERE owner_id=?", (owner,))}
    sid, stamp, added, updated = seller_id(conn), now(), 0, 0
    for k, r in wanted.items():
        values = [json.dumps(r[c], sort_keys=True, default=str) if c == "evidence" else r[c] for c in COLUMNS]
        old = have.get(k)
        if old is None:
            conn.execute(f"INSERT INTO pattern_observations(family,key,subject,{','.join(COLUMNS)},seller_id,created_at) "
                         f"VALUES ({','.join('?' * (len(COLUMNS) + 5))})", (*k, *values, sid, stamp))
            added += 1
        elif any(old[c] != v for c, v in zip(COLUMNS, values)):
            conn.execute(f"UPDATE pattern_observations SET {', '.join(c + '=?' for c in COLUMNS)} WHERE id=?",
                         (*values, old["id"]))
            updated += 1
    stale = [r["id"] for k, r in have.items() if k not in wanted and r["family"] in FAMILIES]
    for oid in stale:
        conn.execute("DELETE FROM pattern_observations WHERE id=?", (oid,))
    return {"added": added, "updated": updated, "removed": len(stale), "rows": len(wanted)}
