"""A user's own data, as a zip of JSON files (a data request; a rep leaving who wants their notes).

  /me/export                    the acting user downloads their own data, streamed as it is read
  salescoach export --user E    an operator exports one user's data (the owner role, DATABASE_MIGRATE_URL, on
                                Postgres: an admin reads no rep's content through the app)

What is in it: one `<table>.json` per table (a JSON array of row objects), plus README.json describing the files:
  * every OWNED table (store/tenancy.py): the rows whose owner_id is the user: their calls and transcripts,
    deals, loops, emails, coaching, learning, the comments on their work (whoever wrote them) ...;
  * their users row (profile), user_state, speaker labels, the access log of their objects, their sign-in
    sessions (times, address, browser; never the session key) and their Google grants' status (never a token);
  * source_connections without the encrypted key or webhook secrets.
Every query names the user (owner_id = the user) besides what row-level security already guarantees, so a
manager's export holds their own rows, never their team's. Binary columns (speaker and text embeddings) are
{"$base64": "..."}. The shared directory (accounts, people) is the org's, not the user's: rows refer to it by id.
"""
import base64
import json
import zipfile
from typing import Iterator, Optional

from .. import identity
from ..store import tenancy
from ..store.stores import now

CHUNK = 500
# SYSTEM / ORG tables that hold rows about the user: {table: (where column, columns left out)}
PERSONAL_SYSTEM = {
    "users": ("id", ()),
    "user_state": ("user_id", ()),
    "user_speaker_labels": ("user_id", ()),
    "access_log": ("owner_user_id", ()),
    "sessions": ("user_id", ("id",)),
    "oauth_tokens": ("user_id", ("refresh_token_enc", "access_token_enc", "key_id")),
}
SECRET_COLUMNS = {"source_connections": ("secret_enc", "key_id", "webhook_token_hash", "webhook_secret_enc")}


class _Pipe:
    """A write-only sink zipfile writes into; what it collected is handed out in pieces (no tell(): zipfile
    then writes a streamable archive with data descriptors)."""

    def __init__(self):
        self.parts: list = []

    def write(self, data) -> int:
        self.parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def drain(self) -> bytes:
        out, self.parts = b"".join(self.parts), []
        return out


def _jsonable(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$base64": base64.b64encode(bytes(value)).decode()}
    return value


def plan(conn) -> list:
    """[(table, where column, columns left out, description)] in a stable order, for the tables that exist."""
    out = []
    for table in sorted(tenancy.tables_of(tenancy.OWNED)):
        if conn.table_exists(table):
            out.append((table, "owner_id", SECRET_COLUMNS.get(table, ()), "your work (owner_id is you)"))
    for table, (column, hidden) in PERSONAL_SYSTEM.items():
        if conn.table_exists(table):
            out.append((table, column, hidden, "about you"))
    return out


def stream(conn, user_id: str, email: Optional[str] = None) -> Iterator[bytes]:
    """The zip, piece by piece. `conn` reads as the user (or as the owner role, for an operator); every query
    names the user."""
    pipe = _Pipe()
    zf = zipfile.ZipFile(pipe, "w", compression=zipfile.ZIP_DEFLATED)
    files = {}
    for table, column, hidden, what in plan(conn):
        cols = [c for c in conn.columns(table) if c not in hidden]
        order = " ORDER BY id" if "id" in cols else ""
        cur = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {column}=?{order}", (user_id,))
        n = 0
        with zf.open(f"{table}.json", "w", force_zip64=True) as f:
            f.write(b"[")
            while True:
                rows = cur.fetchmany(CHUNK)
                if not rows:
                    break
                for r in rows:
                    f.write((b",\n" if n else b"\n") + json.dumps({c: _jsonable(r[c]) for c in cols},
                                                                  ensure_ascii=False, default=str).encode())
                    n += 1
                yield pipe.drain()
            f.write(b"\n]\n")
        files[f"{table}.json"] = {"rows": n, "what": what, "selected_by": f"{column} = the user",
                                  **({"left_out": list(hidden)} if hidden else {})}
        yield pipe.drain()
    readme = {
        "what": "Your data from the sales coach: one JSON file per table, each an array of rows.",
        "user_id": user_id, "email": email, "exported_at": now(),
        "files": files,
        "notes": [
            "Only rows that are yours: owner_id (or user_id) is you. A manager's export does not hold their team's work.",
            "Comments on your work are included whoever wrote them; comments you wrote on someone else's work are theirs.",
            "Accounts and people are the org's shared directory; rows refer to them by id (deal_id, person_id, account_id).",
            "Binary values (embeddings) are {\"$base64\": ...}. Times are ISO-8601 text.",
            "Never included: session keys, OAuth tokens, recorder API keys, webhook secrets.",
        ],
    }
    zf.writestr("README.json", json.dumps(readme, indent=2, ensure_ascii=False))
    zf.close()
    yield pipe.drain()


def write(conn, user_id: str, path, email: Optional[str] = None) -> int:
    """The zip to a file. Returns its size."""
    size = 0
    with open(path, "wb") as out:
        for part in stream(conn, user_id, email):
            out.write(part)
            size += len(part)
    return size


def filename(user_id: str) -> str:
    return f"salescoach-export-{user_id}-{now()[:10]}.zip"


def stream_as(actor: identity.Actor) -> Iterator[bytes]:
    """/me/export: the response is produced after the handler returned, piece by piece, each piece possibly in
    another thread and context, so no contextvar is set across a yield: the connection is bound to `actor`
    directly (conn.actor and, on Postgres, the session settings the policies read)."""
    from .. import users
    from ..store import stores
    with identity.activate(None):
        conn = stores.sales()
    try:
        identity.bind(conn, actor)
        row = users.get(conn, actor.user_id)
        yield from stream(conn, actor.user_id, (row or {}).get("email"))
    finally:
        conn.close()
