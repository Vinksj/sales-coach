"""End-to-end check of a cloud (team) install against the running Docker stack (e2e/docker-compose.yml).

    E2E=1 pytest -q e2e/test_e2e.py         (e2e/run.sh builds, starts, runs this and tears down)

Skipped unless E2E=1. The tests run IN ORDER and share one world (`S`): each step builds on the last, the
way a launch would go. The "browser" is an httpx client on http://127.0.0.1:18140 (the public URL) that
also visits the fake Google at 127.0.0.1:19000 when the app sends it to http://fakes:9000. Every action is
an HTTP request a person would make; the database is only READ, as the owner role, to find ids and to
check what the pages cannot show (owners, event states, heartbeats). The two exceptions, both operator
acts with no page: the daily budget is written into org_settings (there is no budget form; docs say
env or settings), and `salescoach import-sqlite` runs in the web container as the deploy docs say.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("E2E") != "1", reason="end-to-end: set E2E=1 (e2e/run.sh)")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WEB = os.environ.get("E2E_WEB", "http://127.0.0.1:18140")
FAKES = os.environ.get("E2E_FAKES", "http://127.0.0.1:19000")
FAKES_INSIDE = "http://fakes:9000"
OWNER_DB = os.environ.get("E2E_DB", "postgresql://salescoach_owner:owner-e2e@127.0.0.1:15432/salescoach")
IMPORT_DB = OWNER_DB.rsplit("/", 1)[0] + "/salescoach_import"
IMPORT_DB_INSIDE = "postgresql://salescoach_owner:owner-e2e@db:5432/salescoach_import"
PROJECT = ["docker", "compose", "-p", "sc-e2e", "-f", str(HERE / "docker-compose.yml")]
MODEL_BASE = "http://10.231.77.10:9000/v1"
MODEL = "claude-sonnet-5"                      # priced in config/models.yaml: the budget can count it

ADMIN = "admin@tessel.test"
A, B, M, C = "asha@tessel.test", "bina@tessel.test", "mona@tessel.test", "chitra@tessel.test"
NAMES = {ADMIN: "Ada Admin", A: "Asha Rao", B: "Bina Shah", M: "Mona Iyer", C: "Chitra Das"}
BUYER = "arjun@northwind.test"
MEETING_ID = "ffmeet01"
KEYS = {A: "ff-key-asha-0001", B: "ff-key-bina-0002"}
MY_LINE = {A: "Thanks for joining, Arjun. Let me walk you through the pilot.",
           B: "I will send the plant-wise breakdown by Friday."}

S = {}                                          # the shared world, step to step
EVIDENCE = {}                                   # what the last test prints: the numbers behind each check


# ---- helpers ------------------------------------------------------------------------------------------

def db(url=OWNER_DB):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def q(sql, *args, url=OWNER_DB):
    with db(url) as conn:
        return conn.execute(sql, args).fetchall()


def q1(sql, *args, url=OWNER_DB):
    rows = q(sql, *args, url=url)
    return rows[0] if rows else None


def wait_until(what, fn, timeout=120.0, every=1.0):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last!r})")


def fakes(method, path, **kw):
    r = httpx.request(method, FAKES + path, timeout=10, **kw)
    r.raise_for_status()
    return r.json()


def flash(response) -> str:
    """The msg/err a redirect carries (the app puts it in the query string)."""
    loc = response.headers.get("location") or ""
    return httpx.URL(loc).params.get("msg") or httpx.URL(loc).params.get("err") or ""


def compose(*args, check=True, **kw):
    return subprocess.run([*PROJECT, *args], capture_output=True, text=True, check=check, **kw)


class Browser:
    """One person's browser: cookies, the Origin header every page post carries, and the trip through Google."""

    def __init__(self, email):
        self.email = email
        self.c = httpx.Client(base_url=WEB, follow_redirects=False, timeout=30,
                              headers={"origin": WEB, "accept": "text/html"})

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, **kw):
        return self.c.post(path, data=data or {}, **kw)

    def _google(self, start: httpx.Response) -> httpx.Response:
        """Follow the app's redirect to Google (the fake approves as this person) and back to the app."""
        assert start.status_code == 303, start.text[:300]
        target = start.headers["location"]
        assert target.startswith(FAKES_INSIDE + "/o/oauth2/v2/auth?"), target
        parts = urlsplit(target)
        google = httpx.get(FAKES + parts.path + "?" + parts.query + "&" + urlencode({"e2e_user": self.email}),
                           timeout=10)
        assert google.status_code == 302, google.text
        back = google.headers["location"]
        assert back.startswith(WEB + "/auth/"), back
        return self.c.get(back[len(WEB):])

    def sign_in(self):
        done = self._google(self.get("/auth/google"))
        assert done.status_code == 303, done.text[:500]
        assert self.c.cookies, "no session cookie"
        return done

    def connect_google(self, feature):
        return self._google(self.get("/auth/connect/google", params={"feature": feature}))


