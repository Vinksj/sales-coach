"""Row-level security, generated from the table classification in tenancy.py.

store/pg/rls.sql is `generate()` written to disk (scripts/gen_pg_rls.py); tests/isolation/
test_rls_generated.py fails when the file and the generator disagree, so the SQL and the
classification cannot drift: a table that changes class is re-policied by regenerating, and a
table nobody classified never gets to Postgres (tests/isolation/test_catalog_lint.py).

rls.sql is a REPEATABLE step, not a numbered migration (store/pgmigrate.py): `salescoach migrate`
applies it after every numbered step whenever its sha256 differs from the one recorded in
schema_repeatables, and stores.sales() refuses to serve a schema whose recorded checksum is not this
build's. A numbered file can only police the tables that existed when it ran; a table created by a
LATER migration (sessions, invites, oauth_tokens in 0004; org_settings, raw_payloads in 0005) would
have no policy at all. So the file is idempotent end to end and describes the WHOLE policy set:

  * a leading DO block drops every policy on every table of the current schema, so a policy that was
    removed or renamed never lingers;
  * functions are CREATE OR REPLACE, triggers are dropped before they are created;
  * every classified table gets ENABLE ROW LEVEL SECURITY, then FORCE (OWNED) or NO FORCE (ORG,
    SYSTEM), then its policies from scratch;
  * the app role's grants are re-issued.
It runs in one transaction under the migration lock, so a reader sees the old policy set or the new
one, never a table with none.

The contract (docs/architecture.md, "Isolation"):

  * Two session settings, and only two: app.user_id (the acting user's id, '' for nobody) and
    app.mode ('interactive' | 'service'). PostgresConnection.bind_actor sets them for the session
    and _on_begin re-issues them transaction-locally at the start of EVERY transaction.
  * Two roles. The OWNER role runs migrations (it owns every table and the SECURITY DEFINER
    functions; it has BYPASSRLS, so a migration sees every row) and is never used at runtime. The
    APP role (`salescoach_app`: no superuser, no BYPASSRLS, no CREATE) is what DATABASE_URL names;
    every table has row-level security ENABLED for it, and every OWNED table has it FORCED so that
    not even the owner's own statements skip the policies unless the role bypasses RLS outright.
  * app_visible_owners(): the acting user's id plus the ids of every member of the teams the user
    manages (team_managers + users.team_id). It reads the directory live, so demoting a manager
    takes effect on the next query. A user whose row is missing or not `active` sees nothing;
    an admin who manages no team sees no content.

Policies, per class:

  OWNED   SELECT   owner_id = ANY(app_visible_owners())        (nodes: a NULL owner_id, the org
                   directory's account and person nodes, is readable by any active user)
          INSERT / UPDATE / DELETE   app_can_write(owner_id): the row is the acting user's own,
                   the user is active and a mode is bound. A manager reads a rep's rows and can
                   never write one; nobody bound reads and writes nothing.
          The one exception (OWNED_EXCEPTIONS, with its reason): comments. A comment is owned by the
                   rep whose object it is on; its author (the rep, or a manager of the rep's team)
                   inserts it, the rep or the author resolves it, the author deletes it.
  ORG     accounts, people: readable and writable by any active user (every rep contributes to
                   the shared directory). users, teams, team_managers: readable by every connection
                   (a connection has to read `users` to learn who it is); writable by admins, and
                   a user may update their own users row except id, role, team_id and status
                   (trg_users_guard; the one exception is accepting their own invite, invited ->
                   active, at their first sign-in); the first user of an empty directory may be inserted by
                   anyone (the bootstrap).
  SYSTEM  wf_events: open to every connection of the app role (the bus carries ids and step names,
                   never content; the web layer publishes from interactive requests, the worker
                   claims with no user bound). state: readable by any active user, written by
                   admins and service-mode duties; the process heartbeats (keys 'ops:%': host, pid,
                   timestamps, counts) are also read and written with nobody bound, because the
                   worker and scheduler processes that write them act for nobody and /health and
                   the container health check read them before anyone signs in.
                   user_state and user_speaker_labels: the acting user's own rows.
                   schema_migrations, schema_repeatables: readable; the app role has no write privilege.
          sessions: open to the app role. The AuthGate resolves a session by its id BEFORE any
                   actor exists, so no owner-based rule can apply; the id column holds sha256 of the
                   random session id (salescoach/sessions.py), so the row is found only by someone
                   who already holds the cookie's secret, and a leaked table cannot be replayed.
                   The same machinery-keyed-by-an-unguessable-value reasoning as wf_events.
          invites: SELECT and UPDATE open (the Google callback reads the allow-list and marks the
                   invite accepted with nobody bound yet); INSERT and DELETE by admins only.
          oauth_tokens: the acting user's own grant (user_id = app_actor_id(), active), or any row
                   for an active admin (disabling a user revokes their grants; the admin page shows
                   link status). Nobody bound reads nothing. `salescoach tokens rotate` runs as the
                   owner role (DATABASE_MIGRATE_URL) or as an admin.
          access_log: insert-only. The viewer logs their own view of an object they may read, under
                   its true owner (app_entity_owner); that owner and their managers read the log.
          org_settings: SELECT open to the app role: config.load() reads the overlay from every
                   thread, including before any actor exists (the scheduler's intervals, the sign-in
                   page's brand, process start); it holds no per-user data and no secrets (those stay
                   in the environment). Written by active admins only (the setup wizard, which is
                   admin-only in cloud mode).

The SECURITY DEFINER helpers read users / team_managers / nodes as the owner role, which is why
those tables are ENABLED but not FORCED (a policy that reads its own table would recurse) and why
the owner role must bypass RLS for app_owner_of() (the worker learns an event's owner before it can
bind that owner; the function returns an owner id, never content).
"""
from pathlib import Path

