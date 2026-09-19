"""Phase A: the user-settings layer, the seller profile, prompt templating, the first-run gate,
and the guard that keeps one person's identity out of the tracked prompts and config.

Every test here runs on a throwaway settings folder (conftest.seller_settings). No real
secrets.env or token file is opened: the secret tests write and read their own tmp files.
"""
import importlib.util
import logging
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import write_seller
from salescoach import config, repo, seller

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = {"origin": "http://127.0.0.1:8140"}

PRIYA = {
    "name": "Priya Nair",
    "emails": ["priya@acmecloud.com"],
    "company": "Acme Cloud",
    "role": "Account executive",
    "website": "www.acmecloud.com",
    "offering": "HR software that replaces spreadsheets for companies with hourly staff",
    "icp": "US mid-market companies, 200 to 2,000 employees",
    "buyer_titles": "HR directors and CFOs",
    "languages": ["en"],
    "timezone": "America/New_York",
    "signature": "Best,\nPriya",
}
# What must never reach another seller's model, and must never come back into a tracked prompt.
# The generic part is the test fixture's own identity plus the industry and language words its
# offering uses. An install can add its own words (its real name, company, domains) in a private,
# untracked file, one per line: <data dir>/settings/guard-words.txt, or SALESCOACH_GUARD_WORDS
# (comma-separated). That keeps the real identity out of the public source while the guard still
# checks for it on the machine where it matters.
def _private_guard_words() -> tuple:
    words = []
    for raw in (os.environ.get("SALESCOACH_GUARD_WORDS") or "").split(","):
        if raw.strip():
            words.append(raw.strip())
    for candidate in (Path(os.environ.get("SALESCOACH_DATA") or (ROOT_DIR / "data")) / "settings" / "guard-words.txt",):
        try:
            words += [w.strip() for w in candidate.read_text().splitlines() if w.strip() and not w.startswith("#")]
        except OSError:
            pass
    return tuple(dict.fromkeys(words))


ROOT_DIR = Path(__file__).resolve().parent.parent
GENERIC_PERSONAL = ("Maya Iyer", "Tessel", "freight", "logistics", "Hindi", "Hinglish", "Indian")
PERSONAL = GENERIC_PERSONAL + _private_guard_words()
PERSONAL_RE = re.compile("|".join(re.escape(w) for w in PERSONAL) + r"|tessel\.test|tesselops\.test", re.I)


# =====================================================================================
# config: deep merge, cache, atomic saves, precedence
# =====================================================================================

@pytest.fixture
def tracked(tmp_path, monkeypatch):
    folder = tmp_path / "tracked-config"
    folder.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", folder)
    return folder


def test_user_dir_is_resolved_at_call_time(tmp_path, monkeypatch):
    monkeypatch.delenv("SALESCOACH_SETTINGS")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data-one")
    assert config.user_dir() == tmp_path / "data-one" / "settings"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data-two")          # what the db fixture does
    assert config.user_dir() == tmp_path / "data-two" / "settings"
    monkeypatch.setenv("SALESCOACH_SETTINGS", str(tmp_path / "elsewhere"))
    assert config.user_dir() == tmp_path / "elsewhere"


def test_load_deep_merges_user_over_tracked_and_replaces_lists(tracked, seller_settings):
    (tracked / "automation.yaml").write_text(
        "followup:\n  run_at: '09:30'\n  max_wait_days: 30\ncalendar:\n  own_domains: [a.com, b.com]\nkeep: 1\n")
    (seller_settings / "automation.yaml").write_text(
        "followup:\n  run_at: '08:00'\ncalendar:\n  own_domains: [mine.com]\nextra: yes\n")
    merged = config.load("automation")
    assert merged["followup"] == {"run_at": "08:00", "max_wait_days": 30}       # key by key, user wins
    assert merged["calendar"]["own_domains"] == ["mine.com"]                     # a list is replaced, never appended
    assert merged["keep"] == 1 and merged["extra"] is True
    assert config.load("no-such-file") == {}
    assert config.load_user("automation")["followup"] == {"run_at": "08:00"}     # the user's part only


