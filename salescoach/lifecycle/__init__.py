"""The data's life cycle in a cloud install (Phase 8): bringing a single-user install in, keeping it only as
long as the org says, handing a user their own data, and a rep leaving.

  importer.py   `salescoach import-sqlite PATH --as <email>`: a local SQLite install into an empty Postgres org
  retention.py  org setting retention.days; the scheduler's org-level `retention` duty; `salescoach retention`
  export.py     a user's own rows as a zip of JSON files (/me/export, `salescoach export --user`)
  offboard.py   a rep leaves: reassign their work to another rep, or purge it; sessions and grants revoked
  owner.py      the owner-role connection (DATABASE_MIGRATE_URL) the operator commands run on
"""
