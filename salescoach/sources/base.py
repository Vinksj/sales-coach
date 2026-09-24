"""The one import path every transcript takes, whatever recorder it came from.

  NormalizedTranscript     what a parser or an adapter hands over
  import_normalized(...)   dedupe on source_ref -> keep the raw payload -> people -> speakers ->
                           turns -> CALL_ENDED (once)
  resolve_speaker(...)     the seller's answer to "which speaker are you?" for a held import

WHO IS THE SELLER. Channels are not decoration: validators/evidence.py only accepts the seller's
explicit commitment when the quoted words are on channel `me`, and talk share is computed per
channel. A recorder labels speakers with real names, so a label becomes `me` when it equals
(ignoring case, spaces and punctuation) the seller's name, an alias, the local part of one of
their addresses, "Me", a label the seller picked before (remembered per user in user_speaker_labels), the
explicit me_label of this import, or the name of a participant whose address is the seller's.
Everyone else is `them`, with the label kept as the speaker cluster.

If a transcript has two or more speakers and NONE of them is recognisably the seller, the
import does not guess. The call is created in wf_state `needs_speaker`, no CALL_ENDED is
published (so no worker touches it, and workflow.run_pipeline refuses it), and the call page
asks. A transcript with one label or with no seller-side ambiguity behaves as it always has.

Everything in a transcript is untrusted text. It is parsed and stored, never followed.
"""
import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .. import config, identity, repo, seller
from ..orchestrator import bus
from ..schemas.events import Event
from ..store.stores import engine, now

NEEDS_SPEAKER = "needs_speaker"
NOT_PRESENT = "__none__"          # me_label: "none of these speakers is me"
MAX_TURNS = 20000
ACTOR = "salescoach"


@dataclass
class NormalizedTranscript:
    source_kind: str                                   # calls.source: paste|granola|upload|folder|webhook|fireflies|...
    source_ref: Optional[str]                          # '<kind>:<id>', UNIQUE: the same meeting imports once
    title: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    participants: list = field(default_factory=list)   # [{name, email}]
    turns: list = field(default_factory=list)          # [{speaker_label, text, t_start?, t_end?, channel?}]
    summary: Optional[str] = None
    raw: Any = None                                    # the payload as received, kept under data/inbox/<kind>/


@dataclass
class ImportResult:
    call_id: str
    created: bool
    needs_speaker: bool = False
    labels: list = field(default_factory=list)         # the distinct speaker labels, in order of appearance

    def __str__(self):                                 # so a caller that only wants the id can print it
        return self.call_id


def from_parsed(parsed, kind: str, raw: Any = None, source_ref: Optional[str] = None,
                title: Optional[str] = None, trusted: bool = True) -> NormalizedTranscript:
    """A parsers.Parsed as a NormalizedTranscript for adapter `kind`.

    source_ref: the meeting's id at its recorder when the payload carries one (so the same meeting
    arriving twice is one call), else the caller's.

    trusted=False is for payloads nobody at the keyboard chose (the webhook, the watched folder):
      * an id the payload supplies is ALWAYS kept under `ext:`, so a payload that calls itself
        "fireflies" with id X cannot occupy `fireflies:X` and keep the real meeting out;
      * a turn's `channel` is dropped, so the payload cannot declare words to be the seller's (the
        channel is what the explicit-commitment check trusts); the label decides, as for any export.
    The seller's own upload and the API adapters (whose ids come from the service) stay trusted."""
    turns = list(parsed.turns)
    if not trusted:
        turns = [{k: v for k, v in t.items() if k != "channel"} for t in turns]
    if parsed.ext_id:
        owner = parsed.ext_source or "generic"
        native = trusted and owner in ("fireflies", "fathom")
        source_ref = f"{owner}:{parsed.ext_id}" if native else f"ext:{owner}:{parsed.ext_id}"
    return NormalizedTranscript(source_kind=kind, source_ref=source_ref, title=title or parsed.title or "",
                                started_at=parsed.started_at, ended_at=parsed.ended_at,
                                participants=list(parsed.participants), turns=turns,
                                summary=parsed.summary, raw=raw)


