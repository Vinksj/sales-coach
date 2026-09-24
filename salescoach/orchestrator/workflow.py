"""The post-call state machine.

  captured -> final_transcribed -> diarized -> quality_done -> summarized
  -> analyzed -> actions_extracted -> loops_reconciled -> email_drafted
  -> awaiting_review   (then, from the review UI: reviewed -> email_sent|email_skipped -> done)

A transcript import whose speaker labels do not say which one is the seller waits in
`needs_speaker` (HOLD_STATES) until the seller answers on the call page. Nothing here runs a
held call, not even a re-run from a named step: channels decide whose commitment a quote is.

Each step runs, commits, and advances wf_state, so a crash resumes at the
next step. An agent step is skipped when an artifact already exists for the
same input hash and prompt version, unless forced. Agents only produce
structured output; this module validates it and decides what is stored.
"""
import difflib
import hashlib
import json
import logging
from datetime import date

from .. import config, identity, repo
from ..agents.actions import ActionAgent
from ..agents.base import record_items
from ..agents.call_analyst import CallAnalystAgent
from ..agents.email_drafter import EmailAgent
from ..agents.quality import QualityAgent
from ..agents.summary import SummaryAgent
from ..execution import cadence, policy
from ..memory import gate, patterns
from ..schemas.common import CONFIDENCE_RANK, conf_min
from ..schemas.events import Event
from ..store.stores import engine, now
from ..store import db
from ..validators import evidence, recipients, voice_lint
from . import bus, context

ACTOR = "workflow"
log = logging.getLogger("salescoach.workflow")
QUALITY_ORDER = {None: 0, "ok": 0, "partial": 1, "garbled": 2}
HOLD_STATES = ("needs_speaker",)      # waiting for the seller, not for the worker


def has_audio(call) -> bool:
    """A call that arrived as text (a paste, a recorder's transcript) has no audio to transcribe or
    diarize. What it is called (`source`) says nothing about that; the missing audio_dir does."""
    return bool(call["audio_dir"])


class PipelineError(RuntimeError):
    pass


# ---- steps -----------------------------------------------------------------

def step_transcribe(conn, call_id, force=False):
    call = repo.get_call(conn, call_id)
    if not has_audio(call):
        return
    from ..speech import final
    final.transcribe_call(conn, call_id)


def step_diarize(conn, call_id, force=False):
    call = repo.get_call(conn, call_id)
    if not has_audio(call):
        return
    from ..speech import diarize
    diarize.diarize_call(conn, call_id)


class Step:
    """Result of an agent step: the output, the run that produced it, and
    whether it was reused from an artifact with identical inputs."""

    def __init__(self, output, run_id, cached, prompt_version, input_sha):
        self.output, self.run_id, self.cached = output, run_id, cached
        self.prompt_version, self.input_sha = prompt_version, input_sha

    def save(self, conn, call_id, kind, obj):
        context.save_artifact(conn, call_id, kind, obj, self.run_id, self.input_sha, self.prompt_version)


def _agent_step(conn, call_id, agent, kind, ctx, force) -> Step:
    system, prompt = agent.system_prompt(ctx), agent.build_prompt(ctx)
    prompt_version = hashlib.sha256(system.encode()).hexdigest()[:12]
    input_sha = hashlib.sha256((system + "\n" + prompt).encode()).hexdigest()
    existing, row = context.artifact(conn, call_id, kind)
    if not force and row is not None and row["input_sha"] == input_sha:
        return Step(existing, row["run_id"], True, prompt_version, input_sha)
    output, run_id, prompt_version, input_sha = agent.run(conn, ctx)
    return Step(output, run_id, False, prompt_version, input_sha)


