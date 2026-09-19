"""Phase 4, agentic execution: the follow-up agent, reply ingestion, calendar-
verified slots, and the policy-gated auto-send executor.

It attaches to the core only through plugins/execution.py. Every email it
produces is an ordinary emails row that waits for the seller's Send; the one
exception, autosend.py, refuses unless he has configured a policy that allows
it and every per-email condition holds.
"""