def content_ref(prefix: str, data, owned: bool = False) -> str:
    """'<prefix>:<sha>' of the content. owned=True (a user's own upload) puts the acting user in the key
    (repo.user_source_ref), so two users uploading one file get two calls; a watched folder's or a
    webhook's file is org-level and stays as it was until Phase 4."""
    if isinstance(data, str):
        data = data.encode()
    digest = hashlib.sha256(data).hexdigest()[:32]
    return repo.user_source_ref(prefix, digest) if owned else f"{prefix}:{digest}"


# ---------------------------------------------------------------------------------------- speakers

def norm_label(label) -> str:
    return re.sub(r"[\W_]+", "", str(label or "").casefold())


def remembered_me_labels(conn=None) -> list:
    """The labels the ACTING user said were theirs (user_speaker_labels; was sources.yaml me_labels,
    which named one seller for the whole install). Without a connection: none."""
    if conn is None:
        return []
    from .. import users
    return users.remembered_labels(conn)


def me_keys(me_label: Optional[str] = None, participants=(), conn=None) -> set:
    """Every normalised label that means the seller."""
    names = ["Me", *seller.aliases(), *remembered_me_labels(conn)]
    if me_label and me_label != NOT_PRESENT:
        names.append(me_label)
    own = {e.lower() for e in seller.emails()}
    names += [p.get("name") for p in participants or () if (p.get("email") or "").lower() in own]
    return {k for k in (norm_label(n) for n in names) if k}


def distinct_labels(turns) -> list:
    seen, out = set(), []
    for t in turns:
        label = " ".join(str(t.get("speaker_label") or "").split())
        if norm_label(label) not in seen:
            seen.add(norm_label(label))
            out.append(label)
    return out


def cluster_for(label: str) -> str:
    return "them_1" if norm_label(label) in ("them", "") else label


def map_speakers(nt: NormalizedTranscript, me_label: Optional[str] = None, conn=None) -> tuple:
    """-> ([(channel, cluster, turn)], hold). A turn that already carries a channel (the recorder
    separated mic from speaker audio, as Granola does) keeps it."""
    labels = distinct_labels(nt.turns)
    if me_label and me_label != NOT_PRESENT and norm_label(me_label) not in {norm_label(l) for l in labels}:
        raise ValueError(f"no speaker is labelled {me_label!r}; the labels are: {', '.join(labels) or '(none)'}")
    keys = me_keys(me_label, nt.participants, conn)
    mapped, any_me = [], False
    for t in nt.turns:
        label = " ".join(str(t.get("speaker_label") or "").split())
        channel = t.get("channel") if t.get("channel") in ("me", "them") else None
        if channel is None:
            channel = "me" if norm_label(label) in keys else "them"
        any_me = any_me or channel == "me"
        mapped.append((channel, "me" if channel == "me" else cluster_for(label), t))
    explicit = all(t.get("channel") in ("me", "them") for t in nt.turns)
    hold = (len(labels) >= 2 and not any_me and not explicit and me_label != NOT_PRESENT)
    return mapped, hold


def transcript_sha(rows) -> str:
    """rows: (channel, text). The same digest paste.import_text has always stored."""
    return hashlib.sha256("\n".join(f"{c}|{t}" for c, t in rows).encode()).hexdigest()


# ------------------------------------------------------------------------------------------ people

def me_addresses(conn=None) -> set:
    """The ACTING user's own addresses (their profile, plus their person row): that participant is ME,
    not a buyer. A colleague's address is not here: on this user's call a colleague is a speaker."""
    found = {e.lower() for e in seller.emails()}
    if conn is not None:
        found |= {r["email"].lower() for r in conn.execute(
            "SELECT email FROM people WHERE user_id=? AND email IS NOT NULL", (identity.actor_of(conn).user_id,))}
    return found


