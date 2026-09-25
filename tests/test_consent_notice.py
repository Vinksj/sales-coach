"""The recording-consent notice and the retention setting in Settings (Phase 8), on the local install: the
admin form saves both; a local install keeps its own reminder beside Start recording and shows no org notice.
The cloud pages (every call page, My meetings, the Today card; admin-only editing) are in
tests/isolation/test_consent_cloud.py."""
from salescoach import config
from salescoach.lifecycle import settings
from test_web import ORIGIN, app, client, gmail, live, processed  # noqa: F401

NOTICE = "Say at the start that the call is recorded for coaching; stop if anyone objects."


def test_settings_save_both_and_the_local_install_keeps_its_own_reminder(client, processed):  # noqa: F811
    page = client.get("/setup/review").text
    assert 'action="/setup/compliance"' in page
    r = client.post("/setup/compliance", data={"notice": NOTICE, "retention_days": "730"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303 and "730" in r.headers["location"]
    assert settings.retention_days() == 730 and config.load("org")["consent"]["notice"] == NOTICE
    assert settings.consent_notice() == ""                       # local: no org notice on the pages
    call = client.get(f"/calls/{processed['call']}").text
    assert NOTICE not in call and "Recording consent" not in call
    assert "Tell everyone the call is being recorded" in client.get("/").text
    r = client.post("/setup/compliance", data={"notice": "", "retention_days": "soon"}, headers=ORIGIN,
                    follow_redirects=False)
    assert "whole+number" in r.headers["location"] or "whole%20number" in r.headers["location"]
    assert settings.retention_days() == 730                      # a bad value changes nothing
    client.post("/setup/compliance", data={"notice": "", "retention_days": ""}, headers=ORIGIN)
    assert settings.retention_days() is None
