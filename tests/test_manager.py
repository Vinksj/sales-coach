"""The manager product (Phase 7), the parts that hold on either backend:

  * nothing a person writes for another person (comments, coaching notes) or reads about a team (the
    roll-up) ever reaches a prompt: no prompt-building module reads those tables or imports the
    package, and a pipeline run over a call and a deal carrying comments sends none of their text;
  * the single-user install: the team pages say they need the cloud install; comments work for the
    one user on their own calls (a rep may comment on their own work) and are not "comments from
    someone" on Today; the calls index lists the user's calls;
  * the read-only rule's building blocks (manager/access.py) and the bus refusing an interactive
    publish on someone else's object.
Team visibility itself is Postgres-only: tests/isolation/test_manager_review.py.
"""
import re
from pathlib import Path

import pytest

import salescoach
from salescoach import identity
from salescoach.manager import access, comments, team, views
from salescoach.orchestrator import bus, worker
from salescoach.schemas.events import Event
from salescoach.sources import paste
from test_core_pipeline import CALL2, _script
from test_web import ORIGIN, FakeGmail, FakeLive, app, client, gmail, live, processed  # noqa: F401

PKG = Path(salescoach.__file__).parent
MARKER = "ZEBRA-QUOKKA-7731"
TABLES = re.compile(r"\b(FROM|JOIN|INTO|UPDATE|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+(comments|access_log)\b", re.I)

# The only modules that may run SQL on comments / access_log: the manager package itself and the schema.
# lifecycle/retention.py only DELETES them, with the expired call they are about (Phase 8); it builds no prompt.
SQL_ALLOWED = {"manager/comments.py", "manager/views.py", "store/migrate.py", "store/rls.py", "lifecycle/retention.py"}
# Modules allowed to import salescoach.manager: page and route code, and the bus's ownership check.
# None of them builds a prompt (the list below is checked against the prompt builders too).
IMPORT_ALLOWED = {"web/app.py", "intel/web.py", "learning/web.py", "automation/web.py", "plugins/live_coach.py",
                  "plugins/manager.py", "orchestrator/bus.py"}
IMPORTS_MANAGER = re.compile(r"(from\s+(\.\.|salescoach\.)manager\b|from\s+\.\.\s+import\s+[^\n]*\bmanager\b|"
                             r"import\s+salescoach\.manager)")


def _sources():
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith("manager/"):
            continue
        yield rel, path.read_text()


def _prompt_builders() -> set:
    """Modules that assemble text for a model: they call seller.prompt/render, build an agent, or run one."""
    found = set()
    for rel, text in _sources():
        if re.search(r"seller\.(prompt|render)\(|\bAgent\b|\.run_agent\(|extract_structured\(|\.generate\(|"
                     r"for_prompt\(|prompts?/", text):
            found.add(rel)
    return found


def test_no_module_outside_the_manager_package_reads_comments_or_the_access_log():
    offenders = [rel for rel, text in _sources() if TABLES.search(text) and rel not in SQL_ALLOWED]
    assert not offenders, f"only salescoach/manager may read comments / access_log: {offenders}"
    for rel, text in _sources():                               # nor names the tables in a prompt template
        if rel.endswith(".py") and ("prompts" in rel):
            assert "comments" not in text.lower(), rel
    for prompt in (PKG / "prompts").rglob("*") if (PKG / "prompts").exists() else ():
        if prompt.is_file():
            assert not TABLES.search(prompt.read_text(errors="ignore")), prompt


def test_no_prompt_building_module_imports_the_manager_package():
    importers = {rel for rel, text in _sources() if IMPORTS_MANAGER.search(text)}
    assert importers <= IMPORT_ALLOWED, f"unexpected importers of salescoach.manager: {importers - IMPORT_ALLOWED}"
    builders = _prompt_builders()
    assert builders, "the prompt-builder scan found nothing; the pattern is stale"
    assert not (importers & builders), f"a prompt-building module imports salescoach.manager: {importers & builders}"
    # the team roll-up and the coaching notes are read by the manager pages alone
    users_of = {p.relative_to(PKG).as_posix() for p in (PKG / "manager").glob("*.py")
                if re.search(r"\bteam\.(dashboard|rollup)\(|from \. import[^\n]*\bteam\b", p.read_text())}
    assert users_of <= {"manager/web.py", "manager/__init__.py"}, users_of


def test_a_pipeline_run_over_commented_work_sends_none_of_the_comments(db, fake_llm, processed):  # noqa: F811
    call, deal, email = processed["call"], processed["deal"], processed["email"]
    comments.add(db, "call", call, f"general {MARKER}")
    comments.add(db, "call", call, f"moment {MARKER}", turn_idx=1)
    comments.add(db, "deal", deal, f"deal {MARKER}")
    comments.add(db, "email", email["id"], f"email {MARKER}")
    db.commit()
    fake_llm.calls.clear()
    _script(fake_llm, {"actions": [], "loop_updates": [], "notes": ""})
    paste.import_text(db, CALL2, "NWP second", deal_id=deal, participants=processed["people"])
    bus.publish(db, Event(type="PROCESS_CALL", entity_id=call, dedupe_key="redraft:test",
                          payload={"from": "email_drafted", "force": True}))
    db.commit()
    worker.drain(db)
    assert fake_llm.calls, "nothing was sent to the model: the test proves nothing"
    for sent in fake_llm.calls:
        assert MARKER not in (sent["system"] or "") and MARKER not in (sent["prompt"] or ""), sent["schema"]


