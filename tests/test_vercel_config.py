"""The Vercel deployment's files agree with the code (vercel.json, pyproject.toml [tool.vercel], .python-version,
.vercelignore; docs/deploy-vercel.md):

  * the entrypoint named in pyproject imports and is the ServerlessApp;
  * the crons call exactly the cron door's paths, every minute, and the function's maxDuration leaves room for an
    event claimed at the end of the drain's claim window;
  * the Python version is one Vercel runs (3.12; vercel-runtime needs >= 3.12) and inside requires-python;
  * .vercelignore keeps the package, config/ and pyproject.toml in the upload and leaves the tests and docs out.
"""
import fnmatch
import importlib
import json
import tomllib
from pathlib import Path

from salescoach import cron

ROOT = Path(__file__).resolve().parent.parent


def test_the_entrypoint_is_the_serverless_app(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    module, _, attr = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["vercel"]["entrypoint"].partition(":")
    mod = importlib.import_module(module)
    assert type(getattr(mod, attr)).__name__ == "ServerlessApp"
    assert (ROOT / (module.replace(".", "/") + ".py")).exists()


def test_the_crons_call_the_cron_door_every_minute_with_room_to_finish():
    from salescoach import vercel_app
    config = json.loads((ROOT / "vercel.json").read_text())
    assert {c["path"] for c in config["crons"]} == set(vercel_app.CRON_PATHS)
    assert all(c["schedule"] == "* * * * *" for c in config["crons"])
    [(entry, fn)] = config["functions"].items()
    assert entry == "salescoach/vercel_app.py"
    assert fn["maxDuration"] >= cron.DRAIN_BUDGET_S + 240                  # the last event claimed still has minutes
    assert cron.SESSION_IDLE_S > fn["maxDuration"]                          # a live drain's session is never cut off


def test_the_python_version_is_one_vercel_runs_and_the_project_allows():
    version = (ROOT / ".python-version").read_text().strip()
    assert version == "3.12"
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    assert requires == ">=3.11,<3.14"                                       # a laptop on 3.11 still installs


def test_the_upload_keeps_the_app_and_drops_the_rest():
    patterns = [p.strip() for p in (ROOT / ".vercelignore").read_text().splitlines()
                if p.strip() and not p.startswith("#")]

    def ignored(path):
        parts = path.split("/")
        return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(parts[0], p) or fnmatch.fnmatch(parts[-1], p)
                   for p in patterns)
    for kept in ("salescoach/vercel_app.py", "salescoach/web/templates/base.html", "salescoach/store/pg/rls.sql",
                 "config/models.yaml", "pyproject.toml", "vercel.json"):
        assert not ignored(kept), kept
    for dropped in ("tests/conftest.py", "e2e/test_e2e.py", "docs/deploy-vercel.md", "callcap/build.sh", "Dockerfile",
                    "data/sales.db", "salescoach/__pycache__/cli.cpython-312.pyc"):
        assert ignored(dropped), dropped
