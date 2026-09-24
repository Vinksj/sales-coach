"""Shared pieces for Phase 4: the IST clock, automation config, timestamp
parsing, the agent base for prompts kept in automation/prompts, and the run
log for decisions made by rules rather than a model.

Timestamps in sales.db come in two shapes: engine.now() writes UTC
('...+00:00'), call imports write IST ('...+05:30'). Compared as strings they
are wrong across the offset, so everything here parses before comparing.
"""
import hashlib
import json
from datetime import date, datetime, time, timezone
from pathlib import Path

from .. import config, seller
from ..agents.base import Agent
from ..store.stores import now
from ..store import db

PROMPTS = Path(__file__).with_name("prompts")
RULES_VERSION = "rules-v1"


def __getattr__(name):
    """`IST` is the seller's zone (Asia/Kolkata unless the profile says otherwise), read on every
    access so a profile edit needs no restart. The name is historical."""
    if name == "IST":
        return seller.zone()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def cfg(section: str | None = None) -> dict:
    data = config.load("automation") or {}
    if section is None:
        return data
    return data.get(section) or {}


def now_ist() -> datetime:
    return datetime.now(seller.zone())


def today_ist() -> date:
    return now_ist().date()


def ts(raw) -> datetime | None:
    """Any stored timestamp (or date) as an aware datetime; naive means UTC."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, date):
        value = datetime.combine(raw, time(0), seller.zone())
    else:
        try:
            value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def ist_date(raw) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    value = ts(raw)
    return value.astimezone(seller.zone()).date() if value else None


def day_label(raw) -> str:
    d = ist_date(raw)
    return f"{d:%a} {d.day} {d:%b}" if d else "an unknown date"


def hhmm(text, default: str = "00:00") -> time:
    raw = str(text or default)
    hours, _, minutes = raw.partition(":")
    return time(int(hours), int(minutes or 0))


def my_addresses(conn=None) -> set[str]:
    """Addresses that are never a buyer's: automation.yaml my_addresses when set (an override), else the
    acting user's profile, plus the person row of EVERY internal user (a colleague's reply in a thread
    is not a stakeholder reply; a colleague on an invite is not a guest)."""
    addrs = {a.lower().strip() for a in (cfg().get("my_addresses") or seller.emails()) if a}
    if conn is not None:
        addrs |= {r["email"].lower() for r in conn.execute(
            "SELECT email FROM people WHERE is_me=1 AND email IS NOT NULL")}
    return addrs


class PluginAgent(Agent):
    """An agent whose system prompt lives in automation/prompts/<name>.md.

    The caller builds the user prompt (ctx['prompt']) from the database, so
    the agent itself never queries anything; ctx['refs'] is what agent_runs
    records as its inputs.
    """

    def system_prompt(self, ctx):
        return seller.prompt(PROMPTS / f"{self.name}.md")

    def build_prompt(self, ctx):
        return ctx["prompt"]

    def input_refs(self, ctx):
        return ctx.get("refs") or {}


def record_rules_run(conn, agent: str, refs: dict, output: dict, call_id=None) -> int:
    """An agent_runs row for a decision made by rules, so the Why? view covers it too."""
    refs_json = json.dumps(refs, sort_keys=True, default=str)
    cur = conn.execute(
        "INSERT INTO agent_runs(agent,call_id,prompt_version,provider,model,isolation,input_refs,input_sha,output,"
        "status,duration_ms,started_at) VALUES (?,?,?,?,?,?,?,?,?,'ok',0,?)",
        (agent, call_id, RULES_VERSION, "rules", None, "n/a", refs_json,
         hashlib.sha256(refs_json.encode()).hexdigest(), json.dumps(output, default=str), now()))
    return db.insert_id(cur)
