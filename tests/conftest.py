"""Shared fixtures. Every test gets its own sales.db and never sees the real
world.db (the Jarvis bridge tests build their own copy).

Every test also gets its own user-settings folder holding a CONFIGURED seller profile: the
seller this coach was first built for. The tracked prompts and config name nobody, so every
existing assertion about his name, company or languages in a prompt now passes only because
seller.render() put it there, which is the point.
"""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

SELLER = {
    "name": "Maya Iyer",
    "emails": ["maya@tessel.test", "maya@tesselops.test", "maya.iyer@gmail.com"],
    "company": "Tessel",
    "role": "Founder, CEO",
    "website": "www.tessel.test",
    "offering": ("AI agents that do the multi-party coordination work in freight and logistics operations "
                 "(payment follow-ups, invoice acknowledgement, carrier check-ins, load planning)"),
    "icp": "large Indian enterprises, mostly logistics companies",
    "buyer_titles": "promoters, MDs, CEOs, CFOs and COOs, not IT",
    "vocabulary": "plant, lane, dispatch, detention",
    "own_domains": ["tessel.test", "tesselops.test", "gmail.com"],
    "languages": ["en", "hi"],
    "timezone": "Asia/Kolkata",
    "signature": "Best,\nMaya\n\nMaya Iyer\nFounder, CEO\nTessel\nwww.tessel.test",
    "call_context": "Sales are founder-led. Deals are multi-stakeholder and slow.",
}


def write_seller(folder, profile=None) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "seller.yaml").write_text(yaml.safe_dump(SELLER if profile is None else profile, sort_keys=False))
    return folder


# Modules read the profile at import time too (common.IST in a test module's globals), before any
# fixture runs: point the settings at a throwaway folder first, so an import never sees a real one.
os.environ["SALESCOACH_SETTINGS"] = str(write_seller(Path(tempfile.mkdtemp(prefix="salescoach-test-settings-"))))

from salescoach import config, providers  # noqa: E402


@pytest.fixture(autouse=True)
def seller_settings(tmp_path, monkeypatch):
    """An isolated, configured settings folder per test. Tests that need another seller (or none)
    call write_seller(seller_settings, {...})."""
    folder = write_seller(tmp_path / "user-settings")
    monkeypatch.setenv("SALESCOACH_SETTINGS", str(folder))
    # No test may read a real secrets file: the legacy location points at nothing.
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "no-legacy-secrets.env")
    return folder


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DB", str(tmp_path / "sales.db"))
    monkeypatch.setenv("SALESCOACH_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    from salescoach.store import stores
    monkeypatch.setattr(stores, "WORLD_DB", tmp_path / "absent-world.db")
    conn = stores.sales()
    yield conn
    conn.close()


@pytest.fixture
def fake_llm():
    from salescoach.providers.fake import FakeProvider
    fake = FakeProvider()
    providers.set_override(fake)
    yield fake
    providers.clear_override()
