"""The setup wizard / Settings (salescoach/setupui): six steps under /setup.

What is pinned here: the first-run gate still leads to /setup and only the PROFILE lifts it; every
step saves through the engine that owns the setting; an API key is write-only (it appears in no
body, header or redirect); the webhook secret is shown exactly once; the read-only step touches no
service; the progress rail is computed from the settings, not from visits; every POST passes the
same-origin guard. No network, no model: list_models / test_connection are monkeypatched.
"""
import json
import re
import stat

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import SELLER
from salescoach import config, providers, seller, sources
from salescoach.intel import methodology
from salescoach.setupui import forms, state
from salescoach.web.app import create_app
from test_p3_strategy import intel_env, run_call  # noqa: F401  (intel_env is a fixture run_call needs)

ORIGIN = {"origin": "http://127.0.0.1:8140"}
STEPS = ("you", "method", "model", "sources", "connections", "review")
FAKE_KEY = "sk-test-DISTINCTIVE-9f3a7c21e8b64d05"
PRIYA_FORM = {"name": "Priya Nair", "emails": "Priya@AcmeCloud.com, priya.nair@gmail.com", "company": "Acme Cloud",
              "offering": "HR software that replaces spreadsheets for companies with hourly staff",
              "role": "Account executive", "icp": "US mid-market companies", "buyer_titles": "HR directors and CFOs",
              "languages": "en, es", "timezone": "America/New_York", "signature": "Best,\r\nPriya",
              "own_domains": "acmecloud.com", "website": "www.acmecloud.com"}


@pytest.fixture
def client(db):
    return TestClient(create_app(start_worker=False, live_factory=None))


@pytest.fixture
def fresh(seller_settings):
    """An install nobody has set up yet."""
    (seller_settings / "seller.yaml").unlink()
    return seller_settings


@pytest.fixture
def no_cli(monkeypatch):
    monkeypatch.setattr(providers, "claude_cli_available", lambda: False)


def rail(html) -> dict:
    return dict(re.findall(r'data-step="(\w+)" data-status="(\w+)"', html))


def post(client, url, data=None, **kw):
    return client.post(url, data=data or {}, headers=ORIGIN, follow_redirects=False, **kw)


# =====================================================================================
# the gate and the frame
# =====================================================================================

