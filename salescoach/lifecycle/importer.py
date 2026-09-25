"""`salescoach import-sqlite PATH --as <email> [--dry-run]`: a single-user SQLite install into an EMPTY
Postgres org, as the user with that email (created as an active rep when there is none).

What happens, in order:
  1. The source file is copied (sqlite3's backup API, the source opened read-only) into a temporary folder,
     and the COPY is brought to this build's schema by the SQLite migrations (store/migrate.py, the plugin
     SQL, the runtime ensure_columns helpers). The source file is never written, whatever its version.
  2. On the target (the owner role, DATABASE_MIGRATE_URL; row_security off) the schema must be this build's,
     no OWNED table may hold a row, and no directory row may collide (a person's email, a person who already
     is this user). The user is looked up by email, else created (active rep, profile from the source's
     `local` users row).
  3. In ONE transaction: every table below is copied in foreign-key order, ids verbatim (integers included,
     because ids are embedded in JSON and strings: evidence, payloads, created_items), and every identity
     sequence is set past the largest id. Then a verification pass re-counts every table and checks that
     no 'local' owner or owner-embedding string is left. Any failure rolls everything back. --dry-run does all
     of it and rolls back at the end: the counts it prints are what an import would write.

Rewrite rules (the owner is in these values, so 'local' becomes the user's id `<uid>`):
  owner_id                               'local' -> <uid> on every OWNED row (NULL stays NULL: the directory's
                                         account and person nodes); any other owner refuses the import
  learned pattern ids                    lp:<family>:u:local:<key> -> lp:<family>:u:<uid>:<key>, in every column
                                         (learned_patterns.id / merged_into, learning_proposals, field_provenance,
                                         memory_conflicts, events, payloads)
  a user's own imports                   paste:local:<sha> / upload:local:<sha> -> paste:<uid>:... / upload:<uid>:...
  recorder-native refs (calls.source_ref, fireflies|fathom|granola|tldv:<id> -> <kind>:<uid>:<id>, and
    and the same value in sources.uri,   ext:<source>:<id> -> ext:<source>:<uid>:<id>: the cloud's form, so the
    raw_payloads.source_ref,             rep's own recorder connection finds the meeting already imported
    events.source_id)                    instead of importing it twice. file:/webhook:/capture refs are unchanged
  user:local                             -> user:<uid> (events.actor, emails.approved_by, wf_events.entity_id)
  user columns                           people.user_id (the local person row; is_me stays 1),
                                         user_state.user_id, user_speaker_labels.user_id, events.actor_user_id,
                                         comments.author_id / resolved_by (and a coaching note's entity_id),
                                         access_log.viewer_id / owner_user_id, wf_events.owner: 'local' -> <uid>
  wf_events.dedupe_key                   a ':'-separated segment 'local' -> <uid> (FOLLOW_UP_RUN:local:<ts>,
                                         REPLY:local:<msg>, PROCESS:local:<call>:..., the paste refs inside)
  wf_events.payload                      "owner_id": "local" -> <uid>
Transcript text (turns.text) and raw payload bodies are never rewritten.

Not copied: the `local` users row (the target user stands for it), teams and team_managers (a single-user
install has none), `state` (the laptop's org-wide machinery: the local poller's cursors, the setup wizard,
Jarvis, heartbeats; except STATE_TO_USER, the per-user keys an older install still keeps there, which become
the user's user_state rows), org_settings (a local install keeps its settings in yaml files: the admin sets the org's
in Setup), sessions, invites and oauth_tokens (sign in and connect Google again), and the payload FILES under
data/inbox (a local install keeps raw payloads as files; only raw_payloads rows are copied). user_state and
user_speaker_labels rows the target user already has are kept (the source's are skipped for those keys).
"""
import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import identity, users
from ..store import tenancy
from ..store.stores import now