from . import tenancy

APP_ROLE = "salescoach_app"
NAME = "rls"                               # its row in schema_repeatables
OUT = Path(__file__).with_name("pg") / "rls.sql"

HEADER = f"""-- GENERATED by scripts/gen_pg_rls.py from store/tenancy.py. Do not edit: change the classification
-- and regenerate (tests/isolation/test_rls_generated.py fails when this file and the generator differ).
-- Postgres REPEATABLE step (store/pgmigrate.py): row-level security. Applied after every numbered
-- migration whenever this file's sha256 differs from the one in schema_repeatables, in one transaction,
-- so it must stay idempotent: every policy in the schema is dropped first and re-created below.
-- Session settings: app.user_id and app.mode ('interactive' | 'service'), set by store/db.py
-- PostgresConnection.bind_actor / _on_begin. Roles: the OWNER role applies this file (it must have
-- BYPASSRLS, see docs/architecture.md "Isolation"); the APP role {APP_ROLE} is what the running app
-- connects as and is subject to every policy below. Policy summary per class: store/rls.py.

"""

DROP_POLICIES = """-- ---- start clean: every policy on every table of this schema -------------------------------------
-- The policy set below is complete; anything a previous version of this file created and this one does not
-- (a removed or renamed policy) must not survive.
DO $$
DECLARE p record;
BEGIN
  FOR p IN SELECT policyname, tablename FROM pg_policies WHERE schemaname = current_schema() LOOP
    EXECUTE format('DROP POLICY %I ON %I.%I', p.policyname, current_schema(), p.tablename);
  END LOOP;
END $$;

"""

ROLE_BOOTSTRAP = f"""-- ---- the app role: privileges on this schema, never a bypass ----------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    RAISE EXCEPTION 'role {APP_ROLE} does not exist: CREATE ROLE {APP_ROLE} LOGIN PASSWORD ''...'' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE (docs/architecture.md, "Isolation")';
  END IF;
  IF (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    RAISE EXCEPTION 'role {APP_ROLE} must be neither a superuser nor BYPASSRLS';
  END IF;
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO {APP_ROLE}', current_schema());
  EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO {APP_ROLE}', current_schema());
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO {APP_ROLE}', current_schema());
  EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON %I.schema_migrations FROM {APP_ROLE}', current_schema());
  EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON %I.schema_repeatables FROM {APP_ROLE}', current_schema());
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}', current_user, current_schema());
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}', current_user, current_schema());
END $$;

"""

