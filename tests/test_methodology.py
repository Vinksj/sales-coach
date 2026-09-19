"""Phase C: the sales methodology is data.

MEDDPICC stays the default and behaves exactly as it did (tests/test_p3_*.py pass unchanged: that is
the proof; here the YAML is held to the code-level fallback). Every other built-in validates, and
BANT, a conversation framework and a custom methodology run end to end on the fake model: schema,
rows in the `meddpicc` table, caps, rule-made risks, deal page, prep brief, prompts. Switching hides
the old rows, keeps them out of the strategist's prompt, queues a run per open deal, and switching
back restores them with the seller's edits still protected.

No test here reaches a model: every pipeline run uses the fake_llm fixture.
"""
import copy
import json

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import write_seller
from salescoach import config, repo
from salescoach.agents.call_analyst import CallAnalystAgent
from salescoach.coach import settings as coach_settings
from salescoach.coach.slow_pass import LiveCoachAgent
from salescoach.intel import history, methodology, prep, schemas, strategist, tables
from salescoach.intel.strategist import StrategistAgent
from salescoach.orchestrator import worker
from salescoach.schemas import analysis as analysis_schemas
from salescoach.sources import paste
from salescoach.web.app import create_app
from test_core_pipeline import CALL1, CALL2, _script, _setup, analysis
from test_p3_strategy import ev, ids_in, intel_env, reconcile_all, run_call, script_intel, strategy  # noqa: F401

ORIGIN = {"origin": "http://127.0.0.1:8140"}
BUILTINS = ("meddpicc", "meddic", "bant", "spiced", "spin", "challenger", "sandler", "command_of_the_message")
CFO_QUOTE = "our CFO will want to see the savings split by plant"
PRIYA = {"name": "Priya Nair", "emails": ["priya@acmecloud.com"], "company": "Acme Cloud", "role": "Account executive",
         "offering": "HR software that replaces spreadsheets for companies with hourly staff",
         "icp": "US mid-market companies", "buyer_titles": "HR directors and CFOs", "languages": ["en"],
         "timezone": "America/New_York", "signature": "Best,\nPriya"}


def element(key, status="unknown", quote=None, call=None, turns=(1,), conf="high", question=""):
    return {"element": key, "status": status, "what_we_know": f"about {key}" if status != "unknown" else "",
            "gap": f"{key} gap", "next_question": question,
            "evidence": [ev(call, list(turns), quote)] if quote else [], "confidence": conf}


def bant_elements(call):
    """authority is verified; need claims 'known' on a quote nobody said; timeline is partial; budget is left out."""
    return [element("authority", "known", CFO_QUOTE, call, question="Who signs after the CFO?"),
            element("need", "known", "this is our top priority this quarter", call),
            element("timeline", "partial", "probably first week of next month", call, turns=(3,), conf="medium",
                    question="What has to be live by then?")]


def script_under(fake, lens, elements_for):
    """The whole pipeline on the fake, with the analyst's gap tagged `lens` and the strategist answering with
    `elements_for(call_id)`."""
    _script(fake, {"actions": [], "loop_updates": [], "notes": ""})      # a second call adds no loops
    a = analysis()
    a["gaps"][0]["lens"] = lens
    fake.responses["CallAnalysis"] = lambda s, p: copy.deepcopy(a)

    def answer(system, prompt):
        call, arjun = ids_in(prompt)
        out = strategy(call, arjun)
        out["meddpicc"] = elements_for(call)
        return out
    fake.responses["DealStrategy"] = answer
    fake.responses["AssessmentReconciliation"] = reconcile_all


def run_pipeline(db, fake, lens, elements_for, deal=None, people=None, text=CALL1, title="NWP weekly"):
    if deal is None:
        deal, people = _setup(db)
    script_under(fake, lens, elements_for)
    call = paste.import_text(db, text, title, deal_id=deal, participants=people)
    worker.drain(db)
    row = repo.get_call(db, call)
    assert row["wf_state"] == "awaiting_review", row["wf_error"]
    return deal, call, people


def calls_of(fake, schema):
    return [c for c in fake.calls if c["schema"] == schema]


@pytest.fixture
def client(db):
    return TestClient(create_app(start_worker=False, live_factory=None))


# =====================================================================================
# the library
# =====================================================================================

