"""Recipient check: an email may only go to people the deal actually involves.

The allowed set is the call's participants plus the deal's people, minus
the seller. An address the model produced that is not in that set is a
violation, however plausible it looks. The seller can add an address by typing
it into the draft, which the UI passes as user_added.
"""
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def allowed_recipients(conn, call_id=None, deal_id=None) -> dict:
    """email -> name for everyone this email may address."""
    rows = []
    if call_id:
        rows += conn.execute(
            "SELECT p.name, p.email, p.is_me FROM call_participants cp "
            "JOIN people p ON p.node_id=cp.person_id WHERE cp.call_id=?", (call_id,)).fetchall()
    if deal_id:
        rows += conn.execute(
            "SELECT p.name, p.email, p.is_me FROM deal_people dp "
            "JOIN people p ON p.node_id=dp.person_id WHERE dp.deal_id=?", (deal_id,)).fetchall()
    return {r["email"].lower(): r["name"] for r in rows if r["email"] and not r["is_me"]}


def check(to, cc, allowed: dict, user_added=()) -> list[str]:
    """Return violations; an empty list means the recipients are acceptable."""
    violations = []
    user_added = {a.lower().strip() for a in user_added}
    addresses = [a.lower().strip() for a in list(to or []) + list(cc or [])]
    if not [a for a in (to or []) if a.strip()]:
        violations.append("no recipient in To")
    for address in addresses:
        if not EMAIL_RE.match(address):
            violations.append(f"not an email address: {address}")
        elif address not in allowed and address not in user_added:
            violations.append(f"{address} is not on this call or deal")
    if len(set(addresses)) != len(addresses):
        violations.append("duplicate recipient")
    return violations
