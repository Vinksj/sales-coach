"""The setup wizard, which is also the Settings pages (design doc, section 6).

Six steps, each its own URL under /setup, each saving on its own POST:
  you          the seller profile and the writing style guide   (seller.yaml, style.md)
  method       the sales methodology, and a builder for a custom one   (methodology.yaml)
  model        the model provider, its key and the two tier models     (models.yaml, secrets.env)
  sources      where calls come from                                   (sources.yaml, secrets.env)
  connections  read-only status of Gmail, the calendar connector, live capture, speech models, the CLI
  review       every choice, what is still on a default, what is missing, Finish

Nothing here owns behaviour. Every save goes through the engine that owns the setting
(config.save_user / save_user_text / set_secret, methodology.switch / save_custom / delete_custom,
providers.save_choice, sources.save / new_webhook_secret), so a choice made here takes effect
everywhere on the next read.

  forms.py   form text -> validated data, with one message per field
  state.py   what is done, what is on a default and what is missing, computed from the settings
             and the state table (never from "this page was visited")
  web.py     the routes

Only step 1 gates the app (web/app.py FirstRunGate, unchanged). Steps 2 to 5 ship with working
defaults and never block anything.
"""