def test_every_builtin_validates_and_is_listed():
    raw = yaml.safe_load((config.ROOT / "config" / "methodologies.yaml").read_text())
    assert tuple(raw["methodologies"]) == BUILTINS and raw["default"] == "meddpicc"
    for key, d in raw["methodologies"].items():
        assert methodology.validate_definition({**d, "key": key}) == [], key
    listed = methodology.available()
    assert [m["key"] for m in listed] == list(BUILTINS)
    assert [m["key"] for m in listed if m["active"]] == ["meddpicc"]
    for m in listed:
        assert m["builtin"] and m["name"] and m["kind"] in methodology.KINDS and len(m["description"]) > 120, m["key"]
        assert 2 <= len(m["elements"]) <= 12
        assert m["health"]["min_known"] and m["health"]["rubric"].count("\n") == 4 and m["health"]["bottleneck_hint"]
        assert any(e["critical"] for e in m["elements"]), m["key"]
        for e in m["elements"]:
            assert e["label"] and e["known_when"] and e["partial_when"] and len(e["questions"]) >= 2, (m["key"], e["key"])
            assert ("buyer" in e["known_when"].lower() or "person" in e["known_when"].lower()
                    or "stakeholder" in e["known_when"].lower()), (m["key"], e["key"])   # a buyer-side fact
            assert e["critical"] == (e["cap_when_not_known"] is not None)
    kinds = {m["key"]: m["kind"] for m in listed}
    assert [k for k, v in kinds.items() if v == "conversation"] == ["spin", "challenger", "sandler",
                                                                    "command_of_the_message"]
    # the same concept has the same key everywhere, so what is known carries over a switch
    by_key = {m["key"]: {e["key"] for e in m["elements"]} for m in listed}
    assert {"champion", "economic_buyer", "decision_process", "identify_pain", "metrics"} <= by_key["meddic"] & by_key["meddpicc"]
    assert "identify_pain" in by_key["spiced"] & by_key["spin"] & by_key["sandler"]
    assert "budget" in by_key["bant"] & by_key["sandler"] and "champion" in by_key["challenger"]
    # the analyst and the strategist never hear of MEDDPICC from another framework's texts
    for m in listed:
        if m["key"] != "meddpicc":
            assert "MEDDPICC" not in m["coaching"]["secondary_lenses"]
            assert "MEDDPICC" not in json.dumps({k: m[k] for k in ("elements", "health", "coaching")})


def test_meddpicc_default_is_exactly_what_the_code_did_before():
    m = methodology.active()
    assert (m.key, m.name, m.lens, m.kind) == ("meddpicc", "MEDDPICC", "MEDDPICC", "qualification")
    assert m.keys == schemas.MEDDPICC and m.labels == schemas.MEDDPICC_LABELS and m.count == 8
    assert m.gap_order == ("economic_buyer", "decision_process", "champion", "paper_process", "metrics",
                           "identify_pain", "decision_criteria", "competition")
    eb, = m.critical
    assert (eb.key, eb.cap_when_not_known, eb.cap_rule, eb.cap_why) == (
        "economic_buyer", 60, "economic_buyer_not_confirmed", "the economic buyer is not confirmed and engaged")
    assert m.min_known == {"count": 3, "cap": 50, "rule": "meddpicc_known_below_3"}
    assert not any(e.risk_when_unknown for e in m.elements)              # no new rule-made risks for MEDDPICC
    assert m.lenses == analysis_schemas.DEFAULT_LENSES and (m.analyst, m.live, m.live_weights) == ("", "", {})
    caps = config.load("intel")["health"]["caps"]
    assert (caps["economic_buyer_not_confirmed"], caps["meddpicc_known_below_3"]) == (60, 50)

    # the YAML and the code-level fallback agree on everything behaviour depends on
    fb = methodology.fallback()
    for attr in ("key", "name", "lens", "kind", "keys", "labels", "gap_order", "min_known", "rubric", "bottleneck_hint",
                 "lenses", "analyst", "live", "live_weights"):
        assert getattr(fb, attr) == getattr(m, attr), attr
    assert [(e.key, e.critical, e.cap_when_not_known, e.cap_rule, e.cap_why, e.risk_when_unknown) for e in fb.elements] \
        == [(e.key, e.critical, e.cap_when_not_known, e.cap_rule, e.cap_why, e.risk_when_unknown) for e in m.elements]

    # the contracts are the static ones, and the prompts read as they always did
    assert schemas.strategy_model(m.keys) is schemas.DealStrategy and StrategistAgent().schema is schemas.DealStrategy
    assert CallAnalystAgent().schema is analysis_schemas.CallAnalysis
    system = StrategistAgent().system_prompt({})
    for line in ("## MEDDPICC\nGive all eight elements exactly once: metrics, economic_buyer, decision_criteria, "
                 "decision_process, paper_process, identify_pain, champion, competition.",
                 "(usually access to the economic buyer, an undefined decision process, or no urgency)",
                 "- 25 to 49: interest without access to the economic buyer or a known process.",
                 "(one buyer-side voice, no economic buyer, no dated commitment)"):
        assert line in system, line
    analyst = CallAnalystAgent().system_prompt({})
    assert "Use MEDDPICC, SPIN, Challenger and Gap Selling as lenses to find what is MISSING" in analyst
    assert "How this team sells" not in analyst and "How this team sells" not in LiveCoachAgent().system_prompt({})
    assert coach_settings.load()["scoring"]["weights"] == config.load("live_coach")["scoring"]["weights"]
    assert methodology.grid_columns(8) == 4


