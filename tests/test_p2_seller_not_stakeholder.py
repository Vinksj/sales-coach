"""The seller is a KNOWN person, never an unexplored stakeholder.

On the real 2026-09-10 Northwind leadership call the live coach fired, at 51:22,
"Ask how Maya is involved in this" — because the deal-context query filters
is_me=0 (correctly, for the buyer list), so his own name was absent from
known_people. "Maya ji" then matched the " ([a-z]{3,}) ji" name pattern and
looked like a stakeholder nobody had explored.
"""
from salescoach import repo
from salescoach.coach.slow_pass import load_deal_context


def test_seller_is_known_but_not_a_stakeholder(db):
    deal = repo.create_deal(db, "Northwind freight pilot")
    arjun = repo.create_person(db, "Arjun Kumar", email="arjun@northwind.example", title="AVP Logistics")
    repo.link_deal_person(db, deal, arjun, role="champion")
    me = repo.create_person(db, "Maya Iyer", email="maya@tessel.example", is_me=True)
    repo.link_deal_person(db, deal, me, role="seller")
    call = repo.create_call(db, source="capture", title="NWP leadership", deal_id=deal, wf_state="live")
    db.commit()

    ctx = load_deal_context(db, call)
    known = {k.lower() for k in ctx["known_people"]}
    assert "maya iyer" in known and "maya" in known   # the fix: first name too, for "Maya ji"
    assert "arjun kumar" in known and "arjun" in known    # buyers stay known, as before

    # ...and he is still not offered to the model as a buyer stakeholder.
    assert not any("Maya" in person for person in ctx["people"])
    assert any("Arjun Kumar" in person for person in ctx["people"])
