"""learned_patterns: counted from pattern_observations, in code, the way
memory/patterns.recompute counts seller_patterns (which stays as it is).

Counting rules
  * distinct calls and distinct deals, never raw observations;
  * an observation does not count when it is low-confidence, comes from a
    replay, or the user marked it wrong (`excluded`);
  * a pattern merged into another folds its observations into the target.

Promotion (design §7), window = the last `window_calls` analysed calls:
  emerging     >= 3 calls AND >= 2 deals (all time) AND seen in >= 30% of the window
  established  >= 6 calls over >= 3 deals AND seen in both halves of the window
  dormant      not seen in the last 10 analysed calls
  retired      not seen in the last 20 analysed calls, or retired by the user
  a dormant (or absence-retired) pattern that recurs is revived with returned=1;
  one the user retired or marked wrong never is.
Labels, never percentages; n is always stored and shown.

Family specifics
  email_voice    active at >= 3 supporting edits or when the user confirms; never goes
                 dormant (no more edits is what a working rule looks like)
  followup       buckets of raw counts; a rate only at n >= 20 decided sends
  nudge_trigger  shown/followed/ignored/dismissed; at n >= 15 shown a PROPOSAL, never a config edit
  persona, objection   observation-only: nothing is promoted before >= 8 deals in the
                 family and >= 3 deals in the bucket

user_state, merged_into and no_prompt are gated fields written as user_input;
recompute reads them and never writes them, so the user outranks every recompute.

Proposals: at most one open per subject. A decided one stays as history, and the same
subject is proposed again only when the evidence has grown to `proposals.repropose_factor`
times the n at the decision and the condition still holds.

Every change of a pattern's status or label emits a `learned_pattern_status` event, which
is what the weekly digest (weekly.py) reads; a recompute that changes nothing emits nothing.
"""
import json
import re

from .. import config, identity
from ..automation import common
from ..memory import gate
from ..memory import patterns as seller_memory
from ..store.stores import engine, now
from . import cfg, ensure_columns, observe, seller_id, voice

SCOPE = "global"
CALL_FAMILIES = ("seller", "objection")
OBSERVATION_ONLY = ("persona", "objection")
PATTERN_FAMILIES = ("seller", "email_voice", "followup", "nudge_trigger", "persona", "objection")
FAMILY_LABELS = {"seller": "How you sell", "email_voice": "Your email voice", "followup": "Follow-up effectiveness",
                 "nudge_trigger": "Live nudges", "persona": "Buyer personas", "objection": "Objections"}
USER = {"kind": "user_input", "ref": "learning page"}
USER_STATES = ("confirmed", "wrong", "retired")
WEEKDAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
            "sat": "Saturday", "sun": "Sunday"}


class ActionRefused(ValueError):
    """A user action that cannot be applied; the message is for the user."""


def _owner(conn=None) -> str:
    return identity.actor_of(conn).user_id if conn is not None else identity.current_user_id()


def pattern_id(family: str, key: str, scope: str = SCOPE, owner: str | None = None) -> str:
    """lp:<family>:u:<owner>:<key>. Scope is always 'global' today and is not in the id; UNIQUE(owner_id,
    family, key, scope) keeps the column for a per-deal scope later (which would then need the id)."""
    return f"lp:{family}:u:{owner or _owner()}:{key}"


def parse_pattern_id(pid: str):
    """(family, owner, key) for a well-formed id, else None."""
    parts = pid.split(":", 4)
    if len(parts) == 5 and parts[0] == "lp" and parts[2] == "u" and parts[1] and parts[3] and parts[4]:
        return parts[1], parts[3], parts[4]
    return None


def countable(o) -> bool:
    return not o["excluded"] and not o["source_is_replay"] and (o["confidence"] or "medium") != "low"


# ---- promotion ---------------------------------------------------------------------------

def promote(n_calls: int, n_deals: int, calls_with: set, window: list, rules: dict | None = None) -> str:
    """'' | 'emerging' | 'established'. `window` is newest first."""
    rules = rules or cfg("promotion")
    em, es = rules["emerging"], rules["established"]
    if n_calls >= int(es["min_calls"]) and n_deals >= int(es["min_deals"]):
        half = len(window) // 2
        newer, older = window[:half], window[half:]
        both = any(c in calls_with for c in newer) and any(c in calls_with for c in older)
        if both or not es.get("both_halves", True):
            return "established"
    in_window = sum(1 for c in window if c in calls_with)
    if n_calls >= int(em["min_calls"]) and n_deals >= int(em["min_deals"]) and window \
            and in_window >= float(em["min_window_share"]) * len(window) - 1e-9:
        return "emerging"
    return ""