def test_fallback_when_yaml_or_overlay_is_missing_or_broken(seller_settings, tmp_path, monkeypatch, caplog):
    (seller_settings / "methodology.yaml").write_text("active: no_such_methodology\ncustom: {}\n")
    assert methodology.active().key == "meddpicc" and methodology.active_key() == "meddpicc"
    (seller_settings / "methodology.yaml").write_text("active: [unclosed\n")
    methodology._cache_clear()
    assert methodology.active().key == "meddpicc"
    (seller_settings / "methodology.yaml").write_text(
        "active: mine\ncustom:\n  mine: {name: Mine, elements: [{key: only_one, label: One, known_when: x}]}\n")
    methodology._cache_clear()
    assert methodology.active().key == "meddpicc" and "mine" not in [m["key"] for m in methodology.available()]

    empty = tmp_path / "no-config"
    empty.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", empty)
    (seller_settings / "methodology.yaml").unlink()
    methodology._cache_clear()
    m = methodology.active()
    assert m.keys == schemas.MEDDPICC and m.labels == schemas.MEDDPICC_LABELS
    assert [x["key"] for x in methodology.available()] == ["meddpicc"]


def test_set_active_is_stored_in_the_user_overlay(seller_settings):
    with pytest.raises(KeyError):
        methodology.set_active("nope")
    assert methodology.set_active("bant").key == "bant"
    assert yaml.safe_load((seller_settings / "methodology.yaml").read_text()) == {"active": "bant", "custom": {}}
    assert methodology.active().keys == ("budget", "authority", "need", "timeline")
    assert [m["key"] for m in methodology.available() if m["active"]] == ["bant"]
    assert not (config.ROOT / "config" / "methodology.yaml").exists()      # the tracked config is never written


# =====================================================================================
# custom methodologies
# =====================================================================================

CUSTOM = {
    "key": "value_pilot", "name": "Value Pilot", "lens": "Pilot", "kind": "qualification",
    "description": "How we qualify paid pilots.",
    "elements": [
        {"key": "sponsor", "label": "Pilot Sponsor", "known_when": "The buyer named who owns the pilot and that person joined a call.",
         "questions": ["Who owns the pilot on your side?"], "critical": True, "cap_when_not_known": 40,
         "risk_when_unknown": "weak_champion"},
        {"key": "success_measure", "label": "Success Measure", "known_when": "The buyer said which number the pilot must move."},
        {"key": "decision_process", "label": "Path to Rollout", "known_when": "The buyer described what happens after a good pilot."},
    ],
    "health": {"min_known": {"count": 2, "cap": 55}},
    "coaching": {"analyst": "Check the pilot sponsor first.", "live": "Get the sponsor named.",
                 "live_weights": {"stakeholder_gap": 0.95}},
}


def _errors(**change):
    d = copy.deepcopy(CUSTOM)
    d.update(change)
    return methodology.validate_definition(d)


def _with_element(i, **change):
    d = copy.deepcopy(CUSTOM)
    d["elements"][i].update(change)
    return methodology.validate_definition(d)


def test_custom_definition_validation_messages():
    assert methodology.validate_definition(CUSTOM) == []
    assert any("lower-case letters" in e for e in _errors(key="Value Pilot"))
    assert any("lower-case letters" in e for e in _with_element(0, key="Sponsor!"))
    assert any("cannot contain a colon" in e for e in _with_element(0, key="a:b"))
    assert any("'health' is reserved" in e for e in _with_element(1, key="health"))
    assert any("cannot start with 'risk'" in e for e in _with_element(1, key="risk_appetite"))
    assert any("'sponsor' is used twice" in e for e in _with_element(1, key="sponsor"))
    assert any("needs 2 to 12 elements; this one has 1" in e for e in _errors(elements=CUSTOM["elements"][:1]))
    assert any("needs 2 to 12 elements" in e for e in _errors(
        elements=[{"key": f"e{i}", "label": "L", "known_when": "k"} for i in range(13)]))
    assert any("label is required" in e for e in _with_element(1, label=""))
    assert any("known_when is required" in e for e in _with_element(1, known_when=None))
    assert any("whole number from 0 to 100" in e for e in _with_element(0, cap_when_not_known=140))
    assert any("a critical element needs cap_when_not_known" in e for e in _with_element(0, cap_when_not_known=None))
    assert any("only applies to a critical element" in e for e in _with_element(1, cap_when_not_known=50))
    assert any("risk_when_unknown must be one of" in e for e in _with_element(0, risk_when_unknown="bad_vibes"))
    assert any("cap_rule 'single_threaded' is reserved" in e for e in _with_element(0, cap_rule="single_threaded"))
    assert any("kind must be one of" in e for e in _errors(kind="vibes"))
    assert any("name is required" in e for e in _errors(name=""))
    assert any("unknown field 'elemnts'" in e for e in _errors(elemnts=[]))
    assert any("unknown trigger 'shout'" in e for e in _errors(coaching={"live_weights": {"shout": 0.5}}))
    assert any("number from 0 to 1" in e for e in _errors(coaching={"live_weights": {"objection": 3}}))
    assert any("min_known.count" in e for e in _errors(health={"min_known": {"count": 9, "cap": 50}}))
    assert any("cannot contain {{" in e for e in _with_element(0, known_when="The buyer told {{seller_name}}"))
    assert any("not an element of this methodology" in e for e in _errors(gap_order=["sponsor", "nope"]))
    assert methodology.validate_definition(["not", "a", "mapping"]) == ["a methodology must be a mapping of fields"]


