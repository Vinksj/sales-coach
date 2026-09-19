from salescoach import onboard, repo

YAML = """
accounts:
  - name: Acme Freight
    domains: [acmefreight.test]
    deal: {name: Acme pilot, stage: discovery}
    people:
      - {name: Asha Rao, email: Asha.Rao@acmefreight.test, title: COO, role: champion}
      - {name: Ravi K, email: ravi@acmefreight.test}
"""


def test_onboard_is_idempotent(db, tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text(YAML)
    first = onboard.run(db, path)
    assert first == {"accounts": 1, "deals": 1, "people": 2, "links": 2}
    second = onboard.run(db, path)
    assert second["accounts"] == second["deals"] == second["people"] == 0
    deal = db.execute("SELECT node_id FROM deals").fetchone()[0]
    people = {p["email"]: p["role_in_deal"] for p in repo.deal_people(db, deal)}
    assert people["asha.rao@acmefreight.test"] == "champion" and "maya@tessel.test" in people


def test_backfill_maps_meetings_to_deals(db, tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text(YAML)
    onboard.run(db, path)
    meetings = [
        {"id": "m1", "title": "Asha <> Maya", "participants": [{"email": "maya@tessel.test"},
                                                                  {"email": "asha.rao@acmefreight.test"}]},
        {"id": "m2", "title": "Investor chat", "participants": [{"email": "someone@vc.test"}]},
        {"id": "m3", "title": "Internal", "participants": [{"email": "piyush@tessel.test"}]},
    ]
    imported = []
    out = onboard.backfill_granola(db, lister=lambda r: meetings,
                                   importer=lambda conn, mid, deal_id: imported.append((mid, deal_id)) or "call-x")
    assert [r.get("call_id") for r in out] == ["call-x", None, None]
    assert out[1]["skipped"] == "no known deal" and out[2]["skipped"] == "no external participants"
    deal = db.execute("SELECT node_id FROM deals").fetchone()[0]
    assert imported == [("m1", deal)]
    dry = onboard.backfill_granola(db, lister=lambda r: meetings, dry_run=True)
    assert dry[0]["would_import"] and len(imported) == 1
