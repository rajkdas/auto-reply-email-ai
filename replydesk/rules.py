"""Rules engine: one readable ``decide()`` function + post-check.

The decision tree is intentionally explicit so every rule is grep-able and
auditable. Thresholds come from settings so they can be tuned without code
changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import Settings
from .models import (
    Analysis,
    Decision,
    EmailMessage,
    ProductMatches,
    Reply,
)

log = logging.getLogger(__name__)


# Heuristic patterns for the post-check on generated replies.
_URL_RE = re.compile(r"https?://([A-Za-z0-9\-_.]+)")
_MONEY_RE = re.compile(
    r"(?:[$€£¥]\s?\d[\d.,]*|\d[\d.,]*\s?(?:USD|EUR|GBP|JPY))", re.IGNORECASE
)


@dataclass
class SenderHistory:
    """How many replies have been sent to this sender in the last 24h."""

    replies_to_sender_today: int = 0


def decide(
    email: EmailMessage,
    analysis: Analysis,
    matches: ProductMatches,
    settings: Settings,
    history: SenderHistory,
) -> Decision:
    """Return one of ``send``, ``draft``, ``escalate``, ``ignore``.

    The order matters: more specific / safer rules first, default to SEND.
    """
    if email.is_automated or email.from_self:
        return "ignore"

    if analysis.intent == "spam":
        return "ignore"

    if analysis.sentiment_score <= -0.6 and analysis.urgency == "high":
        return "escalate"

    if analysis.confidence < settings.min_confidence:
        return "draft"

    if analysis.intent == "refund":
        return "draft"

    if matches.has_unknown:
        return "draft"

    if history.replies_to_sender_today >= settings.max_replies_per_sender:
        return "draft"

    return "send"


def post_check(
    reply: Reply,
    settings: Settings,
    catalog_text: str,
) -> str | None:
    """Downgrade a SEND to DRAFT when the reply is suspicious.

    Returns a reason string when the reply should be downgraded to DRAFT,
    or ``None`` when it's safe to send.
    """
    text = f"{reply.subject}\n{reply.body}"

    if reply.needs_human:
        return "needs_human"

    # URLs must be inside the allowed-domain allowlist.
    allowed = {d.lower().lstrip(".") for d in settings.allowed_domain_list}
    for host in _URL_RE.findall(text):
        host_lc = host.lower()
        if not any(host_lc == d or host_lc.endswith("." + d) for d in allowed):
            return f"unauthorized_url:{host}"

    # Money amounts must appear verbatim in the catalog/FAQ text (allowlist).
    if catalog_text:
        catalog_lc = catalog_text.lower()
        for amount in _MONEY_RE.findall(text):
            if amount.lower() not in catalog_lc:
                return f"unauthorized_amount:{amount.strip()}"
    else:
        # No catalog context: any money mention is suspicious.
        if _MONEY_RE.search(text):
            return "money_mentioned_without_catalog"

    return None


__all__ = ["decide", "post_check", "SenderHistory"]
