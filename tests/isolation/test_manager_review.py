"""The manager product under the row-level policies (Postgres only, cloud mode).

The org: team West (reps A, B and E) managed by M; team East (rep C) managed by N; D is an admin who
manages no team. Identity comes from a test header, as in test_route_crawl.py.

  * M reads A's and B's calls, deals, coach and learning pages, read only (a banner, no write form);
    N, B and D read none of A's; a rep never sees /team, and /calls shows a rep only their own.
  * Every write route of the app, requested by M on A's objects (enumerated from the route inventory,
    not picked by hand), answers 403 or 404 and changes nothing in any table.
  * Comments: M comments on A's call and turn; A reads and resolves; B cannot read them; N cannot
    comment on A's call; the policy refuses an author who is not the actor, an owner who is not the
    object's, and a rep's edit of a manager's text.
  * The access log: M's view of A's call is logged (once per ten minutes) and shown to A; B reads none.
  * Coaching notes: M writes one on A's Learning page; A sees it, B does not; nobody coaches themselves.
  * /team: every number matches a hand-computed fixture and the list it links to; the pattern roll-up
    shows a tag only at n >= 3 reps, never the email_voice family, and stores nothing.
"""
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from salescoach import identity, users
from salescoach.intel import methodology
from salescoach.manager import team
from salescoach.store import tenancy
from salescoach.store.stores import now
from salescoach.web import app as app_module

import factories
from conftest import seed_org_settings
from test_route_crawl import (FORMS, NOT_A_USERS_OBJECT, ORIGIN, _fill, _strip, cloud,  # noqa: F401
                              parameterised_routes, params_for)

pytestmark = pytest.mark.postgres_only

A, B, E, C, M, N, D = "u-a", "u-b", "u-e", "u-c", "u-m", "u-n", "u-d"
PEOPLE = ((A, "Asha Rao", "rep", "t-west"), (B, "Bala K", "rep", "t-west"), (E, "Esha P", "rep", "t-west"),
          (C, "Chitra S", "rep", "t-east"), (M, "Mani V", "manager", None), (N, "Neha J", "manager", None),
          (D, "Dev A", "admin", None))
ROLES = {uid: role for uid, _, role, _ in PEOPLE}
WRITE_FORM = re.compile(r'<form[^>]*method="post"[^>]*action="([^"]*)"|formaction="([^"]*)"')


def _as(db, uid):
    return identity.as_actor(db, identity.Actor(uid, role=ROLES[uid]))


@pytest.fixture
def org(db):
    users.create_team(db, "West", team_id="t-west")
    users.create_team(db, "East", team_id="t-east")
    for uid, name, role, team_id in PEOPLE:
        users.create(db, f"{uid}@tessel.test", name, role=role, team_id=team_id, user_id=uid)
    users.set_managers(db, "t-west", [M])
    users.set_managers(db, "t-east", [N])
    db.commit()
    seed_org_settings(db)
    return True


@pytest.fixture
def client(db, org, cloud):
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    return TestClient(app, follow_redirects=False)


def get(client, who, url):
    return client.get(url, headers={"x-test-user": who, "accept": "text/html"})


def post(client, who, url, data=None):
    return client.post(url, data=data or {}, headers={"x-test-user": who, "accept": "text/html", **ORIGIN})


def writes(html: str) -> list:
    """Every form action or formaction on a page that is not a comment or the logout."""
    return [a or b for a, b in WRITE_FORM.findall(html) if not (a or b).startswith(("/comments", "/logout"))]


# ---- data -------------------------------------------------------------------------------------------------

def _node(db, kind, owner, title):
    nid = f"{kind}-{uuid.uuid4().hex[:10]}"
    db.execute("INSERT INTO nodes(id,type,title,owner_id) VALUES (?,?,?,?)", (nid, kind, title, owner))
    return nid


