"""The manager product (Phase 7): a manager reviews everything their team records and comments on it;
they never act for a rep.

  access.py     who may change what is on screen (the read-only rule for every page and form), the
                team a user manages, whose page is being read
  comments.py   comments on a call, a moment of a call, a deal, an email or a loop, and coaching notes
  views.py      the access log: who opened someone else's call or deal page, and when
  team.py       the /team numbers (SQL and Python only, never a model) and the pattern roll-up
  calls.py      the /calls index and its filters
  web.py        the routes, mounted by plugins/manager.py

The database already enforces the substance (store/rls.py): a manager SELECTs their team's rows and can
write none of them; comments are the one OWNED table a non-owner inserts into, with the reason written
next to the policy. Nothing here is ever read by a prompt: tests/test_manager.py greps every
prompt-building module for the comments table and for this package.
"""