def test_load_cache_follows_the_mtime_of_both_files(tracked, seller_settings):
    base, mine = tracked / "policy.yaml", seller_settings / "policy.yaml"
    base.write_text("email_policy: A\n")
    os.utime(base, (1_700_000_000, 1_700_000_000))
    assert config.load("policy") == {"email_policy": "A"}
    assert config.load("policy") is config.load("policy")                        # served from the cache

    base.write_text("email_policy: B\n")                                          # tracked file edited
    os.utime(base, (1_700_000_001, 1_700_000_001))
    assert config.load("policy")["email_policy"] == "B"

    mine.write_text("email_policy: C\n")                                          # user file appears
    os.utime(mine, (1_700_000_002, 1_700_000_002))
    assert config.load("policy")["email_policy"] == "C"
    mine.write_text("email_policy: D\n")                                          # ... and is edited
    os.utime(mine, (1_700_000_003, 1_700_000_003))
    assert config.load("policy")["email_policy"] == "D"
    mine.unlink()                                                                 # ... and removed
    assert config.load("policy")["email_policy"] == "B"


def test_save_user_is_atomic_never_touches_tracked_and_wins(tracked, seller_settings, monkeypatch):
    (tracked / "thing.yaml").write_text("a: 1\nb: {c: 2, d: 3}\n")
    before = (tracked / "thing.yaml").read_text()
    path = config.save_user("thing", {"b": {"c": 9}, "note": "line one\nline two"})
    assert path == seller_settings / "thing.yaml"
    assert config.load("thing") == {"a": 1, "b": {"c": 9, "d": 3}, "note": "line one\nline two"}
    assert (tracked / "thing.yaml").read_text() == before
    config.save_user("thing", {"b": {"c": 10}})                                   # same mtime tick or not
    assert config.load("thing")["b"]["c"] == 10

    # A crash half way leaves the previous file whole and no temp file behind.
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(config.os, "replace", boom)
    with pytest.raises(OSError):
        config.save_user("thing", {"b": {"c": 11}})
    monkeypatch.undo()
    assert yaml.safe_load(path.read_text()) == {"b": {"c": 10}}
    assert [p.name for p in seller_settings.iterdir() if p.name.endswith(".tmp")] == []
    with pytest.raises(TypeError):
        config.save_user("thing", ["not", "a", "mapping"])


def test_text_prefers_the_user_file(tracked, seller_settings):
    (tracked / "style.md").write_text("tracked style")
    assert config.text("style.md") == "tracked style"
    assert config.user_file("style.md") == tracked / "style.md"
    config.save_user_text("style.md", "my style")
    assert config.text("style.md") == "my style" and (tracked / "style.md").read_text() == "tracked style"
    assert config.user_file("style.md") == seller_settings / "style.md"
    assert config.text("missing.md") == ""


def test_reset_cache_is_gone():
    assert not hasattr(config, "reset_cache")


# =====================================================================================
# secrets
# =====================================================================================

def test_secret_order_env_then_user_then_legacy(tmp_path, seller_settings, monkeypatch):
    legacy = tmp_path / "legacy-secrets.env"
    legacy.write_text('# comment\nDEMO_KEY="from-legacy"\nONLY_LEGACY=\'legacy-only\'\n')
    monkeypatch.setattr(config, "SECRETS_FILE", legacy)
    monkeypatch.delenv("DEMO_KEY", raising=False)
    assert config.secret("DEMO_KEY") == "from-legacy" and config.has_secret("DEMO_KEY")
    config.set_secret("DEMO_KEY", "from-user")
    assert config.secret("DEMO_KEY") == "from-user"
    assert config.secret("ONLY_LEGACY") == "legacy-only"
    monkeypatch.setenv("DEMO_KEY", "from-env")
    assert config.secret("DEMO_KEY") == "from-env"
    assert config.secret("ABSENT_KEY") is None and config.secret("ABSENT_KEY", "d") == "d"
    assert not config.has_secret("ABSENT_KEY")
    assert "from-legacy" in legacy.read_text()                   # the legacy file is read, never written


def test_set_secret_is_private_atomic_and_silent(seller_settings, monkeypatch, caplog, capsys):
    value = "sk-test-VALUE-that-must-not-leak-1234567890"
    monkeypatch.delenv("PROVIDER_KEY", raising=False)
    with caplog.at_level(logging.DEBUG):
        assert config.set_secret("PROVIDER_KEY", value) is None              # nothing is echoed back
        config.set_secret("OTHER_KEY", 'has "quotes" inside')
    path = seller_settings / "secrets.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.secret("PROVIDER_KEY") == value and config.secret("OTHER_KEY") == 'has "quotes" inside'
    captured = capsys.readouterr()
    assert value not in caplog.text and value not in captured.out and value not in captured.err
    assert [p.name for p in seller_settings.iterdir() if p.name.endswith(".tmp")] == []

    config.set_secret("PROVIDER_KEY", "rotated-value-42")                        # replace keeps the others, and 0600
    assert config.secret("PROVIDER_KEY") == "rotated-value-42" and config.secret("OTHER_KEY") == 'has "quotes" inside'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn", "nul\x00byte", "", " padded ", '"quoted"'])