# Directory and machinery tables copied besides every OWNED one (tenancy.TABLE_CLASS).
EXTRA_TABLES = ("accounts", "people", "wf_events", "user_state", "user_speaker_labels", "access_log")
NOT_COPIED = {
    "users": "the target user stands for the local user",
    "teams": "a single-user install has none", "team_managers": "a single-user install has none",
    "state": "the laptop's org-wide machinery (local poller cursors, setup wizard, Jarvis, heartbeats)",
    "org_settings": "a local install keeps its settings in yaml files; set the org's in Setup",
    "sessions": "sign in again", "invites": "not a local-install concept", "oauth_tokens": "connect Google again",
    "schema_migrations": "Postgres only", "schema_repeatables": "Postgres only", "sqlite_sequence": "SQLite only",
}
# Target rows the user may already have: kept, and the source's row for that key is skipped.
KEEP_TARGET = {"user_state": ("user_id", "key"), "user_speaker_labels": ("user_id", "label_norm")}
# Free text that is never rewritten: what was said, what a source delivered.
VERBATIM = {("turns", "text"), ("raw_payloads", "body")}
RECORDER_KINDS = ("fireflies", "fathom", "granola", "tldv")
# Per-user bookkeeping an older install still keeps in `state` (migration 7 split the keys but left the rows):
# the ones that stop the cloud redoing work the laptop did become the user's user_state rows.
STATE_TO_USER = ("automation:followups:ran_for", "automation:followups:last_eval", "automation:replies:last_poll",
                 "setup:card_dismissed")
USER_COLUMNS = {("people", "user_id"), ("user_state", "user_id"), ("user_speaker_labels", "user_id"),
                ("events", "actor_user_id"), ("comments", "author_id"), ("comments", "resolved_by"),
                ("access_log", "viewer_id"), ("access_log", "owner_user_id"), ("wf_events", "owner")}
REF_COLUMNS = {("calls", "source_ref"), ("sources", "uri"), ("raw_payloads", "source_ref"), ("events", "source_id")}
LOCAL = identity.LOCAL_USER
BATCH = 500


class ImportRefused(RuntimeError):
    """The import cannot go ahead; the message says why. Nothing was written."""


@dataclass
class Report:
    email: str
    user_id: str = ""
    user_created: bool = False
    source_version: int = 0
    counts: dict = field(default_factory=dict)          # table -> rows written
    skipped: dict = field(default_factory=dict)         # table -> rows kept from the target instead
    not_copied: dict = field(default_factory=dict)      # table -> rows in the source that are not imported
    sequences: dict = field(default_factory=dict)       # table -> the value its identity sequence was set to
    from_state: int = 0                                 # per-user keys moved from the source's state
    dry_run: bool = False
    committed: bool = False

    def lines(self) -> list:
        out = [f"{'DRY RUN: nothing written. ' if self.dry_run else ''}import as {self.email} "
               f"(user {self.user_id}{', created as an active rep' if self.user_created else ''}); "
               f"source schema version {self.source_version}"]
        for table in sorted(self.counts):
            extra = f" (kept {self.skipped[table]} the user already had)" if self.skipped.get(table) else ""
            out.append(f"  {table:<22} {self.counts[table]:>7}{extra}")
        out.append(f"  {'total':<22} {sum(self.counts.values()):>7}")
        if self.from_state:
            out.append(f"  ({self.from_state} of the user_state rows are per-user keys an older install kept in state)")
        for table, n in sorted(self.not_copied.items()):
            if n:
                out.append(f"  not imported: {table} ({n} rows): {NOT_COPIED.get(table, 'not a table this build knows')}")
        out.append("  payload files under data/inbox are not imported (only raw_payloads rows are)")
        out.append("verification passed: every table re-counted, no 'local' owner left"
                   + ("; rolled back (dry run)" if self.dry_run else "; committed"))
        return out


# ---- step 1: a migrated temporary copy ------------------------------------------------------------------

def _copy_source(source: Path, folder: Path) -> Path:
    if not source.is_file():
        raise ImportRefused(f"{source}: no such file")
    target = folder / "import-copy.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        try:
            src.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.DatabaseError as exc:
            raise ImportRefused(f"{source}: not a SQLite database ({exc})") from exc
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _migrate_copy(path: Path):
    """Bring the COPY to this build's schema: what opening it as a local store does (store/migrate.py, the plugin
    SQL, reconcile_columns) and the learning and calendar ensure_columns helpers, but not the local-user
    bootstrap (that would read THIS machine's seller.yaml into the copy). Returns the source's version."""
    import os
    from ..automation import calendar
    from ..learning import ensure_columns
    from ..store import db, engine, migrate, stores
    raw = sqlite3.connect(path)
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    has_calls = raw.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone()
    raw.close()
    if not has_calls:
        raise ImportRefused("the source has no calls table: not a sales coach store")
    conn = engine.connect(str(path))
    try:
        migrate.run(conn)
        if os.environ.get("SALESCOACH_NO_PLUGINS") != "1":
            for sql_file in sorted(stores.PLUGINS_DIR.glob("*.sql")):
                text = sql_file.read_text()
                conn.executescript(text)
                stores.reconcile_columns(conn, text)
        if conn.dialect != db.SQLITE:
            raise ImportRefused("the copy did not open as SQLite")
        ensure_columns(conn)
        calendar.ensure_columns(conn)
        conn.commit()
    finally:
        conn.close()
    return version


