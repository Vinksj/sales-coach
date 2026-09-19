from ..orchestrator import context
from ..schemas.quality import QualityReport
from .base import Agent


class QualityAgent(Agent):
    name = "quality"
    schema = QualityReport

    def build_prompt(self, ctx):
        return f"{context.meta_block(ctx)}\n\nTRANSCRIPT\n{context.transcript_block(ctx, asr_only=True)}"
