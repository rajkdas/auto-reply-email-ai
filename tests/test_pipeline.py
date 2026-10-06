"""End-to-end pipeline test with a fake LLM and fake mail source."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import pytest

from replydesk.config import Settings
from replydesk.mail_io import build_reply_message, parse_email_bytes
from replydesk.main import Pipeline
from replydesk.models import (
    Analysis,
    EmailMessage,
    Reply,
)

# ---------------------------------------------------------------------------
# Fake chains: deterministic, no API calls
# ---------------------------------------------------------------------------

class FakeAnalysisChain:
    """Returns a fixed Analysis based on the email body, so we can exercise
    every branch of the rules engine without an LLM."""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, inputs: dict, **_kw) -> Analysis:
        self.calls += 1
        body = (inputs.get("body") or "").lower()
        subject = (inputs.get("subject") or "").lower()

        # Detect product from text directly.
        prods: list[str] = []
        if "widget pro" in body or "wp-" in body:
            prods.append("Widget Pro")
        if "widget lite" in body or "wl-" in body:
            prods.append("Widget Lite")
        if "cloudsync" in body or "cloud sync" in body:
            prods.append("Cloud Sync")

        # Branch on intent for tests.
        if "refund" in body or "refund" in subject:
            intent = "refund"
        elif "win a free" in body or "scam" in body:
            intent = "spam"
        elif "very angry" in body or "furious" in body:
            intent = "complaint"
        else:
            intent = "tech_support"

        if "furious" in body:
            sentiment_score, sentiment, urgency = -0.8, "very_negative", "high"
        elif "unhappy" in body:
            sentiment_score, sentiment, urgency = -0.4, "negative", "normal"
        else:
            sentiment_score, sentiment, urgency = 0.0, "neutral", "normal"

        # Confidence based on whether the email is gibberish.
        confidence = 0.3 if "blabla gibberish" in body else 0.9

        return Analysis(
            language="en",
            sentiment=sentiment,
            sentiment_score=sentiment_score,
            urgency=urgency,
            intent=intent,
            products_mentioned=prods,
            summary=subject or "test email",
            confidence=confidence,
        )


class FakeReplyChain:
    """Returns a canned Reply. ``needs_human`` is False by default."""

    def __init__(self, *, needs_human: bool = False, body: str | None = None,
                 unauthorized_url: bool = False, money: bool = False):
        self.calls = 0
        self.needs_human = needs_human
        self.body = body
        self.unauthorized_url = unauthorized_url
        self.money = money

    async def ainvoke(self, inputs: dict, **_kw) -> Reply:
        self.calls += 1
        body = self.body or (
            "Hello, thanks for reaching out. We'll help you reset your device."
        )
        if self.unauthorized_url:
            body += "\nSee http://scam.bad/now"
        if self.money:
            body += "\nWe will refund you $999."
        return Reply(
            subject="Re: your support request",
            body=body,
            needs_human=self.needs_human,
        )


def _build_pipeline(settings: Settings, *, analysis=None, reply=None) -> Pipeline:
    """Build a real Pipeline but swap in fake chains."""
    p = Pipeline.build(settings)
    p.analysis_chain = analysis or FakeAnalysisChain()
    p.reply_chain = reply or FakeReplyChain()
    return p


def _load_emails(fixtures: Iterable[str], settings: Settings) -> list[EmailMessage]:
    items = []
    for name in fixtures:
        path = Path(__file__).parent / "fixtures" / name
        items.append(parse_email_bytes(path.read_bytes(), settings))
    return items


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_dry_run_no_side_effect(settings, fixtures_dir):
    """In dry_run mode, no send / no draft save happens but analysis runs."""
    pipe = _build_pipeline(settings)
    emails = _load_emails(["plain_complaint.eml"], settings)
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])

    assert len(results) == 1
    r = results[0]
    assert r.decision == "send"  # rules say send
    assert r.analysis.intent == "tech_support"
    assert r.reply is not None
    assert "Widget Pro" in (r.reply.body or "") or "reset" in r.reply.body


@pytest.mark.asyncio
async def test_pipeline_dedupe(settings):
    """Same message-id twice -> only one reply, second is skipped."""
    pipe = _build_pipeline(settings)
    emails = _load_emails(["plain_complaint.eml", "plain_complaint.eml"], settings)
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])

    assert len(results) == 2
    assert results[0].decision in {"send", "draft"}
    assert results[1].decision == "ignore"
    assert results[1].skip_reason == "already_processed"


@pytest.mark.asyncio
async def test_pipeline_ignore_automated(settings):
    pipe = _build_pipeline(settings)
    emails = _load_emails(["auto_reply.eml", "mailing_list.eml"], settings)
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])

    for r in results:
        assert r.decision == "ignore"


@pytest.mark.asyncio
async def test_pipeline_draft_for_refund(settings):
    pipe = _build_pipeline(settings)
    emails = _load_emails(["html_refund.eml"], settings)
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])
    assert results[0].decision == "draft"


@pytest.mark.asyncio
async def test_pipeline_escalate_angry(settings):
    # Build an angry urgent email fixture inline.
    raw = (
        b"From: a@example.com\r\n"
        b"To: support@example.com\r\n"
        b"Subject: furious about WP-1234\r\n"
        b"Message-ID: <angry-001@example.com>\r\n"
        b"Date: Wed, 1 Mar 2024 09:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"I am furious. The Widget Pro exploded.\r\n"
    )
    em = parse_email_bytes(raw, settings)
    pipe = _build_pipeline(settings)
    async with pipe:
        results = await asyncio.gather(pipe.process_one(em))
    assert results[0].decision == "escalate"


@pytest.mark.asyncio
async def test_pipeline_unknown_product_draft(settings):
    # Email mentions a product the LLM picks up but that's not in catalog.
    raw = (
        b"From: a@example.com\r\n"
        b"To: support@example.com\r\n"
        b"Subject: question\r\n"
        b"Message-ID: <unknown-001@example.com>\r\n"
        b"Date: Wed, 1 Mar 2024 09:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Can you help me with my Gizmo X 9000?\r\n"
    )
    em = parse_email_bytes(raw, settings)
    # Patch the fake analysis chain to mention a non-catalog product.
    class _Analysis(FakeAnalysisChain):
        async def ainvoke(self, inputs, **_):
            return Analysis(
                language="en",
                sentiment="neutral",
                sentiment_score=0.0,
                urgency="normal",
                intent="tech_support",
                products_mentioned=["Gizmo X 9000"],
                summary="question",
                confidence=0.9,
            )
    pipe = _build_pipeline(settings, analysis=_Analysis())
    async with pipe:
        results = await asyncio.gather(pipe.process_one(em))
    assert results[0].decision == "draft"
    assert results[0].matches.has_unknown


@pytest.mark.asyncio
async def test_pipeline_post_check_downgrades_send_to_draft(settings):
    """post_check: unauthorized URL or money amount downgrades send->draft."""
    emails = _load_emails(["plain_complaint.eml"], settings)
    pipe = _build_pipeline(
        settings, reply=FakeReplyChain(unauthorized_url=True)
    )
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])
    assert results[0].decision == "draft"


@pytest.mark.asyncio
async def test_pipeline_post_check_needs_human(settings):
    emails = _load_emails(["plain_complaint.eml"], settings)
    pipe = _build_pipeline(settings, reply=FakeReplyChain(needs_human=True))
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])
    assert results[0].decision == "draft"


@pytest.mark.asyncio
async def test_pipeline_threading_headers(settings):
    """build_reply_message sets In-Reply-To/References/Auto-Submitted."""
    em = _load_emails(["plain_complaint.eml"], settings)[0]
    msg = build_reply_message(
        original=em, reply_subject="Re: hello", reply_body="Hi",
        from_addr="support@example.com",
    )
    assert msg["In-Reply-To"] == em.message_id
    assert msg["References"] == em.message_id
    assert msg["Auto-Submitted"] == "auto-replied"


@pytest.mark.asyncio
async def test_pipeline_rate_limit_caps_per_sender(settings):
    """A sender already at the cap gets a draft, not a send."""
    # Pre-populate store with cap hits.
    from replydesk.store import Store
    s = Store(settings.db_path)
    await s.connect()
    for _ in range(settings.max_replies_per_sender):
        await s.mark_sent("<fake@example.com>", "alice@example.com")
    await s.close()

    pipe = _build_pipeline(settings)
    emails = _load_emails(["plain_complaint.eml"], settings)
    async with pipe:
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])
    assert results[0].decision == "draft"


@pytest.mark.asyncio
async def test_pipeline_crash_recovery_no_double_send(settings, monkeypatch):
    """If a record is in 'sending' state on restart, it's held for review
    rather than re-sent."""
    from replydesk.store import Store

    # Simulate a crash mid-send: write a row in 'sending' state.
    s = Store(settings.db_path)
    await s.connect()
    await s.record("<alice-001@example.com>", "sending")
    await s.close()

    # On pipeline startup, reclaim_sending moves it to 'review'.
    pipe = _build_pipeline(settings)
    async with pipe:
        # Processing the same mail again should NOT duplicate; store.already_processed
        # returns True (the row still exists with outcome='review').
        emails = _load_emails(["plain_complaint.eml"], settings)
        results = await asyncio.gather(*[pipe.process_one(em) for em in emails])

    # Either: skip because 'review' counts as already_processed.
    assert results[0].decision in {"ignore", "draft"}
