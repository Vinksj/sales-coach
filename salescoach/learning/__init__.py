"""Learning layer (design §7, phase F1): outcomes, a general pattern store, and
the "what the coach believes" page.

  outcomes.py   deal outcome edits (through the memory gate, with history) and
                derived_outcomes, recomputed from facts in the store
  observe.py    turns stored facts into pattern_observations, one family at a time
  voice.py      deterministic draft-vs-final heuristics for the email_voice family
  patterns.py   counts observations into learned_patterns, promotion rules,
                proposals, user actions, and for_prompt (what a prompt may see)
  feedback.py   phase F2: the blocks of learned patterns each prompt receives, the live
                coach's bounded weight boost, and the "Used in" read for the page
  weekly.py     digest(conn, since): "what changed" over the last N days, counted in code
  web.py        /learning and the deal-page outcome fragment

Nothing in this package calls a model. What reaches a prompt is a rule, a tag name and a
coarse count: never text from a call or an email.
"""
from .. import config
from ..memory import gate

DEAL_COLUMNS = {"value": "REAL", "currency": "TEXT", "lost_reason": "TEXT"}
PATTERN_GATED = ("user_state", "merged_into", "no_prompt")

DEFAULTS = {
    "promotion": {
        "window_calls": 10,
        "emerging": {"min_calls": 3, "min_deals": 2, "min_window_share": 0.30},
        "established": {"min_calls": 6, "min_deals": 3, "both_halves": True},
        "dormant_after_calls": 10, "retire_after_calls": 20,
    },
    "email_voice": {"active_at_edits": 3, "shorten_ratio": 0.8, "lengthen_ratio": 1.25, "min_draft_words": 25,
                    "filler_phrases": {}, "signoffs": ["thanks", "best", "regards"], "closing_lines": []},
    "followup": {"reply_business_days": 5, "meeting_within_days": 10, "rate_min_n": 20,
                 "gap_buckets": [[0, 2], [3, 5], [6, 10], [11, None]]},
    "nudge_trigger": {"propose_min_shown": 15, "unhelpful_share": 0.6, "lower_factor": 0.8, "weight_floor": 0.3},
    "observation_only": {"min_deals": 8, "min_per_bucket": 3},
    "persona_buckets": {},
    "objection": {"claim_subjects": {}},
    "tag_hygiene": {"min_similarity": 0.5, "stop_words": []},
    "deal": {"stages": [], "lost_reasons": {"other": "Other"}},
    "prompt_targets": {"prep": ["seller"], "live_coach": ["seller"], "email_drafter": ["email_voice"],
                       "nudge_drafter": ["email_voice"], "strategist": ["persona", "objection"]},
    # Phase F2. live_triggers (seller tag -> live trigger) ships in config/learning.yaml only: a default
    # here could not be removed by the user, because dicts merge key by key.
    "feedback": {"prep": True, "email_drafter": True, "nudge_drafter": True, "strategist": True,
                 "live_coach": {"enabled": True, "boost": 0.1}, "max_patterns": 3,
                 "count_buckets": [1, 2, 3, 6, 10, 20, 50, 100], "live_triggers": {}},
    "proposals": {"repropose_factor": 2},
}


def _merge(base, extra):
    out = dict(base or {})
    for key, value in (extra or {}).items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def cfg(section: str | None = None) -> dict:
    """config/learning.yaml over the code defaults (so a missing file or key never breaks a recompute)."""
    data = _merge(DEFAULTS, config.load("learning"))
    return data if section is None else (data.get(section) or {})


PROPOSALS_DDL = """CREATE TABLE learning_proposals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK(kind IN ('merge','trigger_weight')),
  subject     TEXT NOT NULL,
  pattern_id  TEXT,
  target_id   TEXT,
  summary     TEXT NOT NULL,
  payload     TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','dismissed')),
  applied     TEXT,
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  decided_n   INTEGER
)"""
PROPOSAL_COLUMNS = "id,kind,subject,pattern_id,target_id,summary,payload,status,applied,created_at,resolved_at"


def ensure_columns(conn) -> None:
    """Idempotent. deals gets value, currency, lost_reason (close_target already exists);
    learned_patterns gets no_prompt; a learning_proposals table made by phase F1 (one row per subject,
    for ever) is rebuilt so a decided proposal stays as history and a later one can be opened."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    for col, decl in DEAL_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE deals ADD COLUMN {col} {decl}")
    have = {r[1] for r in conn.execute("PRAGMA table_info(learned_patterns)")}
    if have and "no_prompt" not in have:
        conn.execute("ALTER TABLE learned_patterns ADD COLUMN no_prompt INTEGER NOT NULL DEFAULT 0")
    have = {r[1] for r in conn.execute("PRAGMA table_info(learning_proposals)")}
    if have and "decided_n" not in have:
        _rebuild_proposals(conn)


def _rebuild_proposals(conn) -> None:
    """F1's UNIQUE(kind, subject) cannot be dropped in place. Copy the rows into the new shape inside a
    savepoint, so a failure leaves the old table exactly as it was."""
    conn.execute("SAVEPOINT learning_proposals_f2")
    try:
        conn.execute("ALTER TABLE learning_proposals RENAME TO learning_proposals_f1")
        conn.execute(PROPOSALS_DDL)
        conn.execute(f"INSERT INTO learning_proposals({PROPOSAL_COLUMNS}) SELECT {PROPOSAL_COLUMNS} "
                     "FROM learning_proposals_f1 ORDER BY id")
        conn.execute("DROP TABLE learning_proposals_f1")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lprop_open ON learning_proposals(kind, subject) "
                     "WHERE status='open'")
        conn.execute("RELEASE learning_proposals_f2")
    except Exception:
        conn.execute("ROLLBACK TO learning_proposals_f2")
        conn.execute("RELEASE learning_proposals_f2")
        raise


def register_gate() -> None:
    """Put the outcome fields and the user's verdicts on patterns behind the memory gate."""
    key, fields = gate.GATED["deals"]
    gate.register_table("deals", key, set(fields) | set(DEAL_COLUMNS))
    gate.register_table("learned_patterns", "id", PATTERN_GATED)


def digest(conn, since) -> dict:
    """What changed since `since` (weekly.py): a structured dict whose "text" is the plain rendering."""
    from . import weekly
    return weekly.digest(conn, since)


def seller_id(conn) -> str:
    row = conn.execute("SELECT node_id FROM people WHERE is_me=1 ORDER BY node_id LIMIT 1").fetchone()
    return row[0] if row else "me"


register_gate()