FUNCTIONS = """-- ---- who is acting ------------------------------------------------------------------------------
-- The acting user's id; NULL when nothing bound one (fails closed everywhere below).
CREATE OR REPLACE FUNCTION app_actor_id() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.user_id', true), '')
$$;

-- A mode is bound. Writes need one; 'interactive' is a person at the keyboard, 'service' a background duty.
CREATE OR REPLACE FUNCTION app_mode_ok() RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT current_setting('app.mode', true) IN ('interactive', 'service')
$$;

-- The acting user exists and is active (SECURITY DEFINER: reads users as the owner, so the users policies
-- cannot recurse into themselves).
CREATE OR REPLACE FUNCTION app_actor_active() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT EXISTS (SELECT 1 FROM users WHERE id = app_actor_id() AND status = 'active')
$$;

-- The acting user's role when active, else NULL (a disabled admin is nobody).
CREATE OR REPLACE FUNCTION app_actor_role() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT role FROM users WHERE id = app_actor_id() AND status = 'active'
$$;

-- The owners whose rows the acting user may read: themselves, plus every member of every team they manage.
-- Empty for nobody, for an unknown or inactive user, and for an admin who manages no team.
CREATE OR REPLACE FUNCTION app_visible_owners() RETURNS text[] LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT COALESCE(
    (SELECT ARRAY[me.id] || ARRAY(
       SELECT u.id FROM team_managers tm JOIN users u ON u.team_id = tm.team_id
       WHERE tm.user_id = me.id AND u.id <> me.id)
     FROM users me WHERE me.id = app_actor_id() AND me.status = 'active'),
    '{}'::text[])
$$;

-- The acting user may write a row owned by `owner`: it is their own, they are active, a mode is bound.
CREATE OR REPLACE FUNCTION app_can_write(owner text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT owner IS NOT NULL AND owner = app_actor_id() AND app_mode_ok() AND app_actor_active()
$$;

-- No user exists yet: the first users row (the local user, the bootstrap admin) may be inserted by anyone.
CREATE OR REPLACE FUNCTION app_users_empty() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT NOT EXISTS (SELECT 1 FROM users)
$$;

-- Whose node this is, readable with nobody bound: the worker resolves an event's owner before it can bind
-- that owner (orchestrator/workflow.owner_of_event). An id, never content; needs the owner role to BYPASSRLS.
CREATE OR REPLACE FUNCTION app_owner_of(node text) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT owner_id FROM nodes WHERE id = node
$$;

-- Model spend since `since` (budget.py, Phase 6), numbers only. The org's daily cap must count every rep's
-- runs, which no rep may read; the Usage panel under Settings (admin-only in cloud mode) lists spend per
-- user. Both answer only to an active user; per owner, an admin gets every owner and anyone else the owners
-- they may read (app_visible_owners). A sum per owner, never a row of agent_runs.
CREATE OR REPLACE FUNCTION app_org_spend_since(since text) RETURNS double precision
LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT CASE WHEN app_actor_active() THEN COALESCE(SUM(cost_usd), 0)::double precision ELSE 0 END
  FROM agent_runs WHERE started_at >= since
$$;

CREATE OR REPLACE FUNCTION app_spend_by_owner_since(since text)
RETURNS TABLE(owner_id text, spent double precision, runs bigint, unpriced bigint, deferred bigint)
LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT r.owner_id, COALESCE(SUM(r.cost_usd), 0)::double precision, COUNT(*),
         SUM(CASE WHEN r.cost_usd IS NULL AND r.status = 'ok' THEN 1 ELSE 0 END),
         SUM(CASE WHEN r.error LIKE 'budget_deferred:%' THEN 1 ELSE 0 END)
  FROM agent_runs r
  WHERE r.started_at >= since AND app_actor_active()
    AND (app_actor_role() = 'admin' OR r.owner_id = ANY (app_visible_owners()))
  GROUP BY r.owner_id ORDER BY r.owner_id
$$;

-- The child-owner trigger (0002) reads the parent row; as the app role it would see nothing of another user's
-- parent and let a mismatched child through. As the owner it sees every parent and refuses the mismatch.
ALTER FUNCTION app_child_owner() SECURITY DEFINER;

-- A user may edit their own profile; only an admin changes who someone is in the org. The one status change a
-- user makes themselves is accepting their own invite at their first Google sign-in (invited -> active,
-- web/auth.py _resolve_user); every other status change is an admin's. The guard constrains the APP role
-- (session_user: current_user is the definer in here); the owner role, an operator at psql or the migrator,
-- bypasses row security altogether and is not the app.
CREATE OR REPLACE FUNCTION app_users_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  IF session_user = '@APP_ROLE@' AND app_actor_role() IS DISTINCT FROM 'admin' AND (
       NEW.id IS DISTINCT FROM OLD.id OR NEW.role IS DISTINCT FROM OLD.role
       OR NEW.team_id IS DISTINCT FROM OLD.team_id
       OR (NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status = 'invited' AND NEW.status = 'active' AND OLD.id = app_actor_id()))) THEN
    RAISE EXCEPTION 'only an admin may change a user''s id, role, team or status'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_users_guard ON users;
CREATE TRIGGER trg_users_guard BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION app_users_guard();

-- Whose object an (entity_type, entity_id) names (Phase 7: comments and access_log), readable by the policies
-- whoever asks: a call, deal or loop node's owner, an email's owner, or, for a coaching note, the user it is
-- about. An owner id, never content (like app_owner_of); NULL for anything that does not exist.
-- (Parameters are prefixed: nodes has a column named `kind`, which would shadow a parameter of that name.)
CREATE OR REPLACE FUNCTION app_entity_owner(p_type text, p_id text) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT CASE
    WHEN p_type IN ('call', 'deal', 'loop') THEN (SELECT n.owner_id FROM nodes n WHERE n.id = p_id AND n.type = p_type)
    WHEN p_type = 'email' THEN (SELECT e.owner_id FROM emails e
                                WHERE e.id = CASE WHEN p_id ~ '^[0-9]{1,9}$' THEN p_id::integer END)
    WHEN p_type = 'coaching' THEN (SELECT u.id FROM users u WHERE u.id = p_id)
  END
$$;

-- A comment's thread, author and time never change; only its author edits the text; a resolve is signed by
-- whoever resolved it. The policies say WHO may update a comment (its rep or its author); this says WHAT.
CREATE OR REPLACE FUNCTION app_comments_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  IF session_user = '@APP_ROLE@' AND (
       NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.author_id IS DISTINCT FROM OLD.author_id OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
       OR NEW.entity_id IS DISTINCT FROM OLD.entity_id OR NEW.turn_idx IS DISTINCT FROM OLD.turn_idx
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NEW.body IS DISTINCT FROM OLD.body AND OLD.author_id IS DISTINCT FROM app_actor_id())
       OR (NEW.resolved_by IS DISTINCT FROM OLD.resolved_by AND NEW.resolved_by IS DISTINCT FROM app_actor_id()
           AND NEW.resolved_by IS NOT NULL)) THEN
    RAISE EXCEPTION 'a comment keeps its thread, author and time; only its author edits it; a resolve is signed by who resolved it'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_comments_guard ON comments;
CREATE TRIGGER trg_comments_guard BEFORE UPDATE ON comments FOR EACH ROW EXECUTE FUNCTION app_comments_guard();

"""