def test_save_and_delete_custom(seller_settings):
    bad = copy.deepcopy(CUSTOM)
    bad["elements"][1]["key"] = "sponsor"
    with pytest.raises(methodology.InvalidMethodology) as exc:
        methodology.save_custom(bad)
    assert any("used twice" in e for e in exc.value.errors) and not (seller_settings / "methodology.yaml").exists()
    with pytest.raises(methodology.InvalidMethodology, match="built-in"):
        methodology.save_custom({**CUSTOM, "key": "bant"})
    with pytest.raises(methodology.InvalidMethodology, match="key is required"):
        methodology.save_custom({k: v for k, v in CUSTOM.items() if k != "key"})

    assert methodology.save_custom(CUSTOM) == "value_pilot"
    listed = {m["key"]: m for m in methodology.available()}
    mine = listed["value_pilot"]
    assert not mine["builtin"] and not mine["active"] and list(listed)[-1] == "value_pilot"
    assert [e["key"] for e in mine["elements"]] == ["sponsor", "success_measure", "decision_process"]
    assert mine["elements"][0]["cap_rule"] == "sponsor_not_known"                       # defaults are filled in
    assert mine["health"]["min_known"] == {"count": 2, "cap": 55, "rule": "value_pilot_known_below_2"}
    assert "Pilot Sponsor" in mine["health"]["rubric"] and "pilot sponsor" in mine["health"]["bottleneck_hint"]
    assert methodology.active().key == "meddpicc"                                        # saving does not switch

    methodology.set_active("value_pilot")
    with pytest.raises(ValueError, match="active methodology"):
        methodology.delete_custom("value_pilot")
    methodology.set_active("meddpicc")
    methodology.delete_custom("value_pilot")
    assert "value_pilot" not in [m["key"] for m in methodology.available()]
    with pytest.raises(KeyError):
        methodology.delete_custom("value_pilot")


# =====================================================================================
# the dynamic contracts
# =====================================================================================

def test_strategy_schema_follows_the_methodology():
    bant = methodology.get("bant")
    model = schemas.strategy_model(bant.keys)
    assert model.__name__ == "DealStrategy" and model is schemas.strategy_model(bant.keys)   # FakeProvider's key; cached
    from salescoach.providers.base import json_schema_for
    js = json_schema_for(model)
    assert js["properties"]["meddpicc"]["description"] == "All four elements, each exactly once"
    assert js["properties"]["meddpicc"]["items"]["properties"]["element"]["enum"] == list(bant.keys)
    assert list(js["properties"]) == list(json_schema_for(schemas.DealStrategy)["properties"])
    answer = strategy("call-x", "person-x")
    answer["meddpicc"] = [element(k) for k in bant.keys]
    assert [m.element for m in model.model_validate(answer).meddpicc] == list(bant.keys)
    answer["meddpicc"][0]["element"] = "economic_buyer"
    with pytest.raises(ValidationError):
        model.model_validate(answer)
    with pytest.raises(ValidationError):                       # and MEDDPICC's contract refuses BANT's
        schemas.DealStrategy.model_validate({**answer, "meddpicc": [element("budget")]})

    lenses = analysis_schemas.analysis_model(bant.lenses, tuple(bant.labels.values()))
    assert lenses.__name__ == "CallAnalysis"
    a = analysis()
    with pytest.raises(ValidationError):
        lenses.model_validate(a)                               # the fixture's gap is tagged MEDDPICC
    a["gaps"][0]["lens"] = "BANT"
    assert lenses.model_validate(a).gaps[0].lens == "BANT"
    a["gaps"][0]["lens"] = "SPIN"                              # a secondary lens
    assert lenses.model_validate(a).gaps[0].lens == "SPIN"


