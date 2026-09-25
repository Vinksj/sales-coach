"""A new call is filed only on a deal its owner owns (Postgres, cloud mode).

A manager can READ a rep's deals. /import/text, /import/file and /import/audio checked only that a named
deal_id existed (under the read policy: a team member's deal passed), and the guess from participants'
domains (onboard.deal_for_emails -> _deal_for_account, also reached by a recorder connection's poll and
push) picked any visible deal of the account: the manager's own call landed inside the rep's deal. Now a
named deal that is not the actor's is 404, like one that does not exist, and the guess only ever returns
the actor's own deal.
"""
import pytest

from salescoach import onboard, repo
from salescoach.sources import base

from test_route_crawl import cloud  # noqa: F401
from test_manager_review import A, M, _as, client, mk_deal, org, post  # noqa: F401

pytestmark = pytest.mark.postgres_only

TRANSCRIPT = "Me: Hello Arjun, thanks for joining.\nThem: Happy to. We need a pilot for the plant.\n"


@pytest.fixture
def a_deal(db, org):
    with _as(db, A):
        account = repo.create_account(db, "Acme Freight", ["acme.test"])
        deal = mk_deal(db, A, "Acme pilot")
        db.execute("UPDATE deals SET account_id=? WHERE node_id=?", (account, deal))
        db.commit()
    return deal


def _calls_of(pg_owner, who):
    return pg_owner.execute("SELECT node_id, deal_id FROM calls WHERE owner_id=?", (who,)).fetchall()


def test_a_named_deal_of_someone_else_is_not_found(client, pg_owner, a_deal):
    r = post(client, M, "/import/text", {"text": TRANSCRIPT, "title": "M's call", "deal_id": a_deal})
    assert r.status_code == 404
    r = client.post("/import/file", files={"file": ("call.txt", TRANSCRIPT.encode(), "text/plain")},
                    data={"deal_id": a_deal, "title": "M's file"},
                    headers={"x-test-user": M, "accept": "text/html", "origin": "http://127.0.0.1:8140"})
    assert r.status_code == 404
    r = client.post("/import/audio", files={"file": ("call.wav", b"RIFF0000WAVE", "audio/wav")},
                    data={"deal_id": a_deal, "title": "M's audio"},
                    headers={"x-test-user": M, "accept": "text/html", "origin": "http://127.0.0.1:8140"})
    assert r.status_code == 404
    assert _calls_of(pg_owner, M) == []
    # the owner files on their own deal as before
    r = post(client, A, "/import/text", {"text": TRANSCRIPT, "title": "A's call", "deal_id": a_deal})
    assert r.status_code == 303 and r.headers["location"].startswith("/calls/"), r.headers
    assert [row["deal_id"] for row in _calls_of(pg_owner, A)] == [a_deal]


def test_the_guess_from_participants_returns_only_the_actors_own_deal(db, a_deal):
    nt = base.NormalizedTranscript(source_kind="upload", source_ref=None, title="x", turns=[],
                              participants=[{"name": "Arjun", "email": "arjun@acme.test"}])
    with _as(db, M):
        assert db.execute("SELECT COUNT(*) FROM deals WHERE node_id=?", (a_deal,)).fetchone()[0] == 1   # M reads it
        assert onboard.deal_for_emails(db, ["arjun@acme.test"]) is None
        assert base.guess_deal(db, nt) is None
        assert repo.own_deal(db, a_deal) is None
    with _as(db, A):
        assert onboard.deal_for_emails(db, ["arjun@acme.test"]) == a_deal
        assert base.guess_deal(db, nt) == a_deal
        assert repo.own_deal(db, a_deal)["node_id"] == a_deal
