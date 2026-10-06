"""Unit tests for the rules engine (decide + post_check)."""
from __future__ import annotations

from replydesk.config import Settings
from replydesk.models import (
    Analysis,
    EmailMessage,
    ProductMatch,
    ProductMatches,
    Reply,
)
from replydesk.rules import SenderHistory, decide, post_check


def _settings():
    return Settings(
        mode="dry_run",
        imap_user="support@example.com",
        smtp_user="support@example.com",
        openai_api_key="x",
        allowed_domains="example.com",
        max_replies_per_sender=3,
        min_confidence=0.7,
    )


def _analysis(**overrides) -> Analysis:
    base = {
        "language": "en",
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "urgency": "normal",
        "intent": "tech_support",
        "products_mentioned": ["Widget Pro"],
        "summary": "",
        "confidence": 0.9,
    }
    base.update(overrides)
    return Analysis(**base)


def _email(**overrides) -> EmailMessage:
    base = {
        "message_id": "<t@example.com>",
        "subject": "s",
        "from_addr": "c@example.com",
        "body_plain": "b",
    }
    base.update(overrides)
    return EmailMessage(**base)


def _matches(*catalog_ids: str, has_unknown: bool = False) -> ProductMatches:
    ms = [ProductMatch(raw="x", catalog_id=cid if not has_unknown else None,
                      catalog_name=cid, score=99) for cid in catalog_ids]
    if has_unknown:
        ms.append(ProductMatch(raw="Unknown", catalog_id=None, score=40))
    return ProductMatches(matches=ms)


def _history(n=0):
    return SenderHistory(replies_to_sender_today=n)


def test_decide_ignore_automated():
    s = _settings()
    em = _email(is_automated=True)
    assert decide(em, _analysis(), _matches("widget_pro"), s, _history()) == "ignore"


def test_decide_ignore_self():
    s = _settings()
    em = _email(from_self=True)
    assert decide(em, _analysis(), _matches("widget_pro"), s, _history()) == "ignore"


def test_decide_ignore_spam():
    s = _settings()
    assert decide(_email(), _analysis(intent="spam"), _matches(), s, _history()) == "ignore"


def test_decide_escalate_angry_urgent():
    s = _settings()
    em = _email()
    a = _analysis(sentiment="very_negative", sentiment_score=-0.8, urgency="high")
    assert decide(em, a, _matches("widget_pro"), s, _history()) == "escalate"


def test_decide_escalate_needs_both_negative_and_urgent():
    s = _settings()
    em = _email()
    # Negative but low urgency -> not escalate
    a = _analysis(sentiment="very_negative", sentiment_score=-0.8, urgency="low")
    assert decide(em, a, _matches("widget_pro"), s, _history()) == "send"
    # High urgency but neutral sentiment -> not escalate
    a2 = _analysis(urgency="high", sentiment="neutral", sentiment_score=0.0)
    assert decide(em, a2, _matches("widget_pro"), s, _history()) == "send"


def test_decide_draft_low_confidence():
    s = _settings()
    a = _analysis(confidence=0.5)
    assert decide(_email(), a, _matches("widget_pro"), s, _history()) == "draft"


def test_decide_draft_refund():
    s = _settings()
    a = _analysis(intent="refund")
    assert decide(_email(), a, _matches("widget_pro"), s, _history()) == "draft"


def test_decide_draft_unknown_product():
    s = _settings()
    assert decide(_email(), _analysis(), _matches(has_unknown=True), s, _history()) == "draft"


def test_decide_draft_rate_limit():
    s = _settings()
    assert decide(_email(), _analysis(), _matches("widget_pro"), s, _history(n=3)) == "draft"


def test_decide_send_default():
    s = _settings()
    assert decide(_email(), _analysis(), _matches("widget_pro"), s, _history()) == "send"


# -- post_check -----------------------------------------------------------

def test_post_check_clean_reply_passes():
    s = _settings()
    r = Reply(subject="Re: Hello", body="Hi there, please reset your Widget Pro.", needs_human=False)
    assert post_check(r, s, catalog_text="Widget Pro ships within 2 days") is None


def test_post_check_needs_human():
    s = _settings()
    r = Reply(subject="Re: Hello", body="x", needs_human=True)
    assert post_check(r, s, "x") == "needs_human"


def test_post_check_unauthorized_url():
    s = _settings()
    r = Reply(subject="Re: Hello", body="See http://scam.bad/now", needs_human=False)
    assert post_check(r, s, "") is not None  # any reason


def test_post_check_allowed_url():
    s = _settings()
    r = Reply(subject="Re: Hello", body="See https://www.example.com/help", needs_human=False)
    assert post_check(r, s, "") is None


def test_post_check_money_outside_catalog():
    s = _settings()
    r = Reply(subject="Re: Hello", body="We will refund you $999.", needs_human=False)
    assert post_check(r, s, "") is not None


def test_post_check_money_in_catalog():
    s = _settings()
    r = Reply(subject="Re: Hello", body="Standard price is $99.", needs_human=False)
    assert post_check(r, s, "Standard price is $99.") is None