def test_set_secret_rejects_values_it_cannot_store_and_never_repeats_them(seller_settings, bad):
    with pytest.raises(ValueError) as err:
        config.set_secret("PROVIDER_KEY", bad)
    assert "PROVIDER_KEY" in str(err.value)
    if bad.strip():
        assert bad not in str(err.value) and bad not in repr(err.value)           # the value is never in the error
    assert not (seller_settings / "secrets.env").exists()


def test_set_secret_rejects_names_that_would_break_the_file(seller_settings):
    for name in ("", "has space", "A=B", "line\nbreak", "1starts_with_digit"):
        with pytest.raises(ValueError):
            config.set_secret(name, "value")


# =====================================================================================
# seller profile
# =====================================================================================

def test_tracked_seller_yaml_ships_unconfigured(seller_settings):
    (seller_settings / "seller.yaml").unlink()
    assert not seller.is_configured()
    p = seller.profile()
    assert p["name"] == "" and p["emails"] == [] and p["company"] == ""
    assert seller.timezone() == "Asia/Kolkata" and seller.tz_label() == "IST"     # the historical default zone
    assert seller.render("Hello {{seller_first_name}} of {{company}}") == "Hello the seller of the seller's company"
    with pytest.raises(seller.NotConfigured):
        seller.require_configured()


def test_is_configured_needs_name_email_company_and_offering(seller_settings):
    assert seller.is_configured()
    for missing in ("name", "emails", "company", "offering"):
        write_seller(seller_settings, {k: v for k, v in PRIYA.items() if k != missing})
        assert not seller.is_configured(), missing
    write_seller(seller_settings, {k: PRIYA[k] for k in ("name", "emails", "company", "offering")})
    assert seller.is_configured()


def test_profile_helpers(seller_settings):
    assert seller.first_name() == "Maya" and seller.name() == "Maya Iyer"
    assert seller.primary_email() == "maya@tessel.test"
    assert {"Maya Iyer", "Maya", "maya.iyer"} <= set(seller.aliases())
    assert seller.own_domains() == ["tessel.test", "tesselops.test", "gmail.com"]
    assert seller.internal_domains() == {"tessel.test", "tesselops.test"}             # a mailbox provider is not a company
    assert seller.languages() == ["English", "Hindi"] and "Hindi" in seller.language_note()
    assert seller.team_label() == "Tessel team" and seller.from_name() == "Maya Iyer"

    write_seller(seller_settings, PRIYA)
    assert seller.first_name() == "Priya" and seller.own_domains() == ["acmecloud.com"]   # derived from the address
    assert seller.language_note() == "Calls are in English."
    assert seller.language_detail() == "" and seller.voice_language_note() == ""
    assert seller.timezone() == "America/New_York" and seller.tz_label() in ("EST", "EDT")

    write_seller(seller_settings, {**PRIYA, "languages": ["en", "es"], "timezone": "Not/AZone"})
    assert "English and Spanish" in seller.language_note() and "Spanish" in seller.language_detail()
    assert seller.zone().key == "Asia/Kolkata"                                     # a bad zone never crashes a page


def test_render_substitutes_double_braces_only(seller_settings):
    text = ("{{seller_first_name}} of {{ company }} met them. Turns marked {garbled} or {bleed} or {asr:partial} "
            "are weak. JSON looks like {\"a\": 1}. Literal {single} stays.")
    out = seller.render(text)
    assert out.startswith("Maya of Tessel met them.")
    assert "{garbled}" in out and "{bleed}" in out and "{asr:partial}" in out and '{"a": 1}' in out
    assert "{single}" in out and "{{" not in out
    assert seller.render("no variables {here}") == "no variables {here}"
    assert seller.render("{{ELEMENTS}} end", {"ELEMENTS": "metrics"}) == "metrics end"   # callers may add their own


def test_render_raises_on_an_unknown_variable(seller_settings):
    with pytest.raises(seller.UnknownVariable) as err:
        seller.render("Hello {{sellr_name}}")
    assert "sellr_name" in str(err.value) and "seller_name" in str(err.value)      # names it, and lists the real ones


def test_render_drops_optional_guidance_cleanly(seller_settings):
    write_seller(seller_settings, PRIYA)
    out = seller.render('in their own voice. {{voice_language_note}} Null only if none.\n\n{{language_detail}}\n\n'
                        'next ("let\'s see"{{eg_hedge}}, "next week").')
    assert out == 'in their own voice. Null only if none.\n\nnext ("let\'s see", "next week").'


# =====================================================================================
# prompts: every agent, through its real loader
# =====================================================================================

