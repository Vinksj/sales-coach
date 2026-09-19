"""Local embeddings for "have I been here before?".

nomic-embed-text runs in Ollama on loopback (already installed; nothing is
downloaded). Indexed: validated claims, per-call coaching insights, seller
observations, and 3-turn transcript windows. Vectors are float32 BLOBs in
sales.db; similarity is cosine in numpy, which is plenty at founder call volume.

Loopback only, so this does not go through lib/safefetch (the Ollama provider
makes the same call). When Ollama is down everything here is a quiet no-op:
index_pending() reports "unavailable" and similar() returns nothing.

Indexing never runs inside the post-call pipeline: `salescoach serve` keeps the
index fresh in the background, prep briefs top it up before searching, and
`salescoach embed-index` runs it by hand.
"""
import hashlib
import json
import time

import httpx
import numpy as np

from .. import config
from ..store.stores import now

_EMBEDDER = None
_AVAIL = {"at": 0.0, "ok": False}


class EmbedUnavailable(RuntimeError):
    pass


def _cfg() -> dict:
    return config.load("intel").get("embeddings") or {}


class OllamaEmbedder:
    """nomic-embed-text wants task prefixes: search_document for what is stored, search_query for the question."""

    def __init__(self, endpoint=None, model=None, timeout_s=None, batch=None):
        c = _cfg()
        self.endpoint = (endpoint or c.get("endpoint", "http://localhost:11434")).rstrip("/")
        self.model = model or c.get("model", "nomic-embed-text")
        self.timeout_s = timeout_s or c.get("timeout_s", 60)
        self.batch = batch or c.get("batch", 32)

    def available(self) -> bool:
        if time.monotonic() - _AVAIL["at"] < 60 and _AVAIL.get("endpoint") == self.endpoint:
            return _AVAIL["ok"]
        try:
            ok = httpx.get(f"{self.endpoint}/api/tags", timeout=2).status_code == 200
        except httpx.HTTPError:
            ok = False
        _AVAIL.update(at=time.monotonic(), ok=ok, endpoint=self.endpoint)
        return ok

    def embed(self, texts, kind="document") -> list[list[float]]:
        prefix = "search_query: " if kind == "query" else "search_document: "
        out = []
        for i in range(0, len(texts), self.batch):
            chunk = [prefix + t[:2000] for t in texts[i:i + self.batch]]
            try:
                resp = httpx.post(f"{self.endpoint}/api/embed", json={"model": self.model, "input": chunk},
                                  timeout=self.timeout_s)
                resp.raise_for_status()
                out += resp.json()["embeddings"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                _AVAIL.update(at=time.monotonic(), ok=False, endpoint=self.endpoint)
                raise EmbedUnavailable(str(exc)) from exc
        return out


def set_embedder(embedder):
    """Tests and offline dev: any object with .model, .available() and .embed(texts, kind)."""
    global _EMBEDDER
    _EMBEDDER = embedder


def embedder():
    return _EMBEDDER or OllamaEmbedder()


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# ---- what gets indexed -------------------------------------------------------------

def _collect(conn) -> list[tuple]:
    """(entity_type, entity_id, call_id, deal_id, text) for everything worth recalling."""
    items = []
    for r in conn.execute("SELECT id, call_id, deal_id, subject, statement, evidence_quote FROM claims"):
        quote = f' "{r["evidence_quote"]}"' if r["evidence_quote"] else ""
        items.append(("claim", str(r["id"]), r["call_id"], r["deal_id"], f"{r['subject']}: {r['statement']}{quote}"))
    for r in conn.execute("SELECT o.id, o.call_id, c.deal_id, o.tag, o.polarity, o.evidence_quote "
                          "FROM seller_observations o LEFT JOIN calls c ON c.node_id=o.call_id"):
        items.append(("observation", str(r["id"]), r["call_id"], r["deal_id"],
                      f"{r['tag'].replace('_', ' ')} ({r['polarity']}): \"{r['evidence_quote'] or ''}\""))
    seen = set()
    for r in conn.execute("SELECT a.call_id, a.json, c.deal_id FROM artifacts a LEFT JOIN calls c "
                          "ON c.node_id=a.call_id WHERE a.kind='analysis' ORDER BY a.id DESC"):
        if r["call_id"] in seen:
            continue
        seen.add(r["call_id"])
        try:
            insight = (json.loads(r["json"]) or {}).get("coaching_insight") or {}
        except ValueError:
            continue
        if insight.get("insight"):
            items.append(("insight", r["call_id"], r["call_id"], r["deal_id"],
                          f"{insight['insight']} Practise: {insight.get('practice_next_call', '')}"))
    size = int(_cfg().get("window_turns", 3))
    calls = [c["node_id"] for c in conn.execute("SELECT node_id FROM calls")]
    deals = {c["node_id"]: c["deal_id"] for c in conn.execute("SELECT node_id, deal_id FROM calls")}
    for cid in calls:
        turns = conn.execute("SELECT idx, channel, text, quality FROM turns WHERE call_id=? AND tier='final' "
                             "ORDER BY idx", (cid,)).fetchall()
        for i in range(0, len(turns), size):
            window = turns[i:i + size]
            if not window or all(t["quality"] == "garbled" for t in window):
                continue
            text = "\n".join(f"{'ME' if t['channel'] == 'me' else 'THEM'}: {t['text']}" for t in window)
            items.append(("window", f"{cid}:{window[0]['idx']}", cid, deals.get(cid), text))
    return items


def index_pending(conn, emb=None) -> dict:
    """Embed anything new or changed, drop vectors whose source is gone. Commits."""
    emb = emb or embedder()
    items = _collect(conn)
    existing = {(r["entity_type"], r["entity_id"]): r["text_sha"] for r in conn.execute(
        "SELECT entity_type, entity_id, text_sha FROM embeddings WHERE model=?", (emb.model,))}
    wanted = {(t, e) for t, e, *_ in items}
    stale = [k for k in existing if k not in wanted]
    for t, e in stale:
        conn.execute("DELETE FROM embeddings WHERE entity_type=? AND entity_id=? AND model=?", (t, e, emb.model))
    conn.commit()     # never hold the write lock across the HTTP calls below
    todo = [i for i in items if existing.get((i[0], i[1])) != _sha(i[4])]
    result = {"status": "ok", "indexed": 0, "pruned": len(stale), "total": len(items), "model": emb.model}
    if todo and not emb.available():
        conn.commit()
        return {**result, "status": "unavailable"}
    try:
        vectors = emb.embed([i[4] for i in todo]) if todo else []
    except EmbedUnavailable as exc:
        conn.commit()
        return {**result, "status": "unavailable", "error": str(exc)[:200]}
    stamp = now()
    for (etype, eid, call_id, deal_id, text), vec in zip(todo, vectors):
        arr = np.asarray(vec, dtype=np.float32)
        conn.execute(
            "INSERT INTO embeddings(entity_type,entity_id,call_id,deal_id,text,text_sha,model,dim,vector,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,model) DO UPDATE SET "
            "call_id=excluded.call_id, deal_id=excluded.deal_id, text=excluded.text, text_sha=excluded.text_sha, "
            "dim=excluded.dim, vector=excluded.vector, created_at=excluded.created_at",
            (etype, eid, call_id, deal_id, text, _sha(text), emb.model, int(arr.shape[0]), arr.tobytes(), stamp))
    conn.commit()
    return {**result, "indexed": len(todo)}


def similar(conn, text, k=5, entity_types=None, exclude_calls=(), emb=None) -> list[dict]:
    """The k stored items closest to `text` by cosine similarity. [] when nothing is indexed or Ollama is down."""
    emb = emb or embedder()
    sql = "SELECT entity_type, entity_id, call_id, deal_id, text, dim, vector FROM embeddings WHERE model=?"
    params = [emb.model]
    if entity_types:
        sql += f" AND entity_type IN ({','.join('?' * len(entity_types))})"
        params += list(entity_types)
    rows = [r for r in conn.execute(sql, params).fetchall() if r["call_id"] not in set(exclude_calls)]
    if not rows or not (text or "").strip():
        return []
    try:
        query = np.asarray(emb.embed([text], kind="query")[0], dtype=np.float32)
    except EmbedUnavailable:
        return []
    rows = [r for r in rows if r["dim"] == query.shape[0]]
    if not rows:
        return []
    matrix = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    denom = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query) or 1.0)
    scores = (matrix @ query) / np.where(denom == 0, 1.0, denom)
    order = np.argsort(-scores)[:k]
    return [{"entity_type": rows[i]["entity_type"], "entity_id": rows[i]["entity_id"], "call_id": rows[i]["call_id"],
             "deal_id": rows[i]["deal_id"], "text": rows[i]["text"], "score": round(float(scores[i]), 4)}
            for i in order]


def stats(conn) -> dict:
    return {r["entity_type"]: r["n"] for r in conn.execute(
        "SELECT entity_type, COUNT(*) AS n FROM embeddings GROUP BY entity_type")}