def step_quality(conn, call_id, force=False):
    ctx = context.load(conn, call_id)
    if not ctx["turns"]:
        raise PipelineError("no final transcript turns")
    step = _agent_step(conn, call_id, QualityAgent(), "quality", ctx, force)
    report = step.output if step.cached else step.output.model_dump()
    if not step.cached:
        step.save(conn, call_id, "quality", report)
    known = {t["idx"] for t in ctx["turns"]}
    current = {t["idx"]: t.get("quality") for t in ctx["turns"]}
    for flag in report["flagged_turns"]:
        idx = flag["idx"]
        if idx not in known:
            continue
        if QUALITY_ORDER[flag["quality"]] > QUALITY_ORDER[current.get(idx)]:
            conn.execute("UPDATE turns SET quality=?, quality_note=? WHERE call_id=? AND tier='final' AND idx=?",
                         (flag["quality"], flag["note"], call_id, idx))
    conn.execute("UPDATE turns SET quality='ok' WHERE call_id=? AND tier='final' AND quality IS NULL", (call_id,))
    repo.update_call(conn, call_id, quality_score=report["overall_score"])


def step_summary(conn, call_id, force=False):
    ctx = context.load(conn, call_id)
    ctx["quality"], _ = context.artifact(conn, call_id, "quality")
    step = _agent_step(conn, call_id, SummaryAgent(), "summary", ctx, force)
    if not step.cached:
        run_id = step.run_id
        summary = step.output.model_dump()
        turns = {t["idx"]: t for t in ctx["turns"]}
        rejected = []
        for section in ("key_discussions", "decisions", "commitments", "open_questions", "risks"):
            for item in summary[section]:
                bad = [i for i in item["evidence_turns"] if i not in turns]
                if bad or not item["evidence_turns"]:
                    item["unsupported"] = True
                    rejected.append({"section": section, "text": item["text"], "why": "cites no valid turn"})
        step.save(conn, call_id, "summary", summary)
        record_items(conn, run_id, created=[f"summary for {call_id}"], rejected=rejected)


def step_analysis(conn, call_id, force=False):
    ctx = context.load(conn, call_id)
    ctx["quality"], _ = context.artifact(conn, call_id, "quality")
    ctx["summary"], _ = context.artifact(conn, call_id, "summary")
    step = _agent_step(conn, call_id, CallAnalystAgent(), "analysis", ctx, force)
    if step.cached:
        return
    out, run_id = step.output, step.run_id
    analysis = out.model_dump()
    turns = {t["idx"]: t for t in ctx["turns"]}
    deal_id = ctx["call"]["deal_id"]
    conn.execute("DELETE FROM claims WHERE call_id=?", (call_id,))
    conn.execute("DELETE FROM assessments WHERE call_id=?", (call_id,))
    created, rejected = [], []
    for claim in analysis["claims"]:
        conf, notes, _ = evidence.judge(claim["confidence"], claim["evidence_quote"], claim["evidence_turns"],
                                        turns, requires_quote=claim["kind"] != "assumption")
        if claim["kind"] == "assumption":
            conf = conf_min(conf, "low")          # unconfirmed by definition
        claim["validated_confidence"], claim["validation_notes"] = conf, notes
        if notes:
            rejected.append({"claim": claim["statement"], "notes": notes, "confidence": f"{claim['confidence']}->{conf}"})
        conn.execute(
            "INSERT INTO claims(call_id,deal_id,agent,subject,statement,kind,confidence,evidence_turns,evidence_quote,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (call_id, deal_id, "call_analyst", claim["subject"], claim["statement"], claim["kind"], conf,
             json.dumps(claim["evidence_turns"]), claim["evidence_quote"], now()))
        created.append(f"claim:{claim['subject']}")
    subject_prefix = f"deal:{deal_id}" if deal_id else f"call:{call_id}"
    for a in analysis["assessments"]:
        conn.execute(
            "INSERT INTO assessments(subject,call_id,agent,stance,confidence,rationale,evidence_turns,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"{subject_prefix}/{a['subject'].split('.', 1)[1]}", call_id, "call_analyst", a["stance"],
             a["confidence"], a["rationale"], json.dumps(a["evidence_turns"]), now()))
    validated_obs = []
    for ob in out.observations:
        conf, notes, _ = evidence.judge(ob.confidence, ob.evidence_quote, ob.evidence_turns, turns)
        ob.confidence = conf
        validated_obs.append(ob)
    patterns.record_observations(conn, call_id, validated_obs)
    step.save(conn, call_id, "analysis", analysis)
    patterns.recompute(conn)
    record_items(conn, run_id, created=created, rejected=rejected)
    bus.publish(conn, Event(type="POST_CALL_ANALYSIS_COMPLETE", entity_id=call_id,
                            dedupe_key=f"ANALYSIS:{call_id}:{run_id}"))


