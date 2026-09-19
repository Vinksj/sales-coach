"""Bleed detection: the call's audio leaking into the 'me' channel.

Voice processing in callcap removes most of it, but on laptop speakers some
of the other side still reaches the mic. Whisper then transcribes the same
sentence twice, once per channel, and the analyst would credit the seller with
the prospect's words. A 'me' turn is flagged when its text near-duplicates a
'them' turn that overlaps it within +/- window_s.

Short turns ("yeah", "okay") are never flagged: both sides say them at the
same moment all the time, and a near-identical "okay" is not evidence of
bleed.
"""
import re
from difflib import SequenceMatcher

WINDOW_S = 2.0
RATIO = 0.8
MIN_WORDS = 3
_PUNCT = re.compile(r"[^\w\s]")


def _norm(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", (text or "").lower()).split())


def _get(turn, key, default=None):
    try:
        value = turn[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _near(me, them, window_s: float) -> bool:
    m0, m1 = _get(me, "t_start", 0.0), _get(me, "t_end", _get(me, "t_start", 0.0))
    t0, t1 = _get(them, "t_start", 0.0), _get(them, "t_end", _get(them, "t_start", 0.0))
    return m0 <= t1 + window_s and m1 >= t0 - window_s


def is_bleed(me_text: str, them_text: str, ratio: float = RATIO, min_words: int = MIN_WORDS) -> bool:
    a, b = _norm(me_text), _norm(them_text)
    if len(a.split()) < min_words or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= ratio:
        return True
    # a fragment of a longer 'them' turn (Whisper cut the channels differently)
    return len(a.split()) >= min_words + 1 and f" {a} " in f" {b} "


def flag_bleed(turns, window_s: float = WINDOW_S, ratio: float = RATIO) -> list[int]:
    """Return list positions of 'me' turns that duplicate a nearby 'them' turn.

    Mutable turns (dicts) also get bleed_flag set to 1 in place; for sqlite
    Rows use the returned positions."""
    them = [t for t in turns if _get(t, "channel") == "them"]
    flagged = []
    for pos, turn in enumerate(turns):
        if _get(turn, "channel") != "me":
            continue
        for other in them:
            if _near(turn, other, window_s) and is_bleed(_get(turn, "text", ""), _get(other, "text", ""), ratio):
                flagged.append(pos)
                if isinstance(turn, dict):
                    turn["bleed_flag"] = 1
                break
    return flagged


def flag_bleed_call(conn, call_id: str, tier: str = "final") -> int:
    """Recompute bleed flags for stored turns. Does not commit."""
    rows = conn.execute("SELECT * FROM turns WHERE call_id=? AND tier=? ORDER BY idx",
                        (call_id, tier)).fetchall()
    flagged = {rows[i]["idx"] for i in flag_bleed(rows)}
    for row in rows:
        want = 1 if row["idx"] in flagged else 0
        if row["bleed_flag"] != want:
            conn.execute("UPDATE turns SET bleed_flag=? WHERE call_id=? AND tier=? AND idx=?",
                         (want, call_id, tier, row["idx"]))
    return len(flagged)