# ---- rewriting -----------------------------------------------------------------------------------------

def _ref(value, uid):
    """A calls.source_ref (or the same value elsewhere) in the cloud's form for this owner."""
    if not isinstance(value, str):
        return value
    value = value.replace("paste:local:", f"paste:{uid}:").replace("upload:local:", f"upload:{uid}:")
    kind, sep, rest = value.partition(":")
    if sep and kind in RECORDER_KINDS and rest and not rest.startswith(f"{uid}:"):
        return f"{kind}:{uid}:{rest}"
    if kind == "ext" and sep:
        source, sep2, ext_id = rest.partition(":")
        if sep2 and ext_id and not ext_id.startswith(f"{uid}:"):
            return f"ext:{source}:{uid}:{ext_id}"
    return value


_SEGMENT = re.compile(r"(?<![^:])local(?![^:])")          # a whole ':'-separated segment 'local'


def _text(value, uid):
    """The owner-embedding patterns that may sit in any id-like or JSON text."""
    if not isinstance(value, str) or "local" not in value:
        return value
    if ":u:local:" in value:
        value = value.replace(":u:local:", f":u:{uid}:")
    value = value.replace("paste:local:", f"paste:{uid}:").replace("upload:local:", f"upload:{uid}:")
    return re.sub(r"\buser:local\b", f"user:{uid}", value)


class Rewriter:
    def __init__(self, uid: str):
        self.uid = uid
        self.refs: dict = {}                              # old calls.source_ref -> new

    def learn_refs(self, rows):
        for r in rows:
            old = r["source_ref"]
            if old:
                self.refs[old] = _ref(old, self.uid)

    def value(self, table, column, value, row):
        uid = self.uid
        if column == "owner_id" and table in tenancy.TABLE_CLASS and tenancy.TABLE_CLASS[table] == tenancy.OWNED:
            if value is None or value == LOCAL:
                return uid if value == LOCAL else None
            raise ImportRefused(f"{table} holds a row owned by {value!r}: the source is not a single-user install")
        if (table, column) in USER_COLUMNS:
            # A single-user install names one user, `local`. Any other id would reach the cloud verbatim and name
            # somebody else there (another rep's people link, a comment "by" a manager, an access-log viewer).
            if value is None or value == LOCAL:
                return uid if value == LOCAL else None
            raise ImportRefused(f"{table}.{column} names user {value!r}: a single-user install names only 'local'")
        if table == "comments" and column == "entity_id" and row.get("entity_type") == "coaching" and value == LOCAL:
            return uid
        if (table, column) in REF_COLUMNS:
            if value in self.refs:
                return self.refs[value]
            return _text(value, uid) if table != "calls" else _ref(value, uid)
        if table == "wf_events" and column == "dedupe_key" and isinstance(value, str):
            return _SEGMENT.sub(uid, _text(value, uid))
        if table == "wf_events" and column == "payload" and isinstance(value, str) and '"local"' in value:
            try:
                data = json.loads(value)
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("owner_id") == LOCAL:
                data["owner_id"] = uid
                value = json.dumps(data)
        if (table, column) in VERBATIM:
            return value
        return _text(value, uid)


# ---- the target ----------------------------------------------------------------------------------------