def absence(calls_with: set, analysed: list, rules: dict | None = None) -> str:
    """'' | 'dormant' | 'retired', from where the pattern was last seen among the analysed calls."""
    rules = rules or cfg("promotion")
    if not calls_with or not any(c in calls_with for c in analysed):
        return ""                                    # nothing to place on the timeline: say nothing
    dormant_n, retire_n = int(rules["dormant_after_calls"]), int(rules["retire_after_calls"])
    if len(analysed) > retire_n and not any(c in calls_with for c in analysed[:retire_n]):
        return "retired"
    if len(analysed) > dormant_n and not any(c in calls_with for c in analysed[:dormant_n]):
        return "dormant"
    return ""


# ---- merges ----------------------------------------------------------------------------------

def _merge_targets(existing: dict) -> dict:
    """{(family, key): (family, key)} resolved through chains; a cycle resolves to itself."""
    direct = {}
    for p in existing.values():
        target = existing.get(p["merged_into"]) if p["merged_into"] else None
        if target is not None and target["family"] == p["family"] and target["id"] != p["id"]:
            direct[(p["family"], p["key"])] = (target["family"], target["key"])
    resolved = {}
    for src in direct:
        seen, cur = {src}, direct[src]
        while cur in direct and cur not in seen:
            seen.add(cur)
            cur = direct[cur]
        resolved[src] = src if cur in seen else cur
    return {s: t for s, t in resolved.items() if s != t}


# ---- summaries (fixed templates; every number is a count made here) ----------------------------

def _humanise(key: str) -> str:
    return key.removeprefix("new:").replace("_", " ").strip().capitalize()


def _followup_label(key: str) -> str:
    dim, _, bucket = key.partition(":")
    if dim == "weekday":
        return f"Nudges sent on a {WEEKDAYS.get(bucket, bucket)}"
    if dim == "seq":
        return f"Nudge number {bucket} on a loop"
    if dim == "gap":
        return f"Nudges {bucket} days after the last touch"
    if dim == "role":
        return f"Nudges to the {bucket.replace('_', ' ')}"
    return _humanise(key)


def _summary(family: str, key: str, stats: dict, taxonomy: dict) -> str:
    if family == "seller":
        return (taxonomy.get(key) or {}).get("name") or _humanise(key)
    if family == "email_voice":
        return voice.sentence(key)
    if family == "followup":
        text = (f"{_followup_label(key)}: {stats['replied']} replied, {stats['no_reply']} not, "
                f"{stats['pending']} still open (n={stats['decided']} decided)")
        return text + (f"; reply rate {round(stats['rate'] * 100)}%" if stats.get("rate") is not None else "")
    if family == "nudge_trigger":
        return (f"'{key.replace('_', ' ')}' shown {stats['shown']} times live: followed {stats['followed']}, "
                f"ignored {stats['ignored']}, dismissed {stats['dismissed']}, unrated {stats['unknown']}")
    if family == "persona":
        return f"{_humanise(key)} stakeholders"
    if family == "objection":
        return f"{_humanise(key)} objection"
    return _humanise(key)


# ---- recompute ------------------------------------------------------------------------------------