def mk_call(db, owner, title, day, state="awaiting_review", deal=None, error=None):
    nid = _node(db, "call", owner, title)
    db.execute("INSERT INTO calls(node_id,deal_id,source,title,started_at,wf_state,wf_error,owner_id) "
               "VALUES (?,?,'paste',?,?,?,?,?)", (nid, deal, title, f"{day}T10:00:00+05:30", state, error, owner))
    db.execute("INSERT INTO turns(call_id,idx,tier,channel,text,owner_id) VALUES (?,0,'final','me','Hello there',?)",
               (nid, owner))
    db.execute("INSERT INTO turns(call_id,idx,tier,channel,text,owner_id) VALUES (?,1,'final','them','We need a pilot',?)",
               (nid, owner))
    return nid


def mk_deal(db, owner, name, status="active", score=None):
    nid = _node(db, "deal", owner, name)
    db.execute("INSERT INTO deals(node_id,name,status,owner_id) VALUES (?,?,?,?)", (nid, name, status, owner))
    if score is not None:
        db.execute("INSERT INTO deal_health(id,deal_id,score,owner_id) VALUES (?,?,?,?)", (f"h:{nid}", nid, score, owner))
    return nid


def mk_loop(db, owner, due, status="open", review="confirmed"):
    nid = _node(db, "loop", owner, "a loop")
    db.execute("INSERT INTO loops(node_id,type,description,owner,source,confidence,created_at,due_date,status,"
               "review_state,owner_id) VALUES (?,'my_action','Send the deck','me','explicit_commitment','high',?,?,?,?,?)",
               (nid, now(), due, status, review, owner))
    return nid


def mk_email(db, owner, call_id, status, sent_at=None):
    db.execute("INSERT INTO emails(call_id,kind,status,subject,body,created_at,sent_at,owner_id) "
               "VALUES (?,'followup',?,'Next steps','Hi',?,?,?)", (call_id, status, now(), sent_at, owner))


def mk_element(db, owner, deal, element, status):
    db.execute("INSERT INTO meddpicc(id,deal_id,element,status,owner_id) VALUES (?,?,?,?,?)",
               (f"{deal}:{element}", deal, element, status, owner))


def mk_talk(db, owner, values):
    for i, v in enumerate(values):
        db.execute("INSERT INTO pattern_observations(family,key,subject,value,observed_at,created_at,owner_id) "
                   "VALUES ('seller_series','talk_share',?,?,?,?,?)", (f"call:{owner}:{i}", v, f"2026-09-{i + 1:02d}", now(), owner))


def mk_pattern(db, owner, family, key, status="active", label="established"):
    db.execute("INSERT INTO learned_patterns(id,family,key,status,label,owner_id) VALUES (?,?,?,?,?,?)",
               (f"lp:{family}:u:{owner}:{key}", family, key, status, label, owner))


@pytest.fixture
def work(db, org):
    """A's work (the numbers below are computed by hand from it), one call of B's and one of C's."""
    with _as(db, A):                                              # cloud mode: the seller's zone needs an actor
        today = app_module.today_ist()
    day = lambda n: (today - timedelta(days=n)).isoformat()      # noqa: E731
    out = {"today": today, "day": day}
    with _as(db, A):
        d1, d2, d3 = mk_deal(db, A, "Northwind", score=40), mk_deal(db, A, "Eastline", score=80), mk_deal(db, A, "Crane")
        mk_deal(db, A, "Old win", status="won", score=10)
        c1 = mk_call(db, A, "Northwind weekly", day(0), deal=d1)
        c2 = mk_call(db, A, "Eastline intro", day(10), state="done", deal=d2)
        c3 = mk_call(db, A, "Old failure", day(40), error="analysis: boom")
        mk_email(db, A, c1, "sent", sent_at=f"{day(0)}T12:00:00+05:30")
        mk_email(db, A, c2, "drafted")
        mk_loop(db, A, day(1))                                    # overdue
        mk_loop(db, A, day(-1))                                   # due tomorrow
        mk_loop(db, A, day(1), status="done")                     # closed
        mk_loop(db, A, day(1), review="rejected")                 # rejected
        mk_element(db, A, d1, "metrics", "known")
        mk_element(db, A, d1, "champion", "partial")
        mk_element(db, A, d2, "metrics", "known")
        mk_talk(db, A, [0.7, 0.6, 0.65, 0.4, 0.45, 0.5])
        db.commit()
        out.update(a_call=c1, a_call2=c2, a_call3=c3, a_deal=d1, a_deals=(d1, d2, d3))
    with _as(db, B):
        out["b_call"] = mk_call(db, B, "Bala discovery", day(2))
        db.commit()
    with _as(db, C):
        out["c_call"] = mk_call(db, C, "Chitra demo", day(1))
        db.commit()
    return out