def step_actions(conn, call_id, force=False):
    ctx = context.load(conn, call_id)
    ctx["summary"], _ = context.artifact(conn, call_id, "summary")
    ctx["analysis"], _ = context.artifact(conn, call_id, "analysis")
    step = _agent_step(conn, call_id, ActionAgent(), "actions", ctx, force)
    if step.cached:
        return
    run_id = step.run_id
    result = step.output.model_dump()
    turns = {t["idx"]: t for t in ctx["turns"]}
    open_ids = {l["node_id"] for l in ctx["open_loops"]}
    world_ids = {w["id"] for w in ctx["world_commitments"]}
    rejected = []
    for a in result["actions"]:
        if a.get("world_commitment_id") and a["world_commitment_id"] not in world_ids:
            rejected.append({"action": a["description"], "notes": [f"unknown tracker id {a['world_commitment_id']}"]})
            a["world_commitment_id"] = None
        conf, notes, check = evidence.judge(a["confidence"], a["evidence_quote"], a["evidence_turns"], turns,
                                            owner=a["owner"], requires_quote=a["source"] != "recommended",
                                            source=a["source"])
        if a["source"] == "recommended":
            conf = conf_min(conf, "medium")       # the system's idea; only the seller's confirmation makes it firm
        a["validated_confidence"], a["validation_notes"] = conf, notes
        a["evidence_found"], a["evidence_how"] = check.found, check.how
        if notes:
            rejected.append({"action": a["description"], "notes": notes})
    for u in result["loop_updates"]:
        if u["loop_id"] not in open_ids:
            u["rejected"] = "not an open loop of this deal"
            rejected.append({"loop_update": u["loop_id"], "notes": [u["rejected"]]})
            continue
        conf, notes, check = evidence.judge(u["confidence"], u["evidence_quote"], u["evidence_turns"], turns)
        u["validated_confidence"], u["validation_notes"], u["evidence_found"] = conf, notes, check.found
        u["evidence_how"] = check.how
    step.save(conn, call_id, "actions", result)
    record_items(conn, run_id, created=[a["description"] for a in result["actions"]], rejected=rejected)


def _loop_id(call_id, description, owner):
    norm = evidence.normalize(description)
    return "loop-" + hashlib.sha1(f"{call_id}|{owner}|{norm}".encode()).hexdigest()[:12]


def _similar(a, b) -> float:
    return difflib.SequenceMatcher(None, evidence.normalize(a), evidence.normalize(b)).ratio()


