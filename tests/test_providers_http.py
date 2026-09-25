"""Phase B: bring-your-own-key providers, tiers, and the functions Setup calls.

No network, ever: every request goes to an httpx.MockTransport. The key used here is a
made-up string, planted in the environment, and hunted for in everything that comes back.
"""
import inspect
import json
import logging
import shutil
from typing import Optional

import httpx
import pytest
import yaml
from pydantic import BaseModel, Field

from salescoach import config, providers
from salescoach.agents import actions, call_analyst, email_drafter, quality, summary
from salescoach.agents.base import Agent
from salescoach.automation import followup, replies
from salescoach.coach import slow_pass
from salescoach.intel import agentkit, coach, prep, reconcile, strategist
from salescoach.providers import setup as provider_setup
from salescoach.providers.base import ProviderError, RateLimited, SchemaViolation, json_schema_for
from salescoach.providers.http_chat import (RESULT_TOOL, AnthropicProvider, OpenAICompatProvider,
                                            check_base_url, strict_schema)

SECRET = "sk-test-DO-NOT-LEAK-7f3a9c2e"
TRACKED = config.ROOT / "config" / "models.yaml"
AGENT_MODULES = [quality, summary, call_analyst, actions, email_drafter, coach, prep, reconcile, strategist,
                 slow_pass, followup, replies]
INTEL = {"deal_strategist", "assessment_reconciler", "longitudinal_coach", "prep_writer"}


def agent_schemas() -> dict:
    found = {}
    for module in AGENT_MODULES:
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, Agent) and cls.schema is not None:
                found[cls.name] = cls.schema
    return found


class Verdict(BaseModel):
    label: str
    score: int = Field(0, ge=0, le=10)
    note: Optional[str] = None


class Recorder:
    """A MockTransport handler that replays scripted responses and keeps the requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt(request) if callable(nxt) else nxt

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def anthropic_ok(payload, model="claude-sonnet-5-echo", stop="tool_use"):
    return httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "stop_reason": stop,
        "content": [{"type": "text", "text": "Recording."},
                    {"type": "tool_use", "id": "toolu_1", "name": RESULT_TOOL, "input": payload}],
        "usage": {"input_tokens": 10, "output_tokens": 5}})


def openai_ok(content, model="gpt-echo", finish="stop"):
    return httpx.Response(200, json={"id": "c1", "model": model, "choices": [
        {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}]})


def api_error(status, message="nope", headers=None):
    return httpx.Response(status, json={"error": {"type": "x", "message": message}}, headers=headers)


@pytest.fixture(autouse=True)
def keys(monkeypatch, tmp_path):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.setenv(env, SECRET)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "absent-secrets.env")     # never a real secrets file
    monkeypatch.delenv("SALESCOACH_SETTINGS", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    providers._INSTANCES.clear()
    yield
    providers._INSTANCES.clear()


@pytest.fixture
def models_yaml(tmp_path, monkeypatch):
    """Write a models.yaml (the tracked one with changes on top) into a private config dir."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)

    def write(changes=None, raw=None):
        data = raw if raw is not None else _merge(yaml.safe_load(TRACKED.read_text()), changes or {})
        (cfg_dir / "models.yaml").write_text(yaml.safe_dump(data))
        return data
    return write


def _merge(base, over):
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def openai(rec, **kw):
    kw.setdefault("base_url", "https://api.openai.com/v1")
    kw.setdefault("api_key_env", "OPENAI_API_KEY")
    return OpenAICompatProvider(name="openai", label="OpenAI", transport=rec.transport, **kw)


# ---------------------------------------------------------------- Anthropic

def test_anthropic_happy_path_forces_the_tool_and_parses_its_input():
    rec = Recorder(anthropic_ok({"label": "hot", "score": 7, "note": None}))
    result = AnthropicProvider(transport=rec.transport).extract_structured(
        system="You judge.", prompt="Judge this.", schema=Verdict, model="claude-sonnet-5", effort="low")
    req, body = rec.requests[0], rec.body()
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == SECRET and req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req.headers
    assert body["model"] == "claude-sonnet-5" and body["max_tokens"] >= 16000
    assert body["system"] == "You judge." and body["messages"] == [{"role": "user", "content": "Judge this."}]
    assert body["tools"] == [{"name": RESULT_TOOL, "description": body["tools"][0]["description"],
                              "input_schema": json_schema_for(Verdict)}]
    assert body["tool_choice"] == {"type": "tool", "name": RESULT_TOOL}
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body and "thinking" not in body
    assert result.output == Verdict(label="hot", score=7)
    assert json.loads(result.raw_text) == {"label": "hot", "score": 7, "note": None}
    # usage 10 in / 5 out at the shipped claude-sonnet-5 price (the -echo suffix matches by dash-prefix)
    assert (result.provider, result.model, result.cost_usd) == ("anthropic", "claude-sonnet-5-echo", 7e-05)
    assert isinstance(result.duration_ms, int)