# ---- policy expressions -------------------------------------------------------------------------

OWNED_READ = "owner_id = ANY (app_visible_owners())"
OWNED_WRITE = "app_can_write(owner_id)"
NULLABLE_READ = "(owner_id IS NULL AND app_actor_active()) OR owner_id = ANY (app_visible_owners())"
NULLABLE_WRITE = "(owner_id IS NULL AND app_actor_active() AND app_mode_ok()) OR app_can_write(owner_id)"
ACTIVE = "app_actor_active()"
ACTIVE_WRITE = "app_actor_active() AND app_mode_ok()"
ADMIN = "app_actor_role() = 'admin'"
ANY = "true"
ACTIVE_ADMIN_WRITE = f"{ACTIVE_WRITE} AND {ADMIN}"
OWN_GRANT = f"(user_id = app_actor_id() AND app_actor_active()) OR {ADMIN}"
SERVICE_OR_ADMIN = f"{ACTIVE_WRITE} AND ({ADMIN} OR current_setting('app.mode', true) = 'service')"
HEARTBEAT = "key LIKE 'ops:%'"                 # ops.py: process heartbeats and the scheduler leader row

# {table: (select, insert, update, delete)}; None = no policy for that command (the command is refused,
# because RLS is enabled and no policy permits it).
ORG_POLICIES = {
    "accounts": (ACTIVE, ACTIVE_WRITE, ACTIVE_WRITE, ACTIVE_WRITE),
    "people": (ACTIVE, ACTIVE_WRITE, ACTIVE_WRITE, ACTIVE_WRITE),
    "users": (ANY, f"{ADMIN} OR app_users_empty()", f"{ADMIN} OR id = app_actor_id()", ADMIN),
    "teams": (ANY, ADMIN, ADMIN, ADMIN),
    "team_managers": (ANY, ADMIN, ADMIN, ADMIN),
}
SYSTEM_POLICIES = {
    "wf_events": (ANY, ANY, ANY, ANY),
    "state": (f"{ACTIVE} OR {HEARTBEAT}", f"{SERVICE_OR_ADMIN} OR {HEARTBEAT}",
              f"{SERVICE_OR_ADMIN} OR {HEARTBEAT}", SERVICE_OR_ADMIN),
    "user_state": ("user_id = app_actor_id() AND app_actor_active()",) * 4,
    "user_speaker_labels": ("user_id = app_actor_id() AND app_actor_active()",) * 4,
    "schema_migrations": (ANY, None, None, None),
    "schema_repeatables": (ANY, None, None, None),
    # Phase 3. The reasons are in the module docstring.
    "sessions": (ANY, ANY, ANY, ANY),
    "invites": (ANY, ADMIN, ANY, ADMIN),
    "oauth_tokens": (OWN_GRANT,) * 4,
    # Phase 6.
    "org_settings": (ANY, ACTIVE_ADMIN_WRITE, ACTIVE_ADMIN_WRITE, ACTIVE_ADMIN_WRITE),
    # Phase 7. Insert-only: a view is logged by the viewer, of an object they may read, whose owner is
    # recorded truthfully; the object's owner (and that owner's managers) read who looked. Nobody edits it.
    "access_log": ("owner_user_id = ANY (app_visible_owners())",
                   "viewer_id = app_actor_id() AND app_mode_ok() AND owner_user_id = ANY (app_visible_owners()) "
                   "AND owner_user_id = app_entity_owner(entity_type, entity_id)", None, None),
}