def _agents():
    from salescoach.agents.actions import ActionAgent
    from salescoach.agents.call_analyst import CallAnalystAgent
    from salescoach.agents.email_drafter import EmailAgent
    from salescoach.agents.quality import QualityAgent
    from salescoach.agents.summary import SummaryAgent
    from salescoach.automation.followup import FollowupAgent, NudgeAgent
    from salescoach.automation.replies import ReplyAgent
    from salescoach.coach.slow_pass import LiveCoachAgent
    from salescoach.intel.coach import CoachAgent
    from salescoach.intel.prep import PrepAgent
    from salescoach.intel.reconcile import ReconcilerAgent
    from salescoach.intel.strategist import StrategistAgent
    return [QualityAgent(), SummaryAgent(), CallAnalystAgent(), ActionAgent(), EmailAgent(), FollowupAgent(),
            NudgeAgent(), ReplyAgent(), LiveCoachAgent(), StrategistAgent(), CoachAgent(), PrepAgent(),
            ReconcilerAgent()]


def _prompt_files():
    return sorted((ROOT / "salescoach").glob("*/prompts/*.md"))


def test_every_prompt_file_has_an_agent_in_this_test():
    assert {p.stem for p in _prompt_files()} == {a.name for a in _agents()}


def test_existing_seller_prompts_still_carry_his_context(seller_settings):
    """The conftest profile is the seller the prompts were tuned for; rendering must put him back."""
    by_name = {a.name: a.system_prompt({}) for a in _agents()}
    for name, text in by_name.items():
        assert "{{" not in text and "}}" not in text, name
    analyst = by_name["call_analyst"]
    assert "Maya" in analyst and "Tessel" in analyst and "freight and logistics" in analyst
    assert "promoters, MDs, CEOs, CFOs and COOs, not IT" in analyst and "Hinglish" in analyst
    assert "{garbled}" in analyst and "## Seller taxonomy" in analyst
    assert '"Hi chloral cara march"' in by_name["quality"] and "{bleed}" in by_name["quality"]
    assert '"dekhte hain"' in by_name["live_coach"] and '"abhi nahi"' in by_name["live_coach"]
    assert "SET BY MAYA" in by_name["deal_strategist"]
    assert "(plant, lane, dispatch, detention)" in by_name["prep_writer"]
    for name in ("email", "nudge"):                                # the appended style guide is rendered too
        assert "## Style guide" in by_name[name]
        assert by_name[name].rstrip().endswith("Maya Iyer\nFounder, CEO\nTessel\nwww.tessel.test"), name


def test_another_seller_gets_prompts_with_nothing_of_the_first_one(seller_settings):
    write_seller(seller_settings, PRIYA)
    for agent in _agents():
        text = agent.system_prompt({})
        assert "{{" not in text, agent.name
        found = [w for w in PERSONAL if w.lower() in text.lower()]
        assert not found, f"{agent.name}: {found}"
        if agent.name not in ("assessment_reconciler",):
            assert "Priya" in text, agent.name
    analyst = _agents()[2].system_prompt({})
    assert "Acme Cloud" in analyst and "HR software" in analyst and "HR directors and CFOs" in analyst
    assert "Calls are in English." in analyst and "{garbled}" in analyst
    nudge = next(a for a in _agents() if a.name == "nudge").system_prompt({})
    assert nudge.rstrip().endswith("Best,\nPriya")

    from salescoach.coach import detectors
    assert not PERSONAL_RE.search(detectors.local_system()) and "{{" not in detectors.local_system()


def test_prompt_side_python_strings_follow_the_profile(seller_settings):
    from salescoach.agents.actions import ActionAgent
    from salescoach.agents.email_drafter import EmailAgent
    ctx = {"call": {"title": "t", "lang_mode": "en"}, "call_date": "2026-09-02", "call_date_human": "2 Sep",
           "participants": [], "deal": None, "deal_people": [], "allowed_recipients": {}, "email_actions": [],
           "recent_edits": [{"draft_body": "d", "final_body": "f"}], "open_loops": [], "world_commitments": [],
           "turns": []}
    mine = EmailAgent().build_prompt(ctx)
    assert "WHAT MAYA SENT" in mine and "HOW MAYA EDITED EARLIER DRAFTS" in mine      # as it always read
    assert "Maya's commitment tracker" in ActionAgent().build_prompt(ctx)
    write_seller(seller_settings, PRIYA)
    theirs = EmailAgent().build_prompt(ctx)
    assert "WHAT PRIYA SENT" in theirs and "HOW PRIYA EDITED" in theirs and "MAYA" not in theirs
    tracker = ActionAgent().build_prompt(ctx)
    assert "Priya's commitment tracker" in tracker and "Maya" not in tracker

    from salescoach.automation import schemas as auto_schemas
    from salescoach.intel import schemas as intel_schemas
    from salescoach.providers.base import json_schema_for
    for model in (intel_schemas.DealStrategy, intel_schemas.CoachReport, intel_schemas.PrepWriting,
                  intel_schemas.AssessmentReconciliation, auto_schemas.FollowupDecision,
                  auto_schemas.ReplyAnalysis, auto_schemas.NudgeDraft):
        assert not PERSONAL_RE.search(str(json_schema_for(model))), model.__name__


