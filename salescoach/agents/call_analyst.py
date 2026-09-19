import json

from .. import seller
from ..memory import patterns
from ..orchestrator import context
from ..schemas.analysis import CallAnalysis, analysis_model
from .base import PROMPTS, Agent


class CallAnalystAgent(Agent):
    """The analyst looks for what is missing through the team's sales methodology (intel/methodology.py):
    its lens and secondary lenses are the only values `Gap.lens` accepts, and its `coaching.analyst`
    text, when it has one, is part of the system prompt. One agent per run, so prompt and schema agree."""
    name = "call_analyst"
    schema = CallAnalysis                      # the default (MEDDPICC) contract; an instance carries its own

    def __init__(self, m=None):
        from ..intel import methodology
        self.methodology = m or methodology.active()
        self.schema = analysis_model(self.methodology.lenses, tuple(self.methodology.labels.values()))

    def system_prompt(self, ctx):
        from ..intel import methodology
        taxonomy = "\n".join(f"- {tag} ({p['polarity']}): {p['name']}" for tag, p in patterns.taxonomy().items())
        block = methodology.analyst_block(self.methodology)
        return (methodology.render_prompt(PROMPTS / f"{self.name}.md", self.methodology)
                + (f"\n\n{block}" if block else "")
                + "\n\n## Seller taxonomy\n" + seller.render(taxonomy) + "\n")

    def build_prompt(self, ctx):
        quality = ctx.get("quality") or {}
        cannot = "\n".join(f"- {c}" for c in quality.get("cannot_judge", [])) or "- nothing flagged"
        return (f"{context.meta_block(ctx)}\n\n"
                f"DEAL STATE BEFORE THIS CALL\n{context.deal_block(ctx)}\n\n"
                f"OPEN LOOPS BEFORE THIS CALL\n{context.loops_block(ctx['open_loops'])}\n\n"
                f"TRANSCRIPT QUALITY\n{quality.get('summary', 'not assessed')}\nCannot be judged fairly:\n{cannot}\n\n"
                f"FACTUAL SUMMARY (from the summary agent)\n{json.dumps(ctx.get('summary') or {}, indent=1)}\n\n"
                f"TRANSCRIPT\n{context.transcript_block(ctx)}")
