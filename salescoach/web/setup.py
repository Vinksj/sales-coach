"""/setup: the wizard that is also the Settings pages. The code lives in salescoach/setupui.

The router is included by the core (create_app), not by a plugin, because the first-run gate
sends every page to /setup: it has to exist even with SALESCOACH_NO_PLUGINS=1. The plugin
(salescoach/plugins/setup.py) adds only the "Settings" nav entry. Its POSTs pass the same
origin/Host guard as every other POST (SameOriginGuard wraps the whole app, this router included).
"""
from ..setupui.web import install_globals, router  # noqa: F401  (create_app includes `router`)

install_globals()          # today.html asks setup_today(request) whether to show "Finish setting up"