def _pg_columns(pg, table) -> dict:
    """{column: data_type} in order."""
    return {r[0]: r[1] for r in pg.raw.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s ORDER BY ordinal_position", (table,)).fetchall()}


def _fk_order(pg, tables) -> list:
    """`tables` ordered so that every table comes after the tables its foreign keys point at."""
    deps = {t: set() for t in tables}
    for child, parent in pg.raw.execute(
            "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c "
            "JOIN pg_class r ON r.oid = c.conrelid JOIN pg_namespace n ON n.oid = r.relnamespace "
            "WHERE c.contype = 'f' AND n.nspname = current_schema()").fetchall():
        child, parent = child.split(".")[-1].strip('"'), parent.split(".")[-1].strip('"')
        if child in deps and parent in deps and child != parent:
            deps[child].add(parent)
    order, done = [], set()
    pending = sorted(tables, key=lambda t: (t != "nodes", t))
    while pending:
        ready = [t for t in pending if deps[t] <= done]
        if not ready:
            raise ImportRefused(f"circular foreign keys among {pending}")
        for t in ready:
            order.append(t)
            done.add(t)
        pending = [t for t in pending if t not in done]
    return order


def _coerce(value, pg_type):
    """SQLite is loosely typed: a number in a TEXT column, digits in an INTEGER one. Postgres is not."""
    if value is None:
        return None
    if pg_type in ("integer", "bigint", "smallint"):
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    if pg_type in ("double precision", "real", "numeric"):
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value
    if pg_type == "text" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if pg_type == "bytea" and isinstance(value, str):
        return value.encode()
    return value


def _owned_rows(pg) -> dict:
    out = {}
    for table in sorted(tenancy.tables_of(tenancy.OWNED)):
        if not pg.table_exists(table):
            continue
        where = " WHERE owner_id IS NOT NULL" if table in tenancy.OWNER_NULLABLE else ""   # directory nodes are the org's
        n = pg.raw.execute(f'SELECT COUNT(*) FROM "{table}"{where}').fetchone()[0]
        if n:
            out[table] = n
    return out


def _target_user(pg, email, src_local) -> tuple:
    row = users.by_email(pg, email)
    if row is not None:
        if row["id"] == LOCAL:
            raise ImportRefused(f"{email} is the target's own local user: import-sqlite fills a cloud org, as a "
                                "cloud user")
        if row["status"] == "disabled":
            raise ImportRefused(f"{email} is disabled: enable them in Admin first")
        return row, False
    profile = {}
    if src_local:
        for key in ("extra_emails", "aliases", "languages"):
            try:
                profile[key] = json.loads(src_local.get(key) or "[]")
            except ValueError:
                pass
        for key in ("signature", "timezone", "role_title", "style", "call_context"):
            if src_local.get(key):
                profile[key] = src_local[key]
    name = (src_local or {}).get("name") or ""
    row = users.create(pg, email, name, role="rep", status="active", **profile)
    return row, True


# ---- the import ----------------------------------------------------------------------------------------

def run(source, email: str, url=None, dry_run: bool = False, log=print) -> Report:
    """Import `source` (a sales.db path) into the owner-role URL's database as `email`. Returns the Report;
    raises ImportRefused (nothing written) when it cannot."""
    from ..store import pgmigrate
    from . import owner
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ImportRefused("--as needs the user's email address")
    report = Report(email=email, dry_run=dry_run)
    folder = Path(tempfile.mkdtemp(prefix="salescoach-import-"))
    try:
        copy = _copy_source(Path(source).expanduser(), folder)
        report.source_version = _migrate_copy(copy)
        src = sqlite3.connect(copy)
        src.row_factory = sqlite3.Row
        try:
            with owner.connection(url) as pg:
                pgmigrate.assert_current(pg)
                _import(src, pg, email, report, dry_run)
        finally:
            src.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    for line in report.lines():
        log(line)
    return report


def _source_tables(src) -> dict:
    return {r[0]: src.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
            for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _import(src, pg, email, report, dry_run):
    present = _source_tables(src)
    wanted = [t for t in (*sorted(tenancy.tables_of(tenancy.OWNED)), *EXTRA_TABLES) if t in present]
    report.not_copied = {t: n for t, n in present.items() if t not in wanted and n}
    unknown = [t for t in report.not_copied if t not in NOT_COPIED]
    if unknown:
        raise ImportRefused(f"the source has tables this build does not know, with rows: {sorted(unknown)} "
                            "(is it from a newer build?)")
    owned = _owned_rows(pg)
    if owned:
        raise ImportRefused("the target org is not empty: " + ", ".join(f"{t} ({n})" for t, n in owned.items())
                            + ". import-sqlite only fills an empty org")
    missing = [t for t in wanted if not pg.table_exists(t)]
    if missing:
        raise ImportRefused(f"the target has no {missing}: run `salescoach migrate`")
    local_row = src.execute("SELECT * FROM users WHERE id=?", (LOCAL,)).fetchone() if "users" in present else None
    others = src.execute("SELECT COUNT(*) FROM users WHERE id != ?", (LOCAL,)).fetchone()[0] if "users" in present else 0
    if others:
        raise ImportRefused("the source has users other than the local one: not a single-user install")
    pg.execute("BEGIN")
    try:
        target, created = _target_user(pg, email, dict(local_row) if local_row else None)
        uid = report.user_id = target["id"]
        report.user_created = created
        _check_directory(src, pg, uid)
        rw = Rewriter(uid)
        rw.learn_refs(src.execute("SELECT source_ref FROM calls").fetchall())
        before = {t: pg.raw.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in wanted}
        for table in _fk_order(pg, wanted):
            written, kept = _copy_table(src, pg, table, rw, before[table])
            report.counts[table] = written
            if kept:
                report.skipped[table] = kept
        report.from_state = _state_to_user(src, pg, uid, present)
        if report.from_state:
            report.counts["user_state"] = report.counts.get("user_state", 0) + report.from_state
        report.sequences = _set_sequences(pg, wanted)
        _audit(pg, uid, report)
        report.counts["events"] = report.counts.get("events", 0) + 1       # the import's own audit row
        _verify(src, pg, uid, wanted, before, report)
        if dry_run:
            pg.rollback()
        else:
            pg.commit()
            report.committed = True
    except BaseException:
        pg.rollback()
        raise


def _check_directory(src, pg, uid):
    """accounts and people are the org's shared directory: the source's rows must not collide with the target's."""
    emails = [r[0].strip().lower() for r in src.execute("SELECT email FROM people WHERE email IS NOT NULL").fetchall()
              if r[0] and r[0].strip()]
    clash = 0
    for i in range(0, len(emails), BATCH):
        chunk = emails[i:i + BATCH]
        clash += pg.raw.execute("SELECT COUNT(*) FROM people WHERE lower(email) = ANY(%s)", (chunk,)).fetchone()[0]
    if clash:
        raise ImportRefused(f"{clash} of the source's people already exist in the target's directory (same email): "
                            "import-sqlite only fills an empty org")
    if pg.raw.execute("SELECT 1 FROM people WHERE user_id = %s", (uid,)).fetchone() and \
            src.execute("SELECT 1 FROM people WHERE user_id = ?", (LOCAL,)).fetchone():
        raise ImportRefused("the target directory already has a person row for this user: import-sqlite only fills "
                            "an empty org")
    ids = [r[0] for r in src.execute("SELECT id FROM nodes").fetchall()]
    for i in range(0, len(ids), BATCH):
        if pg.raw.execute("SELECT 1 FROM nodes WHERE id = ANY(%s) LIMIT 1", (ids[i:i + BATCH],)).fetchone():
            raise ImportRefused("a node id of the source already exists in the target")


def _copy_table(src, pg, table, rw, target_before) -> tuple:
    import psycopg
    from psycopg import sql
    pg_cols = _pg_columns(pg, table)
    src_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")').fetchall()]
    extra = [c for c in src_cols if c not in pg_cols]
    for col in extra:
        if src.execute(f'SELECT 1 FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 1').fetchone():
            raise ImportRefused(f"{table}.{col} holds data and the target has no such column")
    cols = [c for c in src_cols if c in pg_cols]
    if table in ("wf_events", "access_log") and target_before:
        # machinery the target may already hold rows of: nothing references these integer ids, so the target
        # numbers them instead of risking a collision with its own
        cols = [c for c in cols if c != "id"]
    insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols))
    if table in KEEP_TARGET:
        insert = insert + sql.SQL(" ON CONFLICT ({}) DO NOTHING").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in KEEP_TARGET[table]))
    order = " ORDER BY id" if "id" in src_cols else " ORDER BY rowid"
    cur = src.execute(f'SELECT * FROM "{table}"{order}')
    written = total = 0
    types = [pg_cols[c] for c in cols]
    with pg.raw.cursor() as out:
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            batch = []
            for r in rows:
                d = dict(r)
                batch.append([_coerce(rw.value(table, c, d[c], d), t) for c, t in zip(cols, types)])
            total += len(batch)
            try:
                if table in KEEP_TARGET:
                    for values in batch:
                        out.execute(insert, values)
                        written += out.rowcount
                else:
                    out.executemany(insert, batch)
                    written += len(batch)
            except psycopg.Error as exc:
                # The primary message names the table and the constraint; the detail would quote the row (a
                # transcript line, an email), which an operator's terminal must not show.
                raise ImportRefused(f"{table}: {exc.diag.message_primary or type(exc).__name__}") from None
    return written, total - written


