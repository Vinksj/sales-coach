"""Phase 7 plugin: the manager product (salescoach/manager).

What it attaches through the plugin seam:
  NAV      Team (base.html shows it only to someone who manages a team) and Calls (everyone: a rep's
           own calls, a manager's own and their team's; row-level security decides)
  router   /team, /calls, /comments; built on first access (module __getattr__) so the CLI and the
           worker never import the web layer
No tables of its own: comments and access_log are core schema (store/schema-sales.sql, migration 11,
store/pg/0007_manager.sql), because the call and deal pages render the comment threads and the
"Viewed by" line with SALESCOACH_NO_PLUGINS=1 too. No pipeline step, no duty, no CLI: a manager's
comments and the team numbers never reach the worker, let alone a prompt.
"""
NAV = [("/team", "Team"), ("/calls", "Calls")]


def __getattr__(name):
    if name == "router":
        from ..manager.web import router
        return router
    raise AttributeError(name)