def step_reconcile(conn, call_id, force=False):
    ctx = context.load(conn, call_id)
    result, _ = context.artifact(conn, call_id, "actions")
    if result is None:
        raise PipelineError("no actions artifact")
    call = ctx["call"]
    call_day = date.fromisoformat(ctx["call_date"])
    new_ids = []
    for i, a in enumerate(result["actions"]):
        conf = a.get("validated_confidence", a["confidence"])
        dup = next((l for l in ctx["open_loops"]
                    if l["owner"] == a["owner"] and _similar(l["description"], a["description"]) >= 0.85), None)
        if dup:
            engine._emit(conn, ACTOR, "loop_reaffirmed", node_id=dup["node_id"],
                         after={"call_id": call_id, "quote": a["evidence_quote"]}, source_id=call_id)
            conn.execute("UPDATE loops SET last_activity_at=? WHERE node_id=?", (now(), dup["node_id"]))
            a["merged_into"] = dup["node_id"]
            new_ids.append(dup["node_id"])
            continue
        lid = _loop_id(call_id, a["description"], a["owner"])
        new_ids.append(lid)
        if engine.node_exists(conn, lid):
            continue
        engine.add_node(conn, ACTOR, id=lid, type="loop", kind=a["type"], title=a["description"][:200],
                        status="full", confidence=CONFIDENCE_RANK[conf] / 3, source_id=call_id)
        loop = {"type": a["type"], "priority": a["priority"], "due_date": a["due_date"],
                "due_date_confidence": a["due_date_confidence"]}
        # world_commitment_id adopts a promise Jarvis already tracks: linked, never mirrored twice.
        conn.execute(
            "INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,owner_name,source,confidence,"
            "evidence_quote,evidence_turns,priority,due_date,due_date_confidence,status,follow_up_required,"
            "follow_up_strategy,next_check_at,dependencies,review_state,world_commitment_id,world_link,created_at,"
            "last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,'proposed',?,?,?,?)",
            (lid, call["deal_id"], call_id, a["type"], a["description"], a["owner"], a.get("owner_name"),
             a["source"], conf, a["evidence_quote"], json.dumps(a["evidence_turns"]), a["priority"],
             a["due_date"], a["due_date_confidence"], int(a["follow_up_required"]), a["follow_up_strategy"],
             cadence.next_check(loop, call_day).isoformat(), json.dumps(a["dependencies"]),
             a.get("world_commitment_id"), "adopted" if a.get("world_commitment_id") else None, now(), now()))
        engine.set_source(conn, ACTOR, lid, uri=f"{call_id}#turns={','.join(map(str, a['evidence_turns']))}",
                          capture="call", lineage=[call_id])
        prov = {"kind": "call", "ref": call_id, "turns": a["evidence_turns"]}
        gate.set_initial(conn, lid, "loops", "status", "open", conf, prov)
        if a["due_date"]:
            gate.set_initial(conn, lid, "loops", "due_date", a["due_date"], conf, prov)
    # Re-run safety: proposed loops from an earlier run of this call that the new run no longer produces.
    for row in conn.execute("SELECT node_id FROM loops WHERE call_id=? AND review_state='proposed' AND status='open'",
                            (call_id,)).fetchall():
        if row["node_id"] not in new_ids:
            # A link the model guessed goes with the loop; only our own mirror stays for the bridge to drop.
            conn.execute("UPDATE loops SET status='cancelled', review_state='rejected', closed_at=?, "
                         "world_commitment_id=CASE WHEN world_link='mirror' THEN world_commitment_id END, "
                         "world_link=CASE WHEN world_link='mirror' THEN world_link END WHERE node_id=?",
                         (now(), row["node_id"]))
            engine._emit(conn, ACTOR, "loop_dropped_on_rerun", node_id=row["node_id"], source_id=call_id)
    for u in result["loop_updates"]:
        if u.get("rejected"):
            continue
        status = {"done": "done", "cancelled": "cancelled", "superseded": "superseded", "waiting": "waiting"}[
            u["proposed_status"]]
        conf = u.get("validated_confidence", u["confidence"])
        prov = {"kind": "call", "ref": call_id, "turns": u["evidence_turns"], "reason": u["reason"]}
        proposal = gate.Proposed(u["loop_id"], "loops", "status", status, conf, prov)
        # Only the speaker's exact words may change an existing loop; a loose match waits for review.
        strong = u.get("evidence_how") == "exact" and CONFIDENCE_RANK[conf] >= CONFIDENCE_RANK["high"]
        if strong:
            outcome = gate.propose(conn, proposal, actor=ACTOR)
            if outcome == "applied" and status in ("done", "cancelled", "superseded"):
                conn.execute("UPDATE loops SET closed_at=?, last_activity_at=? WHERE node_id=?",
                             (now(), now(), u["loop_id"]))
                if status == "superseded" and u.get("superseded_by_action") is not None \
                        and u["superseded_by_action"] < len(new_ids):
                    conn.execute("UPDATE loops SET superseded_by=? WHERE node_id=?",
                                 (new_ids[u["superseded_by_action"]], u["loop_id"]))
            u["outcome"] = outcome
        else:
            gate.park(conn, proposal)
            u["outcome"] = "needs_review"
    context.save_artifact(conn, call_id, "reconcile", {"loop_ids": new_ids, "loop_updates": result["loop_updates"]})


def _supported(summary):
    """Drop summary items the validator flagged as citing no real turn; they must not reach an email."""
    if not summary:
        return summary
    clean = dict(summary)
    for section in ("key_discussions", "decisions", "commitments", "open_questions", "risks"):
        clean[section] = [item for item in summary.get(section, []) if not item.get("unsupported")]
    # Summary commitments carry a turn index but no checked quote. The drafter gets commitments only
    # through email_actions, whose quotes the evidence validator has verified, so an invented
    # commitment with a real-looking turn number cannot reach the email through the summary.
    clean["commitments"] = []
    return clean