def test_unconfigured_install_leads_to_the_wizard_and_every_step_renders(client, fresh):
    for path in ("/", "/loops", "/deals", "/coach", "/import", "/learning", "/calendar"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup", path
    first = client.get("/setup")
    assert first.status_code == 200 and 'name="offering"' in first.text and "Step 1 of 6" in first.text
    assert "Tell the coach who is selling" in first.text and "Tessel" not in first.text
    for step in STEPS:                                         # steps 2 to 6 are reachable before step 1 is done
        r = client.get(f"/setup/{step}", follow_redirects=False)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", step
        assert 'aria-current="step"' in r.text and "/static/setup.js" in r.text
    assert client.get("/setup/method/custom").status_code == 200
    assert client.get("/static/setup.css").status_code == 200 and client.get("/static/setup.js").status_code == 200


def test_settings_is_in_the_nav_and_setup_shows_the_review_once_configured(client):
    home = client.get("/").text
    assert '<a href="/setup" class="">Settings</a>' in home
    page = client.get("/setup", follow_redirects=False)
    assert page.status_code == 200 and "Review" in page.text and 'id="review-you"' in page.text
    assert '<a href="/setup" class="active">Settings</a>' in client.get("/setup/model").text


def test_every_setup_post_is_refused_without_an_origin(client, db):
    posts = ["/setup/you", "/setup/method", "/setup/method/custom", "/setup/method/custom/x",
             "/setup/method/custom/x/delete", "/setup/model/models", "/setup/model/test", "/setup/model/use",
             "/setup/sources/fireflies", "/setup/sources/webhook/secret", "/setup/sources/allowed", "/setup/finish",
             "/setup/dismiss-card"]
    for url in posts:
        assert client.post(url, data={"provider": "anthropic", "api_key": FAKE_KEY}).status_code == 403, url
        assert client.post(url, data={}, headers={"origin": "http://evil.example"}).status_code == 403, url
    assert not config.has_secret("ANTHROPIC_API_KEY") and not config.has_secret("WEBHOOK_SECRET")
    # and the list above is every POST the wizard has
    from salescoach.setupui.web import router
    declared = {r.path for r in router.routes if "POST" in r.methods}
    assert {re.sub(r"/x(?=/|$)", "/{key}", u) for u in posts} | {"/setup/method/custom/{editing}",
                                                                 "/setup/sources/{kind}"} >= declared


# =====================================================================================
# step 1: you and your org
# =====================================================================================

def test_saving_step_one_configures_the_profile_lifts_the_gate_and_reaches_the_prompts(client, db, fresh):
    assert rail(client.get("/setup").text)["you"] == "attention"
    r = post(client, "/setup/you", {**PRIYA_FORM, "go": "next"})
    assert r.status_code == 303 and r.headers["location"].startswith("/setup/method?msg=")
    assert seller.is_configured() and seller.emails() == ["priya@acmecloud.com", "priya.nair@gmail.com"]
    p = seller.profile()
    assert p["signature"] == "Best,\nPriya" and p["timezone"] == "America/New_York" and p["languages"] == ["en", "es"]
    assert seller.internal_domains() == {"acmecloud.com"}
    assert yaml.safe_load((fresh / "seller.yaml").read_text())["company"] == "Acme Cloud"
    assert not yaml.safe_load((config.CONFIG_DIR / "seller.yaml").read_text())["company"]     # tracked file untouched

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200 and "Acme Cloud" in home.text
    assert rail(client.get("/setup/you").text)["you"] == "done"

    from salescoach.agents.call_analyst import CallAnalystAgent
    prompt = CallAnalystAgent().system_prompt({})
    assert "Acme Cloud" in prompt and "Priya" in prompt and "Tessel" not in prompt and "{{" not in prompt


def test_step_one_saves_and_stays_when_asked(client):
    r = post(client, "/setup/you", {**PRIYA_FORM, "go": "stay"})
    assert r.headers["location"].startswith("/setup/you?msg=")


@pytest.mark.parametrize("change, message", [
    ({"emails": "not-an-address"}, "does not look like an email address"),
    ({"emails": "a@b"}, "does not look like an email address"),
    ({"own_domains": "acme cloud"}, "does not look like an email domain"),
    ({"own_domains": "acmecloud"}, "does not look like an email domain"),
    ({"timezone": "Mars/Base"}, "Mars/Base is not a timezone name"),
    ({"name": ""}, "Your name is needed"),
    ({"offering": ""}, "what you sell is needed"),
    ({"name": "Priya\r\nBcc: someone@evil.example"}, "cannot contain a line break"),
    ({"name": "Priya\nNair"}, "cannot contain a line break"),
    ({"company": "Acme {{company}}"}, "do not use {{"),
    ({"style": "Write like {{nobody_knows_this}}."}, "which the coach does not know"),
])
def test_step_one_rejects_bad_input_with_a_message_and_saves_nothing(client, seller_settings, change, message):
    before = (seller_settings / "seller.yaml").read_text()
    r = post(client, "/setup/you", {**PRIYA_FORM, **change})
    assert r.status_code == 400 and message in r.text and 'role="alert"' in r.text
    assert 'aria-invalid="true"' in r.text                                       # the message sits beside its field
    assert (seller_settings / "seller.yaml").read_text() == before and seller.name() == SELLER["name"]
    assert not (seller_settings / "style.md").exists()


def test_a_pasted_url_or_at_sign_is_read_as_the_domain():
    data, errors = forms.parse_profile({**PRIYA_FORM, "own_domains": "https://www.AcmeCloud.com/, @acme.io"})
    assert not errors and data["own_domains"] == ["www.acmecloud.com", "acme.io"]
    assert "Asia/Kolkata" in forms.timezones() and "UTC" in forms.timezones()
    assert not any(z.startswith(("posix/", "right/")) for z in forms.timezones())


def test_style_guide_is_saved_to_the_user_folder_and_used_by_the_email_drafter(client, seller_settings):
    page = client.get("/setup/you").text
    assert "No em dash" in page and "{{seller_first_name}}" in page               # prefilled with the shipped guide, raw
    shipped = config.text("style.md")

    post(client, "/setup/you", {**PRIYA_FORM, "style": shipped.replace("\n", "\r\n")})
    assert not (seller_settings / "style.md").exists()                           # unchanged text is not forked

    mine = "# How {{seller_first_name}} writes\nNever say synergy. Always sign as the PURPLE-HERON."
    assert post(client, "/setup/you", {**PRIYA_FORM, "style": mine}).status_code == 303
    assert (seller_settings / "style.md").read_text() == mine + "\n"
    from salescoach.agents.email_drafter import EmailAgent
    system = EmailAgent().system_prompt({})
    assert "PURPLE-HERON" in system and "How Priya writes" in system and "No em dash" not in system
    assert "PURPLE-HERON" in client.get("/setup/you").text

    post(client, "/setup/you", {**PRIYA_FORM, "style": ""})                       # cleared: back to the shipped guide
    assert not (seller_settings / "style.md").exists() and "No em dash" in EmailAgent().system_prompt({})


# =====================================================================================
# step 2: how you sell
# =====================================================================================

def test_method_page_lists_every_methodology_with_its_elements(client):
    page = client.get("/setup/method").text
    for m in methodology.available():
        assert f'id="m-{m["key"]}"' in page and m["name"] in page
    assert "tag-kind-qualification" in page and "tag-kind-conversation" in page
    meddpicc = methodology.get("meddpicc")
    assert meddpicc.elements[0].label in page and "Known when" in page
    assert "in use · default" in page                                           # nothing chosen yet: the shipped one
    assert re.search(r'id="pick-meddpicc"[^>]* checked', page)


def test_switching_methodology_changes_the_active_one_and_queues_the_open_deals(client, db, fake_llm):
    deal, _call, _people = run_call(db, fake_llm)
    assert rail(client.get("/setup/method").text)["method"] == "default"
    r = post(client, "/setup/method", {"key": "bant", "go": "stay"})
    assert r.status_code == 303 and "1+open+deal+will+be+re-read" in r.headers["location"]
    assert methodology.active().key == "bant"
    queued = db.execute("SELECT entity_id, payload FROM wf_events WHERE type='STRATEGY_REQUESTED' "
                        "AND status='pending'").fetchall()
    assert [q["entity_id"] for q in queued] == [deal]
    assert json.loads(queued[0]["payload"])["methodology"] == "bant"
    page = client.get("/setup/method").text
    assert rail(page)["method"] == "done" and re.search(r'id="pick-bant"[^>]* checked', page)

    again = post(client, "/setup/method", {"key": "bant", "go": "next"})        # the same one again queues nothing
    assert again.headers["location"].startswith("/setup/model?msg=")
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='STRATEGY_REQUESTED'").fetchone()[0] == 1
    assert "err=" in post(client, "/setup/method", {"key": "nonsense"}).headers["location"]
    assert methodology.active().key == "bant"


def test_keeping_the_default_records_the_choice_without_touching_any_deal(client, db):
    post(client, "/setup/method", {"key": "meddpicc"})
    assert config.load_user("methodology")["active"] == "meddpicc"
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='STRATEGY_REQUESTED'").fetchone()[0] == 0
    assert rail(client.get("/setup/method").text)["method"] == "done"


def builder_form(**over):
    form = {"name": "Acme Way", "key": "", "kind": "qualification", "description": "How Acme qualifies.",
            "el_key": ["", "", ""], "el_label": ["Budget holder", "Compelling event", ""],
            "el_known_when": ["The person who signs has said so on a call", "A dated reason to act exists", ""],
            "el_partial_when": ["We have a name", "", ""], "el_questions": ["Who signs?\nWho else?", "", ""],
            "el_cap": ["50", "", ""], "el_critical": ["0"], "action": "save"}
    form.update(over)
    return form


def test_custom_methodology_errors_are_shown_next_to_the_field(client, seller_settings):
    bad = builder_form(name="", el_known_when=["", "A dated reason to act exists", ""], el_cap=["", "", ""])
    r = post(client, "/setup/method/custom", bad)
    assert r.status_code == 400 and not (seller_settings / "methodology.yaml").exists()
    assert 'id="err-name"' in r.text and "Name is required" in r.text
    assert 'id="err-el0_known_when"' in r.text and "“Known when” is required" in r.text
    assert 'id="err-el0_cap"' in r.text and "A deal-breaker needs a health cap" in r.text
    assert 'id="err-key"' not in r.text                                          # the id comes from the name: one message
    assert 'value="Budget holder"' in r.text and "Who signs?" in r.text          # what was typed is kept
    assert 'id="err-el1_known_when"' not in r.text

    one = builder_form(el_label=["Only one", "", ""], el_known_when=["x", "", ""], el_critical=[])
    r = post(client, "/setup/method/custom", one)
    assert r.status_code == 400 and 'id="err-elements"' in r.text and "needs 2 to 12 elements" in r.text

    taken = post(client, "/setup/method/custom", builder_form(key="bant", el_critical=[]))
    assert taken.status_code == 400 and 'id="err-key"' in taken.text and "already taken" in taken.text
    risky = post(client, "/setup/method/custom", builder_form(el_label=["Risk appetite", "Compelling event", ""],
                                                              el_critical=[]))
    assert risky.status_code == 400 and 'id="err-el0_label"' in risky.text and "cannot start with" in risky.text


def test_custom_methodology_is_saved_activated_edited_and_deleted(client, db, seller_settings):
    more = post(client, "/setup/method/custom", builder_form(action="add"))
    assert more.status_code == 200 and more.text.count('name="el_label"') == 4 and "autofocus" in more.text
    assert not (seller_settings / "methodology.yaml").exists()                   # adding a row saves nothing

    r = post(client, "/setup/method/custom", builder_form())
    assert r.status_code == 303 and "/setup/method?msg=" in r.headers["location"]
    saved = methodology.get("acme_way")
    assert saved and not saved.builtin and saved.keys == ("budget_holder", "compelling_event")
    first = saved.element("budget_holder")
    assert first.critical and first.cap_when_not_known == 50 and first.questions == ("Who signs?", "Who else?")
    assert methodology.active().key == "meddpicc"                                # saving does not switch
    page = client.get("/setup/method").text
    assert 'id="m-acme_way"' in page and "your own" in page and "/setup/method/custom/acme_way" in page

    assert post(client, "/setup/method", {"key": "acme_way"}).status_code == 303  # ... choosing it does
    assert methodology.active().key == "acme_way"

    refused = post(client, "/setup/method/custom/acme_way/delete")               # the engine refuses the active one
    assert "err=" in refused.headers["location"] and "in+use" in refused.headers["location"]
    assert "is the methodology in use" in client.get(refused.headers["location"]).text
    assert methodology.get("acme_way") is not None

    edit = client.get("/setup/method/custom/acme_way").text
    assert 'value="Budget holder"' in edit and 'name="el_key" value="budget_holder"' in edit
    renamed = builder_form(name="Acme Way 2", el_key=["budget_holder", "compelling_event", ""],
                           el_label=["Economic buyer", "Compelling event", ""])
    assert post(client, "/setup/method/custom/acme_way", renamed).status_code == 303
    again = methodology.get("acme_way")
    assert again.name == "Acme Way 2" and again.keys == ("budget_holder", "compelling_event")   # keys outlive labels
    assert again.element("budget_holder").label == "Economic buyer"

    post(client, "/setup/method", {"key": "meddpicc"})
    assert "msg=" in post(client, "/setup/method/custom/acme_way/delete").headers["location"]
    assert methodology.get("acme_way") is None
    assert "err=" in post(client, "/setup/method/custom/acme_way/delete").headers["location"]
    assert client.get("/setup/method/custom/acme_way", follow_redirects=False).status_code == 303


def test_custom_methodology_form_can_only_write_methodology_yaml(client, seller_settings, tmp_path):
    for key in ("../../evil", "/etc/passwd", "a/b", "evil.yaml", "..", "x:y"):
        r = post(client, "/setup/method/custom", builder_form(key=key, el_critical=[]))
        assert r.status_code == 400 and 'id="err-key"' in r.text, key
    assert post(client, "/setup/method/custom/..%2F..%2Fevil", builder_form()).status_code in (303, 404)
    assert post(client, "/setup/method/custom/nothing_here", builder_form()).headers["location"].startswith(
        "/setup/method?err=")
    assert not (seller_settings / "methodology.yaml").exists()
    assert sorted(p.name for p in seller_settings.iterdir()) == ["seller.yaml"]
    assert not list(tmp_path.rglob("evil*"))
    definition, _rows, _map = forms.parse_methodology(builder_form(unknown_field="x"))
    assert set(definition) == {"key", "name", "kind", "description", "elements"}  # only what the form shows


# =====================================================================================
# step 3: model
# =====================================================================================

@pytest.fixture
def provider_calls(monkeypatch):
    """list_models / test_connection replaced: no network, and the arguments are on record."""
    calls = {"models": [], "test": []}

    def fake_models(provider_key, cfg=None, **_):
        calls["models"].append((provider_key, cfg, config.secret("OPENAI_API_KEY")))
        if provider_key == "xai":
            raise providers.ProviderError(f"xAI refused the key {config.secret('XAI_API_KEY')} (401)")
        return ["gpt-big", "gpt-small", "gpt-tiny"]

    def fake_test(provider_key, cfg=None, **_):
        calls["test"].append((provider_key, cfg))
        if (cfg or {}).get("tiers", {}).get("light") == "broken":
            return {"ok": False, "model": "broken", "latency_ms": 12, "error": f"401 for key {FAKE_KEY}"}
        return {"ok": True, "model": "gpt-small", "latency_ms": 840, "error": None}

    monkeypatch.setattr(providers, "list_models", fake_models)
    monkeypatch.setattr(providers, "test_connection", fake_test)
    return calls


def everything(response) -> str:
    return response.text + json.dumps(dict(response.headers)) + str(response.url)


def test_provider_flow_stores_the_key_and_never_shows_it(client, db, seller_settings, provider_calls, no_cli):
    seen = []
    page = client.get("/setup/model?provider=openai")
    seen.append(page)
    assert "not set" in page.text and 'type="password"' in page.text and "OpenAI platform dashboard" in page.text
    assert "Heavy does the analysis and the drafting" in page.text
    assert 'name="base_url"' not in page.text                                   # editable only for the compatible one
    assert 'name="base_url"' in client.get("/setup/model?provider=openai_compatible").text
    assert 'name="api_key"' not in client.get("/setup/model?provider=claude_code").text

    form = {"provider": "openai", "api_key": f"  {FAKE_KEY}\n", "heavy": "", "light": ""}
    listed = client.post("/setup/model/models", data=form, headers={**ORIGIN, "accept": "application/json"})
    seen.append(listed)
    assert listed.json() == {"ok": True, "models": ["gpt-big", "gpt-small", "gpt-tiny"], "error": None, "key_set": True}
    assert config.secret("OPENAI_API_KEY") == FAKE_KEY                           # stored before the provider is asked
    assert provider_calls["models"][-1] == ("openai", {"tiers": {}}, FAKE_KEY)
    secrets_file = seller_settings / "secrets.env"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600

    plain = post(client, "/setup/model/models", {"provider": "openai", "api_key": ""})     # no JS: a page comes back
    seen.append(plain)
    assert plain.status_code == 200 and "<option value=\"gpt-big\"" in plain.text and "key is set" in plain.text
    assert config.secret("OPENAI_API_KEY") == FAKE_KEY                           # an empty field keeps the stored key

    chosen = {"provider": "openai", "api_key": "", "heavy": "gpt-big", "light": "gpt-small"}
    tested = client.post("/setup/model/test", data=chosen, headers={**ORIGIN, "accept": "application/json"})
    seen.append(tested)
    body = tested.json()
    assert body["ok"] and body["model"] == "gpt-small" and body["latency_ms"] == 840 and body["key_set"]
    assert provider_calls["test"][-1] == ("openai", {"tiers": {"heavy": "gpt-big", "light": "gpt-small"}})
    seen.append(post(client, "/setup/model/test", chosen))                      # and without JS
    assert "Connection works" in seen[-1].text and "0.8 seconds" in seen[-1].text

    failed = client.post("/setup/model/test", data={**chosen, "light": "broken", "api_key": FAKE_KEY},
                         headers={**ORIGIN, "accept": "application/json"})
    seen.append(failed)
    assert failed.json()["ok"] is False and "401 for key [key]" in failed.json()["error"]
    assert "401 for key [key]" in state.last_test(db)["error"]
    seen.append(post(client, "/setup/model/test", chosen))                      # last test on record: a pass

    used = post(client, "/setup/model/use", {**chosen, "go": "next"})
    seen.append(used)
    assert used.status_code == 303 and used.headers["location"].startswith("/setup/sources?msg=")
    overlay = yaml.safe_load((seller_settings / "models.yaml").read_text())
    assert overlay["provider"] == "openai" and overlay["providers"]["openai"]["tiers"] == {"heavy": "gpt-big",
                                                                                         "light": "gpt-small"}
    assert providers.active() == "openai" and providers.model_for("openai", {"tier": "heavy"})[0] == "gpt-big"

    seen += [client.get(p) for p in ("/setup/model", "/setup/model?provider=openai", "/setup/review", "/setup", "/")]
    assert "in use" in seen[-4].text and rail(seen[-4].text)["model"] == "done"
    for response in seen:
        assert FAKE_KEY not in everything(response), response.url
    for path in seller_settings.iterdir():                                       # only secrets.env holds it
        assert (FAKE_KEY in path.read_text()) == (path.name == "secrets.env"), path.name
    assert FAKE_KEY not in "".join(str(r["value"]) for r in db.execute("SELECT value FROM state"))


def test_provider_errors_are_scrubbed_and_a_bad_key_is_refused(client, provider_calls, no_cli):
    r = client.post("/setup/model/models", data={"provider": "xai", "api_key": FAKE_KEY},
                    headers={**ORIGIN, "accept": "application/json"})
    assert r.json()["ok"] is False and FAKE_KEY not in r.text and "refused the key [key]" in r.json()["error"]
    page = post(client, "/setup/model/models", {"provider": "xai", "api_key": ""})
    assert "Could not load the models" in page.text and FAKE_KEY not in page.text

    bad = post(client, "/setup/model/test", {"provider": "anthropic", "api_key": 'abc"def\'ghi'})
    assert "does not look like an API key" in bad.text and not config.has_secret("ANTHROPIC_API_KEY")
    assert 'abc"def' not in bad.text and "abc&#34;def" not in bad.text


def test_use_this_provider_refuses_what_cannot_work(client, seller_settings, provider_calls, no_cli):
    def problem(data):
        r = post(client, "/setup/model/use", data)
        assert r.status_code == 303 and "err=" in r.headers["location"]
        return client.get(r.headers["location"]).text
    assert "Add an API key first" in problem({"provider": "openai", "heavy": "a", "light": "b"})
    assert "both tiers" in problem({"provider": "openai", "api_key": FAKE_KEY, "heavy": "a", "light": ""})
    assert "address of your endpoint" in problem({"provider": "openai_compatible", "heavy": "a", "light": "b"})
    assert "not installed" in problem({"provider": "claude_code", "heavy": "opus", "light": "sonnet"})
    assert "err=" in post(client, "/setup/model/use", {"provider": "nonsense"}).headers["location"]
    assert not (seller_settings / "models.yaml").exists() and providers.active() == "claude_code"

    ok = post(client, "/setup/model/use", {"provider": "openai_compatible", "base_url": "http://localhost:8000/v1",
                                           "heavy_custom": "my-model", "light_custom": "my-model", "go": "stay"})
    assert ok.headers["location"].startswith("/setup/model?provider=openai_compatible&msg=")
    assert providers.active() == "openai_compatible"
    assert providers.provider_config("openai_compatible")["base_url"] == "http://localhost:8000/v1"
    # api_key_env is not something a form can set
    post(client, "/setup/model/use", {"provider": "openai", "api_key": FAKE_KEY, "api_key_env": "PATH",
                                      "heavy": "a", "light": "b"})
    assert providers.provider_config("openai")["api_key_env"] == "OPENAI_API_KEY"


# =====================================================================================
# step 4: where calls come from
# =====================================================================================

def test_sources_page_groups_every_source_and_shows_the_drop_folder(client, db):
    page = client.get("/setup/sources").text
    for d in sources.catalog():
        assert f'id="src-{d["kind"]}"' in page, d["kind"]
    assert page.count("untested against the live API") == 2                     # fireflies and fathom
    assert str((config.DATA_DIR / "inbox" / "drop").resolve()) in page
    assert "/import/webhook" in page and "X-Salescoach-Secret" in page and "curl -X POST" in page
    assert "always on" in page and "Otter" in page and "Microsoft Teams" in page
    assert page.index("Recording on this Mac") < page.index("Send a transcript in") < page.index("Other recorders")


def test_enabling_fireflies_takes_a_write_only_key(client, db, seller_settings):
    assert rail(client.get("/setup/sources").text)["sources"] == "default"
    refused = post(client, "/setup/sources/fireflies", {"enabled": "1", "poll_minutes": "20"})
    assert "err=" in refused.headers["location"] and "API+key" in refused.headers["location"]
    assert not sources.settings()["fireflies"]["enabled"]

    r = post(client, "/setup/sources/fireflies", {"enabled": "1", "poll_minutes": "20", "api_key": FAKE_KEY,
                                                  "only_deals": "1"})
    assert r.status_code == 303 and r.headers["location"].endswith("#src-fireflies")
    assert FAKE_KEY not in everything(r) and config.secret("FIREFLIES_API_KEY") == FAKE_KEY
    chosen = sources.settings()["fireflies"]
    assert chosen == {"enabled": True, "poll_minutes": 20, "options": {"only_deals": True}}
    assert FAKE_KEY not in (seller_settings / "sources.yaml").read_text()
    page = client.get("/setup/sources")
    assert FAKE_KEY not in everything(page) and "key is set" in page.text
    assert rail(page.text)["sources"] == "done" and "Fireflies" in client.get("/setup/review").text

    post(client, "/setup/sources/fireflies", {"poll_minutes": "20"})             # off; the empty key field keeps the key
    assert not sources.settings()["fireflies"]["enabled"] and config.secret("FIREFLIES_API_KEY") == FAKE_KEY
    assert "err=" in post(client, "/setup/sources/fireflies", {"poll_minutes": "0"}).headers["location"]
    assert "err=" in post(client, "/setup/sources/otter", {"enabled": "1"}).headers["location"]   # export-only
    assert "err=" in post(client, "/setup/sources/nonsense", {"enabled": "1"}).headers["location"]


def test_db_state_shows_last_run_and_last_error(client, db):
    from salescoach.store.stores import set_state
    set_state(db, "sources:fathom:last_error", json.dumps({"at": "2026-09-17T10:00:00+00:00", "error": "401 refused"}))
    set_state(db, "sources:folder:last_run", "2026-09-17T10:00:00+00:00")
    db.commit()
    page = client.get("/setup/sources").text
    assert "401 refused" in page and "Last checked" in page


def test_webhook_secret_is_shown_once_and_never_again(client, db):
    from salescoach.sources.adapters import webhook
    assert "no secret yet" in client.get("/setup/sources").text
    created = post(client, "/setup/sources/webhook/secret")
    assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
    secret = config.secret(webhook.SECRET_NAME)
    assert secret and created.text.count(secret) == 1 and "shown once" in created.text and "data-copy" in created.text
    for path in ("/setup/sources", "/setup/review", "/setup", "/"):
        assert secret not in everything(client.get(path)), path
    assert "Set. It cannot be shown again." in client.get("/setup/sources").text
    assert secret not in "".join(str(r["value"]) for r in db.execute("SELECT value FROM state"))

    typed = post(client, "/setup/sources/webhook", {"enabled": "1", "api_key": "chosen-by-a-form"})
    assert typed.status_code == 303 and config.secret(webhook.SECRET_NAME) == secret       # never typed, only generated
    post(client, "/setup/sources/webhook", {})
    assert not sources.settings()["webhook"]["enabled"]

    replaced = post(client, "/setup/sources/webhook/secret")
    new = config.secret(webhook.SECRET_NAME)
    assert new != secret and new in replaced.text and secret not in replaced.text


def test_watched_folder_can_be_moved_and_a_bad_path_is_refused(client, tmp_path):
    target = tmp_path / "my transcripts"
    assert "msg=" in post(client, "/setup/sources/folder", {"enabled": "1", "path": str(target)}).headers["location"]
    assert str(target) in client.get("/setup/sources").text and not target.exists()      # shown, not created here
    assert "err=" in post(client, "/setup/sources/folder", {"enabled": "1", "path": "relative/folder"}).headers["location"]
    assert sources.settings()["folder"]["options"] == {"path": str(target)}
    post(client, "/setup/sources/folder", {"enabled": "1", "path": ""})
    assert sources.settings()["folder"]["options"] == {}


# =====================================================================================
# step 5: email and calendar (read-only)
# =====================================================================================

@pytest.fixture
def untouchable(monkeypatch, tmp_path, no_cli):
    """Everything missing, and every door to a real service raises if it is opened."""
    from salescoach.automation import connector
    from salescoach.execution import gmail
    from salescoach.speech import models

    def boom(*_a, **_k):
        raise AssertionError("the connections page must not call a service")
    monkeypatch.setattr(gmail, "GMAIL_DIR", tmp_path / "no-gmail")
    monkeypatch.setattr(gmail, "_load_credentials", boom)
    monkeypatch.setattr(gmail.GmailProvider, "_svc", boom)
    monkeypatch.setattr(connector, "_run", boom)
    monkeypatch.setattr(connector, "discover", boom)
    monkeypatch.setattr("subprocess.run", boom)
    monkeypatch.setattr("httpx.get", boom)
    monkeypatch.setattr("httpx.post", boom)
    monkeypatch.setattr(models, "is_downloaded", lambda repo, cache_dir=None: False)
    monkeypatch.setattr(models, "pull", boom, raising=False)
    monkeypatch.setattr(state, "capture_binary", lambda: tmp_path / "no-callcap")
    return tmp_path


def test_connections_page_renders_with_everything_missing_and_calls_nothing(client, db, untouchable):
    r = client.get("/setup/connections")
    assert r.status_code == 200 and r.text.count('data-ok="no"') == 5 and 'data-ok="yes"' not in r.text
    for key in ("gmail", "calendar", "capture", "speech", "cli"):
        assert f'id="conn-{key}"' in r.text
    assert "How to fix it" in r.text and "salescoach models pull" in r.text and "System Settings" in r.text
    assert "<button" not in r.text.split('class="status-list"')[1].split("setup-foot")[0]   # no action buttons
    assert rail(r.text)["connections"] == "attention"
    review = client.get("/setup/review").text
    assert "Still missing" in review and "Gmail" in review and "the coach works without it" in review


def test_connections_report_a_token_file_without_opening_it(client, db, untouchable, monkeypatch):
    from salescoach.execution import gmail
    from salescoach.store.stores import set_state
    folder = untouchable / "no-gmail"
    (folder / "tokens").mkdir(parents=True)
    (folder / "credentials.json").write_text("not json: never parsed")
    (folder / "tokens" / "work.json").write_text("not json: never parsed")
    (folder / "tokens" / "work.json").chmod(0o000)                              # unreadable: existence is enough
    monkeypatch.setattr(providers, "claude_cli_available", lambda: True)
    set_state(db, "automation:calendar_tools", json.dumps({"tools": ["x__list_events"], "discovered_at": "2026-09-16T09:00:00"}))
    db.commit()
    rows = {r["key"]: r for r in state.connections(db, {"available": False})}
    assert rows["gmail"]["ok"] and rows["calendar"]["ok"] and rows["cli"]["ok"] and not rows["capture"]["ok"]
    assert "2026-09-16" in rows["calendar"]["detail"] and gmail.GMAIL_DIR == folder
    assert rail(client.get("/setup/connections").text)["connections"] == "done"


# =====================================================================================
# step 6: review, the rail, and the Today card
# =====================================================================================

def test_review_separates_defaults_from_choices_and_finish_goes_to_today(client, db, provider_calls, no_cli):
    page = client.get("/setup/review").text
    assert page.count("using the default") == 4                                 # style, methodology, model, sources
    for step in STEPS[:5]:
        assert f'href="/setup/{step}"' in page
    assert "MEDDPICC" in page and SELLER["name"] in page and "Finish" in page
    assert rail(page) == {"you": "done", "method": "default", "model": "attention", "sources": "default",
                          "connections": "attention", "review": "todo"}

    post(client, "/setup/method", {"key": "spiced"})
    post(client, "/setup/model/test", {"provider": "openai", "api_key": FAKE_KEY, "heavy": "gpt-big", "light": "gpt-small"})
    post(client, "/setup/model/use", {"provider": "openai", "heavy": "gpt-big", "light": "gpt-small"})
    post(client, "/setup/sources/folder", {"enabled": "1"})
    page = client.get("/setup/review").text
    assert page.count("using the default") == 1 and "SPICED" in page and "OpenAI" in page and "gpt-big" in page
    assert "passed" in page and FAKE_KEY not in page

    done = post(client, "/setup/finish")
    assert done.status_code == 303 and done.headers["location"].startswith("/?msg=")
    assert rail(client.get("/setup/review").text)["review"] == "done"


def test_finish_without_a_profile_goes_back_to_step_one(client, fresh):
    page = client.get("/setup/review").text
    assert "Your profile" in page and "Fill in your profile" in page and rail(page)["review"] == "todo"
    r = post(client, "/setup/finish")
    assert r.headers["location"].startswith("/setup/you?err=")
    assert rail(client.get("/setup/review").text)["review"] == "todo"


def test_rail_follows_the_settings_not_the_visits(client, db, seller_settings, provider_calls):
    for step in STEPS:                                                           # opening every page changes nothing
        client.get(f"/setup/{step}")
    assert rail(client.get("/setup/review").text)["method"] == "default"
    methodology.set_active("bant")                                               # a change made outside the wizard
    sources.save("folder", True)
    assert {k: v for k, v in rail(client.get("/setup/you").text).items() if k in ("method", "sources")} == \
        {"method": "done", "sources": "done"}
    (seller_settings / "methodology.yaml").unlink()
    methodology._cache_clear()
    assert rail(client.get("/setup/you").text)["method"] == "default"


def test_today_card_shows_until_a_test_passes_or_it_is_dismissed(client, db, provider_calls, monkeypatch):
    monkeypatch.setattr(providers, "claude_cli_available", lambda: True)
    home = client.get("/").text
    assert 'id="finish-setup"' in home and "has not passed a connection test" in home and "/setup/model" in home
    assert post(client, "/setup/dismiss-card").headers["location"] == "/"
    assert 'id="finish-setup"' not in client.get("/").text

    monkeypatch.setattr(providers, "claude_cli_available", lambda: False)        # now unusable: it comes back
    home = client.get("/").text
    assert 'id="finish-setup"' in home and "cannot analyse calls yet" in home

    monkeypatch.setattr(providers, "claude_cli_available", lambda: True)
    post(client, "/setup/model/test", {"provider": "claude_code", "heavy": "opus", "light": "sonnet"})
    assert state.today_card(db) is None and 'id="finish-setup"' not in client.get("/").text
    assert {r["key"] for r in db.execute("SELECT key FROM state WHERE key LIKE 'setup:%'")} == {"setup:provider_test"}
    assert {r["key"] for r in db.execute("SELECT key FROM user_state WHERE key LIKE 'setup:%'")} == {"setup:card_dismissed"}
