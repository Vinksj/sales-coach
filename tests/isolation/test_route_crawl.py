"""Every parameterised route, requested as rep B with rep A's ids, answers 404 (Postgres, cloud mode).

The app is built in cloud mode; identity comes from a test header the AuthGate's session lookup is
patched to read (the login mechanism itself is tests/isolation/test_actor_gate.py and
tests/test_google_auth.py; this
file is about what a route does once it knows who is asking). Rep A owns a call, a deal, a loop,
a follow-up email, a nudge, a reply, a prep brief, a learned pattern, a proposal, a calendar
meeting, an agent run, a memory conflict, a live-coach nudge, a recorder connection, a comment and a bus event; B asks for each of
them through every route that takes their id, with GET and with the route's methods and a minimal
valid form body. A 200 would be a leak, a 403 would confirm the object exists, a 500 is a route
that trips over a missing row instead of saying 404: only 404 passes.

Inventory: every route with a path parameter must either resolve all its parameters from PARAMS /
ROUTE_PARAMS, or be listed in NOT_A_USERS_OBJECT with the reason; a new route fails the inventory
until it is placed deliberately.
"""
import pytest
from fastapi.testclient import TestClient

from salescoach import identity, users
from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.store import stores
from salescoach.web import app as app_module

import factories
from conftest import seed_org_settings

pytestmark = pytest.mark.postgres_only

ORIGIN = {"origin": "http://127.0.0.1:8140"}
A, B = "u-a", "u-b"

# Routes whose parameter is not one user's object: org configuration and the org directory, both
# admin-only in cloud mode (setupui: 403 to anyone else; adminui._admin_only: 404 to anyone else).
NOT_A_USERS_OBJECT = {
    "/setup/method/custom/{key}": "an org methodology definition",
    "/setup/method/custom/{editing}": "an org methodology definition",
    "/setup/method/custom/{key}/delete": "an org methodology definition",
    "/setup/sources/{kind}": "a local install's org-level source (refused in cloud mode; reps connect their own)",
    "/me/recorders/{kind}/connect": "acts on the ACTING rep's own connection of that recorder kind; names nobody's id",
    "/import/webhook/{connection_id}": ("a recorder's push, no session: authenticated by the connection's own secret "
                                        "or signature and imported as that connection's owner only "
                                        "(tests/test_recorders_web.py)"),
    "/admin/teams/{team_id}": "a team of the org directory (admin only)",
    "/admin/teams/{team_id}/managers": "a team of the org directory (admin only)",
    "/admin/users/{user_id}": "a person in the org directory (admin only)",
    "/admin/users/{user_id}/disable": "a person in the org directory (admin only)",
    "/admin/users/{user_id}/enable": "a person in the org directory (admin only)",
    "/admin/users/{user_id}/logout": "a person in the org directory (admin only)",
    "/admin/users/{user_id}/offboard": "a person in the org directory (admin only)",
}

# Minimal valid bodies for POST routes that validate their form before looking the object up.
FORMS = {
    "/calls/{call_id}/rerun": {"from_step": "analysis"},
    "/conflicts/{conflict_id}/resolve": {"accept": "yes"},
    "/followups/{loop_id}/snooze": {"until": "2030-01-01"},
    "/deals/{deal_id}/meddpicc/{element}": {"status": "known"},
    "/deals/{deal_id}/risks/{risk_type}/status": {"status": "open"},
    "/loops/{loop_id}/status": {"status": "done"},
    "/loops/{loop_id}/edit": {"description": "x"},
    "/calls/{call_id}/loops": {"description": "x", "owner": "me", "type": "my_action"},
    "/calls/{call_id}/deal": {"deal_id": ""},
    "/calls/{call_id}/participants": {"person_id": ""},
    "/calls/{call_id}/speakers": {},
    "/calls/{call_id}/speaker": {"cluster": "S1", "who": "me"},
    "/deals/{deal_id}/people": {"person_id": ""},
    "/deals/{deal_id}/outcome": {"status": "won"},
    "/coach/replay/{call_id}": {},
}


