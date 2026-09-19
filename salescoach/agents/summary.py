from ..orchestrator import context
from ..schemas.summary import CallSummary
from .base import Agent


class SummaryAgent(Agent):
    name = "summary"
    schema = CallSummary

    def build_prompt(self, ctx):
        quality = ctx.get("quality") or {}
        return (f"{context.meta_block(ctx)}\n\n"
                f"Transcript quality: {quality.get('summary', 'not assessed')}\n\n"
                f"TRANSCRIPT\n{context.transcript_block(ctx)}")