def test_model_facing_names_are_the_stored_ones(monkeypatch):
    """Migration 5 made the stored spellings neutral, so the model's answer is stored as written."""
    from salescoach.automation import schemas
    monkeypatch.delenv("SALESCOACH_LEGACY_WORDS", raising=False)
    assert seller.legacy_words() == {}
    base = dict(still_relevant=True, wait_until=None, rationale="r", relationship_risk="low", relationship_note="n")
    assert schemas.FollowupDecision.model_validate({**base, "decision": "ask_user"}).decision == "ask_user"
    with pytest.raises(ValueError):
        schemas.FollowupDecision.model_validate({**base, "decision": "ask_owner"})
    reply = dict(summary="s", items=[], ignored_instructions=[])
    assert schemas.ReplyAnalysis.model_validate({**reply, "needs_user": True}).needs_user is True
    with pytest.raises(ValueError):
        schemas.ReplyAnalysis.model_validate({**reply, "needs_owner": True})
    assert seller.display("ask_user") == "ask you" and seller.display("user:ui") == "you"
    assert seller.display("user") == "you" and seller.display("rules") == "rules"
    assert seller.display("ask_owner") == "ask_owner"


def test_an_install_can_name_the_spellings_an_older_build_stored(monkeypatch, seller_settings):
    """The old spellings live only in a private file (or the environment), never in the source: an
    output replayed from an old run is stored in today's spelling, and an old stored word reads as
    the person should read it."""
    from salescoach.automation import schemas
    (seller_settings / "legacy-words.txt").write_text("# one old=new per line\nask_owner=ask_user\nowner=user\n")
    monkeypatch.setenv("SALESCOACH_LEGACY_WORDS", "needs_owner=needs_user, owner:ui=user:ui,junk,same=same")
    assert seller.legacy_words() == {"needs_owner": "needs_user", "owner:ui": "user:ui",
                                     "ask_owner": "ask_user", "owner": "user"}
    base = dict(still_relevant=True, wait_until=None, rationale="r", relationship_risk="low", relationship_note="n")
    assert schemas.FollowupDecision.model_validate({**base, "decision": "ask_owner"}).decision == "ask_user"
    assert schemas.FollowupDecision.model_validate({**base, "decision": "escalate"}).decision == "escalate"
    reply = dict(summary="s", items=[], ignored_instructions=[])
    assert schemas.ReplyAnalysis.model_validate({**reply, "needs_owner": True}).needs_user is True
    assert schemas.ReplyAnalysis.model_validate({**reply, "needs_user": False}).needs_user is False
    with pytest.raises(ValueError):                                     # both spellings at once is still an extra key
        schemas.ReplyAnalysis.model_validate({**reply, "needs_user": True, "needs_owner": True})
    assert seller.display("ask_owner") == "ask you" and seller.display("owner") == "you"
    assert seller.display("owner:ui") == "you" and seller.display("needs_owner") == "needs you"
    assert seller.display("agent") == "agent"


def test_prompt_version_and_input_sha_hash_the_rendered_text(db, fake_llm, seller_settings):
    import hashlib

    from salescoach.agents.summary import SummaryAgent
    from salescoach.intel.agentkit import shas

    class Probe(SummaryAgent):
        def build_prompt(self, ctx):
            return "the same user prompt"

    def run():
        system = Probe().system_prompt({})
        return system, shas(system, "the same user prompt")

    first_system, (first_version, first_sha) = run()
    assert first_version == hashlib.sha256(first_system.encode()).hexdigest()[:12]
    raw = (ROOT / "salescoach/agents/prompts/summary.md").read_text()
    assert first_version != hashlib.sha256(raw.encode()).hexdigest()[:12]          # not the template's hash
    write_seller(seller_settings, PRIYA)
    second_system, (second_version, second_sha) = run()
    assert second_system != first_system and second_version != first_version and second_sha != first_sha


# =====================================================================================
# python defaults read the profile
# =====================================================================================