# ---- who sees what ------------------------------------------------------------------------------------

def test_the_manager_reads_the_teams_work_read_only_and_nobody_else_does(client, work):
    call, deal = work["a_call"], work["a_deal"]
    pages = {f"/calls/{call}": "Viewing Asha Rao's call", f"/deals/{deal}": "Viewing Asha Rao's deal",
             f"/coach?rep={A}": "Viewing Asha Rao's Coach page", f"/learning?rep={A}": "Viewing Asha Rao's Learning page",
             f"/loops?rep={A}": "Viewing Asha Rao's loops", f"/deals?rep={A}": "Viewing Asha Rao's deals"}
    for url, banner in pages.items():
        r = get(client, M, url)
        assert r.status_code == 200, url
        assert banner in r.text and "Read only" in r.text, url
        assert writes(r.text) == [], (url, writes(r.text))
    for url in (f"/deals/{deal}/intel", f"/deals/{deal}/outcome", f"/deals/{deal}/prep", f"/coach/intel?rep={A}",
                f"/calls/{call}/runs"):
        r = get(client, M, url)
        assert r.status_code == 200 and writes(r.text) == [], (url, writes(r.text))
    assert get(client, M, f"/calls/{work['b_call']}").status_code == 200           # B is on the team too
    # the other team's manager, a rep of the same team, and an admin who manages nothing: nothing of A's
    for who in (N, B, D):
        for url in (f"/calls/{call}", f"/deals/{deal}", f"/coach?rep={A}", f"/learning?rep={A}",
                    f"/deals/{deal}/intel", f"/coach/intel?rep={A}"):
            assert get(client, who, url).status_code == 404, (who, url)
    # the owner's own page is not read only
    own = get(client, A, f"/calls/{call}").text
    assert "Read only" not in own and f"/calls/{call}/complete" in writes(own)


def test_the_calls_index_is_scoped_by_the_database_not_by_the_query(client, work):
    listed = lambda who, q="": set(re.findall(r'href="/calls/(call-[0-9a-f]+)"', get(client, who, "/calls" + q).text))  # noqa: E731
    assert {work["a_call"], work["a_call2"], work["b_call"]} <= listed(M) and work["c_call"] not in listed(M)
    assert work["c_call"] in listed(N) and not ({work["a_call"], work["b_call"]} & listed(N))
    assert listed(A) == {work["a_call"], work["a_call2"], work["a_call3"]}
    assert listed(A, f"?rep={B}") == set()                                           # a rep sees only their own
    assert listed(M, f"?rep={A}&state=failed") == {work["a_call3"]}
    assert listed(M, f"?rep={B}") == {work["b_call"]}
    assert listed(M, f"?rep={A}&state=awaiting_review") == {work["a_call"]}
    assert listed(M, f"?rep={A}&state=done") == {work["a_call2"]}
    assert work["a_call"] in listed(M, f"?rep={A}&from={work['day'](0)}") and work["a_call2"] not in listed(M, f"?rep={A}&from={work['day'](0)}")
    assert listed(M, f"?deal={work['a_deal']}") == {work["a_call"]}
    assert listed(M, f"?rep={A}&email=sent") == {work["a_call"]}
    assert listed(M, f"?rep={A}&gap=economic_buyer") == {work["a_call"], work["a_call2"]}
    assert listed(M, f"?rep={A}&gap=metrics") == set()                               # known on both calls' deals
    assert "Rep" in get(client, M, "/calls").text and '<select name="rep">' not in get(client, A, "/calls").text