@pytest.mark.parametrize("key,label", [("spin", "Need-payoff"), ("challenger", "Mobilizer"), ("sandler", "Up-Front Contract")])
def test_conversation_frameworks_ask_for_buyer_confirmed_facts(key, label):
    m = methodology.get(key)
    system = StrategistAgent(m).system_prompt({})
    assert "{{" not in system and f"## {m.name}" in system and "MEDDPICC" not in system
    assert f"exactly once: {', '.join(m.keys)}." in system
    assert "every element here is a fact about the BUYER" in system
    assert "A question the seller asked, or a point the seller made, is not evidence" in system
    for e in m.elements:
        assert f"- {e.key} ({e.label}). Known when: {e.known_when}" in system
    assert label in system and m.rubric in system and m.bottleneck_hint in system
    qualification = StrategistAgent(methodology.get("bant")).system_prompt({})
    assert "fact about the BUYER" not in qualification


# =====================================================================================
# BANT end to end
# =====================================================================================

def test_bant_runs_end_to_end(db, fake_llm, client):
    methodology.set_active("bant")
    deal, people = _setup(db)
    script_under(fake_llm, "BANT", bant_elements)
    good = fake_llm.responses["DealStrategy"]

    def meddpicc_keys(system, prompt):                        # the first answer speaks the wrong methodology
        out = good(system, prompt)
        out["meddpicc"] = [element("economic_buyer")]
        return out
    fake_llm.responses["DealStrategy"] = [meddpicc_keys, good]
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    assert repo.get_call(db, call)["wf_state"] == "awaiting_review"
    runs = [r["status"] for r in db.execute("SELECT status FROM agent_runs WHERE agent='deal_strategist' ORDER BY id")]
    assert runs == ["invalid", "ok"]                           # the schema refused the MEDDPICC key; one retry
    refs = db.execute("SELECT input_refs FROM agent_runs WHERE agent='deal_strategist' ORDER BY id DESC").fetchone()[0]
    assert json.loads(refs)["methodology"] == "bant"

    # rows land in the `meddpicc` table under BANT's keys, the missing one filled in
    stored = {r["element"]: r["status"] for r in db.execute("SELECT element, status FROM meddpicc WHERE deal_id=?", (deal,))}
    assert stored == {"budget": "unknown", "authority": "known", "need": "unknown", "timeline": "partial"}
    rows = tables.meddpicc_rows(db, deal)
    assert [r["label"] for r in rows] == ["Budget", "Authority", "Need", "Timeline"]
    art = json.loads(db.execute("SELECT json FROM artifacts WHERE call_id=? AND kind='strategy'", (call,)).fetchone()[0])
    assert art["methodology"] == {"key": "bant", "name": "BANT"} and [m["element"] for m in art["meddpicc"]] == list(stored)

    # caps: BANT's critical element that is not known, and its own coverage floor; MEDDPICC's rules are gone
    h = tables.health(db, deal)
    rules = {c["rule"]: c for c in h["caps"]}
    assert set(rules) == {"single_threaded", "need_not_confirmed", "bant_known_below_2"}
    assert rules["need_not_confirmed"]["cap"] == 45 and "specific need" in rules["need_not_confirmed"]["why"]
    assert rules["bant_known_below_2"]["why"] == "only 1 of 4 BANT elements are known"
    assert (h["model_score"], h["score"], h["label"]) == (85, 45, "weak")
    # an unknown element raises its risk from the data; a known or partial one does not
    risks = {r["type"]: r for r in tables.risks(db, deal)}
    assert set(risks) == {"weak_urgency", "single_threading", "status_quo"}
    assert risks["status_quo"]["source"] == "rule" and risks["status_quo"]["severity"] == "high"
    assert "Need is still unknown" in risks["status_quo"]["description"]

    # deal page: BANT's name, labels and count, in a four-column grid; MEDDPICC's elements are not editable
    frag = client.get(f"/deals/{deal}/intel").text
    for needle in ("<h3>BANT</h3>", "1 of 4 known", 'class="mgrid mgrid-4"', "Authority", "Timeline",
                   f'action="/deals/{deal}/meddpicc/authority"', "Who signs after the CFO?"):
        assert needle in frag, needle
    assert "MEDDPICC" not in frag and "Economic Buyer" not in frag and "of 8 known" not in frag
    assert client.post(f"/deals/{deal}/meddpicc/economic_buyer", headers=ORIGIN, data={"status": "known"},
                       follow_redirects=False).status_code == 404
    r = client.post(f"/deals/{deal}/meddpicc/budget", headers=ORIGIN, follow_redirects=False,
                    data={"status": "known", "what_we_know": "2 crore approved", "gap": "", "next_question": ""})
    assert r.status_code == 303 and "Budget+saved" in r.headers["location"]
    assert "budget" in history.build(db, deal, call)["user_meddpicc"]

    # prep: BANT gaps, critical first, a blank question filled from the methodology
    fake_llm.responses["PrepWriting"] = {"objective": "Confirm the need", "objective_why": "w", "opening": "o",
                                         "close": "c", "watch_for": []}
    brief = prep.get(db, brief_id=prep.generate(db, deal, meeting_title="Next"))
    assert brief["methodology"] == {"key": "bant", "name": "BANT", "lens": "BANT"}
    assert [(g["label"], g["status"]) for g in brief["gaps"]] == [("Need", "unknown"), ("Timeline", "partial")]
    assert brief["gaps"][0]["question"] == "What made you look at this now?"
    assert "BANT GAPS AND THE QUESTION TO ASK:" in prep.render_text(brief)
    page = client.get(f"/deals/{deal}/prep").text
    assert "BANT gaps" in page and "MEDDPICC" not in page and "What has to be live by then?" in page

    from salescoach.cli import main
    import contextlib
    import io
    fake_llm.responses["DealStrategy"] = good                  # the seller's edit changed the prompt: a fresh run
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["strategy", "--deal", deal])
    assert "BANT (2 of 4 known)" in out.getvalue() and "MEDDPICC" not in out.getvalue()


