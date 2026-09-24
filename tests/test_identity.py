"""identity: the acting user, the two modes, and what a session binds.

Local mode (the default, every existing test): the local user is implicit everywhere, so nothing
here changes behaviour. Cloud mode: there is no implicit user; seller.profile() and current_actor()
without a session raise NoActor, which is how a background thread that forgot its session is caught.
"""
import threading

import pytest

from salescoach import identity, seller, users
from salescoach.store import stores


def test_local_mode_has_an_implicit_local_user(db):
    assert identity.mode() == "local" and not identity.cloud()
    actor = identity.current_actor()
    assert actor.user_id == "local" and actor.is_local and actor.mode == "interactive" and actor.role == "admin"
    assert db.actor.user_id == "local"                     # stores.sales() bound it
    assert seller.name() == "Maya Iyer"                    # the local user's profile is seller.yaml, as before
    assert seller.user_profile()["style"] == seller.style_guide()


def test_cloud_mode_has_no_implicit_user(monkeypatch, seller_settings):
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    assert identity.cloud()
    assert identity.current_actor(required=False) is None
    with pytest.raises(identity.NoActor):
        identity.current_actor()
    with pytest.raises(identity.NoActor):
        seller.profile()
    assert seller.org_configured()                         # the org half needs nobody
    assert not seller.user_configured()                    # the user half has nobody to be configured
    assert not seller.is_configured()
    with pytest.raises(seller.NotConfigured):
        seller.require_configured()


def test_a_bare_thread_in_cloud_mode_raises_no_actor(monkeypatch, seller_settings):
    """A thread does not inherit the contextvar (threading.Thread starts with a fresh context), so
    a thread that opens no identity.session() finds no actor: that is the check, not a hole."""
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    outcome = {}
    with identity.activate(identity.Actor("u-alpha")):     # the spawning request HAS an actor
        assert seller.user_profile(identity.Actor("u-alpha", profile={"name": "Asha"}))["name"] == "Asha"

        def bare():
            try:
                seller.profile()
                outcome["error"] = None
            except Exception as exc:                       # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=bare)
        thread.start()
        thread.join(5)
    assert isinstance(outcome["error"], identity.NoActor)
    carried = {}
    actor = identity.Actor("u-alpha", profile={"name": "Asha", "email": "asha@tessel.test"})

    def with_actor():                                      # what plugins/live_coach's replay thread does
        with identity.activate(actor):
            carried["name"] = seller.name()

    thread = threading.Thread(target=with_actor)
    thread.start()
    thread.join(5)
    assert carried["name"] == "Asha"


def test_activate_and_as_actor_restore_what_was_there(db):
    before = db.actor
    with identity.as_actor(db, identity.Actor("u-beta")):
        assert identity.current_actor().user_id == "u-beta" and db.actor.user_id == "u-beta"
        with identity.activate(None):
            assert identity.current_actor().user_id == "local"          # local mode: the fallback
    assert identity.current_actor().user_id == "local" and db.actor is before


def test_cloud_mode_refuses_sqlite(monkeypatch, tmp_path, dialect):
    if dialect != "sqlite":
        pytest.skip("the refusal is about a SQLite path")
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with pytest.raises(RuntimeError, match="needs Postgres"):
        stores.sales(tmp_path / "x.db")


def test_the_app_refuses_an_unknown_mode(monkeypatch, capsys):
    import argparse
    from salescoach import cli
    monkeypatch.setenv(identity.MODE_ENV, "nonsense")
    assert identity.mode() == "local"                      # read leniently ...
    assert cli.cmd_serve(argparse.Namespace(host="127.0.0.1", port=1, no_worker=True, allow_unauthenticated=False)) == 2
    assert "SALESCOACH_MODE" in capsys.readouterr().err   # ... refused at the door


def test_session_and_as_user_bind_the_user_everywhere(db, dialect):
    """A session's profile is the user's row, not seller.yaml; two sessions see two sellers."""
    if dialect != "postgres":
        with pytest.raises(users.UserError):               # SQLite: one user per file
            users.create(db, "asha@tessel.test", "Asha Rao")
        with identity.session("local") as conn:
            assert conn.actor.is_local and seller.name() == "Maya Iyer"
        return
    asha = users.create(db, "asha@tessel.test", "Asha Rao", role="rep", extra_emails=["asha@gmail.test"],
                        aliases=["Ash"], signature="Best,\nAsha", timezone="Europe/London", languages=["en", "hi"],
                        role_title="AE", style="Short sentences.", call_context="I sell to CFOs.")
    bala = users.create(db, "bala@tessel.test", "Bala K", role="manager")
    db.commit()
    with identity.session(asha["id"]) as conn:
        assert conn.actor.user_id == asha["id"] and conn.actor.role == "rep" and not conn.actor.is_local
        assert identity.current_actor() is conn.actor
        assert conn.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == asha["id"]
        assert conn.execute("SELECT current_setting('app.mode', true)").fetchone()[0] == "interactive"
        p = seller.profile()
        assert (p["name"], p["emails"], p["aliases"], p["timezone"], p["role"]) == (
            "Asha Rao", ["asha@tessel.test", "asha@gmail.test"], ["Ash"], "Europe/London", "AE")
        assert p["company"] == "Tessel" and p["offering"].startswith("AI agents")     # the org half is shared
        assert seller.style_guide() == "Short sentences." and p["signature"] == "Best,\nAsha"
        assert "Asha is AE at Tessel." in seller.seller_context()
        assert seller.is_configured()
    with identity.session(bala["id"], mode=identity.SERVICE) as conn:
        assert seller.name() == "Bala K" and conn.actor.role == "manager" and conn.actor.mode == "service"
        assert conn.execute("SELECT current_setting('app.mode', true)").fetchone()[0] == "service"
        assert seller.style_guide() == seller.user_profile_from_yaml()["style"]    # no own guide: the shipped one
    assert identity.current_actor().user_id == "local"
    with identity.as_user(db, asha["id"]):
        assert seller.first_name() == "Asha" and db.actor.user_id == asha["id"]
    assert db.actor.user_id == "local" and seller.name() == "Maya Iyer"
    with pytest.raises(identity.NoActor):
        with identity.session("u-nobody"):
            pass


@pytest.mark.postgres_only
def test_a_closed_pooled_connection_forgets_its_user(db):
    with identity.session("local") as conn:
        raw = conn.raw
        assert raw.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == "local"
    assert raw.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == ""


def test_teams_and_managers(db, dialect):
    team = users.create_team(db, "West")
    assert users.get_team(db, team["id"])["name"] == "West" and users.list_teams(db)[0]["id"] == team["id"]
    if dialect == "postgres":
        m = users.create(db, "m@tessel.test", "Mani", role="manager")
        assert users.set_managers(db, team["id"], [m["id"]]) == [m["id"]]
        assert users.teams_managed_by(db, m["id"]) == [team["id"]]
        assert users.update(db, m["id"], status="disabled")["status"] == "disabled"
        assert users.list_users(db, status="active") == [users.get(db, "local")]
    with pytest.raises(users.UserError):
        users.update(db, "local", role="king")