def recompute(conn) -> dict:
    """Sync observations, then rebuild learned_patterns and the open proposals. Idempotent; no commit."""
    ensure_columns(conn)
    synced = observe.sync(conn)
    rules, taxonomy, sid, stamp = cfg("promotion"), seller_memory.taxonomy(), seller_id(conn), now()
    owner = _owner(conn)
    analysed = seller_memory._analysed_calls(conn)
    window = analysed[:int(rules["window_calls"])]
    existing = {r["id"]: r for r in conn.execute("SELECT * FROM learned_patterns WHERE owner_id=?", (owner,))}

    # The user's "Wrong" covers observations that arrive later too.
    for p in existing.values():
        if p["user_state"] == "wrong":
            conn.execute("UPDATE pattern_observations SET excluded=1 WHERE family=? AND key=? AND excluded=0",
                         (p["family"], p["key"]))

    merges = _merge_targets(existing)
    groups: dict[tuple, list] = {}
    own_obs: dict[tuple, int] = {}
    for o in conn.execute("SELECT * FROM pattern_observations WHERE family IN (%s) ORDER BY observed_at, id"
                          % ",".join("?" * len(PATTERN_FAMILIES)), PATTERN_FAMILIES):
        src = (o["family"], o["key"])
        own_obs[src] = own_obs.get(src, 0) + 1
        groups.setdefault(merges.get(src, src), []).append(o)
    family_deals = {f: len({o["deal_id"] for k, obs in groups.items() if k[0] == f for o in obs
                            if countable(o) and o["deal_id"]}) for f in OBSERVATION_ONLY}
    gate_rule = cfg("observation_only")

    wanted = {}
    for (family, key), obs in groups.items():
        good = [o for o in obs if countable(o)]
        calls_with = {o["call_id"] for o in good if o["call_id"]}
        deals_with = {o["deal_id"] for o in good if o["deal_id"]}
        seen = sorted(o["observed_at"] for o in good if o["observed_at"])
        stats, label, status = {}, "", "candidate"
        support = sum(1 for c in window if c in calls_with)
        if family in CALL_FAMILIES:
            stats = {"window": len(window)}
            label = promote(len(calls_with), len(deals_with), calls_with, window, rules)
        if family in OBSERVATION_ONLY:
            stats.update(family_deals=family_deals[family], min_deals=int(gate_rule["min_deals"]),
                         min_per_bucket=int(gate_rule["min_per_bucket"]))
            eligible = family_deals[family] >= int(gate_rule["min_deals"]) and \
                len(deals_with) >= int(gate_rule["min_per_bucket"])
            if not eligible:
                label = ""
            status = "active" if eligible and (label or family == "persona") else "candidate"
        elif family == "seller":
            status = "active" if label else "candidate"
        elif family == "email_voice":
            need = int(cfg("email_voice")["active_at_edits"])
            stats = {"edits": len(good), "active_at": need}
            status = "active" if len(good) >= need else "candidate"
        elif family == "followup":
            c = {v: sum(1 for o in good if o["outcome_value"] == v) for v in ("replied", "no_reply", "pending")}
            decided, min_n = c["replied"] + c["no_reply"], int(cfg("followup")["rate_min_n"])
            stats = {**c, "sent": len(good), "decided": decided, "rate_min_n": min_n,
                     "rate": round(c["replied"] / decided, 2) if decided >= min_n else None}
            status = "active" if decided >= min_n else "candidate"
        elif family == "nudge_trigger":
            c = {v: sum(1 for o in good if o["outcome_value"] == v) for v in ("followed", "ignored", "dismissed", "unknown")}
            stats = {**c, "shown": len(good), "replays_not_counted": sum(1 for o in obs if o["source_is_replay"])}
            status = "active" if len(good) >= int(cfg("nudge_trigger")["propose_min_shown"]) else "candidate"
        if family in CALL_FAMILIES:
            status = absence(calls_with, analysed, rules) or status
        wanted[pattern_id(family, key, owner=owner)] = {
            "family": family, "key": key, "polarity": next((o["polarity"] for o in reversed(good) if o["polarity"]), None),
            "n_obs": len(good), "n_calls": len(calls_with), "n_deals": len(deals_with), "support": support,
            "label": label if status == "active" else "", "status": status,
            "first_seen": seen[0] if seen else None, "last_seen": seen[-1] if seen else None,
            "summary": _summary(family, key, stats, taxonomy), "stats": stats}

    # Rows that exist only because the user (or a merge) said something about them stay, with zero counts.
    for pid, p in existing.items():
        if pid not in wanted and (p["user_state"] or p["merged_into"] or p["no_prompt"]
                                  or own_obs.get((p["family"], p["key"]))):
            wanted[pid] = {"family": p["family"], "key": p["key"], "polarity": p["polarity"], "n_obs": 0, "n_calls": 0,
                           "n_deals": 0, "support": 0, "label": "", "status": "candidate", "first_seen": p["first_seen"],
                           "last_seen": p["last_seen"], "summary": _summary(p["family"], p["key"], _zero_stats(p["family"]),
                                                                            taxonomy), "stats": {}}

    changed = 0
    for pid, w in wanted.items():
        old = existing.get(pid)
        user_state = old["user_state"] if old else None
        merged = (w["family"], w["key"]) in merges
        if merged:
            target = merges[(w["family"], w["key"])]
            w.update(status="retired", label="", n_obs=0, n_calls=0, n_deals=0, support=0,
                     summary=f"{w['summary']} (merged into {_summary(*target, _zero_stats(target[0]), taxonomy)})")
        elif user_state in ("wrong", "retired"):
            w.update(status="retired", label="")
        elif user_state == "confirmed" and w["status"] == "candidate":
            w["status"] = "active"                     # the user's confirm is a promotion; absence still shows
        returned = old["returned"] if old else 0
        if old and old["status"] in ("dormant", "retired") and w["status"] in ("candidate", "active") \
                and user_state not in ("wrong", "retired") and not old["merged_into"]:
            returned = 1
        row = (w["family"], w["key"], SCOPE, w["polarity"], w["n_obs"], w["n_calls"], w["n_deals"], w["support"],
               w["label"], w["status"], w["first_seen"], w["last_seen"], returned, w["summary"],
               json.dumps(w["stats"], sort_keys=True), sid)
        if old is not None and row == tuple(old[c] for c in _COMPARE):
            continue
        was = (old["status"], old["label"], old["returned"]) if old else (None, None, 0)
        if was != (w["status"], w["label"], returned) and (old is not None or w["status"] != "candidate"):
            engine._emit(conn, "learning", "learned_pattern_status", node_id=pid,
                         before={"status": was[0], "label": was[1], "returned": was[2]},
                         after={"status": w["status"], "label": w["label"], "returned": returned,
                                "family": w["family"]})
        conn.execute(
            "INSERT INTO learned_patterns(id,family,key,scope,polarity,n_obs,n_calls,n_deals,support,label,status,"
            "first_seen,last_seen,returned,summary,stats,seller_id,updated_at,owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET polarity=excluded.polarity, n_obs=excluded.n_obs, n_calls=excluded.n_calls, "
            "n_deals=excluded.n_deals, support=excluded.support, label=excluded.label, status=excluded.status, "
            "first_seen=excluded.first_seen, last_seen=excluded.last_seen, returned=excluded.returned, "
            "summary=excluded.summary, stats=excluded.stats, seller_id=excluded.seller_id, updated_at=excluded.updated_at",
            (pid, *row, stamp, owner))
        changed += 1
    gone = [pid for pid in existing if pid not in wanted]
    for pid in gone:
        conn.execute("DELETE FROM learned_patterns WHERE id=?", (pid,))
        conn.execute("DELETE FROM field_provenance WHERE entity_id=?", (pid,))
    proposals = _propose_merges(conn, taxonomy) + _propose_trigger_weights(conn)
    return {"observations": synced, "patterns": len(wanted), "changed": changed, "removed": len(gone),
            "new_proposals": proposals}