# ---- the single-user install -------------------------------------------------------------------------

@pytest.mark.sqlite_only          # SQLite is the single-user install; a local-mode Postgres store behaves the same
def test_team_pages_need_the_cloud_install(client):  # noqa: F811
    r = client.get("/team", headers={"accept": "text/html"})
    assert r.status_code == 404 and "Team features need the cloud install" in r.text
    home = client.get("/")
    assert 'href="/team"' not in home.text                     # no Team in the nav


def test_the_one_user_comments_on_their_own_call(client, processed, db):  # noqa: F811
    call, deal = processed["call"], processed["deal"]
    r = client.post(headers=ORIGIN, url="/comments", data={"entity_type": "call", "entity_id": call, "body": "Push for the CFO date",
                                       "next": f"/calls/{call}"}, follow_redirects=False)
    assert r.status_code == 303 and "#c-" in r.headers["location"]
    r = client.post(headers=ORIGIN, url="/comments", data={"entity_type": "call", "entity_id": call, "body": "This moment", "turn_idx": "2"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/calls/{call}").text
    assert "Push for the CFO date" in page and "This moment" in page and "Read only" not in page
    assert "Viewed by" not in page                             # the owner's own visits are not logged
    assert db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
    row = db.execute("SELECT * FROM comments WHERE body='This moment'").fetchone()
    assert row["turn_idx"] == 2 and row["owner_id"] == row["author_id"] == identity.LOCAL_USER
    # a comment one wrote oneself is not "a comment from someone" on Today
    assert "Comments on your work" not in client.get("/").text
    # resolve, delete; a turn the call does not have and an empty body are refused with a message
    assert client.post(headers=ORIGIN, url=f"/comments/{row['id']}/resolve", follow_redirects=False).status_code == 303
    assert db.execute("SELECT resolved_by FROM comments WHERE id=?", (row["id"],)).fetchone()[0] == identity.LOCAL_USER
    assert client.post(headers=ORIGIN, url=f"/comments/{row['id']}/delete", follow_redirects=False).status_code == 303
    assert db.execute("SELECT COUNT(*) FROM comments WHERE id=?", (row["id"],)).fetchone()[0] == 0
    bad = client.post(headers=ORIGIN, url="/comments", data={"entity_type": "call", "entity_id": call, "body": "x", "turn_idx": "999"},
                      follow_redirects=False)
    assert "err=" in bad.headers["location"]
    assert "err=" in client.post(headers=ORIGIN, url="/comments", data={"entity_type": "deal", "entity_id": deal, "body": "  "},
                                 follow_redirects=False).headers["location"]
    assert client.post(headers=ORIGIN, url="/comments", data={"entity_type": "call", "entity_id": "call-nope", "body": "x"},
                       follow_redirects=False).status_code == 404
    # nobody coaches themselves
    assert client.post(headers=ORIGIN, url="/comments", data={"entity_type": "coaching", "entity_id": identity.LOCAL_USER, "body": "x"},
                       follow_redirects=False).status_code == 404
    client.post(headers=ORIGIN, url="/comments", data={"entity_type": "deal", "entity_id": deal, "body": "Who signs?"})
    assert "Who signs?" in client.get(f"/deals/{deal}").text


def test_the_calls_index_lists_the_users_calls(client, processed):  # noqa: F811
    page = client.get("/calls").text
    assert "NWP weekly" in page and "Every call you have recorded" in page
    assert "NWP weekly" in client.get("/calls?state=awaiting_review").text
    assert "No calls match" in client.get("/calls?state=done").text
    assert "No calls match" in client.get("/calls?from=2999-01-01").text


def test_access_helpers_on_the_single_user_install(db):
    assert access.visible_owner_ids(db) == [identity.LOCAL_USER]
    assert access.managed_reps(db) == [] and not access.manages_team(db)
    assert not access.readonly(db, identity.LOCAL_USER) and access.readonly(db, "u-someone")
    assert not access.readonly(db, None)                        # the org directory
    with access.write_request():
        with pytest.raises(access.ReadOnly):
            access.guard(db, {"owner_id": "u-someone"})
        assert access.guard(db, {"owner_id": identity.LOCAL_USER})
        assert access.guard(db, {"id": 1}) == {"id": 1}         # a row without an owner is not the guard's
    assert access.guard(db, {"owner_id": "u-someone"})         # a read: the page decides, nothing is refused
    with pytest.raises(LookupError):
        with access.viewing(db, "u-someone"):
            pass
    assert team.dashboard(db, __import__("datetime").date.today())["reps"] == []
    assert views.log_view(db, "call", "call-x", identity.LOCAL_USER) is False


def test_the_bus_refuses_queueing_work_on_someone_elses_object(db, monkeypatch):
    monkeypatch.setattr(bus, "owner_for", lambda conn, event: "u-someone")
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with pytest.raises(access.ReadOnly):
        bus.publish(db, Event(type="PROCESS_CALL", entity_id="call-x", dedupe_key="x:1"))
    with identity.as_actor(db, identity.LOCAL_ACTOR.as_service()):          # a background duty is not refused here
        assert bus.publish(db, Event(type="PROCESS_CALL", entity_id="call-x", dedupe_key="x:2"))
