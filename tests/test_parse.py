"""Unit tests for email parsing & quote/signature stripping."""
from __future__ import annotations

from pathlib import Path

from replydesk.config import Settings
from replydesk.mail_io import parse_email_bytes


def _parse(path: Path, settings: Settings):
    raw = path.read_bytes()
    return parse_email_bytes(raw, settings)


def test_plain_email(settings, fixtures_dir):
    em = _parse(fixtures_dir / "plain_complaint.eml", settings)
    assert em.message_id == "<alice-001@example.com>"
    assert em.from_addr == "alice@example.com"
    assert em.subject == "Widget Pro not turning on"
    assert "Widget Pro" in em.body_plain
    assert "WP-1234" in em.body_plain
    assert em.is_automated is False
    assert em.from_self is False


def test_html_email(settings, fixtures_dir):
    em = _parse(fixtures_dir / "html_refund.eml", settings)
    assert em.from_addr == "bob@example.com"
    assert "refund" in em.body_plain.lower()
    assert "WL-5567" in em.body_plain
    # HTML tags must be stripped.
    assert "<p>" not in em.body_plain
    assert "<html>" not in em.body_plain


def test_multilingual_email(settings, fixtures_dir):
    em = _parse(fixtures_dir / "spanish_question.eml", settings)
    assert "Widget Pro" in em.body_plain
    assert "envíe" in em.body_plain or "tarda" in em.body_plain


def test_auto_reply_detected(settings, fixtures_dir):
    em = _parse(fixtures_dir / "auto_reply.eml", settings)
    assert em.is_automated is True
    assert em.raw_headers.get("Auto-Submitted") == "auto-replied"


def test_mailing_list_detected(settings, fixtures_dir):
    em = _parse(fixtures_dir / "mailing_list.eml", settings)
    assert em.is_automated is True
    # Precedence is "list" -> should be skipped
    assert em.is_automated


def test_quoted_reply_stripped(settings, fixtures_dir):
    em = _parse(fixtures_dir / "quoted_reply.eml", settings)
    # The customer's own question is kept; the quoted support reply is dropped.
    assert "expected delivery date" in em.body_plain
    assert "Tracking number" not in em.body_plain
    assert "Your order has shipped" not in em.body_plain


def test_self_address_skipped(settings, fixtures_dir):
    # Build an email where from == imap_user.
    raw = (
        b"From: support@example.com\r\n"
        b"To: alice@example.com\r\n"
        b"Subject: Test\r\n"
        b"Message-ID: <self-001@example.com>\r\n"
        b"Date: Wed, 1 Mar 2024 09:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Hello\r\n"
    )
    em = parse_email_bytes(raw, settings)
    assert em.from_self is True


def test_long_body_truncated(settings):
    long_body = "x" * 10_000
    raw = (
        f"From: alice@example.com\r\n"
        f"To: support@example.com\r\n"
        f"Subject: Long\r\n"
        f"Message-ID: <long-001@example.com>\r\n"
        f"Date: Wed, 1 Mar 2024 09:00:00 +0000\r\n"
        f"Content-Type: text/plain\r\n\r\n"
        f"{long_body}\r\n"
    ).encode()
    em = parse_email_bytes(raw, settings)
    assert len(em.body_plain) <= settings.max_body_chars + 30  # +truncation note
    assert "[truncated]" in em.body_plain


def test_synthetic_message_id_when_missing(settings):
    raw = (
        b"From: alice@example.com\r\n"
        b"To: support@example.com\r\n"
        b"Subject: No id\r\n"
        b"Date: Wed, 1 Mar 2024 09:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Hello\r\n"
    )
    em = parse_email_bytes(raw, settings)
    assert em.message_id.startswith("synthetic:")


# ---------------------------------------------------------------------------
# IMAP response parsing regressions (shapes taken from real Gmail DEBUG logs)
# ---------------------------------------------------------------------------

from replydesk.mail_io import _extract_body_bytes, _parse_uid_list


def test_parse_uid_list_classic_search():
    lines = [b"* SEARCH 20404 20405 23913", b"OEDE3 OK SEARCH completed (Success)"]
    assert _parse_uid_list(lines) == ["20404", "20405", "23913"]


def test_parse_uid_list_eshow_range():
    lines = [b"* ESEARCH (TAG x) UIDLIST 5:7,20"]
    # comma form is not expanded; colon ranges are
    assert "6" in _parse_uid_list([b"* ESEARCH (UIDS 5:7 20)"])


def test_extract_gmail_literal_shape():
    """aioimaplib stores each literal as ONE whole bytes entry in .lines."""
    body = b"Delivered-To: me@gmail.com\r\nSubject: hi\r\n\r\nplain text body\r\n"
    lines = [
        b"* 3499 FETCH (UID 23913 BODY[] {%d}" % len(body),
        body,
        b"OEDE4 OK Success",
    ]
    out = _extract_body_bytes(lines)
    assert out is not None
    assert out.startswith(b"Delivered-To:")
    assert b"plain text body" in out


def test_extract_tolerates_trailing_crlf():
    body = b"From: a@b.c\r\n\r\nhello\r\n"
    declared = len(body) - 2  # server counts without final CRLF sometimes
    lines = [
        b"* 1 FETCH (UID 5 BODY[] {%d}" % declared,
        body,
        b"TAG OK Success",
    ]
    out = _extract_body_bytes(lines)
    assert out is not None and out.startswith(b"From:")


def test_extract_falls_back_to_heuristic():
    body = b"Message-ID: <x@y>\r\nSubject: s\r\n\r\nbody here\r\n"
    lines = [body]  # no anchor line at all
    out = _extract_body_bytes(lines)
    assert out == body


def test_extract_handles_bytearray_literal():
    """Regression: aioimaplib appends the FETCH literal as a *bytearray*
    (Command.append_literal_data stores _resp_literal_data directly).
    Filtering entries with ``isinstance(item, bytes)`` dropped it and the
    pipeline logged 'no body bytes in FETCH response' for every real email.
    """
    body = bytearray(
        b"Delivered-To: me@gmail.com\r\nFrom: a@b.c\r\nSubject: hi\r\n\r\n"
        b"Hi, your product widget pro is not working.\r\n"
    )
    lines = [
        b"* 3499 FETCH (UID 23913 BODY[] {%d}" % len(body),
        body,          # <-- bytearray, exactly what aioimaplib yields
        b")",          # closing FETCH paren (separate entry)
        b"Success",    # tagged text AFTER the tag (_response_done strips it)
    ]
    out = _extract_body_bytes(lines)
    assert out is not None, "bytearray literal must not be silently dropped"
    assert out.startswith(b"Delivered-To:")
    assert b"widget pro is not working" in out
    assert len(out) == len(body)
    assert not out.endswith(b")")  # trailing paren stays outside the slice


def test_extract_joins_split_marker_and_status():
    """Even if the literal were split across entries, anchored extraction
    reassembles it from the joined stream."""
    body = (
        b"From: a@b.c\r\nSubject: s\r\n\r\n" + b"x" * 300 + b"\r\n"
    )
    lines = [
        b"* 7 FETCH (UID 99 BODY[] {%d}" % len(body),
        body[:150],
        body[150:],
        b")",
        b"OK Success",
    ]
    out = _extract_body_bytes(lines)
    assert out == body


def test_extract_none_on_empty():
    assert _extract_body_bytes([]) is None
    assert _extract_body_bytes([b"TAG OK Success"]) is None