# OWNED tables whose writes are NOT "the owner writes": {table: (select, insert, update, delete, reason)}.
# Reads stay the OWNED read (the owner and the owner's managers); every write expression still names the
# acting user (app_actor_id) and the owners they may see (app_visible_owners), so nothing here is open.
COMMENT_VISIBLE = "owner_id = ANY (app_visible_owners())"
OWNED_EXCEPTIONS = {
    "comments": (
        OWNED_READ,
        # INSERT: the author is the acting user, the owner is one they may read (themselves, or a rep of a
        # team they manage), and that owner really is the owner of the object the comment names. A coaching
        # note is about someone else: nobody writes one on themselves.
        f"author_id = app_actor_id() AND app_mode_ok() AND {COMMENT_VISIBLE} "
        "AND owner_id = app_entity_owner(entity_type, entity_id) "
        "AND (entity_type <> 'coaching' OR owner_id <> author_id)",
        # UPDATE (resolve; the author may also edit the text): the rep whose comment it is, or its author
        # while the rep is still theirs to see. trg_comments_guard fixes which columns may change.
        f"app_can_write(owner_id) OR (author_id = app_actor_id() AND app_mode_ok() AND {COMMENT_VISIBLE})",
        # DELETE: the author only, while the rep is still theirs to see.
        f"author_id = app_actor_id() AND app_mode_ok() AND {COMMENT_VISIBLE}",
        "The one deliberate exception to 'the owner writes' (plan, Phase 7): a manager comments on their "
        "rep's call, and a comment is OWNED by the rep whose work it is about (so it lives, is read and is "
        "exported with that work, and the rep can resolve it), not by the manager who wrote it. So the "
        "author, not the owner, is the acting user on INSERT; the owner must be someone the author may read "
        "AND the actual owner of the commented object (app_entity_owner), so nobody files a comment under a "
        "rep they do not manage or against an object that is not that rep's. Comments never reach a prompt.",
    ),
}