def step_email(conn, call_id, force=False):
    ctx = context.load(conn, call_id)
    call = ctx["call"]
    skip = None
    max_age = (config.load("policy").get("email") or {}).get("max_call_age_hours", 48)
    if call["history"]:
        skip = "imported history: no follow-up is drafted for backfilled calls"
    elif policy.call_age_hours(call) > max_age and not force:
        skip = f"call ended more than {max_age} hours ago; a follow-up now would be stale"
    allowed = recipients.allowed_recipients(conn, call_id, call["deal_id"])
    if not skip and not allowed:
        skip = "no buyer-side participant with an email address; add participants, then redraft"
    if not skip and conn.execute("SELECT 1 FROM emails WHERE call_id=? AND status IN ('sent','sending','saved_to_gmail')",
                                 (call_id,)).fetchone():
        skip = "a follow-up for this call was already sent or saved to Gmail; no second draft"
    if skip:
        context.save_artifact(conn, call_id, "email", {"skipped": skip})
        return
    loops = conn.execute("SELECT * FROM loops WHERE call_id=? AND review_state!='rejected' AND status!='cancelled'",
                         (call_id,)).fetchall()
    from ..validators.gates import can_feed_external
    ctx["email_actions"] = [dict(l) for l in loops
                            if can_feed_external(l["confidence"], l["review_state"], l["source"])]
    ctx["summary"] = _supported(context.artifact(conn, call_id, "summary")[0])
    ctx["allowed_recipients"] = allowed
    # Only this deal's edits: another customer's final email must never be in this prompt.
    ctx["recent_edits"] = [dict(r) for r in conn.execute(
        "SELECT ee.draft_body, ee.final_body FROM email_edits ee JOIN emails e ON e.id=ee.email_id "
        "WHERE e.deal_id IS ? ORDER BY ee.id DESC LIMIT 3", (call["deal_id"],))]
    # Style RULES learned from edits on every deal (phase F2): fixed sentences, never an email's text.
    from ..learning import feedback
    ctx["learned_voice"] = feedback.select(conn, "email_drafter", call["deal_id"])
    out, run_id, _, _ = EmailAgent().run(conn, ctx)
    # The agent call took a while and committed; re-read what the seller may have done meanwhile.
    if conn.execute("SELECT 1 FROM emails WHERE call_id=? AND status IN ('sent','sending','saved_to_gmail')",
                    (call_id,)).fetchone():
        context.save_artifact(conn, call_id, "email",
                              {"skipped": "an earlier draft was sent or saved to Gmail while this one was being "
                                          "written; the new draft was discarded"})
        return
    state_now = (repo.get_call(conn, call_id) or {})["wf_state"]
    if state_now in USER_CLOSED:
        context.save_artifact(conn, call_id, "email",
                              {"skipped": f"the call was closed ({state_now}) while the draft was being written; "
                                          "the new draft was discarded"})
        return
    body = voice_lint.autofix(out.body)
    subject = voice_lint.autofix(out.subject)
    issues = voice_lint.lint(subject, body)
    # Retire every unsent draft, failed ones included, so only the new version can be sent.
    previous = conn.execute("SELECT id FROM emails WHERE call_id=? AND status IN ('drafted','failed')",
                            (call_id,)).fetchall()
    version = (conn.execute("SELECT MAX(version) FROM emails WHERE call_id=?", (call_id,)).fetchone()[0] or 0) + 1
    for p in previous:
        conn.execute("UPDATE emails SET status='rejected', error='superseded by a new draft', updated_at=? WHERE id=?",
                     (now(), p["id"]))
    violations = recipients.check(out.to, out.cc, allowed)
    lint_rows = [i.as_dict() for i in issues] + [{"kind": "recipient", "detail": v, "severity": "block"}
                                                 for v in violations]
    to = [a for a in out.to if a.lower() in allowed]
    cc = [a for a in out.cc if a.lower() in allowed]
    cur = conn.execute(
        "INSERT INTO emails(call_id,deal_id,kind,to_addrs,cc_addrs,subject,body,draft_body,rationale,version,status,"
        "lint,run_id,created_at,updated_at) VALUES (?,?,'followup',?,?,?,?,?,?,?,'drafted',?,?,?,?)",
        (call_id, call["deal_id"], json.dumps(to), json.dumps(cc), subject, body, body, out.rationale, version,
         json.dumps(lint_rows), run_id, now(), now()))
    email_id = db.insert_id(cur)
    context.save_artifact(conn, call_id, "email", {"email_id": email_id, "version": version}, run_id)
    bus.publish(conn, Event(type="EMAIL_DRAFT_CREATED", entity_id=call_id,
                            dedupe_key=f"EMAIL_DRAFT:{call_id}:{version}", payload={"email_id": email_id}))


