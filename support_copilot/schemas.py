"""HTTP inference contract. Training records require an explicit future adapter."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Language = Literal["ru", "kk", "uz", "en", "es", "pt", "und"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Message(ContractModel):
    role: Literal["player", "reported_player"]
    text: Text
    attachments: int = Field(default=0, ge=0)


class TriageRequest(ContractModel):
    kind: Literal["complaint", "report"]
    messages: list[Message] = Field(min_length=1, max_length=100)


class Extracted(ContractModel):
    nickname: str | None = None
    rule_refs: list[str] = Field(default_factory=list)
    has_evidence: bool = False
    server: str | None = None


class DraftReply(ContractModel):
    lang: Language
    text: str
    cited: list[str] = Field(default_factory=list)


class TriageResponse(ContractModel):
    lang: Language
    category: str
    priority: Literal["p0", "p1", "p2", "p3"]
    is_spam_or_duplicate: bool
    extracted: Extracted
    draft_reply: DraftReply
    needs_human: bool
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
