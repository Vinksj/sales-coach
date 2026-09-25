"""Phase 6: daily model budgets and real costs.

A run past the user's or the org's daily cap is recorded as budget_deferred and the event is deferred
without spending an attempt; under the cap it proceeds. The two caps are independent. The HTTP providers
price a call from the usage counts the API returns and the table in config/models.yaml; an unknown
model costs None.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import BaseModel

from salescoach import budget, config, identity, providers, repo, users
from salescoach.agents.base import Agent, AgentFailed
from salescoach.orchestrator import bus, worker
from salescoach.providers import pricing
from salescoach.providers.http_chat import RESULT_TOOL, AnthropicProvider, OpenAICompatProvider
from salescoach.sources import paste
from salescoach.store.stores import now


class Verdict(BaseModel):
    label: str


class Counting:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def extract_structured(self, **kw):
        self.calls += 1
        from salescoach.providers.base import LLMResult
        return LLMResult(output=Verdict(label="ok"), raw_text='{"label":"ok"}', provider=self.name, model="m",
                         duration_ms=1, cost_usd=0.5)


class Quick(Agent):
    name = "quality"
    schema = Verdict

    def build_prompt(self, ctx):
        return "judge"


def _spend(conn, owner, usd, when=None):
    conn.execute("INSERT INTO agent_runs(agent,status,started_at,cost_usd,owner_id) VALUES ('x','ok',?,?,?)",
                 (when or now(), usd, owner))
    conn.commit()


# ---- caps ----------------------------------------------------------------------------------------------

def test_no_cap_means_no_check_and_no_query(db, monkeypatch):
    monkeypatch.delenv(budget.USER_ENV, raising=False)
    monkeypatch.delenv(budget.ORG_ENV, raising=False)
    assert budget.caps() == {"user": None, "org": None}
    _spend(db, "local", 1000)
    budget.check(db)                                             # nothing to enforce


def test_caps_come_from_env_then_settings(db, monkeypatch, seller_settings):
    monkeypatch.delenv(budget.USER_ENV, raising=False)
    monkeypatch.delenv(budget.ORG_ENV, raising=False)
    config.save_user("budget", {"llm": {"user_usd_day": 3, "org_usd_day": 0}})
    assert budget.caps() == {"user": 3.0, "org": None}
    monkeypatch.setenv(budget.USER_ENV, "7.5")
    monkeypatch.setenv(budget.ORG_ENV, "not a number")
    assert budget.caps() == {"user": 7.5, "org": None}


def test_today_is_the_owners_day(db, monkeypatch):
    since = budget.day_start_utc()
    assert since.endswith("+00:00") and since <= now()
    # a run just before the owner's midnight is yesterday's
    _spend(db, "local", 2.0, when=(datetime.fromisoformat(since) - timedelta(seconds=1)).isoformat(timespec="seconds"))
    _spend(db, "local", 1.5)
    assert budget.spent_today(db, "local") == 1.5
    assert budget.spent_today(db) == 1.5


def test_user_cap_defers_the_run_without_calling_the_provider(db, monkeypatch, fake_llm):
    monkeypatch.setenv(budget.USER_ENV, "1.00")
    monkeypatch.delenv(budget.ORG_ENV, raising=False)
    counting = Counting()
    providers.set_override(counting)
    try:
        call = paste.import_text(db, "Me: hello.\nThem: hi.", "Budget call")
        _spend(db, "local", 1.00)
        with pytest.raises(AgentFailed) as caught:
            Quick().run(db, {"call_id": call})
        assert caught.value.rate_limited and "user model budget" in str(caught.value)
        assert counting.calls == 0
        run = db.execute("SELECT status, error, cost_usd FROM agent_runs WHERE agent='quality'").fetchone()
        assert run["status"] == "error" and run["error"].startswith("budget_deferred: the user model budget")
        assert run["cost_usd"] is None
    finally:
        providers.clear_override()


def test_worker_defers_a_budgeted_event_without_spending_an_attempt(db, monkeypatch):
    monkeypatch.setenv(budget.USER_ENV, "1.00")
    counting = Counting()
    providers.set_override(counting)
    try:
        call = paste.import_text(db, "Me: hello there.\nThem: hi, send the deck.", "Budget call")
        _spend(db, "local", 1.00)
        worker.drain(db)
    finally:
        providers.clear_override()
    ev = db.execute("SELECT status, attempts, error FROM wf_events WHERE entity_id=?", (call,)).fetchone()
    assert ev["status"] == "pending" and ev["attempts"] == 0 and "budget" in ev["error"]
    assert bus.claim_next(db) is None                           # parked until later, not retried hot
    assert counting.calls == 0
    assert repo.get_call(db, call)["wf_state"] == "diarized"


def test_under_the_cap_the_run_proceeds_and_is_costed(db, monkeypatch):
    monkeypatch.setenv(budget.USER_ENV, "1.00")
    monkeypatch.setenv(budget.ORG_ENV, "5.00")
    counting = Counting()
    providers.set_override(counting)
    try:
        call = paste.import_text(db, "Me: hello.\nThem: hi.", "Cheap call")
        _spend(db, "local", 0.60)
        output, run_id, *_ = Quick().run(db, {"call_id": call})
        assert output.label == "ok" and counting.calls == 1
        assert db.execute("SELECT cost_usd FROM agent_runs WHERE id=?", (run_id,)).fetchone()[0] == 0.5
        assert budget.spent_today(db, "local") == 1.1
        with pytest.raises(AgentFailed):                        # 0.6 + 0.5 crossed the user cap
            Quick().run(db, {"call_id": call})
    finally:
        providers.clear_override()


def test_org_cap_is_independent_of_the_user_cap(db, monkeypatch, dialect):
    monkeypatch.setenv(budget.USER_ENV, "10.00")
    monkeypatch.setenv(budget.ORG_ENV, "2.00")
    counting = Counting()
    providers.set_override(counting)
    try:
        call = paste.import_text(db, "Me: hello.\nThem: hi.", "Org call")
        if dialect == "postgres":        # a real rep, whose run only they may write (and no one else may read)
            users.create(db, "else@tessel.test", "Someone Else", role="rep", user_id="u-someone-else")
            db.commit()
            with identity.as_actor(db, identity.Actor("u-someone-else", role="rep")):
                _spend(db, "u-someone-else", 1.99)
            assert db.execute("SELECT COUNT(*) FROM agent_runs WHERE owner_id='u-someone-else'").fetchone()[0] == 0
        else:
            _spend(db, "u-someone-else", 1.99)                 # another rep's spend counts for the org
        Quick().run(db, {"call_id": call})                     # 1.99 < 2: allowed, costs 0.5
        with pytest.raises(AgentFailed) as caught:
            Quick().run(db, {"call_id": call})
        assert "org model budget" in str(caught.value) and counting.calls == 1
        assert budget.spent_today(db, "local") == 0.5 < 10     # the user cap alone would have let it through
    finally:
        providers.clear_override()


def test_usage_panel_numbers(db, monkeypatch):
    monkeypatch.setenv(budget.USER_ENV, "4.00")
    _spend(db, "local", 1.25)
    _spend(db, "local", 0.75)
    db.execute("INSERT INTO agent_runs(agent,status,started_at,cost_usd,owner_id,error) "
               "VALUES ('x','error',?,NULL,'local','budget_deferred: nope')", (now(),))
    db.execute("INSERT INTO agent_runs(agent,status,started_at,cost_usd,owner_id) VALUES ('x','ok',?,NULL,'local')", (now(),))
    db.commit()
    usage = budget.usage_today(db)
    assert usage["org"] == 2.0 and usage["mine"]["spent"] == 2.0 and usage["mine"]["deferred"] == 1
    assert usage["unpriced"] == 1 and usage["caps"] == {"user": 4.0, "org": None}
    assert usage["users"][0]["user_id"] == "local" and usage["users"][0]["name"] == "Maya Iyer"
    assert budget.usage_today(db, mine_only=True)["users"] == []


def test_usage_panels_render(db, monkeypatch):
    from fastapi.testclient import TestClient
    from salescoach.web.app import create_app
    monkeypatch.setenv(budget.ORG_ENV, "9.00")
    _spend(db, "local", 3.5)
    client = TestClient(create_app(start_worker=False, live_factory=None, hub=None))
    page = client.get("/setup/model").text
    assert "Model spend today" in page and "$3.50" in page and "of $9.00 cap" in page
    with identity.activate(identity.Actor("u-x", profile={"name": "X", "email": "x@t.test"})):
        pass


# ---- pricing ----------------------------------------------------------------------------------------------

def test_price_table_matches_exactly_or_by_dash_prefix(seller_settings):
    config.save_user("models", {"prices": {"claude-sonnet-5": {"price_per_mtok_in": 2, "price_per_mtok_out": 10},
                                           "claude-sonnet-5-mini": {"price_per_mtok_in": 1, "price_per_mtok_out": 4}}})
    assert pricing.price_for("claude-sonnet-5") == (2.0, 10.0)
    assert pricing.price_for("claude-sonnet-5-20260401") == (2.0, 10.0)
    assert pricing.price_for("claude-sonnet-5-mini-2") == (1.0, 4.0)       # the longest prefix wins
    assert pricing.price_for("claude-sonnet-50") is None
    assert pricing.price_for("gpt-echo") is None and pricing.price_for("") is None
    assert pricing.cost_usd("claude-sonnet-5", 1_000_000, 100_000) == 3.0
    assert pricing.cost_usd("claude-sonnet-5", None, 5) is None
    assert pricing.cost_usd("gpt-echo", 10, 5) is None


def test_shipped_table_prices_the_shipped_tiers():
    tracked = config.load("models")
    for model in tracked["providers"]["anthropic"]["tiers"].values():
        assert pricing.price_for(model) is not None, model


def _anthropic(payload, model, usage):
    return httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t1", "name": RESULT_TOOL, "input": payload}], "usage": usage})


def _openai(content, model, usage):
    body = {"id": "c1", "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                                     "message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def test_anthropic_cost_from_usage(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-x")
    responses = iter([_anthropic({"label": "a"}, "claude-sonnet-5-20260401", {"input_tokens": 1_000_000, "output_tokens": 100_000}),
                      _anthropic({"label": "b"}, "claude-unknown-9", {"input_tokens": 10, "output_tokens": 5}),
                      _anthropic({"label": "c"}, "claude-sonnet-5", {})])
    provider = AnthropicProvider(transport=httpx.MockTransport(lambda req: next(responses)))
    r = provider.extract_structured(system="s", prompt="p", schema=Verdict, model="claude-sonnet-5")
    assert (r.model, r.cost_usd) == ("claude-sonnet-5-20260401", 3.0)
    assert provider.extract_structured(system="s", prompt="p", schema=Verdict, model="claude-sonnet-5").cost_usd is None
    assert provider.extract_structured(system="s", prompt="p", schema=Verdict, model="claude-sonnet-5").cost_usd is None


def test_openai_cost_from_usage(monkeypatch, seller_settings):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-x")
    config.save_user("models", {"prices": {"gpt-echo": {"price_per_mtok_in": 1.0, "price_per_mtok_out": 2.0}}})
    responses = iter([_openai('{"label":"a"}', "gpt-echo", {"prompt_tokens": 500_000, "completion_tokens": 250_000}),
                      _openai('{"label":"b"}', "gpt-echo", None),
                      _openai('{"label":"c"}', "other-model", {"prompt_tokens": 1, "completion_tokens": 1})])
    provider = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY", name="openai",
                                    transport=httpx.MockTransport(lambda req: next(responses)))
    assert provider.extract_structured(system="s", prompt="p", schema=Verdict, model="gpt-echo").cost_usd == 1.0
    assert provider.extract_structured(system="s", prompt="p", schema=Verdict, model="gpt-echo").cost_usd is None
    assert provider.extract_structured(system="s", prompt="p", schema=Verdict, model="gpt-echo").cost_usd is None


def test_claude_code_provider_still_reports_the_clis_figure_only():
    from salescoach.providers import claude_code
    import inspect
    assert "pricing" not in inspect.getsource(claude_code)