def _state_to_user(src, pg, uid, present) -> int:
    if "state" not in present:
        return 0
    moved = 0
    for key in STATE_TO_USER:
        row = src.execute("SELECT value, updated_at FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            continue
        cur = pg.raw.execute("INSERT INTO user_state(user_id,key,value,updated_at) VALUES (%s,%s,%s,%s) "
                             "ON CONFLICT (user_id, key) DO NOTHING", (uid, key, row["value"], row["updated_at"]))
        moved += cur.rowcount
    return moved


def _set_sequences(pg, tables) -> dict:
    out = {}
    for (table,) in pg.raw.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND column_name = 'id' AND is_identity = 'YES'").fetchall():
        if table not in tables:
            continue
        top = pg.raw.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()[0]
        if top is None:
            continue
        pg.raw.execute("SELECT setval(pg_get_serial_sequence(%s, 'id'), %s)", (table, int(top)))
        out[table] = int(top)
    return out


def _audit(pg, uid, report):
    pg.raw.execute("INSERT INTO events(ts, actor, kind, node_id, before, after, owner_id, actor_user_id) "
                   "VALUES (%s, 'salescoach import-sqlite', 'admin.import_sqlite', NULL, NULL, %s, %s, NULL)",
                   (now(), json.dumps({"user_id": uid, "email": report.email, "counts": report.counts,
                                       "source_version": report.source_version}, sort_keys=True), uid))


def _verify(src, pg, uid, tables, before, report):
    """Re-count every table and look for anything still naming the local user."""
    problems = []
    for table in tables:
        have = pg.raw.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        expected = before[table] + report.counts.get(table, 0)
        if have != expected:
            problems.append(f"{table}: {have} rows, expected {expected}")
        src_n = (src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] + (1 if table == "events" else 0)
                 + (report.from_state if table == "user_state" else 0))
        if report.counts.get(table, 0) + report.skipped.get(table, 0) != src_n:
            problems.append(f"{table}: wrote {report.counts.get(table, 0)} (+{report.skipped.get(table, 0)} kept) "
                            f"of {src_n}")
        cols = _pg_columns(pg, table)
        if "owner_id" in cols and tenancy.TABLE_CLASS.get(table) == tenancy.OWNED:
            n = pg.raw.execute(f"SELECT COUNT(*) FROM \"{table}\" WHERE owner_id = 'local'").fetchone()[0]
            if n:
                problems.append(f"{table}: {n} rows still owned by 'local'")
    checks = {
        "people.user_id": "SELECT COUNT(*) FROM people WHERE user_id = 'local'",
        "user_state": "SELECT COUNT(*) FROM user_state WHERE user_id = 'local'",
        "user_speaker_labels": "SELECT COUNT(*) FROM user_speaker_labels WHERE user_id = 'local'",
        "wf_events.owner": "SELECT COUNT(*) FROM wf_events WHERE owner = 'local'",
        "learned pattern ids": "SELECT COUNT(*) FROM learned_patterns WHERE id LIKE '%:u:local:%' "
                               "OR merged_into LIKE '%:u:local:%'",
        "user source refs": "SELECT COUNT(*) FROM calls WHERE source_ref LIKE 'paste:local:%' "
                            "OR source_ref LIKE 'upload:local:%'",
    }
    for name, query in checks.items():
        n = pg.raw.execute(query).fetchone()[0]
        if n:
            problems.append(f"{name}: {n} still name the local user")
    if problems:
        raise ImportRefused("verification failed, nothing was written: " + "; ".join(problems))
