import json
import os

from .. import seller
from ..orchestrator import context
from ..schemas.email import EmailDraft
from .base import Agent


class EmailAgent(Agent):
    name = "email"
    schema = EmailDraft

    def system_prompt(self, ctx):
        return super().system_prompt(ctx) + "\n\n## Style guide\n" + seller.render(seller.style_guide())

    def input_refs(self, ctx):
        # Which learned rules this draft saw (phase F2), so their effect can be measured later.
        from ..learning import feedback
        return {**super().input_refs(ctx), "patterns": feedback.ids(ctx.get("learned_voice"))}

    def build_prompt(self, ctx):
        from ..learning import feedback
        allowed = ctx["allowed_recipients"]
        recipients = "\n".join(f"- {email}: {name}" for email, name in allowed.items()) or "(none)"
        actions = "\n".join(
            f"- [{a['type']} | owner {a['owner']}{' ' + a['owner_name'] if a.get('owner_name') else ''} | "
            f"{a['source']} | confidence {a['confidence']} | due {a.get('due_date') or 'unspecified'}] "
            f"{a['description']}" for a in ctx["email_actions"]) or "(none)"
        summary = ctx.get("summary") or {}
        history = _contact_history(ctx)
        sent_by = seller.first_name_upper()
        edits = "\n\n".join(f"DRAFT:\n{e['draft_body']}\nWHAT {sent_by} SENT:\n{e['final_body']}"
                            for e in ctx.get("recent_edits", [])) or "(none yet)"
        # Rules learned from edits on EVERY deal: rules only, so no other customer's words come with them.
        # The raw edits above them stay limited to this deal. Nothing is added when nothing is learned.
        learned = feedback.voice_block(ctx.get("learned_voice"))
        return (f"{context.meta_block(ctx)}\n\n"
                f"{context.deal_block(ctx)}\n\n"
                f"ALLOWED RECIPIENTS (email: name)\n{recipients}\n\n"
                f"ACTIONS AND COMMITMENTS FROM THIS CALL\n{actions}\n\n"
                f"FACTUAL SUMMARY\n{json.dumps(summary, indent=1)}\n\n"
                f"RELATIONSHIP NOTES\n{history}\n\n"
                f"HOW {sent_by} EDITED EARLIER DRAFTS (learn from these)\n{edits}"
                + (f"\n\n{learned}" if learned else ""))


def _contact_history(ctx) -> str:
    notes = []
    for p in ctx.get("participants", []) + ctx.get("deal_people", []):
        path = p.get("contact_file")
        if not path or p.get("is_me"):
            continue
        path = os.path.expanduser(path)
        if os.path.exists(path):
            with open(path) as fh:
                notes.append(f"{p['name']}:\n{fh.read()[:1500]}")
    return "\n\n".join(notes) or "(no contact notes)"