PIPELINE = [
    ("final_transcribed", step_transcribe),
    ("diarized", step_diarize),
    ("quality_done", step_quality),
    ("summarized", step_summary),
    ("analyzed", step_analysis),
    ("actions_extracted", step_actions),
    ("loops_reconciled", step_reconcile),
    ("email_drafted", step_email),
]
STEP_NAMES = [name for name, _ in PIPELINE]
STEP_LABELS = {
    "captured": "Audio captured", "final_transcribed": "Final transcript", "diarized": "Speakers separated",
    "quality_done": "Quality check", "summarized": "Summary", "analyzed": "Analysis",
    "actions_extracted": "Actions extracted", "loops_reconciled": "Loops reconciled", "email_drafted": "Email draft",
}
POST_PIPELINE = ("awaiting_review", "reviewed", "email_sent", "email_skipped", "done")
USER_CLOSED = ("email_sent", "email_skipped", "done")     # only the seller's actions put a call here
HANDLERS: dict[str, list] = {}
_plugins_ready = False


def register_step(name, fn, after, label=None):
    """Plugins insert a pipeline step after an existing one. STEP_NAMES is updated
    in place so every module holding a reference sees the new order."""
    if name in STEP_NAMES:
        return
    PIPELINE.insert(STEP_NAMES.index(after) + 1, (name, fn))
    STEP_NAMES[:] = [n for n, _ in PIPELINE]
    STEP_LABELS[name] = label or name.replace("_", " ").capitalize()


def register_handler(event_type, fn):
    handlers = HANDLERS.setdefault(event_type, [])
    if fn not in handlers:
        handlers.append(fn)


def ensure_plugins():
    """Load plugins on first use, not at import, so plugin modules may import this one."""
    global _plugins_ready
    if not _plugins_ready:
        _plugins_ready = True
        import sys
        from .. import plugins
        plugins.apply_workflow(sys.modules[__name__])


def _last_email_id(conn, call_id):
    return conn.execute("SELECT MAX(id) FROM emails WHERE call_id=?", (call_id,)).fetchone()[0]


def _claim_for_jarvis(conn):
    """Tell jarvis-evening about this call as soon as its loops exist, not hours later."""
    try:
        from ..integrations import jarvis_bridge
        if jarvis_bridge.available():
            jarvis_bridge.claim_calls(conn)
    except Exception:
        log.exception("publishing Jarvis call claims failed")


