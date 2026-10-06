"""Pydantic models shared across the pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

Sentiment = Literal[
    "very_negative", "negative", "neutral", "positive", "very_positive"
]
Urgency = Literal["low", "normal", "high"]
Intent = Literal[
    "complaint",
    "refund",
    "order_status",
    "tech_support",
    "pricing",
    "feedback",
    "spam",
    "other",
]


class Analysis(BaseModel):
    """Output of the analysis LLM call (one per email)."""

    language: str = Field(
        description="ISO 639-1 language code, e.g. 'en', 'de', 'hi'."
    )
    sentiment: Sentiment
    sentiment_score: float = Field(
        ge=-1.0, le=1.0, description="Signed sentiment, -1.0 to 1.0."
    )
    urgency: Urgency
    intent: Intent
    products_mentioned: list[str] = Field(
        default_factory=list,
        description="Product names as written by the customer (raw strings).",
    )
    summary: str = Field(description="One-sentence summary of the email.")
    confidence: float = Field(ge=0.0, le=1.0)


class Reply(BaseModel):
    """Output of the reply LLM call (one per email)."""

    subject: str
    body: str
    needs_human: bool = Field(
        description="True when the model is unsure and a human should review."
    )


class ProductMatch(BaseModel):
    """A single product detected in the email, after fuzzy matching to the
    catalog. ``catalog_id`` is None when no catalog entry is close enough."""

    raw: str
    catalog_id: str | None = None
    catalog_name: str | None = None
    score: float = 0.0  # 0-100 from rapidfuzz


class ProductMatches(BaseModel):
    """Bundle of product detections for one email."""

    matches: list[ProductMatch] = Field(default_factory=list)

    @property
    def has_unknown(self) -> bool:
        return any(m.catalog_id is None for m in self.matches)

    @property
    def catalog_ids(self) -> list[str]:
        return [m.catalog_id for m in self.matches if m.catalog_id]


# Outcome of the rules engine. Stored in the audit log.
Decision = Literal["send", "draft", "escalate", "ignore"]


class EmailMessage(BaseModel):
    """Parsed/cleaned email ready for the pipeline."""

    message_id: str
    subject: str
    from_addr: str
    from_name: str | None = None
    to_addr: str = ""
    date: datetime | None = None
    body_plain: str = ""  # cleaned, truncated, ready for the LLM
    raw_headers: dict[str, str] = Field(default_factory=dict)
    is_automated: bool = False  # detected auto-reply / mailing list / DSN
    from_self: bool = False

    @property
    def sender_safe(self) -> str:
        """Masked sender for logs: 'a***@example.com'."""
        addr = self.from_addr or ""
        if "@" in addr:
            local, _, domain = addr.partition("@")
            if len(local) > 2:
                local = local[0] + "*" * (len(local) - 2) + local[-1]
            return f"{local}@{domain}"
        return addr


class PipelineResult(BaseModel):
    """Audit-log record for a single email processed end-to-end."""

    message_id: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decision: Decision
    analysis: Analysis | None = None
    matches: ProductMatches | None = None
    reply: Reply | None = None
    skip_reason: str | None = None
    error: str | None = None


def utcnow() -> datetime:
    """UTC now, tz-aware. Tiny helper used across modules."""
    return datetime.now(timezone.utc)
