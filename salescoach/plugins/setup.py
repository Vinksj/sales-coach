"""Phase E plugin: the setup wizard / Settings (salescoach/setupui).

What it attaches through the plugin seam: the "Settings" nav entry.

The routes themselves are included by the core (salescoach/web/setup.py), because the first-run
gate redirects every page to /setup and that page must exist even when plugins are disabled.
No tables of its own: three keys in the state table (setupui/state.py), none of them a secret.
"""
NAV = [("/setup", "Settings")]