def run_pipeline(conn, call_id, from_step=None, force=False, until=None):
    ensure_plugins()
    call = repo.get_call(conn, call_id)
    if call is None:
        raise KeyError(call_id)
    if call["wf_state"] in HOLD_STATES:
        raise PipelineError("the call is waiting for you to say which speaker you are; answer on the call page")
    if from_step:
        if from_step not in STEP_NAMES:
            raise ValueError(f"unknown step {from_step}; one of {STEP_NAMES}")
        start = STEP_NAMES.index(from_step)
    elif call["wf_state"] == "live":
        raise PipelineError("call is still live")
    elif call["wf_state"] == "captured":
        start = 0
    elif call["wf_state"] in STEP_NAMES:
        start = STEP_NAMES.index(call["wf_state"]) + 1
    elif call["wf_state"] in POST_PIPELINE:
        return call["wf_state"]            # already past the pipeline
    else:
        # e.g. a state named after a plugin step that is no longer installed: never assume "finished"
        raise PipelineError(f"call is in state {call['wf_state']!r}, which is not a pipeline step; "
                            "re-run it from a named step")
    original = call["wf_state"]
    last_email = _last_email_id(conn, call_id)
    last_written = original
    for name, fn in PIPELINE[start:]:
        try:
            fn(conn, call_id, force=force)
            # While a slow step ran, the seller may have sent the email or closed the call from the UI.
            # His action wins: the step's writes are dropped and the call keeps his state.
            state_now = repo.get_call(conn, call_id)["wf_state"]
            if state_now in USER_CLOSED and state_now != last_written:
                conn.rollback()
                log.info("call %s was closed (%s) during %s; keeping that state", call_id, state_now, name)
                return state_now
            repo.set_call_state(conn, call_id, name, actor=ACTOR)
            last_written = name
            conn.commit()
            if name == "loops_reconciled":
                _claim_for_jarvis(conn)
        except Exception as exc:
            conn.rollback()
            repo.update_call(conn, call_id, wf_error=f"{name}: {type(exc).__name__}: {exc}"[:2000], actor=ACTOR)
            conn.commit()
            raise
        if until and name == until:
            return name
    final = "awaiting_review"
    if original in ("reviewed", "email_sent", "email_skipped", "done") and _last_email_id(conn, call_id) == last_email:
        final = original          # re-running a finished call must not drag it back into review
    state_now = repo.get_call(conn, call_id)["wf_state"]
    if state_now in USER_CLOSED and state_now != last_written:
        final = state_now         # closed by the seller between the last step and here
    repo.set_call_state(conn, call_id, final, actor=ACTOR)
    _resolve_stale_failures(conn, call_id)
    conn.commit()
    return final


def _resolve_stale_failures(conn, call_id):
    """The pipeline just succeeded, so earlier failed attempts for this call are history, not open
    failures. (2026-09-12: two backfill events that died on a quota fallback kept the dashboard
    saying "2 failed" after the calls were re-processed by hand.)"""
    conn.execute("UPDATE wf_events SET status='done', error=COALESCE(error,'') || ' | resolved: the call was "
                 "processed successfully later', updated_at=? WHERE entity_id=? AND status='failed' "
                 "AND type IN ('CALL_ENDED','PROCESS_CALL')", (now(), call_id))
    # A CALL_ENDED still queued for this call (a synchronous `salescoach process` ran before any worker)
    # would only re-enter the pipeline to find it finished; retire it so status does not show it as work.
    conn.execute("UPDATE wf_events SET status='done', error='resolved: the call was processed synchronously', "
                 "updated_at=? WHERE entity_id=? AND status='pending' AND type='CALL_ENDED'", (now(), call_id))


class UnknownOwner(RuntimeError):
    """Cloud mode, and the event names nothing whose owner is known: it cannot run as anybody."""


def owner_of_event(conn, event: Event) -> str:
    """Whose work an event is. An entity id `user:<id>` says so directly (FOLLOW_UP_RUN, the calendar
    refresh, a coach-report request); a call, deal or loop id resolves through nodes.owner_id; the
    payload may carry owner_id. Local mode falls back to the local user; cloud mode refuses."""
    entity = event.entity_id or ""
    if entity.startswith("user:"):
        return entity[len("user:"):]
    if entity:
        owner = repo.owner_of(conn, entity)
        if owner:
            return owner
    owner = (event.payload or {}).get("owner_id")
    if owner:
        return str(owner)
    if not identity.cloud():
        return identity.LOCAL_USER
    raise UnknownOwner(f"{event.type} {event.entity_id!r}: no owner (nodes.owner_id, user:<id> or payload.owner_id)")


def handle(conn, event: Event):
    """Route one persisted workflow event, as the user whose work it is (identity.as_user, service
    mode): every handler and every seller.profile() read inside runs for that owner. Events with no
    handler are records only."""
    ensure_plugins()
    with conn.as_system():                          # the owner is looked up before anyone is bound
        owner = owner_of_event(conn, event)
    with identity.as_user(conn, owner, mode=identity.SERVICE):
        if event.type in ("CALL_ENDED", "PROCESS_CALL"):
            run_pipeline(conn, event.entity_id, from_step=event.payload.get("from"),
                         force=bool(event.payload.get("force")))
        elif event.type == "REVIEW_COMPLETED":
            from . import review
            review.after_review(conn, event.entity_id)
        for fn in HANDLERS.get(event.type, []):
            fn(conn, event)