def test_analyst_gaps_feed_prep_only_through_the_active_lens(db, fake_llm):
    methodology.set_active("bant")
    deal, _ = _setup(db)
    a = analysis()
    a["gaps"] = [{"lens": "BANT", "element": "Timeline", "missing": "no date", "why_it_matters": "w", "question_to_ask": "When?"},
                 {"lens": "SPIN", "element": "Implication", "missing": "m", "why_it_matters": "w", "question_to_ask": "q"}]
    assert [g["label"] for g in prep._gaps(db, deal, a)] == ["Timeline"]
    a["gaps"][0]["lens"] = "MEDDPICC"                           # an analysis made before the switch
    assert prep._gaps(db, deal, a) == []


# =====================================================================================
# switching
# =====================================================================================

def test_switch_hides_old_rows_queues_runs_and_switching_back_restores(db, fake_llm, client):
    deal, call, (arjun, me) = run_call(db, fake_llm)                          # MEDDPICC, as shipped
    repo.create_deal(db, "No calls yet")                                        # open, nothing analysed: not queued
    db.commit()
    client.post(f"/deals/{deal}/meddpicc/paper_process", headers=ORIGIN, follow_redirects=False,
                data={"status": "known", "what_we_know": "PO through procurement", "gap": "", "next_question": ""})
    assert "paper_process" in history.build(db, deal, call)["user_meddpicc"]
    before = db.execute("SELECT COUNT(*) FROM deal_health_history WHERE deal_id=?", (deal,)).fetchone()[0]

    assert methodology.switch(db, "bant") == 1
    queued = db.execute("SELECT entity_id, payload FROM wf_events WHERE type='STRATEGY_REQUESTED' AND status='pending'").fetchall()
    assert [q["entity_id"] for q in queued] == [deal]
    assert json.loads(queued[0]["payload"]) == {"reason": "methodology_switch", "methodology": "bant"}

    # the old rows are stored and out of sight; the seller's edit on them stays out of the prompt
    assert db.execute("SELECT COUNT(*) FROM meddpicc WHERE deal_id=?", (deal,)).fetchone()[0] == 8
    rows = tables.meddpicc_rows(db, deal)
    assert [r["element"] for r in rows] == ["budget", "authority", "need", "timeline"] and not any(r["status"] for r in rows)
    ctx = history.build(db, deal, call)
    assert ctx["user_meddpicc"] == {} and ctx["methodology"] == "bant"
    rendered = history.render(ctx)
    assert "PO through procurement" not in rendered and "OWN SETTINGS ON BANT AND RISKS" in rendered

    script_under(fake_llm, "BANT", bant_elements)
    assert worker.drain(db) >= 1
    assert db.execute("SELECT COUNT(*) FROM meddpicc WHERE deal_id=?", (deal,)).fetchone()[0] == 12
    assert {r["element"]: r["status"] for r in tables.meddpicc_rows(db, deal)}["authority"] == "known"
    assert {c["rule"] for c in tables.health(db, deal)["caps"]} == {"single_threaded", "need_not_confirmed",
                                                                    "bant_known_below_2"}
    prompt = calls_of(fake_llm, "DealStrategy")[-1]["prompt"]
    assert "PO through procurement" not in prompt and "Paper Process" not in prompt       # nor the previous read's elements
    assert db.execute("SELECT COUNT(*) FROM deal_health_history WHERE deal_id=?", (deal,)).fetchone()[0] == before + 1

    # and back: the MEDDPICC rows and the protection on the seller's edit are as they were
    script_intel(fake_llm)
    assert methodology.switch(db, "meddpicc") == 1
    back = {r["element"]: r for r in tables.meddpicc_rows(db, deal)}
    assert list(back) == list(schemas.MEDDPICC)
    assert back["paper_process"]["status"] == "known" and "status" in back["paper_process"]["user_set"]
    assert back["paper_process"]["what_we_know"] == "PO through procurement"
    assert "paper_process" in history.build(db, deal, call)["user_meddpicc"]
    worker.drain(db)                                                            # the strategist says unknown; the gate keeps his edit
    mid = tables.meddpicc_id(deal, "paper_process")
    assert db.execute("SELECT status FROM meddpicc WHERE id=?", (mid,)).fetchone()[0] == "known"
    assert db.execute("SELECT status FROM meddpicc WHERE id=?", (tables.meddpicc_id(deal, "authority"),)).fetchone()[0] == "known"
    assert "Economic Buyer" in client.get(f"/deals/{deal}/intel").text

    db.execute("UPDATE deals SET status='lost' WHERE node_id=?", (deal,))      # a closed deal is not re-assessed
    db.commit()
    assert methodology.switch(db, "bant") == 0 and methodology.active().key == "bant"