def fireflies_meeting(started_ms: int) -> dict:
    """The one meeting both reps were on, as Fireflies lists it to each of their accounts."""
    sentences = [("Asha Rao", "Thanks for joining, Arjun. Let me walk you through the pilot."),
                 ("Arjun Kumar", "Before that, our CFO will want to see the savings split by plant."),
                 ("Bina Shah", "I will send the plant-wise breakdown by Friday."),
                 ("Arjun Kumar", "Okay. I will try to set up a meeting with our CFO next week.")]
    return {"id": MEETING_ID, "title": "Northwind pilot review", "date": started_ms, "duration": 30,
            "organizer_email": A, "host_email": A, "participants": [BUYER, A, B],
            "meeting_attendees": [{"displayName": "Arjun Kumar", "email": BUYER, "name": "Arjun Kumar"},
                                  {"displayName": "Asha Rao", "email": A, "name": "Asha Rao"},
                                  {"displayName": "Bina Shah", "email": B, "name": "Bina Shah"}],
            "sentences": [{"index": i, "speaker_name": who, "text": text, "raw_text": text, "start_time": 5.0 * i,
                           "end_time": 5.0 * i + 4} for i, (who, text) in enumerate(sentences)],
            "summary": {"overview": "Pilot review."}}


def uid(email):
    return q1("SELECT id FROM users WHERE email=%s", email)["id"]


def call_of(email):
    return q1("SELECT node_id, wf_state, wf_error, source_ref, owner_id FROM calls WHERE owner_id=%s", uid(email))


def email_of(call_id, status=None):
    sql = "SELECT * FROM emails WHERE call_id=%s" + (" AND status=%s" if status else "") + " ORDER BY version DESC LIMIT 1"
    return q1(sql, call_id, *([status] if status else []))


# ---- 1. the processes are up ---------------------------------------------------------------------------