def test_anthropic_large_contract_fits_in_one_request():
    schema = agent_schemas()["deal_strategist"]
    rec = Recorder(anthropic_ok({}))
    with pytest.raises(SchemaViolation):
        AnthropicProvider(transport=rec.transport).extract_structured(
            system="s", prompt="p", schema=schema, model="claude-opus-5")
    assert rec.body()["tools"][0]["input_schema"] == json_schema_for(schema)
    assert "output_config" not in rec.body()               # no effort asked for, none sent


def test_anthropic_model_that_refuses_forced_tool_use_is_asked_with_auto_and_remembered():
    refusal = api_error(400, 'tool_choice: type "tool" and "any" are not supported for this model.')
    rec = Recorder(refusal, anthropic_ok({"label": "a"}), anthropic_ok({"label": "b"}))
    provider = AnthropicProvider(transport=rec.transport)
    first = provider.extract_structured(system="s", prompt="p", schema=Verdict, model="claude-fable-5-1")
    second = provider.extract_structured(system="s", prompt="p", schema=Verdict, model="claude-fable-5-1")
    assert (first.output.label, second.output.label) == ("a", "b") and len(rec.requests) == 3
    assert rec.body(1)["tool_choice"] == {"type": "auto"} == rec.body(2)["tool_choice"]
    assert RESULT_TOOL in rec.body(1)["messages"][0]["content"]


def test_anthropic_model_that_refuses_effort_is_asked_without_and_remembered():
    rec = Recorder(api_error(400, "output_config.effort: not supported on this model"),
                   anthropic_ok({"label": "a"}), anthropic_ok({"label": "b"}))
    provider = AnthropicProvider(transport=rec.transport)
    for _ in range(2):
        provider.extract_structured(system="s", prompt="p", schema=Verdict, model="claude-haiku-4-5", effort="low")
    assert "output_config" in rec.body(0) and "output_config" not in rec.body(1)
    assert "output_config" not in rec.body(2) and len(rec.requests) == 3


def test_anthropic_other_400_is_a_provider_error_not_a_retry():
    rec = Recorder(api_error(400, "prompt is too long"))
    with pytest.raises(ProviderError, match="prompt is too long"):
        AnthropicProvider(transport=rec.transport).extract_structured(
            system="s", prompt="p", schema=Verdict, model="claude-sonnet-5", effort="low")
    assert len(rec.requests) == 1


def test_anthropic_missing_tool_call_truncation_refusal_and_bad_input():
    text_only = httpx.Response(200, json={"model": "m", "stop_reason": "end_turn",
                                          "content": [{"type": "text", "text": "I would rather chat."}]})
    provider = AnthropicProvider(transport=Recorder(text_only).transport)
    with pytest.raises(SchemaViolation) as exc:
        provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m")
    assert exc.value.raw == "I would rather chat."

    provider = AnthropicProvider(transport=Recorder(anthropic_ok({"label": "x"}, stop="max_tokens")).transport)
    with pytest.raises(SchemaViolation, match="cut off"):
        provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m")

    provider = AnthropicProvider(transport=Recorder(anthropic_ok({"score": "many"})).transport)
    with pytest.raises(SchemaViolation) as exc:
        provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m")
    assert json.loads(exc.value.raw) == {"score": "many"}

    refused = httpx.Response(200, json={"model": "m", "stop_reason": "refusal", "content": []})
    with pytest.raises(ProviderError, match="declined") as exc:
        AnthropicProvider(transport=Recorder(refused).transport).extract_structured(
            system="s", prompt="p", schema=Verdict, model="m")
    assert not isinstance(exc.value, SchemaViolation)


