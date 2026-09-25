"""The org's compliance settings (config/org.yaml; org_settings in cloud mode): retention and the recording
consent notice. Read through config.load, so a change made in Settings is seen by every process at once."""
from typing import Optional

from .. import config, identity

MAX_NOTICE = 600
MAX_DAYS = 36500


def org() -> dict:
    return config.load("org") or {}


def retention_days() -> Optional[int]:
    """The retention period in days, or None (keep for ever). A value that is not a positive integer is
    None: a typo must never delete anything."""
    raw = (org().get("retention") or {}).get("days")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        days = int(str(raw).strip())
    except ValueError:
        return None
    return days if 0 < days <= MAX_DAYS else None


def retention_batch() -> int:
    try:
        return max(1, min(1000, int((org().get("retention") or {}).get("batch") or 100)))
    except (TypeError, ValueError):
        return 100


def retention_interval_s() -> float:
    try:
        hours = float((org().get("retention") or {}).get("interval_hours") or 24)
    except (TypeError, ValueError):
        hours = 24.0
    return max(0.25, hours) * 3600


def consent_notice() -> str:
    """The org's recording-consent text, shown to reps in a cloud install; '' on a local install (which keeps
    its own reminder beside Start recording) or when the admin has set none."""
    if not identity.cloud():
        return ""
    return str((org().get("consent") or {}).get("notice") or "").strip()[:MAX_NOTICE]


def save(retention_days_value, notice: str) -> dict:
    """Settings' form: validate and store both (admin-only in cloud: org_settings is an admin's to write).
    Returns what was stored. Raises ValueError with a message for the admin."""
    text = (notice or "").strip()
    if len(text) > MAX_NOTICE:
        raise ValueError(f"The consent notice is limited to {MAX_NOTICE} characters.")
    raw = "" if retention_days_value is None else str(retention_days_value).strip()
    days = None
    if raw:
        if not raw.isdigit() or not 0 < int(raw) <= MAX_DAYS:
            raise ValueError(f"Retention is a whole number of days between 1 and {MAX_DAYS}, or empty to keep everything.")
        days = int(raw)
    data = dict(config.load_user("org") or {})          # the saved overlay only (the row, in cloud mode)
    data["retention"] = {**(data.get("retention") or {}), "days": days}
    data["consent"] = {**(data.get("consent") or {}), "notice": text}
    config.save_user("org", data)
    return {"retention_days": days, "consent_notice": text}