_COMPARE = ("family", "key", "scope", "polarity", "n_obs", "n_calls", "n_deals", "support", "label", "status",
            "first_seen", "last_seen", "returned", "summary", "stats", "seller_id")


def _zero_stats(family: str) -> dict:
    if family == "followup":
        return {"replied": 0, "no_reply": 0, "pending": 0, "decided": 0, "rate": None}
    if family == "nudge_trigger":
        return {"shown": 0, "followed": 0, "ignored": 0, "dismissed": 0, "unknown": 0}
    return {}


# ---- proposals --------------------------------------------------------------------------------------

def tokens(tag: str, stop=()) -> set:
    """Token set of a tag or a name: lower-case words, light suffix stripping, stop words dropped."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", tag.removeprefix("new:").lower()):
        if w in stop:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def similarity(a: str, b: str, stop=()) -> float:
    ta, tb = tokens(a, stop), tokens(b, stop)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _open_proposal(conn, kind, subject, pattern, target, summary, payload, n: int) -> int:
    """Open a proposal unless one is open, or the user already decided this subject and the evidence
    has not materially grown since. `n` is the evidence behind it now (nudges shown live / calls with
    the tag). Decided rows are never touched: they are the history."""
    if conn.execute("SELECT 1 FROM learning_proposals WHERE kind=? AND subject=? AND status='open'",
                    (kind, subject)).fetchone():
        return 0
    last = conn.execute("SELECT id, decided_n FROM learning_proposals WHERE kind=? AND subject=? AND status!='open' "
                        "ORDER BY id DESC LIMIT 1", (kind, subject)).fetchone()
    rounds = conn.execute("SELECT COUNT(*) FROM learning_proposals WHERE kind=? AND subject=?",
                          (kind, subject)).fetchone()[0]
    if last is not None:
        if last["decided_n"] is None:
            # Decided before n was recorded (phase F1): today's evidence becomes the baseline, so the
            # subject comes back after it doubles from HERE, not at once.
            conn.execute("UPDATE learning_proposals SET decided_n=? WHERE id=?", (int(n), last["id"]))
            return 0
        if n < float(cfg("proposals")["repropose_factor"]) * max(int(last["decided_n"]), 1):
            return 0
        payload = {**payload, "previous_proposal": last["id"], "n_at_previous_decision": int(last["decided_n"])}
        summary += f" Raised again: the evidence grew from n={int(last['decided_n'])} to n={int(n)} since you decided."
    conn.execute(
        "INSERT INTO learning_proposals(kind,subject,pattern_id,target_id,summary,payload,status,created_at) "
        "VALUES (?,?,?,?,?,?,'open',?)",
        (kind, subject, pattern, target, summary, json.dumps({**payload, "n": int(n), "round": rounds + 1},
                                                            sort_keys=True), now()))
    return 1


def decided_proposals(conn, limit: int = 50) -> list[dict]:
    """The history: what was proposed and what the user decided, newest first."""
    out = []
    for r in conn.execute("SELECT * FROM learning_proposals WHERE status!='open' ORDER BY resolved_at DESC, id DESC "
                          "LIMIT ?", (limit,)):
        d = dict(r)
        d["payload"] = json.loads(r["payload"] or "{}")
        out.append(d)
    return out


def _propose_merges(conn, taxonomy: dict) -> int:
    """A `new:` seller tag close to a taxonomy tag or to another new tag -> a merge PROPOSAL. Never a merge."""
    hygiene = cfg("tag_hygiene")
    stop, floor = set(hygiene.get("stop_words") or []), float(hygiene["min_similarity"])
    rows = {r["key"]: r for r in conn.execute(
        "SELECT * FROM learned_patterns WHERE owner_id=? AND family='seller' AND merged_into IS NULL "
        "AND (user_state IS NULL OR user_state='confirmed')", (_owner(conn),))}
    made = 0
    for key, p in sorted(rows.items()):
        if not key.startswith("new:"):
            continue
        best = None
        for other, entry in taxonomy.items():
            if entry.get("polarity") and p["polarity"] and entry["polarity"] != p["polarity"]:
                continue
            score = max(similarity(key, other, stop), similarity(key, entry.get("name") or "", stop))
            if score >= floor and (best is None or score > best[0]):
                best = (score, other)
        if best is None:
            for other, q in rows.items():
                # Between two new tags the one seen on more calls (then the older, then the first by name) is kept.
                keeps = (-q["n_calls"], q["first_seen"] or "", other) < (-p["n_calls"], p["first_seen"] or "", key)
                if other == key or not other.startswith("new:") or not keeps or \
                        (q["polarity"] and p["polarity"] and q["polarity"] != p["polarity"]):
                    continue
                score = similarity(key, other, stop)
                if score >= floor and (best is None or score > best[0]):
                    best = (score, other)
        if best is None:
            continue
        score, other = best
        shared = sorted(tokens(key, stop) & tokens(other, stop) or
                        tokens(key, stop) & tokens((taxonomy.get(other) or {}).get("name") or "", stop))
        made += _open_proposal(
            conn, "merge", f"seller:{key}->{other}", pattern_id("seller", key), pattern_id("seller", other),
            f"Merge '{key}' into '{other}': the tags share the words {', '.join(shared)}.",
            {"family": "seller", "from": key, "into": other, "similarity": round(score, 2), "shared": shared},
            n=p["n_calls"])
    return made


def _unhelpful(rule: dict, shown: int, unhelpful: int) -> bool:
    return shown > 0 and unhelpful >= float(rule["unhelpful_share"]) * shown - 1e-9


def _since_decision(conn, trigger: str) -> tuple[int, int] | None:
    """(shown, unhelpful) among the counted live nudges shown AFTER the user last decided a weight proposal
    for this trigger; None when there was no decision. Lowering a weight and then judging it on the
    nudges that led to the lowering would propose the same thing for ever."""
    last = conn.execute("SELECT resolved_at FROM learning_proposals WHERE kind='trigger_weight' AND subject=? "
                        "AND status!='open' ORDER BY id DESC LIMIT 1", (f"trigger:{trigger}",)).fetchone()
    if last is None or not last["resolved_at"]:
        return None
    decided = common.ts(last["resolved_at"])
    rows = [o for o in conn.execute("SELECT * FROM pattern_observations WHERE family='nudge_trigger' AND key=?",
                                    (trigger,))
            if countable(o) and decided and common.ts(o["observed_at"]) and common.ts(o["observed_at"]) > decided]
    return len(rows), sum(1 for o in rows if o["outcome_value"] in ("ignored", "dismissed"))


def resolved_weights() -> dict:
    """The trigger weights actually in force: tracked config < the methodology's live_weights < the
    user's own live_coach.yaml, exactly as the live coach resolves them (coach/settings.load). The
    learned boost of feedback.live_focus is left out on purpose: it is not a setting, and a proposal
    the user accepts is written to their overlay, which outranks the boost anyway."""
    from ..coach import settings as coach_settings
    try:
        return dict((coach_settings.load().get("scoring") or {}).get("weights") or {})
    except RuntimeError:                               # no live_coach.yaml at all: nothing to propose against
        return {}


def _propose_trigger_weights(conn) -> int:
    rule = cfg("nudge_trigger")
    weights = resolved_weights()
    made = 0
    for p in conn.execute("SELECT * FROM learned_patterns WHERE owner_id=? AND family='nudge_trigger' AND merged_into IS NULL "
                          "AND (user_state IS NULL OR user_state='confirmed')", (_owner(conn),)).fetchall():
        s = json.loads(p["stats"] or "{}")
        shown, unhelpful = s.get("shown", 0), s.get("ignored", 0) + s.get("dismissed", 0)
        current = weights.get(p["key"])
        if shown < int(rule["propose_min_shown"]) or current is None or not _unhelpful(rule, shown, unhelpful):
            continue
        since = _since_decision(conn, p["key"])
        if since is not None and not _unhelpful(rule, *since):
            continue
        proposed = max(float(rule["weight_floor"]), round(float(current) * float(rule["lower_factor"]), 2))
        if proposed >= float(current):
            continue
        made += _open_proposal(
            conn, "trigger_weight", f"trigger:{p['key']}", p["id"], None,
            f"Lower the weight of the '{p['key'].replace('_', ' ')}' nudge from {current} to {proposed}: shown {shown} "
            f"times live, followed {s.get('followed', 0)}, ignored {s.get('ignored', 0)}, dismissed {s.get('dismissed', 0)}.",
            {"trigger": p["key"], "config": f"live_coach.scoring.weights.{p['key']}", "current_weight": current,
             "proposed_weight": proposed, "shown": shown, "followed": s.get("followed", 0),
             "ignored": s.get("ignored", 0), "dismissed": s.get("dismissed", 0)}, n=shown)
    return made


def open_proposals(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM learning_proposals WHERE status='open' ORDER BY id"):
        d = dict(r)
        d["payload"] = json.loads(r["payload"] or "{}")
        out.append(d)
    return out


def _apply_trigger_weight(payload: dict) -> str:
    """Write the accepted weight into the user's settings overlay (live_coach.yaml under config.user_dir()),
    keeping whatever else they set there. The tracked config is never edited."""
    overlay = config.load_user("live_coach")
    overlay.setdefault("scoring", {}).setdefault("weights", {})[payload["trigger"]] = payload["proposed_weight"]
    config.save_user("live_coach", overlay)
    return "config"


def _evidence_n(conn, row) -> int:
    """The evidence behind a proposal's subject right now: what decided_n freezes at the decision."""
    p = conn.execute("SELECT n_calls, stats FROM learned_patterns WHERE id=?", (row["pattern_id"],)).fetchone()
    if p is None:
        return int(json.loads(row["payload"] or "{}").get("n") or 0)
    if row["kind"] == "trigger_weight":
        return int(json.loads(p["stats"] or "{}").get("shown") or 0)
    return int(p["n_calls"] or 0)


