"""Weekday/date agreement. Models get weekdays wrong ("Monday Sep 15" when 15 Sep
2026 is a Tuesday; found in a real strategist run), and a wrong day
in something the seller sends or repeats to a buyer costs credibility.

Both orders are recognised: "Tuesday 15 Sep", "Tue, 15th September",
"Monday Sep 15". A date without a year is the next occurrence from `today`.
"""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_WORDS = {d: i for i, d in enumerate(_DAYS)}
_DAY_WORDS.update({"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4,
                   "sat": 5, "sun": 6})
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                            "oct", "nov", "dec"])}
_WORD = r"[A-Za-z]{3,9}"
# The match must START on a weekday word. With any word allowed there, "free Monday 15 Sep" matched
# as "free Monday 15" (rejected, not a weekday) and consumed "Monday", so the real date was never checked.
_DAY_ALT = "|".join(sorted(_DAY_WORDS, key=len, reverse=True))
_PATTERN = re.compile(rf"\b({_DAY_ALT})\.?,?\s+(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({_WORD})|({_WORD})\.?\s+"
                      rf"(\d{{1,2}})(?:st|nd|rd|th)?)\b", re.IGNORECASE)


def _month(word: str):
    word = word.lower()
    if word == "sept":
        return 9
    return _MONTHS.get(word[:3]) if len(word) == 3 or word[:3] in _MONTHS and word.isalpha() else None


def today_ist() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def weekday_mismatches(text: str, today=None) -> list[str]:
    base = date.fromisoformat(today) if isinstance(today, str) else (today or today_ist())
    notes = []
    for m in _PATTERN.finditer(text or ""):
        day_index = _DAY_WORDS.get(m.group(1).lower())
        if day_index is None:
            continue
        month_word, dom = (m.group(3), m.group(2)) if m.group(2) else (m.group(4), m.group(5))
        month = _month(month_word)
        if not month:
            continue
        try:
            when = date(base.year + (1 if month < base.month else 0), month, int(dom))
        except ValueError:
            continue
        if when.weekday() != day_index:
            notes.append(f"says '{m.group(0)}', but {when.isoformat()} is a {_DAYS[when.weekday()].capitalize()}")
    return notes
