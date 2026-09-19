import json

from .. import seller
from ..orchestrator import context
from ..schemas.actions import ActionExtraction
from .base import Agent


class ActionAgent(Agent):
    name = "actions"
    schema = ActionExtraction

    def build_prompt(self, ctx):
        analysis = ctx.get("analysis") or {}
        condensed = {k: analysis.get(k) for k in ("claims", "gaps", "assessments", "verdict", "what_changed")}
        return (f"{context.meta_block(ctx)}\n\n"
                f"{context.deal_block(ctx)}\n\n"
                f"LOOPS ALREADY OPEN FOR THIS DEAL (use these ids in loop_updates)\n"
                f"{context.loops_block(ctx['open_loops'])}\n\n"
                f"OTHER OPEN COMMITMENTS WITH THESE PEOPLE ({seller.first_name() or 'the seller'}'s commitment tracker; "
                f"when an action is "
                f"the same promise, include it and set world_commitment_id)\n"
                f"{context.world_block(ctx['world_commitments'])}\n\n"
                f"FACTUAL SUMMARY\n{json.dumps(ctx.get('summary') or {}, indent=1)}\n\n"
                f"CALL ANALYSIS (condensed)\n{json.dumps(condensed, indent=1)}\n\n"
                f"TRANSCRIPT\n{context.transcript_block(ctx)}")