def test_conflicts_on_hidden_elements_wait_for_their_methodology(db, fake_llm):
    deal, call, _ = run_call(db, fake_llm)
    mid = tables.meddpicc_id(deal, "paper_process")
    tables.upsert(db, "meddpicc", mid, {"deal_id": deal, "element": "paper_process"}, {"status": "known"},
                  "user_input", {"kind": "user_input", "ref": "test"}, actor="user")
    from salescoach.memory import gate
    assert gate.propose(db, gate.Proposed(mid, "meddpicc", "status", "unknown", "high", {"kind": "strategist"}),
                        actor="deal_strategist") == "conflict"
    db.commit()
    assert [c["what"] for c in tables.conflicts(db, deal)] == ["Paper Process"]
    methodology.set_active("bant")
    assert tables.conflicts(db, deal) == []
    methodology.set_active("meddpicc")
    assert len(tables.conflicts(db, deal)) == 1


def test_prep_survives_artifacts_from_another_methodology(db, fake_llm, client):
    deal, call1, people = run_call(db, fake_llm)                               # call 1 under MEDDPICC
    old_brief = prep.generate(db, deal, use_llm=False)
    raw = json.loads(db.execute("SELECT json FROM prep_briefs WHERE id=?", (old_brief,)).fetchone()[0])
    raw.pop("methodology")                                                      # as briefs were stored before this phase
    db.execute("UPDATE prep_briefs SET json=? WHERE id=?", (json.dumps(raw), old_brief))
    db.commit()

    methodology.switch(db, "bant")
    assert prep.generate(db, deal, use_llm=False)                               # before any BANT run: nothing to crash on
    assert methodology.active().gap_rank("economic_buyer") == 4                 # a foreign key sorts last, no ValueError

    script_under(fake_llm, "BANT", lambda call: [
        element("authority", "known", "the CFO meeting is done, we met him on Monday and he is positive", call)])
    call2 = paste.import_text(db, CALL2, "NWP second", deal_id=deal, participants=people)
    worker.drain(db)
    assert repo.get_call(db, call2)["wf_state"] == "awaiting_review"
    latest, row = tables.latest_strategy(db, deal)
    assert row["call_id"] == call2 and latest["methodology"]["key"] == "bant"
    # latest is BANT, the one before it MEDDPICC: only shared elements are compared, labels never raise
    changes = prep._strategy_changes(db, deal)
    assert isinstance(changes, list) and not any("Economic Buyer" in c for c in changes)
    brief = prep.get(db, brief_id=prep.generate(db, deal, use_llm=False))
    assert {g["label"] for g in brief["gaps"]} == {"Budget", "Need", "Timeline"}

    page = client.get(f"/deals/{deal}/prep?id={old_brief}")                    # the stale brief still renders, as MEDDPICC
    assert page.status_code == 200 and "MEDDPICC gaps" in page.text and "Decision Process" in page.text
    assert "MEDDPICC GAPS AND THE QUESTION TO ASK:" in prep.render_text(prep.get(db, brief_id=old_brief))
    assert methodology.label_for("decision_process") == "Decision Process"     # not in BANT: found in the library
    assert methodology.label_for("gone_with_a_deleted_custom") == "Gone With A Deleted Custom"


# =====================================================================================
# a custom methodology, end to end
# =====================================================================================