def test_health_of_web_worker_and_both_schedulers():
    r = httpx.get(WEB + "/health", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok" and body["role"] == "web"
    live = lambda rows: [p for p in rows if not p["stale"]]                     # noqa: E731
    assert len(live(body["processes"]["workers"])) == 1
    assert live(body["processes"]["workers"])[0]["concurrency"] == 2
    assert len(live(body["processes"]["schedulers"])) == 2
    for service in ("web", "worker", "scheduler"):
        ps = json.loads("[" + ",".join(compose("ps", service, "--format", "json").stdout.split("\n")[:-1]) + "]")
        assert ps and all(c["Health"] == "healthy" for c in ps), (service, ps)
        for index in range(1, len(ps) + 1):
            out = compose("exec", "--index", str(index), service, "salescoach", "health", check=False)
            assert out.returncode == 0, (service, out.stdout, out.stderr)
    S["health"] = body
    EVIDENCE["health"] = {"workers": len(live(body["processes"]["workers"])),
                          "schedulers": len(live(body["processes"]["schedulers"])),
                          "leader": body["processes"]["leader"]["host"]}


# ---- 2. the bootstrap admin sets the org up -----------------------------------------------------------

def test_bootstrap_admin_signs_in_and_sets_up_the_org():
    for email, name in NAMES.items():
        fakes("POST", "/_control/google_user", json={"email": email, "name": name})
    stranger = Browser("nobody@tessel.test")
    refused = stranger._google(stranger.get("/auth/google"))
    assert refused.status_code == 403 and "invite" in refused.text.lower()
    admin = Browser(ADMIN)
    admin.sign_in()
    row = q1("SELECT role, status FROM users WHERE email=%s", ADMIN)
    assert (row["role"], row["status"]) == ("admin", "active")
    assert admin.get("/").headers["location"].endswith("/setup")            # the org is not set up yet
    r = admin.post("/setup/you", {"name": NAMES[ADMIN], "emails": ADMIN, "company": "Tessel", "role": "Head of Sales",
                                  "website": "www.tessel.test",
                                  "offering": "AI agents for freight and logistics operations",
                                  "icp": "large logistics companies", "buyer_titles": "CFOs and COOs",
                                  "own_domains": "tessel.test", "languages": "en", "timezone": "Asia/Kolkata",
                                  "go": "stay"})
    assert r.status_code == 303, r.text[:500]
    r = admin.post("/setup/model/use", {"provider": "openai_compatible", "base_url": MODEL_BASE,
                                        "heavy": MODEL, "light": MODEL, "go": "stay"})
    assert r.status_code == 303 and "now uses" in flash(r), flash(r)
    models = json.loads(q1("SELECT body FROM org_settings WHERE name='models'")["body"])
    assert models["provider"] == "openai_compatible"
    assert models["providers"]["openai_compatible"]["base_url"] == MODEL_BASE
    r = admin.post("/me/setup", {"name": NAMES[ADMIN], "emails": ADMIN, "timezone": "Asia/Kolkata", "languages": "en"})
    assert r.status_code == 303 and "saved" in flash(r).lower(), flash(r)
    assert admin.get("/").status_code == 200
    S["admin"] = admin


# ---- 3. people and a team ------------------------------------------------------------------------------

def test_admin_invites_two_reps_a_manager_and_makes_a_team():
    admin = S["admin"]
    r = admin.post("/admin/teams", {"name": "West"})
    assert r.status_code == 303 and "created" in flash(r), flash(r)
    team = q1("SELECT id FROM teams WHERE name='West'")["id"]
    for email, role in ((A, "rep"), (B, "rep"), (M, "manager"), (C, "rep")):
        r = admin.post("/admin/invite", {"email": email, "role": role, "name": NAMES[email],
                                         "team_id": team if email in (A, B, C) else ""})
        assert r.status_code == 303 and "can sign in" in flash(r), (email, flash(r))
    r = admin.post(f"/admin/teams/{team}/managers", {"manager_id": uid(M)})
    assert r.status_code == 303 and "saved" in flash(r).lower(), flash(r)
    rows = {r["email"]: r for r in q("SELECT email, role, status, team_id FROM users")}
    assert {e: (rows[e]["role"], rows[e]["status"]) for e in (A, B, M, C)} == {
        A: ("rep", "invited"), B: ("rep", "invited"), M: ("manager", "invited"), C: ("rep", "invited")}
    assert rows[A]["team_id"] == rows[B]["team_id"] == team
    assert q1("SELECT user_id FROM team_managers WHERE team_id=%s", team)["user_id"] == uid(M)
    S["team"] = team


def test_everyone_signs_in_through_google_and_fills_their_profile():
    for email in (A, B, M, C):
        person = Browser(email)
        person.sign_in()
        r = person.post("/me/setup", {"name": NAMES[email], "emails": email, "timezone": "Asia/Kolkata",
                                      "languages": "en", "role": "Account executive"})
        assert r.status_code == 303 and "saved" in flash(r).lower(), (email, flash(r))
        assert person.get("/").status_code == 200
        S[email] = person
    assert {r["status"] for r in q("SELECT status FROM users WHERE email IN (%s,%s,%s,%s)", A, B, M, C)} == {"active"}


# ---- 4. recorders: each rep's own account, the same meeting ---------------------------------------------

def test_two_reps_connect_their_own_fireflies_accounts():
    started = int((time.time() - 3600) * 1000)                       # an hour ago: fresh enough for a follow-up
    meeting = fireflies_meeting(started)
    for email in (A, B):
        fakes("POST", "/_control/recorder", json={"kind": "fireflies", "key": KEYS[email], "email": email,
                                                   "name": NAMES[email], "meetings": [meeting]})
    for email in (A, B):
        r = S[email].post("/me/recorders/fireflies/connect", {"api_key": KEYS[email]})
        assert r.status_code == 303 and f"connected as {email}" in flash(r), flash(r)
    rows = q("SELECT id, owner_id, kind, account_email, status FROM source_connections ORDER BY owner_id")
    assert {(r["owner_id"], r["account_email"], r["status"]) for r in rows} == {
        (uid(A), A, "active"), (uid(B), B, "active")}
    S["conn"] = {email: q1("SELECT id FROM source_connections WHERE owner_id=%s", uid(email))["id"] for email in (A, B)}
    keys_seen = {r["key"] for r in fakes("GET", "/_control/recorder_requests")["requests"]}
    assert keys_seen == set(KEYS.values())                            # each rep's own key, nobody else's


def test_import_now_gives_each_rep_their_own_copy_analysed_end_to_end():
    for email in (A, B):
        r = S[email].post(f"/me/connections/{S['conn'][email]}/poll", {"back": "meetings"})
        assert r.status_code == 303, r.text[:300]
        assert "imported" in flash(r), flash(r)

    def settled():
        rows = [call_of(e) for e in (A, B)]
        return rows if all(r and r["wf_state"] in ("awaiting_review", "failed") for r in rows) else None
    calls = wait_until("both calls analysed", settled, timeout=180)
    for email, row in zip((A, B), calls):
        assert row["wf_state"] == "awaiting_review", row
        assert row["source_ref"] == f"fireflies:{uid(email)}:{MEETING_ID}"
    assert calls[0]["node_id"] != calls[1]["node_id"]
    assert q1("SELECT COUNT(*) AS n FROM calls")["n"] == 2
    S["call"] = {A: calls[0]["node_id"], B: calls[1]["node_id"]}
    for email in (A, B):
        call = S["call"][email]
        mine = q("SELECT idx, channel, text FROM turns WHERE call_id=%s AND tier='final' ORDER BY idx", call)
        me = [t["text"] for t in mine if t["channel"] == "me"]
        assert me == [MY_LINE[email]], (email, mine)                   # each copy from its owner's side
        draft = email_of(call)
        assert draft and draft["status"] == "drafted" and json.loads(draft["to_addrs"]) == [BUYER], draft
        assert draft["owner_id"] == uid(email)
        runs = q("SELECT agent, status FROM agent_runs WHERE call_id=%s AND status='ok'", call)
        assert {r["agent"] for r in runs} >= {"quality", "summary", "call_analyst", "actions", "email"}, runs
        page = S[email].get(f"/calls/{call}")
        assert page.status_code == 200 and "Northwind pilot review" in page.text
    schemas = {c["schema"] for c in fakes("GET", "/_control/model")["calls"]}
    EVIDENCE["calls"] = {e: {"call": S["call"][e], "source_ref": call_of(e)["source_ref"],
                             "state": call_of(e)["wf_state"], "draft_to": json.loads(email_of(S["call"][e])["to_addrs"])}
                         for e in (A, B)}
    EVIDENCE["model_schemas"] = sorted(schemas)
    assert {"QualityReport", "CallSummary", "CallAnalysis", "ActionExtraction", "EmailDraft"} <= schemas
    meetings = S[A].get("/me/meetings")
    assert meetings.status_code == 200 and "Northwind pilot review" in meetings.text
    assert "Northwind pilot review" not in S[C].get("/me/meetings").text


def test_a_rep_with_no_recorder_connection_has_no_calls():
    assert q1("SELECT COUNT(*) AS n FROM calls WHERE owner_id=%s", uid(C))["n"] == 0
    assert q1("SELECT COUNT(*) AS n FROM source_connections WHERE owner_id=%s", uid(C))["n"] == 0
    home = S[C].get("/")
    assert home.status_code == 200 and "Northwind pilot review" not in home.text
    for call in S["call"].values():
        assert S[C].get(f"/calls/{call}").status_code == 404


# ---- 5. isolation: rep B cannot reach rep A's work -------------------------------------------------------

def test_rep_b_gets_404_for_everything_of_rep_a():
    a = S[A]
    r = a.post("/deals", {"name": "Northwind pilot", "account": "Northwind", "domains": "northwind.test"})
    assert r.status_code == 303, r.text[:300]
    deal = q1("SELECT node_id FROM deals WHERE owner_id=%s", uid(A))["node_id"]
    r = a.post(f"/calls/{S['call'][A]}/deal", {"deal_id": deal})
    assert r.status_code == 303, r.text[:300]
    S["deal"] = deal
    call = S["call"][A]
    email = email_of(call)["id"]
    loop = q1("SELECT node_id FROM loops WHERE call_id=%s", call)["node_id"]
    run = q1("SELECT id FROM agent_runs WHERE call_id=%s LIMIT 1", call)["id"]
    b = S[B]
    gets = [f"/calls/{call}", f"/calls/{call}/runs", f"/deals/{deal}", f"/deals/{deal}/intel", f"/deals/{deal}/prep",
            f"/deals/{deal}/outcome", f"/nudges/{email}", f"/runs/{run}"]
    for path in gets:
        assert b.get(path).status_code == 404, path
    posts = [(f"/emails/{email}/send", {}), (f"/emails/{email}/save", {"body": "x"}), (f"/loops/{loop}/confirm", {}),
             (f"/calls/{call}/redraft", {}), (f"/me/connections/{S['conn'][A]}/poll", {}),
             (f"/me/connections/{S['conn'][A]}/disconnect", {}), ("/comments", {"entity_type": "call",
                                                                               "entity_id": call, "body": "hi"})]
    for path, data in posts:
        assert b.post(path, data).status_code == 404, path
    assert fakes("GET", "/_control/gmail")["sent"] == []
    S["email"], S["loop"] = email, loop
    EVIDENCE["b_404"] = len(gets) + len(posts)


# ---- 6. the manager ---------------------------------------------------------------------------------------

def test_manager_sees_both_reps_views_comments_and_cannot_send():
    m, call_a = S[M], S["call"][A]
    team = m.get("/team")
    assert team.status_code == 200 and NAMES[A] in team.text and NAMES[B] in team.text
    index = m.get("/calls")
    assert index.status_code == 200
    assert f"/calls/{S['call'][A]}" in index.text and f"/calls/{S['call'][B]}" in index.text
    page = m.get(f"/calls/{call_a}")
    assert page.status_code == 200 and "Northwind pilot review" in page.text
    seen = S[A].get(f"/calls/{call_a}")
    assert re.search(r"Viewed by .*" + re.escape(NAMES[M]), seen.text), "A's page does not say M viewed it"
    r = m.post("/comments", {"entity_type": "call", "entity_id": call_a, "turn_idx": "1",
                             "body": "Pin the CFO date before you send the breakdown.", "next": f"/calls/{call_a}"})
    assert r.status_code == 303 and "Comment added" in flash(r), flash(r)
    row = q1("SELECT author_id, owner_id, turn_idx FROM comments WHERE entity_id=%s", call_a)
    assert (row["author_id"], row["owner_id"], row["turn_idx"]) == (uid(M), uid(A), 1)
    assert "Pin the CFO date before you send the breakdown." in S[A].get(f"/calls/{call_a}").text
    refused = m.post(f"/emails/{S['email']}/send", {})
    assert refused.status_code == 403, (refused.status_code, refused.text[:300])
    EVIDENCE["manager_send"] = f"HTTP {refused.status_code}: {refused.text.strip()[:120]}"
    assert email_of(call_a)["status"] == "drafted"
    assert fakes("GET", "/_control/gmail")["sent"] == []


# ---- 7. budgets: B's cap defers B, never A --------------------------------------------------------------

def _deferred_run(owner):
    return q1("SELECT id, error FROM agent_runs WHERE owner_id=%s AND error LIKE 'budget_deferred%%' ORDER BY id DESC",
              owner)


def test_a_tiny_budget_defers_b_without_failing_and_without_blocking_a():
    # B's drafts cost 2 USD each at the fake (1M prompt tokens at claude-sonnet-5 prices); A's cost 0.004.
    fakes("POST", "/_control/model", json={"rules": [{"system": "follow-up email Bina sends",
                                                        "prompt_tokens": 1_000_000, "completion_tokens": 0}]})
    b_call, a_call = S["call"][B], S["call"][A]
    before = email_of(b_call)["version"]
    r = S[B].post(f"/calls/{b_call}/redraft", {})
    assert r.status_code == 303, r.text[:300]
    wait_until("B's redraft", lambda: (email_of(b_call) or {}).get("version", 0) > before, timeout=120)
    spent_b = float(q1("SELECT COALESCE(SUM(cost_usd),0) AS s FROM agent_runs WHERE owner_id=%s", uid(B))["s"])
    spent_a = float(q1("SELECT COALESCE(SUM(cost_usd),0) AS s FROM agent_runs WHERE owner_id=%s", uid(A))["s"])
    assert spent_b > 1.0 > spent_a, (spent_a, spent_b)
    # The operator sets a 1 USD per-user daily cap (the `budget` settings; config.load reads it on its next call).
    with db() as conn:
        conn.execute("INSERT INTO org_settings(name, body, version) VALUES ('budget', %s, 1) ON CONFLICT(name) DO "
                     "UPDATE SET body=excluded.body, version=org_settings.version+1",
                     (json.dumps({"llm": {"user_usd_day": 1.0}}),))
    version_b = email_of(b_call)["version"]
    r = S[B].post(f"/calls/{b_call}/redraft", {})
    assert r.status_code == 303, r.text[:300]
    wait_until("B's job deferred by the budget", lambda: _deferred_run(uid(B)), timeout=60)
    event = wait_until("B's event back to pending", lambda: q1(
        "SELECT status, attempts, not_before, error FROM wf_events WHERE owner=%s AND status='pending' "
        "AND not_before IS NOT NULL ORDER BY id DESC", uid(B)), timeout=30)
    assert event["attempts"] == 0, event                              # deferred, no attempt spent
    assert event["not_before"] > datetime.now(timezone.utc).isoformat(timespec="seconds"), event
    assert q1("SELECT COUNT(*) AS n FROM wf_events WHERE owner=%s AND status='failed'", uid(B))["n"] == 0
    assert call_of(B)["wf_state"] != "failed" and email_of(b_call)["version"] == version_b
    # A is under the cap and not held up behind B.
    version_a = email_of(a_call)["version"]
    r = S[A].post(f"/calls/{a_call}/redraft", {})
    assert r.status_code == 303, r.text[:300]
    wait_until("A's redraft", lambda: (email_of(a_call) or {}).get("version", 0) > version_a, timeout=120)
    assert _deferred_run(uid(A)) is None
    S["email"] = email_of(a_call, "drafted")["id"]
    S["budget_event"] = event
    EVIDENCE["budget"] = {"spent_a": round(spent_a, 4), "spent_b": round(spent_b, 4), "cap": 1.0,
                          "b_event": {k: event[k] for k in ("status", "attempts", "not_before")},
                          "b_run_error": _deferred_run(uid(B))["error"][:90],
                          "a_versions": [version_a, email_of(a_call)["version"]]}


# ---- 8. A sends their own follow-up from their own Gmail ------------------------------------------------

def test_rep_a_connects_gmail_and_sends_their_own_follow_up():
    a, call_a = S[A], S["call"][A]
    before = S[a.email].post(f"/emails/{S['email']}/send", {})
    assert "refused" in flash(before).lower() or "connect" in flash(before).lower(), flash(before)
    assert fakes("GET", "/_control/gmail")["sent"] == []
    done = a.connect_google("gmail")
    assert done.status_code == 303 and "Gmail connected as " + A in flash(done), flash(done)
    grant = q1("SELECT user_id, email, status FROM oauth_tokens WHERE user_id=%s", uid(A))
    assert grant and grant["email"] == A and grant["status"] == "active"
    r = a.post(f"/emails/{S['email']}/send", {})
    assert r.status_code == 303 and flash(r).startswith("Sent"), flash(r)
    sent = fakes("GET", "/_control/gmail")["sent"]
    assert len(sent) == 1, sent
    msg, row = sent[0], q1("SELECT * FROM emails WHERE id=%s", S["email"])
    assert msg["grant_email"] == A
    assert msg["headers"]["to"] == BUYER and A in msg["headers"]["from"]
    assert msg["headers"]["message-id"] == row["rfc822_message_id"]
    assert msg["headers"]["x-salescoach-key"] == row["idempotency_key"]
    assert "in-reply-to" not in msg["headers"] and "references" not in msg["headers"]   # a new thread
    assert row["status"] == "sent" and row["gmail_thread_id"] == msg["threadId"] and row["gmail_message_id"] == msg["id"]
    assert row["approved_by"] == f"user:{uid(A)}"
    assert fakes("GET", "/_control/google")["exchanges"].count(["authorization_code", A]) >= 2   # sign-in + consent
    S["sent"] = msg
    EVIDENCE["gmail"] = {"sent": len(sent), "grant": msg["grant_email"],
                         "headers": {k: msg["headers"][k] for k in ("to", "from", "message-id", "x-salescoach-key")},
                         "thread": msg["threadId"]}


# ---- 9. two schedulers, one leader, and a takeover ------------------------------------------------------

def _leader():
    row = q1("SELECT value FROM state WHERE key='ops:scheduler:leader'")
    return json.loads(row["value"]) if row else None


def _live_schedulers():
    beats = []
    for row in q("SELECT value FROM state WHERE key LIKE 'ops:scheduler:%%:heartbeat'"):
        body = json.loads(row["value"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(body["at"])).total_seconds()
        if age < 50:
            beats.append(body)
    return beats


def test_exactly_one_scheduler_leads_and_a_standby_takes_over():
    beats = wait_until("two live scheduler heartbeats", lambda: (lambda b: b if len(b) == 2 else None)(_live_schedulers()),
                       timeout=40)
    leaders = [b for b in beats if b["leader"]]
    assert len(leaders) == 1, beats
    lead = _leader()
    assert lead["host"] == leaders[0]["host"]
    locks = q("SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted AND objsubid=1 AND "
              "((classid::bigint << 32) | objid::bigint) = hashtext('salescoach:scheduler:public')::bigint")
    assert len(locks) == 1, locks
    standby = next(b["host"] for b in beats if not b["leader"])
    killed = subprocess.run(["docker", "kill", lead["host"]], capture_output=True, text=True)
    assert killed.returncode == 0, killed.stderr
    t0 = time.monotonic()
    new = wait_until("the standby to take the lead",
                     lambda: (lambda x: x if x and x["host"] == standby else None)(_leader()), timeout=30, every=0.5)
    took = time.monotonic() - t0
    # docs/deploy-cloud.md: a standby tries every 5 s; the killed session's lock is released at once.
    assert took <= 5 + 5, took
    S["takeover_s"] = round(took, 1)
    S["leader"] = {"old": lead["host"], "new": new["host"]}
    EVIDENCE["leader"] = {**S["leader"], "takeover_s": S["takeover_s"], "advisory_locks_before": len(locks)}


# ---- 10. offboarding B hands B's work to A ---------------------------------------------------------------

def test_offboarding_b_to_a_ends_b_at_once_and_moves_the_work():
    b_call = S["call"][B]
    assert S[B].get("/").status_code == 200                                  # B is signed in right now
    r = S["admin"].post(f"/admin/users/{uid(B)}/offboard", {"mode": "reassign", "to_user_id": uid(A), "confirm": B})
    assert r.status_code == 303 and "Offboarded" in flash(r), flash(r)
    gone = S[B].get("/")
    assert gone.status_code == 303 and "/login" in gone.headers["location"]
    assert S[B].post(f"/calls/{b_call}/redraft", {}, headers={"accept": "application/json"}).status_code == 401
    assert q1("SELECT status FROM users WHERE email=%s", B)["status"] == "disabled"
    assert q1("SELECT COUNT(*) AS n FROM sessions WHERE user_id=%s AND revoked_at IS NULL", uid(B))["n"] == 0
    assert q1("SELECT owner_id FROM calls WHERE node_id=%s", b_call)["owner_id"] == uid(A)
    assert q1("SELECT COUNT(*) AS n FROM emails WHERE call_id=%s AND owner_id<>%s", b_call, uid(A))["n"] == 0
    assert S[A].get(f"/calls/{b_call}").status_code == 200
    assert S[M].get(f"/calls/{b_call}").status_code == 200
    assert f"/calls/{b_call}" in S[M].get("/calls").text
    again = Browser(B)
    refused = again._google(again.get("/auth/google"))
    assert refused.status_code == 403 and "switched off" in refused.text
    EVIDENCE["offboard"] = {"flash": flash(r), "b_after": f"{gone.status_code} -> {gone.headers['location']}",
                            "moved_call_owner": "A", "m_reads_moved_call": 200}


# ---- 11. import-sqlite into a second, empty database ----------------------------------------------------

def test_import_sqlite_round_trips_a_laptop_install(tmp_path):
    path = tmp_path / "laptop.db"
    env = {**os.environ, "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'tests'}"}
    for name in ("DATABASE_URL", "DATABASE_MIGRATE_URL", "SALESCOACH_MODE", "SALESCOACH_TEST_DATABASE_URL"):
        env.pop(name, None)
    built = subprocess.run([sys.executable, str(HERE / "build_sqlite.py"), str(path)], env=env, capture_output=True,
                           text=True, cwd=str(tmp_path))
    assert built.returncode == 0, built.stderr[-2000:]
    counts = json.loads(built.stdout.strip().splitlines()[-1])["counts"]
    assert counts["calls"] == 2 and counts["emails"] >= 1 and counts["loops"] > 0
    compose("cp", str(path), "web:/tmp/laptop.db")
    run = compose("exec", "-e", f"DATABASE_MIGRATE_URL={IMPORT_DB_INSIDE}", "web",
                  "salescoach", "import-sqlite", "/tmp/laptop.db", "--as", "maya@tessel.test", check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    user = q1("SELECT id, role, status FROM users WHERE email='maya@tessel.test'", url=IMPORT_DB)
    assert (user["role"], user["status"]) == ("rep", "active")
    mismatched = {}
    for table, n in counts.items():
        have = q1(f"SELECT COUNT(*) AS n FROM {table}", url=IMPORT_DB)["n"]
        extra = {"events": 1}.get(table, 0)                            # the import's own audit row
        if have != n + extra:
            mismatched[table] = (n, have)
    assert not mismatched, mismatched
    owners = q1("SELECT array_agg(DISTINCT owner_id) AS o FROM calls", url=IMPORT_DB)["o"]
    assert owners == [user["id"]]
    assert q1("SELECT COUNT(*) AS n FROM calls", url=OWNER_DB)["n"] == 2        # the org database is untouched
    S["import_counts"] = {t: n for t, n in counts.items() if n}
    EVIDENCE["import_sqlite"] = S["import_counts"]


def test_zz_evidence():
    """Not a check: the numbers behind the checks above, printed for the run's record (-rA shows them)."""
    print(json.dumps(EVIDENCE, indent=1, sort_keys=True))
