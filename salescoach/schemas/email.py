from pydantic import Field

from .common import Strict


class EmailDraft(Strict):
    to: list[str] = Field(description="Recipient emails, chosen ONLY from the allowed recipients list")
    cc: list[str]
    subject: str
    body: str = Field(description="Plain text body including sign-off; use [SLOTS] where meeting times must go")
    rationale: str = Field(description="Why this structure and content, in one or two sentences")