def resolve_proposal(conn, proposal_id: int, accept: bool, by: str = "user:ui") -> dict:
    row = conn.execute("SELECT * FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise KeyError(proposal_id)
    if row["status"] != "open":
        raise ActionRefused("That proposal was already decided.")
    applied = None
    decided_n = _evidence_n(conn, row)                 # before a merge folds the counts away
    if accept:
        payload = json.loads(row["payload"] or "{}")
        if row["kind"] == "merge":
            merge_into(conn, row["pattern_id"], row["target_id"], by=by, recompute_after=False)
            applied = "merge"
        else:
            applied = _apply_trigger_weight(payload)
    conn.execute("UPDATE learning_proposals SET status=?, applied=?, resolved_at=?, decided_n=? WHERE id=?",
                 ("accepted" if accept else "dismissed", applied, now(), decided_n, proposal_id))
    engine._emit(conn, by, "learning_proposal_" + ("accepted" if accept else "dismissed"), node_id=row["pattern_id"],
                 after={"proposal_id": proposal_id, "kind": row["kind"], "applied": applied})
    recompute(conn)
    return {"status": "accepted" if accept else "dismissed", "applied": applied}


# ---- user actions (user_input through the gate) ---------------------------------------------------------

def _ensure_row(conn, pid: str):
    row = conn.execute("SELECT * FROM learned_patterns WHERE id=?", (pid,)).fetchone()
    if row is not None:
        return row
    parsed = parse_pattern_id(pid)                     # a taxonomy tag nobody has been seen doing yet
    if parsed and parsed[0] == "seller" and parsed[1] == _owner(conn) and parsed[2] in seller_memory.taxonomy():
        family, owner, key = parsed
        conn.execute("INSERT INTO learned_patterns(id,family,key,scope,summary,seller_id,updated_at,owner_id) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (pid, "seller", key, SCOPE, _summary("seller", key, {}, seller_memory.taxonomy()),
                      seller_id(conn), now(), owner))
        return conn.execute("SELECT * FROM learned_patterns WHERE id=?", (pid,)).fetchone()
    raise KeyError(pid)


