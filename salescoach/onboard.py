"""Onboarding: real accounts, deals and people from config/accounts.yaml, and
mapping any meeting to its deal by participant email domain.

Idempotent by construction: accounts match on domain, people on email, deals
on (account, name). Re-running after editing the YAML adds what is new and
never duplicates. Contact files (~/.claude/contacts/<slug>.md) are linked,
never copied.
"""
import os
from pathlib import Path

import yaml

from . import config, repo, seller

CONTACTS = Path(os.path.expanduser("~/.claude/contacts"))


def _deal_for_account(conn, account_id, name=None):
    """The acting user's own deal with the account (accounts are the org's directory; deals are one rep's):
    a call filed by guessing must never land on a deal the actor can only read, a team member's."""
    from . import identity
    sql = "SELECT node_id FROM deals WHERE account_id=? AND owner_id=?"
    params = [account_id, identity.actor_of(conn).user_id]
    if name:
        sql += " AND name=?"
        params.append(name)
    row = conn.execute(sql + " ORDER BY status='active' DESC, updated_at DESC LIMIT 1", params).fetchone()
    return row["node_id"] if row else None


def run(conn, path=None) -> dict:
    data = yaml.safe_load(Path(path or config.user_file("accounts.yaml")).read_text()) or {}
    summary = {"accounts": 0, "deals": 0, "people": 0, "links": 0}
    repo.ensure_me(conn)
    for acc in data.get("accounts") or []:
        domains = [d.lower() for d in acc.get("domains") or []]
        account_id = next((a for a in (repo.find_account_by_domain(conn, d) for d in domains) if a), None)
        if account_id is None:
            account_id = repo.create_account(conn, acc["name"], domains, source_id="onboard")
            summary["accounts"] += 1
        deal_cfg = acc.get("deal") or {}
        deal_id = None
        if deal_cfg:
            deal_id = _deal_for_account(conn, account_id, deal_cfg["name"])
            if deal_id is None:
                deal_id = repo.create_deal(conn, deal_cfg["name"], account_id=account_id,
                                           stage=deal_cfg.get("stage"), source_id="onboard")
                repo.link_deal_person(conn, deal_id, repo.ensure_me(conn), role="seller")
                summary["deals"] += 1
        for p in acc.get("people") or []:
            email = (p.get("email") or "").lower() or None
            person_id = repo.find_person_by_email(conn, email) if email else None
            contact = str(CONTACTS / f"{p['contact']}.md") if p.get("contact") else None
            if contact and not Path(contact).exists():
                contact = None
            if person_id is None:
                person_id = repo.create_person(conn, p["name"], email=email, account_id=account_id,
                                               title=p.get("title"), contact_file=contact, source_id="onboard")
                summary["people"] += 1
            else:
                conn.execute("UPDATE people SET account_id=COALESCE(account_id, ?), title=COALESCE(title, ?), "
                             "contact_file=COALESCE(contact_file, ?) WHERE node_id=?",
                             (account_id, p.get("title"), contact, person_id))
            if deal_id:
                repo.link_deal_person(conn, deal_id, person_id, role=p.get("role"))
                summary["links"] += 1
    conn.commit()
    return summary


def deal_for_emails(conn, emails) -> str | None:
    """The active deal of the first external participant whose domain belongs to an account."""
    for email in emails or []:
        domain = (email or "").lower().rsplit("@", 1)[-1]
        if not domain or domain in seller.internal_domains():
            continue
        account_id = repo.find_account_by_domain(conn, domain)
        if account_id:
            deal_id = _deal_for_account(conn, account_id)
            if deal_id:
                return deal_id
    return None


def backfill_granola(conn, time_range="last_30_days", only_deals=True, dry_run=False, lister=None,
                     importer=None) -> list[dict]:
    """Import past Granola meetings whose participants belong to a known deal (or all, if only_deals=False).

    Imported calls are source='granola': analysed and used for memory and seller
    patterns, never given a drafted email, never claimed from Jarvis.
    """
    from .sources import granola
    lister = lister or granola.list_meetings
    importer = importer or granola.import_meeting
    results = []
    # Oldest first: a later call must see (and may close) the loops an earlier one opened.
    meetings = sorted(lister(time_range), key=lambda m: granola.parse_date(m.get("date", "")) or "")
    for meeting in meetings:
        emails = [p["email"] for p in meeting.get("participants", [])]
        deal_id = deal_for_emails(conn, emails)
        external = [e for e in emails if e.rsplit("@", 1)[-1] not in seller.internal_domains()]
        if only_deals and not deal_id:
            results.append({"id": meeting["id"], "title": meeting["title"], "skipped": "no known deal"
                            if external else "no external participants"})
            continue
        if dry_run:
            results.append({"id": meeting["id"], "title": meeting["title"], "deal_id": deal_id, "would_import": True})
            continue
        try:
            call_id = importer(conn, meeting["id"], deal_id=deal_id)
        except Exception as exc:
            # One unreadable meeting must not stop the rest of the backfill.
            conn.rollback()
            results.append({"id": meeting["id"], "title": meeting["title"], "deal_id": deal_id,
                            "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        results.append({"id": meeting["id"], "title": meeting["title"], "deal_id": deal_id, "call_id": call_id})
    return results