def deal_domains(conn, deal_id) -> set:
    """The email domains of the deal's account: who is provably on the buyer's side of THIS deal."""
    row = conn.execute("SELECT a.domains FROM deals d JOIN accounts a ON a.node_id=d.account_id WHERE d.node_id=?",
                       (deal_id,)).fetchone() if deal_id else None
    try:
        found = json.loads(row["domains"] or "[]") if row else []
    except ValueError:
        found = []
    return {str(d).lower().strip() for d in found if d}


def resolve_people(conn, participants, source_ref, deal_id=None, link: str = "all") -> list:
    """[{name,email}] -> people ids, in order. Found by email, else created with the account their
    domain belongs to. The seller is never created as a buyer: their address, or (with no address)
    their name, resolves to the is_me row. Someone with neither an address nor the seller's name is
    skipped: a name alone cannot be matched again next time.

    link: who is also put on the DEAL (deal_people = who every later email on the deal may address).
      "all"      everyone on the transcript: the seller imported it by hand and chose the deal;
      "account"  only addresses at the deal's own account domains. For imports nobody watched (webhook,
                 folder, the pollers): the attendee list is the payload's word, and one address at the
                 account's domain must not carry a stranger onto the deal. The others stay participants
                 of this call only."""
    own, keys, out = me_addresses(conn), me_keys(conn=conn), []
    domains = deal_domains(conn, deal_id) if (deal_id and link == "account") else None
    for p in participants or ():
        email = (p.get("email") or "").strip().lower()
        if (email and email in own) or (not email and norm_label(p.get("name")) in keys):
            out.append(repo.ensure_me(conn))
            continue
        if not email:
            continue
        pid = repo.find_person_by_email(conn, email)
        if pid is None:
            domain = email.split("@", 1)[1] if "@" in email else None
            account = repo.find_account_by_domain(conn, domain) if domain else None
            pid = repo.create_person(conn, (p.get("name") or "").strip() or email.split("@", 1)[0], email=email,
                                     account_id=account, source_id=source_ref)
        out.append(pid)
        if deal_id and (domains is None or email.rsplit("@", 1)[-1] in domains):
            repo.link_deal_person(conn, deal_id, pid)
    return list(dict.fromkeys(out))


def guess_deal(conn, nt: NormalizedTranscript) -> Optional[str]:
    from .. import onboard
    return onboard.deal_for_emails(conn, [p.get("email") for p in nt.participants if p.get("email")])


# --------------------------------------------------------------------------------------------- raw

def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")[:120] or "payload"


def save_raw(nt: NormalizedTranscript, conn=None):
    """What arrived, before anything interpreted it. Locally a file, data/inbox/<kind>/<id>.json|.txt|.bin;
    in cloud mode (a container with no volume to keep) a raw_payloads row on `conn`, in the caller's
    transaction, so a refused import rolls it back with everything else. Nothing in the pipeline reads
    a payload back; `salescoach payloads export` does, for support.
    Returns the path when this call CREATED the file (so a failed import can take it back), else None."""
    if nt.raw is None:
        return None
    if conn is not None and identity.cloud():
        _save_raw_row(conn, nt)
        return None
    folder = config.DATA_DIR / "inbox" / _slug(nt.source_kind)
    folder.mkdir(parents=True, exist_ok=True)
    ref = nt.source_ref or f"{nt.source_kind}:{hashlib.sha256(repr(nt.raw).encode()).hexdigest()[:32]}"
    stem = _slug(ref.split(":", 1)[1] if ref.startswith(nt.source_kind + ":") else ref)
    if isinstance(nt.raw, bytes):
        path, write = folder / f"{stem}.bin", lambda p: p.write_bytes(nt.raw)
    elif isinstance(nt.raw, str):
        path, write = folder / f"{stem}.txt", lambda p: p.write_text(nt.raw)
    else:
        path, write = folder / f"{stem}.json", lambda p: p.write_text(json.dumps(nt.raw, ensure_ascii=False,
                                                                                default=str))
    existed = path.exists()
    write(path)
    return None if existed else path