def test_a_rep_and_an_admin_without_a_team_get_no_team_page(client, work):
    assert get(client, A, "/team").status_code == 404
    assert 'href="/team"' not in get(client, A, "/").text
    admin = get(client, D, "/team")
    assert admin.status_code == 200 and "You manage no team" in admin.text and "/admin#teams" in admin.text
    assert 'href="/team"' not in get(client, D, "/").text
    assert "Team setup" in get(client, D, "/admin").text
    assert 'href="/team"' in get(client, M, "/").text


# ---- every write route, as the manager, on the rep's objects -----------------------------------------------

def _digest(pg_owner) -> dict:
    out = {}
    for table in sorted(tenancy.TABLE_CLASS):
        if table in ("sessions", "access_log", "schema_migrations", "schema_repeatables"):
            continue
        out[table] = pg_owner.execute(
            f"SELECT md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {table} x").fetchone()[0]
    return out


@pytest.fixture
def a_objects(db, org):
    """Rep A's objects, one of each kind a route can name (as in test_route_crawl), plus a comment of A's."""
    from salescoach.orchestrator import bus
    from salescoach.schemas.events import Event
    with _as(db, A):
        own = factories.Owner(db, A)
        made = {t: factories.insert(db, t, A, own) for t in
                ("calls", "deals", "loops", "emails", "email_replies", "prep_briefs", "learned_patterns",
                 "learning_proposals", "calendar_meetings", "agent_runs", "memory_conflicts", "nudges", "comments")}
        nudge_email = factories.insert(db, "emails", A, own)
        db.execute("UPDATE emails SET kind='nudge' WHERE id=?", (nudge_email["id"],))
        db.execute("UPDATE learning_proposals SET status='open' WHERE id=?", (made["learning_proposals"]["id"],))
        db.execute("UPDATE memory_conflicts SET status='open' WHERE id=?", (made["memory_conflicts"]["id"],))
        assert bus.publish(db, Event(type="CALL_ENDED", entity_id=made["calls"]["node_id"], dedupe_key="mgr:1"))
        db.commit()
        wf_event = db.execute("SELECT id FROM wf_events WHERE dedupe_key='mgr:1'").fetchone()[0]
        person = own.node("person")
        connection_id = "rc-" + "b" * 32                  # A's recorder connection (Phase 4), a real-shaped id
        db.execute("INSERT INTO source_connections(id,owner_id,kind,secret_enc,key_id,created_at) "
                   "VALUES (?,?,?,?,?,?)", (connection_id, A, "fireflies", "Y2lwaGVy", "k1", "2026-09-24T00:00:00+00:00"))
        db.commit()
    return {
        "connection_id": connection_id,
        "call_id": made["calls"]["node_id"], "deal_id": made["deals"]["node_id"], "loop_id": made["loops"]["node_id"],
        "email_id": made["emails"]["id"], "nudge_email_id": nudge_email["id"], "reply_id": made["email_replies"]["id"],
        "proposal_id": made["learning_proposals"]["id"], "run_id": made["agent_runs"]["id"],
        "conflict_id": made["memory_conflicts"]["id"], "nudge_id": made["nudges"]["id"], "person_id": person,
        "comment_id": made["comments"]["id"], "meeting_event_id": made["calendar_meetings"]["event_id"],
        "wf_event_id": wf_event, "pattern_id": made["learned_patterns"]["id"],
        "element": "metrics", "risk_type": "budget", "decision": "accept",
    }