def _walk(routes):
    for r in routes:
        original = getattr(r, "original_router", None)
        if original is not None:
            yield from _walk(original.routes)
        elif hasattr(r, "endpoint") and getattr(r, "path", None):
            yield r


def parameterised_routes(app):
    return sorted({(r.path, tuple(sorted(r.methods or ()))) for r in _walk(app.routes) if "{" in r.path})


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    # Identity from a header, so the crawl does not depend on the login mechanism of the day.
    # The AuthGate (web/auth.py) resolves the actor; the header stands in for a live session of that rep.
    from salescoach.web import auth
    def from_header(scope, headers):
        user_id = headers.get("x-test-user")
        if not user_id:
            return None
        with identity.activate(None):
            conn = stores.sales()
        try:
            with conn.as_system():                     # as the real gate: the users row becomes the actor
                row = users.get(conn, user_id)
        finally:
            conn.close()
        return (users.as_actor(row), None) if row else None
    monkeypatch.setattr(auth, "_cloud_actor", from_header)
    return True


@pytest.fixture
def two_reps(db):
    """A and B exist before cloud mode is switched on (the local admin creates them)."""
    for uid, name in ((A, "Asha Rao"), (B, "Bala K")):
        users.create(db, f"{uid}@tessel.test", name, role="rep", user_id=uid)
    db.commit()
    seed_org_settings(db)                          # cloud mode reads the org profile from org_settings


@pytest.fixture
def objects(db, two_reps):
    """Rep A's objects, one of each kind a route can name."""
    with identity.as_actor(db, identity.Actor(A, role="rep")):
        own = factories.Owner(db, A)
        made = {t: factories.insert(db, t, A, own) for t in
                ("calls", "deals", "loops", "emails", "email_replies", "prep_briefs", "learned_patterns",
                 "learning_proposals", "calendar_meetings", "agent_runs", "memory_conflicts", "nudges", "comments")}
        nudge_email = factories.insert(db, "emails", A, own)
        db.execute("UPDATE emails SET kind='nudge' WHERE id=?", (nudge_email["id"],))
        db.execute("UPDATE learning_proposals SET status='open' WHERE id=?", (made["learning_proposals"]["id"],))
        assert bus.publish(db, Event(type="CALL_ENDED", entity_id=made["calls"]["node_id"], dedupe_key="crawl:1"))
        db.commit()
        wf_event = db.execute("SELECT id FROM wf_events WHERE dedupe_key='crawl:1'").fetchone()[0]
        person = own.node("person")
        connection_id = "rc-" + "a" * 32                  # A's recorder connection, with a real-shaped id
        db.execute("INSERT INTO source_connections(id,owner_id,kind,secret_enc,key_id,created_at) "
                   "VALUES (?,?,?,?,?,?)", (connection_id, A, "fireflies", "Y2lwaGVy", "k1", "2026-09-24T00:00:00+00:00"))
        db.commit()
    return {
        "call_id": made["calls"]["node_id"], "deal_id": made["deals"]["node_id"], "loop_id": made["loops"]["node_id"],
        "email_id": made["emails"]["id"], "nudge_email_id": nudge_email["id"], "reply_id": made["email_replies"]["id"],
        "proposal_id": made["learning_proposals"]["id"], "run_id": made["agent_runs"]["id"],
        "conflict_id": made["memory_conflicts"]["id"], "nudge_id": made["nudges"]["id"], "person_id": person,
        "comment_id": made["comments"]["id"],
        "meeting_event_id": made["calendar_meetings"]["event_id"], "wf_event_id": wf_event,
        "connection_id": connection_id,
        "element": "metrics", "risk_type": "budget", "decision": "accept",
    }