def set_user_state(conn, pid: str, state: str | None, by: str = "user:ui") -> dict:
    """Confirm / Wrong / Retire, or None to undo. "Wrong" excludes the pattern's observations."""
    if state is not None and state not in USER_STATES:
        raise ActionRefused(f"state must be one of {', '.join(USER_STATES)}")
    row = _ensure_row(conn, pid)
    gate.propose(conn, gate.Proposed(pid, "learned_patterns", "user_state", state, "user_input", dict(USER)), actor=by)
    if state == "wrong":
        conn.execute("UPDATE pattern_observations SET excluded=1 WHERE family=? AND key=?", (row["family"], row["key"]))
    elif row["user_state"] == "wrong":
        conn.execute("UPDATE pattern_observations SET excluded=0 WHERE family=? AND key=?", (row["family"], row["key"]))
    recompute(conn)
    if row["family"] == "seller":
        # Review 3: the legacy seller_patterns table (the prep brief's fallback, the Today card, the coach
        # report) mirrors Wrong / Retire as status 'retired', and an undo brings the counted status back.
        seller_memory.recompute(conn)
    return dict(conn.execute("SELECT * FROM learned_patterns WHERE id=?", (pid,)).fetchone())


def set_prompt_use(conn, pid: str, use: bool, by: str = "user:ui") -> dict:
    """The user's "Do not use in prompts" (use=False) and its undo. It says nothing about whether the
    pattern is true, so counting, labels and the page are untouched; only for_prompt reads it."""
    ensure_columns(conn)
    _ensure_row(conn, pid)
    gate.propose(conn, gate.Proposed(pid, "learned_patterns", "no_prompt", 0 if use else 1, "user_input", dict(USER)),
                 actor=by)
    return dict(conn.execute("SELECT * FROM learned_patterns WHERE id=?", (pid,)).fetchone())


