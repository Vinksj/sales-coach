"""The plugin seam: phases attach steps, handlers, routes, nav, CLI and schema without editing the core."""
import argparse
import types

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from salescoach import plugins, repo
from salescoach.orchestrator import bus, workflow
from salescoach.schemas.events import Event
from salescoach.store import stores


@pytest.fixture
def fake_plugin(monkeypatch):
    seen = {"steps": [], "events": [], "background": []}
    router = APIRouter()

    @router.get("/fake-plugin")
    def page():
        return {"ok": True}

    def step(conn, call_id, force=False):
        seen["steps"].append(call_id)

    def register(wf):
        wf.register_step("fake_step", step, after="loops_reconciled", label="Fake step")
        wf.register_handler("FAKE_EVENT", lambda conn, ev: seen["events"].append(ev.entity_id))

    def register_cli(sub):
        sub.add_parser("fake-cmd").set_defaults(fn=lambda args: "ran")

    mod = types.SimpleNamespace(__name__="fake", NAV=[("/fake-plugin", "Fake")], router=router, register=register,
                                register_cli=register_cli,
                                start_background=lambda db, stop: seen["background"].append(db))
    pipeline, names, labels = list(workflow.PIPELINE), list(workflow.STEP_NAMES), dict(workflow.STEP_LABELS)
    plugins.reset([mod])
    monkeypatch.setattr(workflow, "_plugins_ready", False)
    yield seen
    workflow.PIPELINE[:] = pipeline
    workflow.STEP_NAMES[:] = names
    workflow.STEP_LABELS.clear()
    workflow.STEP_LABELS.update(labels)
    workflow.HANDLERS.clear()
    plugins.reset(None)


def test_steps_and_handlers(db, fake_plugin):
    workflow.ensure_plugins()
    workflow.ensure_plugins()                      # idempotent
    assert workflow.STEP_NAMES.index("fake_step") == workflow.STEP_NAMES.index("loops_reconciled") + 1
    assert workflow.STEP_NAMES.count("fake_step") == 1 and workflow.STEP_LABELS["fake_step"] == "Fake step"
    bus.publish(db, Event(type="FAKE_EVENT", entity_id="x1"))
    db.commit()
    from salescoach.orchestrator.worker import drain
    drain(db)
    assert fake_plugin["events"] == ["x1"]


def test_web_cli_background(db, fake_plugin):
    from salescoach.web.app import create_app
    client = TestClient(create_app(start_worker=False))
    assert client.get("/fake-plugin").json() == {"ok": True}
    assert 'href="/fake-plugin"' in client.get("/").text
    parser = argparse.ArgumentParser()
    plugins.register_cli(parser.add_subparsers(dest="cmd"))
    assert parser.parse_args(["fake-cmd"]).fn(None) == "ran"
    plugins.start_background("db-path", None)
    assert fake_plugin["background"] == ["db-path"]


def test_plugin_sql_applied_on_connect(tmp_path, monkeypatch):
    monkeypatch.delenv("SALESCOACH_NO_PLUGINS", raising=False)
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "zz_fake.sql").write_text("CREATE TABLE IF NOT EXISTS fake_plugin_table (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(stores, "PLUGINS_DIR", plugin_dir)
    conn = stores.sales(tmp_path / "s.db")
    conn2 = stores.sales(tmp_path / "s.db")        # second connect re-applies harmlessly
    assert conn2.execute("SELECT name FROM sqlite_master WHERE name='fake_plugin_table'").fetchone()
    conn.close()
    conn2.close()