def encode_raw(raw) -> tuple[str, str]:
    """(encoding, body text) for a payload: bytes as base64, a str as is, anything else as JSON."""
    if isinstance(raw, bytes):
        return "base64", base64.b64encode(raw).decode("ascii")
    if isinstance(raw, str):
        return "text", raw
    return "json", json.dumps(raw, ensure_ascii=False, default=str)


def decode_raw(encoding: str, body: str):
    if encoding == "base64":
        return base64.b64decode(body.encode("ascii"))
    if encoding == "json":
        return json.loads(body)
    return body


def _save_raw_row(conn, nt: NormalizedTranscript) -> None:
    """One row per distinct payload per owner (UNIQUE(owner_id, sha256)): the same transcript delivered
    twice is stored once. owner_id is the acting user's by column default."""
    encoding, body = encode_raw(nt.raw)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO raw_payloads(source_kind,source_ref,encoding,body,sha256,created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(owner_id, sha256) DO NOTHING",
        (nt.source_kind, nt.source_ref, encoding, body, digest, now()))


_EXT = {"json": ".json", "text": ".txt", "base64": ".bin"}


def export_payloads(conn, out_dir=None, since: Optional[str] = None, as_json: bool = False, out=None) -> int:
    """`salescoach payloads export`: every raw_payloads row the connection can see (its owner's, or all
    on a local install), oldest first. With `out_dir`, each lands as <out_dir>/<kind>/<id><ext> (bytes
    decoded); otherwise, or with as_json, one JSON line per row on `out` (body included). Returns the count."""
    from pathlib import Path
    out = out or sys.stdout
    if since:
        rows = conn.execute("SELECT * FROM raw_payloads WHERE created_at >= ? ORDER BY id", (since,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM raw_payloads ORDER BY id").fetchall()
    written = 0
    for row in rows:
        if out_dir:
            folder = Path(out_dir) / _slug(row["source_kind"])
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{row['id']}{_EXT.get(row['encoding'], '.txt')}"
            if row["encoding"] == "base64":
                target.write_bytes(decode_raw("base64", row["body"]))
            else:
                target.write_text(row["body"])
        if as_json or not out_dir:
            record = {k: row[k] for k in ("id", "owner_id", "source_kind", "source_ref", "encoding", "sha256", "created_at")}
            record["body"] = row["body"]
            print(json.dumps(record, ensure_ascii=False), file=out)
        written += 1
    if out_dir:
        print(f"exported {written} payload{'' if written == 1 else 's'} to {out_dir}", file=sys.stderr)
    return written


# ------------------------------------------------------------------------------------------ import

def _ended_at(nt: NormalizedTranscript) -> Optional[str]:
    if nt.ended_at:
        return nt.ended_at
    ends = [t.get("t_end") or t.get("t_start") for t in nt.turns if (t.get("t_end") or t.get("t_start"))]
    if nt.started_at and ends:
        try:
            return (datetime.fromisoformat(nt.started_at) + timedelta(seconds=max(ends))).isoformat(timespec="seconds")
        except (ValueError, TypeError, OverflowError):
            pass
    return nt.started_at


def existing_call(conn, source_ref) -> Optional[str]:
    if not source_ref:
        return None
    row = conn.execute("SELECT node_id FROM calls WHERE source_ref=?", (source_ref,)).fetchone()
    return row["node_id"] if row else None


def import_normalized(conn, nt: NormalizedTranscript, deal_id=None, history=False, lang_mode="auto",
                      me_label=None, participant_ids=(), add_me=False, link: str = "all") -> ImportResult:
    """Create the call for one transcript, or return the one already made from it. Commits.

    history=True is for explicit backfills of old meetings: analysed and remembered, never given a
    drafted follow-up, not claimed for Jarvis. A recorder's new meeting is NOT history.
    participant_ids are people the caller already resolved (the import form's picker); add_me puts
    the seller on the call even when the recorder's attendee list does not name them.
    link: resolve_people's rule for who is also put on the deal ("all" by hand, "account" unattended).

    Anything that goes wrong after the raw payload was written takes the payload back: a refused
    import leaves no file behind (and the caller rolls the transaction back)."""
    found = existing_call(conn, nt.source_ref)
    if found:
        held = repo.get_call(conn, found)["wf_state"] == NEEDS_SPEAKER
        if held and me_label:
            # The same transcript again, now with the answer: that IS the answer to the held call.
            match = next((q["label"] for q in speaker_question(conn, found)
                          if norm_label(q["label"]) == norm_label(me_label)), None)
            if me_label != NOT_PRESENT and match is None:
                raise ValueError(f"no speaker is labelled {me_label!r} on the held call")
            resolve_speaker(conn, found, match or NOT_PRESENT)
            held = False
        return ImportResult(found, False, held, distinct_labels(nt.turns))
    turns = [t for t in nt.turns if str(t.get("text") or "").strip()]
    if not turns:
        raise ValueError("no speaker turns found; expected lines like 'Me: ...' and 'Them: ...'")
    if len(turns) > MAX_TURNS:
        raise ValueError(f"transcript has {len(turns)} turns; the limit is {MAX_TURNS}")
    nt.turns = turns
    labels = distinct_labels(turns)
    if not any(norm_label(l) for l in labels) and not all(t.get("channel") in ("me", "them") for t in turns):
        raise ValueError("this transcript does not say who is speaking. Export it with speaker names: without "
                         "them the coach cannot tell your commitments from the buyer's.")
    mapped, hold = map_speakers(nt, me_label, conn)
    seller.require_configured()
    raw_path = save_raw(nt, conn)
    try:
        return _create(conn, nt, mapped, hold, labels, deal_id, history, lang_mode, participant_ids, add_me, link)
    except BaseException:
        if raw_path is not None:
            raw_path.unlink(missing_ok=True)
        raise


def _create(conn, nt, mapped, hold, labels, deal_id, history, lang_mode, participant_ids, add_me, link) -> ImportResult:
    people = [*participant_ids, *resolve_people(conn, nt.participants, nt.source_ref, deal_id, link=link)]
    if add_me:
        people.insert(0, repo.ensure_me(conn))
    people = list(dict.fromkeys(people))
    call_id = repo.create_call(conn, source=nt.source_kind, title=nt.title or "Imported call", deal_id=deal_id,
                               lang_mode=lang_mode, started_at=nt.started_at, source_ref=nt.source_ref,
                               wf_state=NEEDS_SPEAKER if hold else "diarized", history=history)
    by_name = _people_by_label(conn, people)
    for p in nt.participants:                          # a label the recorder itself tied to an address
        pid = repo.find_person_by_email(conn, p.get("email")) if p.get("email") else None
        if pid and pid in people and norm_label(p.get("name")) and norm_label(p.get("name")) not in me_keys(None, nt.participants, conn):
            by_name.setdefault(norm_label(p.get("name")), pid)
    for idx, (channel, cluster, t) in enumerate(mapped):
        person_id = by_name.get(norm_label(cluster)) if channel == "them" else None
        conn.execute(
            "INSERT INTO turns(call_id,tier,idx,channel,speaker_cluster,person_id,t_start,t_end,text) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (call_id, "final", idx, channel, cluster, person_id, t.get("t_start"), t.get("t_end"),
             str(t["text"]).strip()))
    repo.update_call(conn, call_id, transcript_sha=transcript_sha((c, str(t["text"]).strip()) for c, _, t in mapped),
                     ended_at=_ended_at(nt))
    for person_id in people:
        repo.add_participant(conn, call_id, person_id)
    if nt.summary:
        from ..orchestrator import context
        context.save_artifact(conn, call_id, f"{_slug(nt.source_kind)}_summary",
                              {"source": nt.source_kind, "trust": "third_party_inference", "text": nt.summary,
                               "captured_at": now()})
    if not hold:
        bus.publish(conn, Event(type="CALL_ENDED", entity_id=call_id, dedupe_key=f"CALL_ENDED:{call_id}"),
                    priority=bus.PRIORITY_BACKFILL if history else bus.PRIORITY_NORMAL)
    conn.commit()
    return ImportResult(call_id, True, hold, labels)


def _people_by_label(conn, person_ids) -> dict:
    """Normalised full name -> person id, for the buyer-side people on this call. A name two
    participants share maps to nobody."""
    names, clash = {}, set()
    for pid in person_ids:
        row = conn.execute("SELECT name, is_me FROM people WHERE node_id=?", (pid,)).fetchone()
        if row is None or row["is_me"]:
            continue
        key = norm_label(row["name"])
        if key in names and names[key] != pid:
            clash.add(key)
        names[key] = pid
    return {k: v for k, v in names.items() if k and k not in clash}


# ----------------------------------------------------------------------- "which speaker are you?"

def speaker_question(conn, call_id) -> list:
    """The labels of a held call, each with its turn count and first words, for the call page."""
    out = {}
    for t in conn.execute("SELECT speaker_cluster, text FROM turns WHERE call_id=? AND tier='final' ORDER BY idx",
                          (call_id,)):
        label = t["speaker_cluster"] or "Unknown"
        entry = out.setdefault(label, {"label": label, "count": 0, "sample": t["text"][:160]})
        entry["count"] += 1
    return list(out.values())


def resolve_speaker(conn, call_id, label: Optional[str], remember: bool = True, actor: str = "user") -> int:
    """The seller says which label is theirs (label=None or NOT_PRESENT: none of them). The turns'
    channels are fixed, the label is remembered for the next import from the same recorder, and
    only now does the pipeline start. Returns the number of turns moved to `me`. Commits."""
    call = repo.get_call(conn, call_id)
    if call is None:
        raise KeyError(call_id)
    if call["wf_state"] != NEEDS_SPEAKER:
        raise ValueError("this call is not waiting for a speaker answer")
    moved = 0
    if label and label != NOT_PRESENT:
        known = {q["label"] for q in speaker_question(conn, call_id)}
        if label not in known:
            raise ValueError(f"no speaker is labelled {label!r} on this call")
        moved = conn.execute(
            "UPDATE turns SET channel='me', speaker_cluster='me', person_id=NULL WHERE call_id=? AND tier='final' "
            "AND speaker_cluster=?", (call_id, label)).rowcount
        rows = conn.execute("SELECT channel, text FROM turns WHERE call_id=? AND tier='final' ORDER BY idx",
                            (call_id,)).fetchall()
        repo.update_call(conn, call_id, transcript_sha=transcript_sha((r["channel"], r["text"]) for r in rows),
                         actor=actor)
        repo.add_participant(conn, call_id, repo.ensure_me(conn))
    engine._emit(conn, actor, "speaker_resolved", node_id=call_id, after={"me_label": label or None, "turns": moved})
    repo.set_call_state(conn, call_id, "diarized", actor=actor)
    history = bool(repo.get_call(conn, call_id)["history"])
    bus.publish(conn, Event(type="CALL_ENDED", entity_id=call_id, dedupe_key=f"CALL_ENDED:{call_id}"),
                priority=bus.PRIORITY_BACKFILL if history else bus.PRIORITY_NORMAL)
    if remember and label and label != NOT_PRESENT:
        remember_me_label(label, conn)
    conn.commit()
    return moved


def remember_me_label(label: str, conn=None) -> None:
    """Recorders label the seller the same way every time ("Priya S."). Asking once is enough. Remembered
    for the ACTING user (user_speaker_labels); without a connection nothing is remembered."""
    if conn is None:
        return
    if norm_label(label) in me_keys(conn=conn) or re.fullmatch(r"(?i)speaker[ _-]?\w{1,3}|them|unknown.*", label.strip()):
        return                                         # "Speaker 2" is a different person in the next meeting
    from .. import users
    users.remember_label(conn, None, label)