def test_every_write_route_refuses_the_manager_and_changes_nothing(db, org, a_objects, cloud, pg_owner):
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    client = TestClient(app, follow_redirects=False)
    assert get(client, M, f"/calls/{a_objects['call_id']}").status_code == 200       # M can read it
    before = _digest(pg_owner)
    problems, checked = [], 0
    targets = []
    for path, methods in parameterised_routes(app):
        writes_here = sorted(m for m in methods if m not in ("GET", "HEAD", "OPTIONS"))
        if path in NOT_A_USERS_OBJECT or not writes_here:
            continue
        values = params_for(path, a_objects)
        assert values is not None, path
        for method in writes_here:
            targets.append((method, path, _fill(path, values), FORMS.get(path, {}), values))
    # the write routes that take the object's id in the form rather than the path
    targets += [("POST", "/learning/patterns", "/learning/patterns", {"id": a_objects["pattern_id"], "action": a}, {})
                for a in ("confirm", "wrong", "retire", "no_prompt")]
    for method, path, url, form, values in targets:
        r = client.request(method, url, data=form, headers={"x-test-user": M, "accept": "text/html", **ORIGIN})
        checked += 1
        if r.status_code not in (403, 404):
            nowhere = {k: (v if k in ("element", "risk_type", "decision") else 999999 if isinstance(v, int)
                           else f"missing-{k}") for k, v in values.items()}
            base = client.request(method, _fill(path, nowhere), data=form,
                                  headers={"x-test-user": M, "accept": "text/html", **ORIGIN})
            if (r.status_code, _strip(r.headers.get("location", ""))) != (base.status_code,
                                                                          _strip(base.headers.get("location", ""))):
                problems.append(f"{method} {url} -> {r.status_code} {r.headers.get('location', '')}")
        after = _digest(pg_owner)
        changed = [t for t in before if before[t] != after[t]]
        if changed:
            problems.append(f"{method} {url} changed {changed}")
            before = after
    assert not problems, "\n".join(problems)
    assert checked >= 40, checked


def test_a_manager_cannot_send_a_reps_email(db, org, a_objects, cloud):
    """The send button is not there, the route answers 403, and policy.approve_and_send refuses anyway."""
    from salescoach.execution import policy
    with _as(db, M):
        with pytest.raises(policy.SendRefused):
            policy.approve_and_send(db, a_objects["email_id"], object(), mode="send", user_added=[])
        db.rollback()


# ---- comments -------------------------------------------------------------------------------------------

def test_comment_rules(client, db, work, pg_owner):
    from psycopg import errors
    call, deal = work["a_call"], work["a_deal"]
    r = post(client, M, "/comments", {"entity_type": "call", "entity_id": call, "body": "Ask for the CFO's date"})
    assert r.status_code == 303 and "#c-" in r.headers["location"]
    assert post(client, M, "/comments", {"entity_type": "call", "entity_id": call, "turn_idx": "1",
                                        "body": "Good moment to probe"}).status_code == 303
    assert post(client, M, "/comments", {"entity_type": "deal", "entity_id": deal, "body": "Who signs?"}).status_code == 303
    row = pg_owner.execute("SELECT * FROM comments WHERE body='Ask for the CFO''s date'").fetchone()
    assert (row["owner_id"], row["author_id"], row["entity_type"]) == (A, M, "call")

    page = get(client, A, f"/calls/{call}").text                 # the rep reads it, and the moment's comment
    assert "Ask for the CFO&#39;s date" in page and "Good moment to probe" in page
    today = get(client, A, "/").text
    assert "3 comments from Mani V" in today
    assert "Who signs?" in get(client, A, f"/deals/{deal}").text
    # the other rep of the team and the other team's manager cannot read or add to it
    with _as(db, B):
        assert db.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0
    with _as(db, N):
        assert db.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0
    assert post(client, N, "/comments", {"entity_type": "call", "entity_id": call, "body": "x"}).status_code == 404
    assert post(client, B, "/comments", {"entity_type": "call", "entity_id": call, "body": "x"}).status_code == 404
    assert "comments from" not in get(client, B, "/").text

    # resolve: the rep (or the author); nobody else. Delete: the author only.
    other = pg_owner.execute("SELECT id FROM comments WHERE body='Good moment to probe'").fetchone()[0]
    assert post(client, N, f"/comments/{row['id']}/resolve").status_code == 404
    assert post(client, A, f"/comments/{row['id']}/delete").status_code == 403
    assert post(client, A, f"/comments/{row['id']}/resolve").status_code == 303
    assert pg_owner.execute("SELECT resolved_by FROM comments WHERE id=?", (row["id"],)).fetchone()[0] == A
    assert post(client, M, f"/comments/{other}/delete").status_code == 303
    assert "2 comments" not in get(client, A, "/").text and "1 comment from Mani V" in get(client, A, "/").text

    # the policies hold without the app: author must be the actor; owner must be the object's; the rep
    # may resolve a manager's comment but not rewrite it
    with _as(db, N):
        with pytest.raises(errors.InsufficientPrivilege):
            db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                       "VALUES (?,?,'call',?,'x',?)", (A, N, call, now()))
        db.rollback()
    with _as(db, M):
        with pytest.raises(errors.InsufficientPrivilege):                         # posing as the rep
            db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                       "VALUES (?,?,'call',?,'x',?)", (A, A, call, now()))
        db.rollback()
        with pytest.raises(errors.InsufficientPrivilege):                         # C's call filed under A
            db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                       "VALUES (?,?,'call',?,'x',?)", (A, M, work["c_call"], now()))
        db.rollback()
    deal_comment = pg_owner.execute("SELECT id FROM comments WHERE body='Who signs?'").fetchone()[0]
    with _as(db, A):
        with pytest.raises(errors.InsufficientPrivilege):
            db.execute("UPDATE comments SET body='edited by the rep' WHERE id=?", (deal_comment,))
        db.rollback()
        assert db.execute("DELETE FROM comments WHERE id=?", (deal_comment,)).rowcount == 0
        db.rollback()