def merge_into(conn, pid: str, target_id: str | None, by: str = "user:ui", recompute_after: bool = True) -> None:
    """Fold one pattern into another of the same family (None undoes it). Only the user does this."""
    row = _ensure_row(conn, pid)
    if target_id is not None:
        target = _ensure_row(conn, target_id)
        if target["id"] == row["id"] or target["family"] != row["family"]:
            raise ActionRefused("A pattern can only be merged into a different pattern of the same family.")
        if target["user_state"] in ("wrong", "retired"):
            raise ActionRefused("That pattern was retired; merge into a live one.")
        hop, seen = target, {row["id"]}
        while hop is not None and hop["merged_into"]:
            if hop["merged_into"] in seen:
                raise ActionRefused("That would merge the two patterns into each other.")
            seen.add(hop["id"])
            hop = conn.execute("SELECT * FROM learned_patterns WHERE id=?", (hop["merged_into"],)).fetchone()
    gate.propose(conn, gate.Proposed(pid, "learned_patterns", "merged_into", target_id, "user_input", dict(USER)),
                 actor=by)
    if recompute_after:
        recompute(conn)


# ---- reads -------------------------------------------------------------------------------------------------

def series(conn, key: str | None = None) -> list[dict]:
    """Numeric per-call series (talk share, questions asked, slots filled): the values and n, no judgement."""
    out = {}
    for o in conn.execute("SELECT o.key, o.call_id, o.value, o.source_is_replay, o.observed_at, c.title FROM "
                          "pattern_observations o LEFT JOIN calls c ON c.node_id=o.call_id WHERE o.family='seller_series' "
                          "AND o.excluded=0 ORDER BY o.observed_at, o.id"):
        if key and o["key"] != key:
            continue
        out.setdefault(o["key"], []).append({"call_id": o["call_id"], "title": o["title"], "at": o["observed_at"],
                                             "value": o["value"], "from_replay": bool(o["source_is_replay"])})
    return [{"key": k, "n": len(points), "points": points} for k, points in sorted(out.items())]