def policies_for(table: str) -> tuple:
    """(select, insert, update, delete) expressions for a table, from its class."""
    kind = tenancy.TABLE_CLASS[table]
    if kind == tenancy.OWNED:
        if table in OWNED_EXCEPTIONS:
            return OWNED_EXCEPTIONS[table][:4]
        if table in tenancy.OWNER_NULLABLE:
            return (NULLABLE_READ, NULLABLE_WRITE, NULLABLE_WRITE, NULLABLE_WRITE)
        return (OWNED_READ, OWNED_WRITE, OWNED_WRITE, OWNED_WRITE)
    table_policies = ORG_POLICIES if kind == tenancy.ORG else SYSTEM_POLICIES
    if table not in table_policies:
        raise KeyError(f"{table} is {kind} but store/rls.py has no policy set for it")
    return table_policies[table]


def table_block(table: str) -> str:
    kind = tenancy.TABLE_CLASS[table]
    select, insert, update, delete = policies_for(table)
    lines = [f"-- {table}: {kind}",
             f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"]
    # FORCE for OWNED; NO FORCE otherwise, stated rather than assumed, so a table that stops being OWNED
    # loses it when this file is applied again.
    lines.append(f"ALTER TABLE {table} {'' if kind == tenancy.OWNED else 'NO '}FORCE ROW LEVEL SECURITY;")
    lines.append(f"CREATE POLICY {table}_select ON {table} FOR SELECT USING ({select});")
    if insert is not None:
        lines.append(f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK ({insert});")
    if update is not None:
        lines.append(f"CREATE POLICY {table}_update ON {table} FOR UPDATE USING ({update}) WITH CHECK ({update});")
    if delete is not None:
        lines.append(f"CREATE POLICY {table}_delete ON {table} FOR DELETE USING ({delete});")
    return "\n".join(lines) + "\n"


def generate() -> str:
    unknown = {t: k for t, k in tenancy.TABLE_CLASS.items() if k not in (tenancy.OWNED, tenancy.ORG, tenancy.SYSTEM)}
    if unknown:
        raise ValueError(f"unknown table classes: {unknown}")
    for table, policies in list(ORG_POLICIES.items()) + list(SYSTEM_POLICIES.items()):
        if tenancy.TABLE_CLASS.get(table) not in (tenancy.ORG, tenancy.SYSTEM):
            raise ValueError(f"store/rls.py names {table} which tenancy.py does not classify ORG or SYSTEM")
    for table, spec in OWNED_EXCEPTIONS.items():
        if tenancy.TABLE_CLASS.get(table) != tenancy.OWNED or not spec[4].strip():
            raise ValueError(f"store/rls.py OWNED_EXCEPTIONS names {table}: it must be OWNED and carry a reason")
    parts = [HEADER, DROP_POLICIES, ROLE_BOOTSTRAP, FUNCTIONS.replace("@APP_ROLE@", APP_ROLE)]
    for kind, title in ((tenancy.OWNED, "OWNED: one rep's work; the owner writes, the owner's managers read"),
                        (tenancy.ORG, "ORG: the shared directory"),
                        (tenancy.SYSTEM, "SYSTEM: the machinery")):
        parts.append(f"-- ---- {title} " + "-" * max(4, 98 - len(title)) + "\n")
        for table in sorted(tenancy.tables_of(kind)):
            parts.append(table_block(table))
        parts.append("\n")
    return "".join(parts).rstrip() + "\n"