def test_the_access_log(client, db, work):
    call = work["a_call"]
    assert get(client, A, f"/calls/{call}").status_code == 200                # the owner's own view is not logged
    assert get(client, M, f"/calls/{call}").status_code == 200
    assert get(client, M, f"/calls/{call}").status_code == 200                # throttled: still one row
    assert get(client, M, f"/deals/{work['a_deal']}").status_code == 200
    with _as(db, A):
        rows = db.execute("SELECT viewer_id, owner_user_id, entity_type FROM access_log ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [(M, A, "call"), (M, A, "deal")]
    with _as(db, B):
        assert db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
    with _as(db, N):
        assert db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
    page = get(client, A, f"/calls/{call}").text
    assert "Viewed by" in page and "Mani V" in page
    from psycopg import errors
    with _as(db, M):                                                              # insert-only, and truthful
        assert db.execute("UPDATE access_log SET viewer_id='x'").rowcount == 0
        assert db.execute("DELETE FROM access_log").rowcount == 0
        with pytest.raises(errors.InsufficientPrivilege):
            db.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
                       "VALUES (?,?,'call',?,?)", (A, A, call, now()))
        db.rollback()
        with pytest.raises(errors.InsufficientPrivilege):
            db.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
                       "VALUES (?,?,'call',?,?)", (M, B, call, now()))
        db.rollback()


def test_coaching_notes(client, db, work):
    r = post(client, M, "/comments", {"entity_type": "coaching", "entity_id": A,
                                     "body": "Let the buyer finish before you pitch."})
    assert r.status_code == 303 and "/learning" in r.headers["location"]
    assert "Let the buyer finish" in get(client, A, "/learning").text
    assert "Let the buyer finish" in get(client, M, f"/learning?rep={A}").text
    assert "Let the buyer finish" not in get(client, B, "/learning").text
    assert "Let the buyer finish" not in get(client, M, f"/learning?rep={B}").text
    assert post(client, A, "/comments", {"entity_type": "coaching", "entity_id": A, "body": "x"}).status_code == 404
    assert post(client, N, "/comments", {"entity_type": "coaching", "entity_id": A, "body": "x"}).status_code == 404
    assert post(client, B, "/comments", {"entity_type": "coaching", "entity_id": A, "body": "x"}).status_code == 404
    assert "1 comment from Mani V" in get(client, A, "/").text


# ---- /team ----------------------------------------------------------------------------------------------

