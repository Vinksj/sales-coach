"""Phase 3: sales intelligence.

The post-call pipeline answers "what happened on this call". This package
answers the deal question: what is the single highest-leverage thing to do
next to win it. It keeps a stakeholder map, MEDDPICC state, deal risks and a
sceptical health score per deal, preserves disagreement between the analyst
and the strategist, writes a longitudinal model of how the seller sells, and
prepares him for the next call.

Same contract as the core: agents only return structured output; the code
here validates every quote against the cited turns, caps optimism with facts,
and writes through the memory gate so the seller's own edits always win.

Modules:
  schemas     agent contracts (pydantic)
  agentkit    the agent runner (per-agent model choice from config/intel.yaml)
  tables      gated tables and read helpers
  history     what the strategist sees: the whole deal, rendered once
  strategist  Deal Strategist agent, validation, and the pipeline step
  reconcile   analyst vs strategist disagreement, kept and reconciled
  coach       longitudinal coach report over seller_patterns
  prep        pre-call brief (public: generate())
  embed       local embeddings and similar-moment search
  web         routes and fragments, mounted by plugins/intelligence.py
"""