def params_for(path: str, objects: dict) -> dict:
    """The values for a route's parameters, or None when one cannot be resolved."""
    route_specific = {
        "/events/{event_id}/retry": {"event_id": objects["wf_event_id"]},
        "/calendar/{event_id}/record": {"event_id": objects["meeting_event_id"]},
    }
    values = dict(route_specific.get(path, {}))
    names = [p.strip("{}") for p in _placeholders(path)]
    for name in names:
        if name in values:
            continue
        if path.startswith("/nudges/") and name == "email_id":
            values[name] = objects["nudge_email_id"]
        elif name in objects:
            values[name] = objects[name]
        else:
            return None
    return values


def _placeholders(path):
    import re
    return re.findall(r"\{[^}]+\}", path)


@pytest.fixture
def client(db, objects, cloud):                    # the store and A's objects first (local mode), then cloud mode
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    return TestClient(app, follow_redirects=False)


def _request(client, method, url, path):
    headers = {"x-test-user": B, "accept": "text/html"}
    if method == "GET":
        if url.endswith("/events"):
            with client.stream("GET", url, headers=headers) as r:
                return r.status_code, ""
        r = client.get(url, headers=headers)
    else:
        r = client.request(method, url, data=FORMS.get(path, {}), headers={**headers, **ORIGIN})
    return r.status_code, r.headers.get("location", "")


def test_inventory_every_parameterised_route_is_placed(db, objects, cloud):
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    routes = parameterised_routes(app)
    assert len(routes) >= 60, "the route inventory shrank; check plugins.routers()"
    unplaced = [path for path, _ in routes if path not in NOT_A_USERS_OBJECT and params_for(path, objects) is None]
    assert not unplaced, ("new routes must be placed: add their parameter to `objects` (rep A's object of that "
                          f"kind) or the route to NOT_A_USERS_OBJECT with a reason: {unplaced}")
    stale = set(NOT_A_USERS_OBJECT) - {p for p, _ in routes}
    assert not stale, f"NOT_A_USERS_OBJECT names routes that no longer exist: {sorted(stale)}"


def _fill(path, values):
    url = path
    for name, value in values.items():
        url = url.replace("{" + name + "}", str(value))
    return url


def test_every_owned_route_answers_404_to_another_rep(client, objects):
    """404, or exactly the answer a nonexistent id gets (a route that redirects with "not on the calendar
    any more" or refuses live capture in cloud before looking says the same to B as to nobody); never
    200, 403, 405, 422 or 500, and never a redirect that differs from the nonexistent case."""
    routes = parameterised_routes(client.app)
    leaks, checked = [], 0
    for path, methods in routes:
        if path in NOT_A_USERS_OBJECT:
            continue
        values = params_for(path, objects)
        nowhere = {k: (v if k in ("element", "risk_type", "decision") else 999999 if isinstance(v, int) else f"missing-{k}")
                   for k, v in values.items()}
        for method in sorted(methods):
            status, location = _request(client, method, _fill(path, values), path)
            checked += 1
            if status == 404:
                continue
            baseline = _request(client, method, _fill(path, nowhere), path)
            if status in (200, 403, 405, 422, 500) or (status, _strip(location)) != (baseline[0], _strip(baseline[1])):
                leaks.append((method, _fill(path, values), status, location, baseline))
    assert not leaks, "\n".join(f"{m} {u} -> {s} {loc} (nonexistent id: {b})" for m, u, s, loc, b in leaks)
    assert checked >= len(routes) - len(NOT_A_USERS_OBJECT)


def _strip(location: str) -> str:
    import re
    return re.sub(r"(call|deal|loop|missing)-[\w-]+", "<id>", location or "")


def test_the_owner_still_reaches_their_pages(client, objects):
    """The 404s above are B's, not everyone's: A gets their call, deal and nudge pages."""
    headers = {"x-test-user": A, "accept": "text/html"}
    for url in (f"/calls/{objects['call_id']}", f"/deals/{objects['deal_id']}", f"/nudges/{objects['nudge_email_id']}",
                f"/runs/{objects['run_id']}", f"/deals/{objects['deal_id']}/intel", f"/deals/{objects['deal_id']}/prep"):
        assert client.get(url, headers=headers).status_code == 200, url