def test_the_team_numbers_match_the_fixture_and_their_lists(client, db, work):
    with _as(db, M):
        data = team.dashboard(db, work["today"])
    rows = {r["id"]: r for r in data["reps"]}
    assert set(rows) == {A, B, E}                                  # never C (another team) or M themselves
    a = rows[A]
    keys = methodology.active().keys
    three = [k for k in keys if k not in ("metrics", "champion")][:3]
    assert (a["calls_week"], a["calls_4w"], a["awaiting"], a["overdue_loops"]) == (1, 2, 1, 1)
    assert (a["open_deals"], a["median_health"], a["n_scored"]) == (3, 60, 2)
    assert [g["key"] for g in a["gaps"]] == three and all((g["unknown"], g["of"]) == (3, 3) for g in a["gaps"])
    assert a["talk_share"]["direction"] == "down" and round(a["talk_share"]["latest"], 2) == 0.45
    assert round(a["talk_share"]["before"], 2) == 0.65 and a["talk_share"]["n"] == 6
    assert (a["emails_sent"], a["emails_drafted"]) == (1, 2)
    assert a["last_activity"] == f"{work['day'](0)}T12:00:00+05:30"
    b = rows[B]
    assert (b["calls_week"], b["calls_4w"], b["awaiting"], b["overdue_loops"], b["open_deals"]) == (1, 1, 1, 0, 0)
    assert b["median_health"] is None and b["gaps"] == [] and b["talk_share"] is None
    assert rows[E]["calls_4w"] == 0 and rows[E]["last_activity"] is None

    page = get(client, M, "/team")
    assert page.status_code == 200 and "Asha Rao" in page.text and "Chitra" not in page.text
    w = data["windows"]
    # each number opens the list it counts, and the list has exactly that many rows
    def n_calls(q):
        return len(set(re.findall(r'href="/calls/(call-[0-9a-f]+)"', get(client, M, "/calls?" + q).text)))
    assert n_calls(f"rep={A}&from={w['week_from']}") == 1 and f"/calls?rep={A}&amp;from={w['week_from']}" in page.text
    assert n_calls(f"rep={A}&from={w['month_from']}") == 2
    assert n_calls(f"rep={A}&state=awaiting_review") == 1
    assert n_calls(f"rep={A}&from={w['month_from']}&email=drafted") == 2
    assert n_calls(f"rep={A}&from={w['month_from']}&email=sent") == 1
    loops = get(client, M, f"/loops?rep={A}&due=overdue").text
    assert "1 overdue" in loops and "Viewing Asha Rao's loops" in loops
    deals = get(client, M, f"/deals?rep={A}&status=active").text
    assert "3 open deals" in deals


def test_the_pattern_rollup_needs_three_reps_and_never_reads_email_voice(client, db, work, pg_owner):
    for rep in (A, B, E):
        with _as(db, rep):
            mk_pattern(db, rep, "seller", "monologues", label="established" if rep == A else "emerging")
            mk_pattern(db, rep, "email_voice", "too_formal")
            if rep != E:
                mk_pattern(db, rep, "seller", "no_next_step")                   # two reps: under the minimum
            db.commit()
    with _as(db, C):
        mk_pattern(db, C, "seller", "no_next_step")                             # another team's rep: not counted
        db.commit()
    stored = pg_owner.execute("SELECT COUNT(*), COUNT(DISTINCT scope) FROM learned_patterns").fetchone()
    with _as(db, M):
        roll = team.rollup(db, [A, B, E])
    assert [(t["key"], t["active"], t["established"]) for t in roll["tags"]] == [("monologues", 3, 1)]
    assert roll["hidden"] == 1 and roll["min_n"] == 3
    page = get(client, M, "/team").text
    assert "Monologues" in page and "3 of 3" in page and "Too formal" not in page and "too_formal" not in page
    assert "No next step" not in page
    assert pg_owner.execute("SELECT COUNT(*), COUNT(DISTINCT scope) FROM learned_patterns").fetchone() == stored
    assert team.NEVER_ROLLED_UP == ("email_voice",) and "email_voice" not in team.ROLLUP_FAMILIES