def for_prompt(conn, target: str, deal_id: str | None = None, limit: int = 3) -> list[dict]:
    """What phase F2 may put in a prompt for `target` (prep | live_coach | email_drafter | nudge_drafter |
    strategist): active patterns only, never one the user marked wrong or retired or flagged "do not use
    in prompts", never a merged one, at most `limit`. With a deal_id, persona/objection patterns are
    limited to buckets seen on that deal.

    Returns [{"id", "family", "key", "summary", "label", "n_calls", "n_deals", "n_obs"}], the user's
    confirmed patterns first, then established before emerging, then by calls and recency.
    """
    families = (cfg("prompt_targets") or {}).get(target) or []
    if not families or limit <= 0:
        return []
    ensure_columns(conn)                               # a store made by phase F1 has no no_prompt column yet
    rows = conn.execute(
        "SELECT * FROM learned_patterns WHERE owner_id=? AND status='active' AND merged_into IS NULL AND no_prompt=0 "
        "AND family IN (%s) AND (user_state IS NULL OR user_state='confirmed') AND scope IN ('global', ?)"
        % ",".join("?" * len(families)), (_owner(conn), *families, f"deal:{deal_id}")).fetchall()
    if deal_id:
        on_deal = {(r["family"], r["key"]) for r in conn.execute(
            "SELECT DISTINCT family, key FROM pattern_observations WHERE deal_id=? AND excluded=0", (deal_id,))}
        rows = [r for r in rows if r["family"] not in OBSERVATION_ONLY or (r["family"], r["key"]) in on_deal]
    order = {"established": 0, "emerging": 1, "": 2}
    rows.sort(key=lambda r: r["id"])                                    # stable sorts: last key first
    rows.sort(key=lambda r: r["last_seen"] or "", reverse=True)
    rows.sort(key=lambda r: (r["user_state"] != "confirmed", order.get(r["label"], 2), -r["n_calls"], -r["n_obs"]))
    return [{"id": r["id"], "family": r["family"], "key": r["key"], "summary": r["summary"], "label": r["label"],
             "n_calls": r["n_calls"], "n_deals": r["n_deals"], "n_obs": r["n_obs"]} for r in rows[:limit]]


def _evidence_link(o) -> dict:
    ev = json.loads(o["evidence"] or "{}")
    if o["nudge_id"] is not None and o["call_id"]:
        return {"href": f"/coach/live/{o['call_id']}", "text": o["call_title"] or "live nudges"}
    if o["email_id"] is not None:
        href = f"/nudges/{o['email_id']}" if o["email_kind"] == "nudge" else (
            f"/calls/{o['email_call_id']}#email" if o["email_call_id"] else None)
        return {"href": href, "text": f"email {o['email_id']}"}
    if o["call_id"]:
        turns = ev.get("turns") or []
        return {"href": f"/calls/{o['call_id']}" + (f"#t{turns[0]}" if turns else ""), "text": o["call_title"] or "call"}
    if o["deal_id"]:
        return {"href": f"/deals/{o['deal_id']}", "text": o["deal_name"] or "deal"}
    return {"href": None, "text": o["subject"]}


def beliefs(conn, evidence_limit: int = 5) -> list[dict]:
    """Everything the page shows, grouped by family: candidate, active and dormant patterns first, then
    the ones retired, merged or marked wrong (so a verdict can be undone)."""
    owner = _owner(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM learned_patterns WHERE owner_id=? ORDER BY family, id", (owner,))]
    by_id = {r["id"]: r for r in rows}
    merges = _merge_targets(by_id)
    folded: dict[str, list] = {}
    for src, dst in merges.items():
        folded.setdefault(pattern_id(*dst, owner=owner), []).append(src[1])
    out = []
    for family in PATTERN_FAMILIES:
        live, closed = [], []
        for r in (x for x in rows if x["family"] == family):
            r["stats"] = json.loads(r["stats"] or "{}")
            r["includes"] = sorted(folded.get(r["id"], []))
            keys = [r["key"], *r["includes"]]
            marks = ",".join("?" * len(keys))
            obs = conn.execute(
                "SELECT o.*, c.title AS call_title, d.name AS deal_name, m.kind AS email_kind, m.call_id AS email_call_id "
                "FROM pattern_observations o LEFT JOIN calls c ON c.node_id=o.call_id LEFT JOIN deals d ON d.node_id=o.deal_id "
                f"LEFT JOIN emails m ON m.id=o.email_id WHERE o.family=? AND o.key IN ({marks}) "
                "ORDER BY o.observed_at DESC, o.id DESC", (family, *keys)).fetchall()
            links, seen_links = [], set()
            for o in obs:
                link = _evidence_link(o) if countable(o) else None
                if link and (link["href"], link["text"]) not in seen_links:
                    seen_links.add((link["href"], link["text"]))
                    links.append(link)
            r["evidence"] = links[:evidence_limit]
            r["not_counted"] = {"low_confidence": sum(1 for o in obs if (o["confidence"] or "") == "low"),
                                "replay": sum(1 for o in obs if o["source_is_replay"]),
                                "marked_wrong": sum(1 for o in obs if o["excluded"])}
            r["merged_into_key"] = by_id[r["merged_into"]]["key"] if r["merged_into"] in by_id else None
            (closed if r["status"] == "retired" else live).append(r)
        rank = {"active": 0, "candidate": 1, "dormant": 2}
        live.sort(key=lambda r: (rank.get(r["status"], 3), -r["n_calls"], -r["n_obs"], r["key"]))
        out.append({"family": family, "title": FAMILY_LABELS[family], "patterns": live, "closed": closed,
                    "merge_targets": [{"id": r["id"], "key": r["key"]} for r in live]})
    return out