def test_custom_methodology_runs_end_to_end(db, fake_llm, client):
    methodology.save_custom(CUSTOM)
    methodology.set_active("value_pilot")

    def elements(call):
        return [element("success_measure", "known", CFO_QUOTE, call),
                element("decision_process", "partial", "set up a meeting with our CFO and CEO", call, turns=(3,), conf="medium")]
    deal, call, _ = run_pipeline(db, fake_llm, "Pilot", elements)
    assert {r["element"]: r["status"] for r in tables.meddpicc_rows(db, deal)} == {
        "sponsor": "unknown", "success_measure": "known", "decision_process": "partial"}
    h = tables.health(db, deal)
    rules = {c["rule"]: c["cap"] for c in h["caps"]}
    assert rules == {"single_threaded": 50, "sponsor_not_known": 40, "value_pilot_known_below_2": 55} and h["score"] == 40
    risks = {r["type"]: r for r in tables.risks(db, deal)}
    assert risks["weak_champion"]["source"] == "rule"
    assert 'Ask on the next call: "Who owns the pilot on your side?"' == risks["weak_champion"]["mitigation"]
    frag = client.get(f"/deals/{deal}/intel").text
    assert "<h3>Value Pilot</h3>" in frag and "1 of 3 known" in frag and 'class="mgrid mgrid-3"' in frag
    assert "Pilot Sponsor" in frag and "Path to Rollout" in frag
    system = calls_of(fake_llm, "DealStrategy")[-1]["system"]
    assert "## Value Pilot\nGive all three elements exactly once: sponsor, success_measure, decision_process." in system
    assert "(one buyer-side voice, no pilot sponsor, no dated commitment)" in system
    analyst = calls_of(fake_llm, "CallAnalysis")[-1]["system"]
    assert "Use Pilot as lenses" in analyst and "Check the pilot sponsor first." in analyst


def test_intel_yaml_can_still_tune_a_methodology_cap(db, fake_llm, seller_settings):
    (seller_settings / "intel.yaml").write_text("health:\n  caps:\n    need_not_confirmed: 30\n")
    methodology.set_active("bant")
    deal, call, _ = run_pipeline(db, fake_llm, "BANT", bant_elements)
    assert tables.health(db, deal)["score"] == 30


# =====================================================================================
# coaching text: analyst and live coach
# =====================================================================================

def test_analyst_and_live_coach_carry_the_active_methodology(seller_settings):
    methodology.set_active("spin")
    spin = methodology.active()
    analyst = CallAnalystAgent().system_prompt({})
    assert "Use SPIN, Challenger and Gap Selling as lenses to find what is MISSING" in analyst and "MEDDPICC" not in analyst
    assert "## How this team sells: SPIN Selling" in analyst and spin.analyst in analyst
    assert "`element` is one of: Situation, Problem, Implication, Need-payoff." in analyst
    assert analyst.index("How this team sells") < analyst.index("## Seller taxonomy") and "{{" not in analyst
    assert set(CallAnalystAgent().schema.model_fields["gaps"].annotation.__args__[0].model_fields["lens"]
               .annotation.__args__) == {"SPIN", "Challenger", "Gap"}

    live = LiveCoachAgent().system_prompt({})
    assert "## How this team sells: SPIN Selling" in live and spin.live in live and "{{" not in live
    assert "- dig_deeper:" in live and "economic_buyer" in live              # slots and triggers are untouched

    base = config.load("live_coach")["scoring"]["weights"]
    weights = coach_settings.load()["scoring"]["weights"]
    assert (weights["quantify_impact"], weights["dig_deeper"], weights["root_cause"]) == (0.9, 0.85, 0.75)
    assert all(weights[k] == v for k, v in base.items() if k not in spin.live_weights) and set(weights) == set(base)
    # the user's own weight (an accepted learning proposal) outranks the methodology; a run override outranks both
    config.save_user("live_coach", {"scoring": {"weights": {"quantify_impact": 0.5}}})
    assert coach_settings.load()["scoring"]["weights"]["quantify_impact"] == 0.5
    assert coach_settings.load({"scoring": {"weights": {"quantify_impact": 0.2}}})["scoring"]["weights"]["quantify_impact"] == 0.2
    assert coach_settings.load()["scoring"]["weights"]["dig_deeper"] == 0.85


def test_another_seller_on_bant_never_reads_meddpicc_in_a_prompt(db, fake_llm, seller_settings):
    write_seller(seller_settings, PRIYA)
    methodology.set_active("bant")
    deal, call, _ = run_pipeline(db, fake_llm, "BANT", bant_elements)
    fake_llm.responses["PrepWriting"] = {"objective": "o", "objective_why": "w", "opening": "o", "close": "c",
                                         "watch_for": []}
    brief = prep.get(db, brief_id=prep.generate(db, deal, meeting_title="Next"))
    assert brief["writing_source"] == "model"
    checked = 0
    for c in fake_llm.calls:
        if c["schema"] in ("DealStrategy", "PrepWriting", "CallAnalysis"):
            text = c["system"] + "\n" + c["prompt"]
            assert "MEDDPICC" not in text and "meddpicc" not in text.lower(), c["schema"]
            assert "Maya" not in text and "{{" not in text, c["schema"]
            assert "Priya" in c["system"] and "BANT" in text, c["schema"]
            checked += 1
    assert checked >= 3
    strategist_system = calls_of(fake_llm, "DealStrategy")[-1]["system"]
    assert "exactly once: budget, authority, need, timeline." in strategist_system
    assert "(one buyer-side voice, no authority, no need, no dated commitment)" in strategist_system
    assert "BANT gaps" in calls_of(fake_llm, "PrepWriting")[-1]["system"]
    assert "MEDDPICC" not in strategist.render_text(db, deal)