def test_anthropic_generate_returns_the_text_and_sends_no_tools():
    rec = Recorder(httpx.Response(200, json={"model": "m", "stop_reason": "end_turn", "content": [
        {"type": "thinking", "thinking": ""}, {"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]}))
    assert AnthropicProvider(transport=rec.transport).generate(system="s", prompt="p", model="m") == "Hello there"
    assert "tools" not in rec.body() and "tool_choice" not in rec.body()


# ---------------------------------------------------------------- OpenAI-compatible

def test_openai_happy_path_uses_strict_json_schema_and_a_bearer_key():
    rec = Recorder(openai_ok('{"label": "hot", "score": 3, "note": "n"}'))
    result = openai(rec).extract_structured(system="You judge.", prompt="Judge.", schema=Verdict,
                                            model="gpt-x", effort="low")
    req, body = rec.requests[0], rec.body()
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {SECRET}" and "x-api-key" not in req.headers
    assert body["model"] == "gpt-x"
    assert body["messages"] == [{"role": "system", "content": "You judge."}, {"role": "user", "content": "Judge."}]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "Verdict"
    assert fmt["json_schema"]["schema"] == strict_schema(json_schema_for(Verdict))
    assert not {"temperature", "max_tokens", "reasoning_effort"} & set(body)     # effort is ignored here
    assert result.output == Verdict(label="hot", score=3, note="n")
    assert result.raw_text == '{"label": "hot", "score": 3, "note": "n"}'
    assert (result.provider, result.model, result.cost_usd) == ("openai", "gpt-echo", None)


def test_strict_schema_transform_on_a_small_model():
    out = strict_schema(json_schema_for(Verdict))
    assert out["required"] == ["label", "score", "note"] and out["additionalProperties"] is False
    assert "default" not in out["properties"]["score"] and "default" not in out["properties"]["note"]
    assert out["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]   # nullable stays
    assert json_schema_for(Verdict)["properties"]["score"]["default"] == 0                 # input untouched


class HasDefaultField(BaseModel):
    default: str = "x"          # a PROPERTY named like the keyword must survive


def test_strict_schema_keeps_a_property_that_is_called_default():
    out = strict_schema(json_schema_for(HasDefaultField))
    assert list(out["properties"]) == ["default"] == out["required"]
    assert "default" not in out["properties"]["default"]


@pytest.mark.parametrize("agent", sorted(agent_schemas()))
def test_strict_schema_on_every_real_agent_contract(agent):
    out = strict_schema(json_schema_for(agent_schemas()[agent]))
    objects = []

    def walk(node, is_property_map=False):
        if isinstance(node, dict):
            if not is_property_map:
                assert "default" not in node, f"{agent}: a default survived"
                assert "$ref" not in node
                if "properties" in node or node.get("type") == "object":
                    objects.append(node)
            for key, value in node.items():
                walk(value, is_property_map=(key == "properties" and not is_property_map))
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(out)
    assert objects
    for obj in objects:
        assert obj["required"] == list(obj["properties"]), f"{agent}: not every property is required"
        assert obj["additionalProperties"] is False


def test_there_are_thirteen_agent_contracts():
    assert len(agent_schemas()) == 13


def test_json_schema_400_falls_back_to_json_object_and_is_remembered():
    rec = Recorder(api_error(400, "response_format json_schema is not supported"),
                   openai_ok('```json\n{"label": "a"}\n```'), openai_ok('{"label": "b"}'))
    provider = openai(rec)
    first = provider.extract_structured(system="Sys.", prompt="p", schema=Verdict, model="grok-x")
    second = provider.extract_structured(system="Sys.", prompt="p", schema=Verdict, model="grok-x")
    assert (first.output.label, second.output.label) == ("a", "b")
    assert len(rec.requests) == 3                               # the refused shape is not tried again
    assert rec.body(0)["response_format"]["type"] == "json_schema"
    for i in (1, 2):
        assert rec.body(i)["response_format"] == {"type": "json_object"}
        system = rec.body(i)["messages"][0]["content"]
        assert system.startswith("Sys.") and json.dumps(json_schema_for(Verdict)) in system


def test_a_400_about_something_else_is_not_remembered_as_a_schema_refusal():
    rec = Recorder(api_error(400, "context length exceeded"), api_error(400, "context length exceeded"),
                   openai_ok('{"label": "ok"}'))
    provider = openai(rec)
    with pytest.raises(ProviderError, match="context length"):
        provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m")
    provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m")
    assert rec.body(2)["response_format"]["type"] == "json_schema"


def test_structured_json_object_is_used_from_the_start_when_configured():
    rec = Recorder(openai_ok('{"label": "a"}'))
    openai(rec, structured="json_object").extract_structured(system="s", prompt="p", schema=Verdict, model="m")
    assert rec.body()["response_format"] == {"type": "json_object"} and len(rec.requests) == 1


def test_garbage_output_is_a_schema_violation_carrying_the_raw_text():
    for content in ("Sure! Here you go: label=hot", '{"score": "many"}'):
        with pytest.raises(SchemaViolation) as exc:
            openai(Recorder(openai_ok(content))).extract_structured(system="s", prompt="p", schema=Verdict, model="m")
        assert exc.value.raw == content
    with pytest.raises(SchemaViolation, match="cut off"):
        openai(Recorder(openai_ok('{"label": "ho', finish="length"))).extract_structured(
            system="s", prompt="p", schema=Verdict, model="m")
    with pytest.raises(SchemaViolation, match="empty"):
        openai(Recorder(openai_ok(None))).extract_structured(system="s", prompt="p", schema=Verdict, model="m")


def test_openai_generate_and_a_keyless_local_endpoint(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY")
    rec = Recorder(openai_ok("plain text"))
    local = OpenAICompatProvider(base_url="http://localhost:1234/v1", key_required=False, transport=rec.transport)
    assert local.available() and local.generate(system="s", prompt="p", model="m") == "plain text"
    assert "authorization" not in rec.requests[0].headers and "response_format" not in rec.body()


# ---------------------------------------------------------------- errors, timeouts, secrets

@pytest.mark.parametrize("make", [lambda rec: AnthropicProvider(transport=rec.transport), openai])
def test_error_mapping(make):
    def call(*responses):
        rec = Recorder(*responses)
        with pytest.raises(ProviderError) as exc:
            make(rec).extract_structured(system="s", prompt="p", schema=Verdict, model="m")
        assert SECRET not in str(exc.value) and SECRET not in repr(exc.value)
        assert exc.value.__cause__ is None                                # no httpx request chained on
        assert exc.value.__context__ is None or exc.value.__suppress_context__
        return exc.value, rec

    limited, _ = call(api_error(429, "slow down", headers={"retry-after": "12"}))
    assert isinstance(limited, RateLimited) and "12" in str(limited)
    for status in (401, 403):
        rejected, rec = call(api_error(status, f"Incorrect API key provided: {SECRET}"))
        assert "the API key was rejected" in str(rejected) and not isinstance(rejected, RateLimited)
        assert len(rec.requests) == 1
    for status in (500, 503, 529):
        assert f"HTTP {status}" in str(call(api_error(status, "boom"))[0])
    assert "did not answer within" in str(call(httpx.ReadTimeout("slow"))[0])
    assert "could not reach" in str(call(httpx.ConnectError("refused"))[0])
    assert "redirect" in str(call(httpx.Response(302, headers={"location": "https://evil.example/"}))[0])
    assert "not JSON" in str(call(httpx.Response(200, text="<html>hi</html>"))[0])
    echoed, _ = call(api_error(422, f"bad header {SECRET} and sk-another-looking-key-123456"))
    assert "[redacted]" in str(echoed) and "sk-another" not in str(echoed)


def test_redirects_are_never_followed():
    rec = Recorder(httpx.Response(307, headers={"location": "https://evil.example/v1/chat/completions"}))
    with pytest.raises(ProviderError):
        openai(rec).generate(system="s", prompt="p", model="m")
    assert len(rec.requests) == 1


@pytest.mark.parametrize("make", [lambda rec: AnthropicProvider(transport=rec.transport, timeout_s=600),
                                  lambda rec: openai(rec, timeout_s=600)])
def test_per_call_timeout_is_honoured(make):
    rec = Recorder(lambda request: anthropic_ok({"label": "a"}) if "anthropic" in request.url.host
                   else openai_ok('{"label": "a"}'))
    provider = make(rec)
    provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m", timeout=40)
    provider.extract_structured(system="s", prompt="p", schema=Verdict, model="m")
    live, default = (r.extensions["timeout"] for r in rec.requests)
    assert live["read"] == 40 and live["write"] == 40 and live["connect"] == 10
    assert default["read"] == 600 and default["connect"] == 10


def test_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(config, "SECRETS_FILE", config.DATA_DIR / "absent-secrets.env")
    rec = Recorder(openai_ok("{}"))
    provider = openai(rec)
    assert not provider.available()
    with pytest.raises(ProviderError, match="no API key set for OpenAI"):
        provider.generate(system="s", prompt="p", model="m")
    assert rec.requests == []


def test_the_key_appears_nowhere(caplog):
    caplog.set_level(logging.DEBUG)
    a_rec, o_rec = Recorder(anthropic_ok({"label": "a"})), Recorder(openai_ok('{"label": "a"}'))
    anthropic, oai = AnthropicProvider(transport=a_rec.transport), openai(o_rec)
    results = [anthropic.extract_structured(system="s", prompt="p", schema=Verdict, model="m"),
               oai.extract_structured(system="s", prompt="p", schema=Verdict, model="m")]
    with pytest.raises(ProviderError):
        openai(Recorder(api_error(401, SECRET))).generate(system="s", prompt="p", model="m")
    for thing in (anthropic, oai, *results):
        assert SECRET not in repr(thing) and SECRET not in str(vars(thing))
    assert "key OPENAI_API_KEY: set" in repr(oai)
    assert SECRET not in caplog.text


def test_base_url_rules():
    assert check_base_url("https://llm.example.com/v1/") == "https://llm.example.com/v1"
    assert check_base_url("http://localhost:1234/v1") and check_base_url("http://192.168.1.20:8000/v1")
    for bad in ("", "llm.example.com/v1", "ftp://x/v1", "http://llm.example.com/v1", "https://user:pw@x.example/v1"):
        with pytest.raises(ProviderError):
            check_base_url(bad)


# ---------------------------------------------------------------- tiers and routing

def test_the_tracked_models_yaml_has_the_tier_shape_and_lists_every_agent():
    cfg = yaml.safe_load(TRACKED.read_text())
    assert cfg["provider"] == "claude_code"
    assert set(cfg["providers"]) == {"claude_code", "anthropic", "openai", "xai", "openai_compatible", "ollama"}
    assert cfg["providers"]["anthropic"]["tiers"] == {"heavy": "claude-opus-5", "light": "claude-sonnet-5"}
    for empty in ("openai", "xai", "openai_compatible"):            # no invented model ids
        assert cfg["providers"][empty]["tiers"] == {}
    assert set(cfg["agents"]) == set(agent_schemas())
    for spec in cfg["agents"].values():
        assert spec["tier"] in ("heavy", "light") and "model" not in spec and "provider" not in spec
    assert "agents" not in yaml.safe_load((config.ROOT / "config" / "intel.yaml").read_text())
    assert "sk-" not in TRACKED.read_text()


def test_the_default_install_routes_exactly_as_before(models_yaml):
    models_yaml()
    before = {"quality": ("sonnet", "low"), "summary": ("sonnet", "low"), "call_analyst": ("opus", "medium"),
              "actions": ("opus", "medium"), "email": ("opus", None), "live_coach": ("sonnet", "low"),
              "followup": ("sonnet", None), "nudge": ("sonnet", None), "reply_analysis": ("sonnet", None),
              "deal_strategist": ("opus", "medium"), "assessment_reconciler": ("sonnet", "low"),
              "longitudinal_coach": ("sonnet", "medium"), "prep_writer": ("sonnet", "low")}
    for agent, (model, effort) in before.items():
        provider, got_model, got_effort = agentkit.route(agent) if agent in INTEL else providers.route(agent)
        assert (provider.name, got_model, got_effort) == ("claude_code", model, effort), agent


@pytest.mark.parametrize("key", ["claude_code", "anthropic", "openai", "xai", "openai_compatible", "ollama"])
def test_every_agent_follows_its_tier_under_every_provider(models_yaml, key):
    cfg = models_yaml({"provider": key, "providers": _merge(
        {"openai_compatible": {"base_url": "https://llm.example.com/v1"}},
        {key: {"tiers": {"heavy": f"{key}-big", "light": f"{key}-small"}}})})
    for agent, spec in cfg["agents"].items():
        provider, model, effort = agentkit.route(agent) if agent in INTEL else providers.route(agent)
        assert provider.name == key, agent
        assert model == f"{key}-{'big' if spec['tier'] == 'heavy' else 'small'}", agent
        assert effort == spec.get("effort")
    assert providers.route("a_plugin_agent_nobody_listed")[1] == f"{key}-small"


def test_http_providers_are_reused_so_they_remember_what_an_endpoint_refused(models_yaml):
    models_yaml({"provider": "xai", "providers": {"xai": {"tiers": {"heavy": "g", "light": "g"}}}})
    assert providers.route("quality")[0] is providers.route("call_analyst")[0]


def test_explicit_model_beats_the_tier_and_an_agent_can_pin_a_provider(models_yaml):
    models_yaml({"provider": "anthropic", "agents": {
        "email": {"tier": "heavy", "model": "claude-haiku-4-5"},
        "summary": {"provider": "ollama", "model": "llama3.2:3b", "effort": "low"},
        "quality": {"provider": "ollama", "tier": "heavy"}}})
    assert providers.route("email")[1:] == ("claude-haiku-4-5", None)
    assert providers.route("email")[0].name == "anthropic"
    provider, model, effort = providers.route("summary")
    assert (provider.name, model, effort) == ("ollama", "llama3.2:3b", "low")
    assert (providers.route("quality")[0].name, providers.route("quality")[1]) == ("ollama", "qwen2.5:14b")


def test_a_tier_without_a_model_fails_clearly_and_sends_nothing(models_yaml, db):
    models_yaml({"provider": "openai"})
    provider, model, _ = providers.route("call_analyst")
    assert model == "" and not provider.available() and provider.name == "openai"
    for call in (lambda: provider.extract_structured(system="s", prompt="p", schema=Verdict, model=model),
                 lambda: provider.generate(system="s", prompt="p", model=model)):
        with pytest.raises(ProviderError, match="no model chosen for the heavy tier of openai; pick one in Setup"):
            call()
    with pytest.raises(ProviderError, match="light tier of openai"):
        providers.route("quality")[0].generate(system="s", prompt="p", model="")

    class Judge(Agent):                     # and through the agent loop it is an ordinary failed run
        name, schema = "call_analyst", Verdict
        def system_prompt(self, ctx): return "s"
        def build_prompt(self, ctx): return "p"
    from salescoach.agents.base import AgentFailed
    with pytest.raises(AgentFailed, match="no model chosen"):
        Judge().run(db, {})
    row = db.execute("SELECT provider, status, error FROM agent_runs").fetchone()
    assert (row["provider"], row["status"]) == ("openai", "error") and "pick one in Setup" in row["error"]


def test_an_unknown_provider_fails_on_use_not_in_route(models_yaml):
    models_yaml({"provider": "mystery", "agents": {"quality": {"model": "m"}}})
    provider, _, _ = providers.route("quality")
    with pytest.raises(ProviderError, match="unknown provider 'mystery'"):
        provider.generate(system="s", prompt="p", model="m")


LEGACY = """
default_provider: claude_code
agents:
  quality:      {provider: claude_code, model: sonnet, effort: low}
  call_analyst: {provider: claude_code, model: opus, effort: medium}
  summary:      {provider: ollama, model: "qwen2.5:14b"}
fallback: {provider: ollama, model: "qwen2.5:14b"}
claude_code:
  binary: /opt/legacy/claude
  timeout_s: 123
ollama:
  endpoint: http://localhost:9999
"""


def test_a_models_yaml_from_before_tiers_still_works(models_yaml, tmp_path):
    models_yaml(raw=yaml.safe_load(LEGACY))
    (tmp_path / "config" / "intel.yaml").write_text("agents:\n  deal_strategist: {model: opus, effort: medium}\n")
    provider, model, effort = providers.route("quality")
    assert (provider.name, model, effort) == ("claude_code", "sonnet", "low")
    assert (provider.binary, provider.timeout_s) == ("/opt/legacy/claude", 123)
    assert providers.route("call_analyst")[1:] == ("opus", "medium")
    local, model, _ = providers.route("summary")
    assert (local.name, local.endpoint, model) == ("ollama", "http://localhost:9999", "qwen2.5:14b")
    assert providers.route("followup")[1] == "sonnet"            # unlisted: the light tier's built-in default
    assert agentkit.route("deal_strategist")[1:] == ("opus", "medium")   # a legacy intel.yaml block
    assert agentkit.route("prep_writer")[1:] == ("sonnet", None)
    assert providers.claude_cli_binary() == "/opt/legacy/claude"


def test_the_fake_override_still_wins_over_any_configured_provider(models_yaml, fake_llm):
    models_yaml({"provider": "openai"})                                   # no tier models at all
    assert providers.route("call_analyst") == (fake_llm, "default", "medium")
    assert agentkit.route("deal_strategist") == (fake_llm, "default", "medium")
    models_yaml({"provider": "anthropic"})
    assert providers.route("call_analyst") == (fake_llm, "claude-opus-5", "medium")
    assert providers.fallback("call_analyst") is None


def test_claude_cli_available(models_yaml, tmp_path):
    binary = tmp_path / "claude"
    models_yaml({"providers": {"claude_code": {"binary": str(binary)}}})
    assert providers.claude_cli_available() is False
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert providers.claude_cli_available() is True
    assert shutil.which("definitely-not-a-real-binary-xyz") is None


# ---------------------------------------------------------------- what Setup calls

def test_catalog_describes_every_provider_without_the_key(models_yaml, monkeypatch):
    models_yaml({"provider": "xai"})
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(config, "SECRETS_FILE", config.DATA_DIR / "absent-secrets.env")
    rows = {d["key"]: d for d in providers.catalog()}
    assert list(rows) == ["claude_code", "anthropic", "openai", "xai", "openai_compatible", "ollama"]
    for row in rows.values():
        assert {"key", "label", "needs_key", "api_key_env", "base_url", "base_url_editable", "notes",
                "tiers", "key_set", "active", "available"} <= set(row)
    assert [k for k, d in rows.items() if d["active"]] == ["xai"]
    assert rows["xai"]["key_set"] is True and rows["openai"]["key_set"] is False
    assert rows["claude_code"]["needs_key"] is False and rows["claude_code"]["api_key_env"] is None
    assert [k for k, d in rows.items() if d["base_url_editable"]] == ["openai_compatible"]
    assert rows["anthropic"]["tiers"] == {"heavy": "claude-opus-5", "light": "claude-sonnet-5"}
    assert rows["openai"]["tiers"] == {"heavy": "", "light": ""}
    assert SECRET not in json.dumps(list(rows.values()))


def test_list_models_parses_both_apis(models_yaml):
    models_yaml()
    rec = Recorder(httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-b"}, {"id": "gpt-a"}, {"x": 1}]}))
    assert providers.list_models("openai", transport=rec.transport) == ["gpt-a", "gpt-b"]
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/models" and rec.requests[0].method == "GET"
    assert rec.requests[0].headers["authorization"] == f"Bearer {SECRET}"

    rec = Recorder(httpx.Response(200, json={"data": [{"id": "grok-z"}]}))
    assert providers.list_models("openai_compatible", {"base_url": "https://llm.example.com/v1"},
                                 transport=rec.transport) == ["grok-z"]
    assert str(rec.requests[0].url) == "https://llm.example.com/v1/models"

    rec = Recorder(httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}], "has_more": True, "last_id": "claude-sonnet-5"}),
                   httpx.Response(200, json={"data": [{"id": "claude-opus-5"}], "has_more": False, "last_id": "claude-opus-5"}))
    assert providers.list_models("anthropic", transport=rec.transport) == ["claude-opus-5", "claude-sonnet-5"]
    assert rec.requests[0].url.path == "/v1/models" and rec.requests[0].headers["x-api-key"] == SECRET
    assert rec.requests[1].url.params["after_id"] == "claude-sonnet-5"
    assert providers.list_models("claude_code") == ["haiku", "opus", "sonnet"]


def test_list_models_raises_provider_error_and_nothing_else(models_yaml):
    models_yaml()
    for response in (api_error(401, SECRET), httpx.Response(200, json={"nope": 1}), httpx.ConnectError("x"),
                     RuntimeError("anything at all")):
        with pytest.raises(ProviderError) as exc:
            providers.list_models("openai", transport=Recorder(response).transport)
        assert SECRET not in str(exc.value)
    with pytest.raises(ProviderError):
        providers.list_models("mystery")
    with pytest.raises(ProviderError, match="plain http"):
        providers.list_models("openai_compatible", {"base_url": "http://llm.example.com/v1"})
    rec = Recorder(httpx.Response(200, json={"data": []}))                 # a fixed base URL is not editable
    providers.list_models("openai", {"base_url": "https://evil.example/v1"}, transport=rec.transport)
    assert rec.requests[0].url.host == "api.openai.com"


def test_test_connection_shapes(models_yaml):
    models_yaml()
    rec = Recorder(anthropic_ok({"reply": "ok"}, model="claude-sonnet-5"))
    got = providers.test_connection("anthropic", transport=rec.transport)
    assert got["ok"] is True and got["model"] == "claude-sonnet-5" and got["error"] is None
    assert isinstance(got["latency_ms"], int) and set(got) == {"ok", "model", "latency_ms", "error"}
    assert rec.body()["model"] == "claude-sonnet-5" and len(rec.requests) == 1          # ONE call, light tier
    assert list(rec.body()["tools"][0]["input_schema"]["properties"]) == ["reply"]

    rec = Recorder(openai_ok('{"reply": "ok"}', model="gpt-mini-2026"))
    got = providers.test_connection("openai", {"tiers": {"light": "gpt-mini"}}, transport=rec.transport)
    assert (got["ok"], got["model"]) == (True, "gpt-mini-2026") and rec.body()["model"] == "gpt-mini"

    rec = Recorder(api_error(401, f"bad key {SECRET}"))
    got = providers.test_connection("xai", {"tiers": {"light": "grok"}}, transport=rec.transport)
    assert got["ok"] is False and got["model"] == "grok" and "the API key was rejected" in got["error"]
    assert SECRET not in json.dumps(got) and isinstance(got["latency_ms"], int)

    rec = Recorder(openai_ok("{}"))
    got = providers.test_connection("openai", transport=rec.transport)                    # nothing chosen yet
    assert got["ok"] is False and "no model chosen for the light tier of openai" in got["error"]
    assert rec.requests == []

    assert providers.test_connection("mystery")["ok"] is False
    got = providers.test_connection("openai", {"model": "m"}, transport=Recorder(RuntimeError(SECRET)).transport)
    assert got == {"ok": False, "model": "m", "latency_ms": got["latency_ms"], "error": "unexpected RuntimeError"}


def test_save_choice_writes_the_overlay_without_secrets(models_yaml):
    tracked = models_yaml()
    written = providers.save_choice(
        "openai_compatible",
        {"base_url": "https://llm.example.com/v1/", "api_key": SECRET, "api_key_env": "AWS_SECRET_ACCESS_KEY",
         "structured": "json_object", "anything_else": "x"},
        {"heavy": " big-model ", "light": "small-model"})
    path = config.user_dir() / "models.yaml"
    assert path == config.DATA_DIR / "settings" / "models.yaml"
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk == written == {"provider": "openai_compatible", "providers": {"openai_compatible": {
        "base_url": "https://llm.example.com/v1", "structured": "json_object",
        "tiers": {"heavy": "big-model", "light": "small-model"}}}}
    assert SECRET not in path.read_text() and "AWS" not in path.read_text()
    assert not list(path.parent.glob(".*tmp"))

    providers.save_choice("anthropic", None, {"light": "claude-haiku-4-5"})               # a second choice
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk["provider"] == "anthropic" and "openai_compatible" in on_disk["providers"]
    assert on_disk["providers"]["anthropic"] == {"tiers": {"light": "claude-haiku-4-5"}}

    models_yaml(raw=_merge(tracked, on_disk))             # what Phase A's overlay-aware load() will return
    assert providers.route("call_analyst")[1] == "claude-opus-5"                          # tracked heavy kept
    assert providers.route("quality")[1] == "claude-haiku-4-5"


def test_save_choice_refuses_what_it_should_not_store(models_yaml):
    models_yaml()
    for args in (("mystery", None, None), ("openai", None, {"medium": "m"}), ("openai", None, {"heavy": 7}),
                 ("openai", None, {"heavy": SECRET}),
                 ("openai_compatible", {"base_url": "http://llm.example.com/v1"}, None),
                 ("openai_compatible", {"base_url": f"https://u:{SECRET}@llm.example.com/v1"}, None)):
        with pytest.raises(ValueError) as exc:
            providers.save_choice(*args)
        assert SECRET not in str(exc.value)
    assert not (config.user_dir() / "models.yaml").exists()


def test_has_secret_and_user_dir(monkeypatch, tmp_path):
    assert config.has_secret("OPENAI_API_KEY") is True
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "absent.env")
    assert config.has_secret("SALESCOACH_NO_SUCH_KEY") is False
    monkeypatch.setenv("SALESCOACH_SETTINGS", str(tmp_path / "elsewhere"))
    assert config.user_dir() == tmp_path / "elsewhere"
    assert provider_setup.test_connection.__test__ is False