def test_ensure_me_uses_the_profile(db, seller_settings):
    me = repo.ensure_me(db)
    row = db.execute("SELECT name, email, is_me FROM people WHERE node_id=?", (me,)).fetchone()
    assert (row["name"], row["email"], row["is_me"]) == ("Maya Iyer", "maya@tessel.test", 1)
    assert repo.ensure_me(db) == me                                                # still one row


def test_ensure_me_for_another_seller_and_sync_after_a_profile_edit(db, seller_settings):
    write_seller(seller_settings, PRIYA)
    me = repo.ensure_me(db)
    row = db.execute("SELECT name, email FROM people WHERE node_id=?", (me,)).fetchone()
    assert (row["name"], row["email"]) == ("Priya Nair", "priya@acmecloud.com")
    write_seller(seller_settings, {**PRIYA, "name": "Priya N. Menon", "emails": ["priya.menon@acmecloud.com"]})
    assert repo.sync_me(db) == me
    row = db.execute("SELECT name, email FROM people WHERE node_id=?", (me,)).fetchone()
    assert (row["name"], row["email"]) == ("Priya N. Menon", "priya.menon@acmecloud.com")


def test_addresses_domains_and_from_name_come_from_the_profile(db, seller_settings):
    from salescoach.automation import calendar, common
    from salescoach.execution import policy
    from salescoach.intel import prep, strategist
    from salescoach.sources import granola
    write_seller(seller_settings, PRIYA)
    assert common.my_addresses() == {"priya@acmecloud.com"}
    assert granola.me_addresses() == {"priya@acmecloud.com"}
    assert calendar.own_domains() == {"acmecloud.com"}
    assert policy._from_name() == "Priya Nair"
    assert policy._rfc822_id("k" * 40, gmail=None).endswith("@acmecloud.com>")
    assert prep._owner_label({"owner": "internal"}) == "Acme Cloud team"
    assert strategist._is_me({}, "Priya Nair") and not strategist._is_me({}, "Priya")
    assert not strategist._is_me({}, "Maya Iyer")
    assert common.IST.key == "America/New_York"                                    # the historical name, the seller's zone
    assert calendar.scheduling()["timezone"] == "America/New_York"


def test_config_overrides_still_win_over_the_profile(seller_settings):
    from salescoach.automation import calendar, common
    from salescoach.execution import policy
    (seller_settings / "automation.yaml").write_text(
        "my_addresses: [override@x.com]\ncalendar:\n  own_domains: [x.com]\n")
    (seller_settings / "policy.yaml").write_text("email:\n  from_name: The Override\n")
    assert common.my_addresses() == {"override@x.com"}
    assert calendar.own_domains() == {"x.com"}
    assert policy._from_name() == "The Override"


def test_existing_seller_keeps_ist_labels_byte_for_byte(seller_settings):
    from datetime import datetime, timezone

    from salescoach.automation import calendar
    from salescoach.orchestrator import context
    slots = [datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc), datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)]
    assert calendar.slots_phrase(slots) == "Tue 15 Sep at 11:00 or Wed 16 Sep at 15:30 IST"
    assert context.call_date_ist({"started_at": "2026-09-02T09:00:00+00:00"}) == \
        ("2026-09-02", "Wednesday 02 September 2026, 14:30 IST")
    write_seller(seller_settings, PRIYA)
    assert calendar.slots_phrase(slots[:1]) == "Tue 15 Sep at 01:30 EDT"


# =====================================================================================
# first-run gate and /setup
# =====================================================================================

@pytest.fixture
def client(db):
    from salescoach.web.app import create_app
    return TestClient(create_app(start_worker=False, live_factory=None))


def test_gate_redirects_every_page_until_the_profile_exists(client, seller_settings):
    (seller_settings / "seller.yaml").unlink()
    for path in ("/", "/loops", "/deals", "/coach", "/import", "/followups", "/calendar"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup", path
    assert client.get("/setup").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/intel/deals.json", follow_redirects=False).status_code != 303      # JSON is not a page
    page = client.get("/setup").text
    assert 'name="offering"' in page and "Sales Coach" in page and "Tessel" not in page


def test_gate_is_open_once_configured(client):
    for path in ("/", "/loops", "/deals", "/import", "/setup"):
        assert client.get(path, follow_redirects=False).status_code == 200, path
    home = client.get("/").text
    assert "Tessel" in home and "<title>" in home                                  # the wordmark is the profile's company


def test_unconfigured_install_refuses_to_create_a_call(client, db, seller_settings):
    (seller_settings / "seller.yaml").unlink()
    r = client.post("/import/text", headers=ORIGIN, follow_redirects=False,
                    data={"title": "x", "text": "Me: hello\nThem: hi"})
    assert r.status_code == 303 and r.headers["location"].startswith("/setup?err=")
    assert "profile" in r.headers["location"].lower()
    r = client.post("/live/start", headers=ORIGIN, data={"title": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/setup?err=")
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    with pytest.raises(seller.NotConfigured):                                       # and so does every other door
        repo.create_call(db, source="paste", title="x")
    from salescoach.sources import paste
    with pytest.raises(seller.NotConfigured):
        paste.import_text(db, "Me: hello\nThem: hi", "x")
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_setup_form_saves_the_profile_and_opens_the_gate(client, db, seller_settings):
    (seller_settings / "seller.yaml").unlink()
    form = {"name": "Priya Nair", "emails": "Priya@AcmeCloud.com, priya.nair@gmail.com", "company": "Acme Cloud",
            "offering": PRIYA["offering"], "role": "Account executive", "languages": "en",
            "timezone": "America/New_York", "signature": "Best,\r\nPriya"}
    # The wizard (Phase E) replaced the single form: the profile is its step 1 and "Save and continue" leads to step 2.
    assert client.post("/setup/you", data=form).status_code == 403                 # same-origin guard, like every POST
    assert not seller.is_configured()
    r = client.post("/setup/you", data=form, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/setup/method?msg=")
    assert seller.is_configured() and seller.emails() == ["priya@acmecloud.com", "priya.nair@gmail.com"]
    assert seller.profile()["signature"] == "Best,\nPriya" and seller.internal_domains() == {"acmecloud.com"}
    assert (seller_settings / "seller.yaml").exists()
    assert not PERSONAL_RE.search((ROOT / "config" / "seller.yaml").read_text())   # the tracked file is untouched
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200 and "Acme Cloud" in home.text

    bad = client.post("/setup/you", data={**form, "emails": "not-an-address", "timezone": "Mars/Base"}, headers=ORIGIN)
    assert bad.status_code == 400 and "does not look like an email" in bad.text and "Mars/Base" in bad.text
    assert seller.emails() == ["priya@acmecloud.com", "priya.nair@gmail.com"]      # a refused form saves nothing


def test_no_page_shows_a_raw_stored_word(client, db):
    from salescoach.web import app as webapp
    assert webapp.human("ask_user") == "ask you" and webapp.human("close_as_stale") == "close as stale"
    assert webapp.ACTOR == "user"
    raw = ("ask_user", "needs_user", "user:ui") + _private_guard_words()
    for path in ("/followups", "/replies", "/"):
        text = client.get(path).text.lower()
        for word in raw:
            assert word.lower() not in text, (path, word)


# =====================================================================================
# migration script
# =====================================================================================

def _migration():
    spec = importlib.util.spec_from_file_location("migrate_personal_settings",
                                                  ROOT / "scripts" / "migrate_personal_settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def old_install(tmp_path):
    """An install as it looked before Phase A: identity in the tracked config files."""
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "salescoach").mkdir()
    (install / "config" / "style.md").write_text(
        "# How Test Seller writes\n\n## Sign-off\nCustomer and prospect emails use the full signature:\n\n"
        "Best,\nTest\n\nTest Seller\nFounder\n")
    (install / "config" / "accounts.yaml").write_text("accounts:\n  - name: Buyer Co\n    domains: [buyer.test]\n")
    (install / "config" / "automation.yaml").write_text(
        "my_addresses: [Test@Seller.test, test@gmail.com]\ncalendar:\n  own_domains: [seller.test, gmail.com]\n")
    (install / "config" / "policy.yaml").write_text("email:\n  account: work\n  from_name: Test Seller\n")
    return install


def test_migration_writes_user_settings_once_and_never_overwrites(old_install):
    m = _migration()
    lines = []
    assert set(m.migrate(old_install, dry_run=True, out=lines.append).values()) == {"would write"}
    assert not (old_install / "data").exists()                                     # a dry run writes nothing

    assert set(m.migrate(old_install, out=lines.append).values()) == {"written"}
    settings = old_install / "data" / "settings"
    profile = yaml.safe_load((settings / "seller.yaml").read_text())
    assert profile["name"] == "Test Seller" and profile["emails"] == ["test@seller.test", "test@gmail.com"]
    assert profile["own_domains"] == ["seller.test", "gmail.com"] and profile["languages"] == ["en", "hi"]
    assert profile["signature"] == "Best,\nTest\n\nTest Seller\nFounder"
    assert profile["timezone"] == "Asia/Kolkata" and profile["company"] and profile["offering"]
    assert (settings / "style.md").read_text() == (old_install / "config" / "style.md").read_text()
    assert (settings / "accounts.yaml").read_text() == (old_install / "config" / "accounts.yaml").read_text()

    # Idempotent, and an edit the user made since is never overwritten.
    (settings / "seller.yaml").write_text("name: Edited By Hand\n")
    snapshot = {p.name: p.read_text() for p in settings.iterdir()}
    assert set(m.migrate(old_install, out=lines.append).values()) == {"exists"}
    assert {p.name: p.read_text() for p in settings.iterdir()} == snapshot
    assert m.main([str(old_install)]) == 0
    assert {p.name: p.read_text() for p in settings.iterdir()} == snapshot
    assert any("differs" in line for line in lines)


def test_migrated_settings_configure_the_install(old_install, monkeypatch):
    _migration().migrate(old_install, out=lambda *_: None)
    monkeypatch.setenv("SALESCOACH_SETTINGS", str(old_install / "data" / "settings"))
    assert seller.is_configured() and seller.name() == "Test Seller"
    assert config.text("style.md").startswith("# How Test Seller writes")
    assert config.user_file("accounts.yaml") == old_install / "data" / "settings" / "accounts.yaml"


def test_migration_refuses_a_folder_that_is_not_an_install_or_has_nothing_personal(tmp_path, old_install):
    m = _migration()
    with pytest.raises(m.MigrationError):
        m.migrate(tmp_path)
    (old_install / "config" / "automation.yaml").write_text("followup: {}\n")      # generic, and no git history here
    assert m.main([str(old_install), "--from-rev", "no-such-rev"]) == 1
    assert not (old_install / "data").exists()


# =====================================================================================
# guard: the identity never comes back into tracked prompts or config
# =====================================================================================

def _tracked(*patterns):
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", *patterns], capture_output=True, text=True)
    files = [ROOT / line for line in out.stdout.splitlines() if line]
    return files


def test_no_personal_strings_in_tracked_prompts():
    files = _prompt_files()
    assert len(files) >= 13
    for path in files:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            assert not PERSONAL_RE.search(line), f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}"
            assert not re.search(r"\b(he|him|his|himself)\b", line, re.I), f"{path.relative_to(ROOT)}:{n}: gendered"


# config/ holds two legitimate kinds of language data: the live coach's cue words (its Hindi cues and its
# everyday business nouns) and the ASR model table. Identity is what is forbidden everywhere.
IDENTITY_RE = re.compile("|".join(re.escape(w) for w in ("Maya Iyer", "Tessel", "northwind", "harborline")
                                  + _private_guard_words()), re.I)
LANGUAGE_DATA = {"live_coach.yaml", "asr.yaml", "models.yaml", "learning.yaml"}   # learning.yaml: generic buyer-persona title keywords


def test_no_personal_strings_in_tracked_config():
    files = [p for p in (ROOT / "config").iterdir() if p.is_file()]
    tracked = set(_tracked("config"))
    if tracked:                                                   # outside a git checkout, check every file
        files = [p for p in files if p in tracked or p.name == "seller.yaml"]
    assert any(p.name == "seller.yaml" for p in files)
    for path in files:
        pattern = IDENTITY_RE if path.name in LANGUAGE_DATA else PERSONAL_RE
        for n, line in enumerate(path.read_text().splitlines(), 1):
            assert not pattern.search(line), f"config/{path.name}:{n}: {line.strip()[:120]}"


def test_tracked_config_ships_no_addresses_accounts_or_sender_name():
    automation = yaml.safe_load((ROOT / "config/automation.yaml").read_text())
    assert "my_addresses" not in automation and "own_domains" not in (automation.get("calendar") or {})
    assert "from_name" not in (yaml.safe_load((ROOT / "config/policy.yaml").read_text()).get("email") or {})
    assert yaml.safe_load((ROOT / "config/accounts.yaml").read_text()) == {"accounts": []}
    shipped = yaml.safe_load((ROOT / "config/seller.yaml").read_text())
    assert not any(shipped.get(k) for k in ("name", "emails", "company", "offering"))
    assert "{{signature}}" in (ROOT / "config/style.md").read_text()


def test_package_data_lists_every_data_file_the_code_reads():
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["salescoach"]
    pkg = ROOT / "salescoach"
    shipped = {p for pattern in data for p in pkg.glob(pattern)}
    wanted = {p for p in pkg.rglob("*") if p.is_file() and p.suffix in (".md", ".sql", ".html", ".css", ".js")
              and "__pycache__" not in p.parts}
    assert wanted - shipped == set(), sorted(str(p.relative_to(pkg)) for p in wanted - shipped)
