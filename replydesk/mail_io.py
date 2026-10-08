"""Mail I/O: fetch (IMAP), parse/clean, send (SMTP), draft (IMAP APPEND).

All network ops are async. Parsing/cleaning uses stdlib ``email`` plus
``beautifulsoup4`` for HTML-to-text fallback. Quote/signature stripping is
intentionally conservative — we'd rather keep a line too many than lose a
customer's actual question.
"""

from __future__ import annotations

import contextlib
import email
import email.utils
import logging
import re
from collections.abc import AsyncIterator, Iterable
from email.message import Message
from email.mime.text import MIMEText
from email.policy import default as default_policy
from typing import Any

import aioimaplib
import aiosmtplib
from bs4 import BeautifulSoup

from .config import Settings
from .models import EmailMessage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing / cleaning
# ---------------------------------------------------------------------------

# Patterns that mark the start of a quoted reply block from common clients.
_QUOTE_PATTERNS = [
    re.compile(r"^\s*On .+ wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s.+$"),  # Outlook header block
    re.compile(r"^>+"),  # "> " quoted lines
    re.compile(r"^\s*Am .+ schrieb.+:?\s*$", re.IGNORECASE),  # de
    re.compile(r"^\s*El .+ escribi[oó]:?\s*$", re.IGNORECASE),  # es
    re.compile(r"^\s*Le .+ a [ée]crit\s*:?\s*$", re.IGNORECASE),  # fr
]

# Common signature markers; cut the rest of the body when we hit one.
_SIGNATURE_MARKERS = [
    re.compile(r"^\s*--\s*$"),
    re.compile(r"^\s*__+\s*$"),
    re.compile(r"^\s*Regards,?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Best regards,?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Best,?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Thanks & regards", re.IGNORECASE),
    re.compile(r"^\s*Sent from my iPhone", re.IGNORECASE),
    re.compile(r"^\s*Sent from my Galaxy", re.IGNORECASE),
]

_NOREPLY_LOCALPARTS = {
    "noreply", "no-reply", "no.reply", "do-not-reply", "donotreply",
    "mailer-daemon", "mailerdaemon", "postmaster", "auto-reply",
    "autoreply", "bounce", "bounces", "notification",
}

_AUTOMATED_HEADERS = ("auto-submitted", "precedence", "list-unsubscribe",
                      "x-auto-response-suppress")


def _is_automated(msg: Message, own_addr: str) -> tuple[bool, str]:
    """Return (skip?, reason) for auto-replies / mailing lists / DSNs."""
    own_addr = (own_addr or "").lower()

    from_addr = (email.utils.parseaddr(msg.get("From", ""))[1] or "").lower()
    if own_addr and from_addr == own_addr:
        return True, "from_self"

    auto_submitted = (msg.get("Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return True, f"auto_submitted={auto_submitted}"

    precedence = (msg.get("Precedence") or "").lower()
    if precedence in {"bulk", "list", "junk"}:
        return True, f"precedence={precedence}"

    if msg.get("List-Unsubscribe") is not None:
        return True, "mailing_list"

    if msg.get("X-Failed-Recipients") is not None:
        return True, "bounce"

    # Content-Type: report/disposition-notification etc.
    ct = (msg.get_content_type() or "")
    if ct.startswith("multipart/report") or "delivery-status" in ct:
        return True, "dsv_report"

    local = from_addr.split("@", 1)[0]
    if local in _NOREPLY_LOCALPARTS:
        return True, "noreply_sender"

    return False, ""


def _strip_quotes_and_signatures(text: str) -> str:
    """Drop quoted replies and trailing signature blocks from plain text."""
    out_lines: list[str] = []
    for line in text.splitlines():
        if any(p.match(line) for p in _QUOTE_PATTERNS):
            break
        if any(p.match(line) for p in _SIGNATURE_MARKERS):
            break
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(["p", "div", "li"]):
        blk.append("\n")
    text = soup.get_text(separator=" ", strip=False)
    # Collapse runs of spaces but keep newlines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _best_text_part(msg: Message) -> str:
    """Pick the plain text body, falling back to HTML→text."""
    plain = None
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and plain is None:
                plain = _decode_part(part)
            elif ctype == "text/html" and html is None:
                html = _decode_part(part)
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            plain = _decode_part(msg)
        elif ctype == "text/html":
            html = _decode_part(msg)

    if plain and plain.strip():
        return plain
    if html:
        return _html_to_text(html)
    return ""


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def parse_email_bytes(raw: bytes, settings: Settings) -> EmailMessage:
    """Parse raw email bytes into a cleaned :class:`EmailMessage`."""
    msg = email.message_from_bytes(raw, policy=default_policy)

    own_addr = (settings.imap_user or "").lower()
    is_automated, skip_reason = _is_automated(msg, own_addr)

    from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
    to_name, to_addr = email.utils.parseaddr(msg.get("To", ""))
    subject = msg.get("Subject", "") or ""
    message_id = msg.get("Message-ID", "").strip()
    if not message_id:
        # Fall back to a synthetic id so dedupe still works.
        message_id = f"synthetic:{hash(raw) & 0xFFFFFFFF:x}@replydesk.local"

    date_hdr = msg.get("Date")
    date_dt = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else None

    body = _best_text_part(msg)
    body = _strip_quotes_and_signatures(body)
    if len(body) > settings.max_body_chars:
        body = body[: settings.max_body_chars] + "\n…[truncated]"

    raw_headers = {
        k: v
        for k, v in msg.items()
        if k.lower() in _AUTOMATED_HEADERS or k.lower() in {"message-id", "references", "in-reply-to"}
    }

    return EmailMessage(
        message_id=message_id,
        subject=subject,
        from_addr=from_addr,
        from_name=from_name or None,
        to_addr=to_addr,
        date=date_dt,
        body_plain=body,
        raw_headers=raw_headers,
        is_automated=is_automated,
        from_self=bool(own_addr and from_addr.lower() == own_addr),
    )


# ---------------------------------------------------------------------------
# IMAP fetch
# ---------------------------------------------------------------------------

async def _connect_imap(settings: Settings) -> aioimaplib.IMAP4_SSL:
    """Connect + login + select INBOX. Raises on failure.

    ``timeout`` is aioimaplib's *per-command* timeout (default 10s). Gmail
    logins over slow/IPv6 links can exceed that, so we raise it to 60s.
    NOTE: aioimaplib passes this straight into ``asyncio.wait_for``, which
    requires ``float`` - an ``int`` raises TypeError there, hence ``60.0``.
    """
    client = aioimaplib.IMAP4_SSL(
        host=settings.imap_host,
        port=settings.imap_port,
        timeout=60.0,
    )
    await client.wait_hello_from_server()
    await client.login(settings.imap_user, settings.imap_password)
    await client.select(settings.imap_mailbox)
    return client


async def fetch_unseen(settings: Settings) -> AsyncIterator[EmailMessage]:
    """Yield each unseen email, marking them as seen only after the caller
    is done with them (the caller must call :func:`mark_seen`).

    IMPORTANT: all IMAP commands here are UID-based (``UID SEARCH`` /
    ``UID FETCH`` / ``UID STORE``). Plain ``SEARCH``/``FETCH`` return and
    interpret *sequence numbers*, while ``STORE`` in :func:`mark_seen` uses
    UIDs - mixing the two flags the wrong messages and makes dedupe/UID
    bookkeeping unreliable.
    """
    client = await _connect_imap(settings)
    try:
        # Search for UNSEEN, fetch bodies without marking as seen (PEEK).
        # BODY.PEEK[] never sets \Seen, so emails stay UNSEEN until the
        # pipeline finishes and explicitly calls mark_seen().
        #
        # NOTE: aioimaplib's uid() helper only supports FETCH/STORE/COPY/MOVE/
        # EXPUNGE - calling uid("SEARCH", ...) raises Abort. Use the dedicated
        # uid_search() API instead, which sends a real "UID SEARCH". The
        # results are UIDs (safe across re-syncs), unlike plain
        # sequence-number SEARCH.
        response = await client.uid_search("UNSEEN")
        # aioimaplib's ``Response`` is a namedtuple with fields
        # ``result`` ("OK"/"NO"/"BAD") and ``lines`` (untagged + tagged
        # response byte-strings). It has NO ``status`` or ``data``
        # attributes - reading those raises AttributeError. Verified against
        # aioimaplib 2.0.1: Response._fields == ('result', 'lines').
        typ, data = response.result, response.lines
        if typ != "OK":
            log.warning("IMAP UID SEARCH UNSEEN failed: %s", typ)
            return
        uids = _parse_uid_list(data)
        log.info("found %d unseen message(s) in %r", len(uids), settings.imap_mailbox)

        # Process NEWEST FIRST: IMAP UIDs increase monotonically within a
        # mailbox (UIDVALIDITY is stable for Gmail INBOX), so sorting by
        # descending UID gives us the most recent messages at the front.
        # This matters especially with MAX_EMAILS - you want the cap to
        # apply to fresh mail, not years-old backlog like UID 20404.
        uids.sort(key=lambda u: int(u), reverse=True)

        # Testing cap: process at most ``max_emails`` per run (0 = all).
        if settings.max_emails and len(uids) > settings.max_emails:
            log.info(
                "limiting this run to the newest %d of %d unseen message(s) "
                "(MAX_EMAILS=%d); the rest stay UNSEEN for the next run",
                settings.max_emails, len(uids), settings.max_emails,
            )
            uids = uids[: settings.max_emails]
        for uid in uids:
            # aioimaplib has no ``uid_fetch()`` method; the generic ``uid()``
            # dispatcher supports FETCH/STORE/COPY/MOVE/EXPUNGE and returns
            # the same Response namedtuple as every other command.
            fetch = await client.uid("FETCH", uid, "(BODY.PEEK[])")
            typ, msg_data = fetch.result, fetch.lines
            if typ != "OK":
                log.warning("IMAP UID FETCH failed uid=%s: %s", uid, typ)
                continue

            raw = _extract_body_bytes(msg_data)
            if raw is None:
                log.warning("no body bytes in FETCH response uid=%s", uid)
                continue
            try:
                em = parse_email_bytes(raw, settings)
                em.raw_headers["x-imap-uid"] = uid
                yield em
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Failed to parse UID=%s: %s", uid, exc)
    finally:
        with contextlib.suppress(Exception):
            await client.logout()


async def mark_seen(settings: Settings, uid: str) -> None:
    """Mark a single message as seen by UID."""
    client = await _connect_imap(settings)
    try:
        await client.uid("STORE", uid, "+FLAGS.SILENT", r"\Seen")
    finally:
        with contextlib.suppress(Exception):
            await client.logout()


async def append_draft(settings: Settings, reply_email: Message) -> None:
    """Append a draft reply to the IMAP Drafts folder.

    aioimaplib's real signature is
    ``append(message_bytes: bytes, mailbox: str = 'INBOX', flags: str = None,
    date: Any = None)`` - message bytes FIRST, and ``flags`` is a plain
    string that gets wrapped in parentheses (a list would be mangled into
    ``"('\\Draft')"``). Passing ``("Drafts", raw, ...)`` raised
    AttributeError/protocol errors at runtime.
    """
    client = await _connect_imap(settings)
    try:
        raw = reply_email.as_bytes()
        resp = await client.append(raw, "Drafts", flags=r"\Draft")
        if resp.result != "OK":
            raise RuntimeError(f"IMAP APPEND to Drafts failed: {resp.result}")
    finally:
        with contextlib.suppress(Exception):
            await client.logout()


def _parse_uid_list(data: list) -> list[str]:
    """Extract UID numbers from an untagged SEARCH/ESEARCH response line.

    Handles both classic ``b"SEARCH 101 102"`` and ESEARCH
    ``b"ESEARCH (UIDS 5:7 20)"`` formats, expanding colon ranges.
    """
    import re

    for item in data:
        if isinstance(item, bytes):
            txt = item.decode("ascii", errors="ignore").strip()
            if not txt:
                continue
            uids: list[str] = []
            for token in txt.split():
                bare = token.strip("()")
                m = re.fullmatch(r"(\d+):(?:(\d+))?", bare)
                if m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else start
                    uids.extend(str(u) for u in range(start, end + 1))
                elif bare.isdigit():
                    uids.append(bare)
            if uids:
                return uids
    return []


def _extract_body_bytes(resp_lines: list) -> bytes | None:
    """Pull the raw RFC822 bytes out of a UID FETCH response.

    ``aioimaplib`` returns ``Response(result, lines)`` where ``lines`` is a
    list of *separate* byte strings - it does NOT bundle the literal into a
    ``(meta, body)`` tuple like imaplib does. For a Gmail ``UID FETCH n
    (BODY.PEEK[])`` the lines look like::

        [b'* 3499 FETCH (UID 23913 BODY[] {7303}',   # header + literal size
         b'<...7303 bytes of raw RFC822 mail...>',    # the literal itself
         b'OEDE4 OK Success']                          # tagged completion

    Strategy (in order):
    1. Anchor on the ``N FETCH (... {size})`` marker line and return the
       following bytes entry whose length matches ``size`` (literal data is
       appended as ONE whole entry by aioimaplib's FetchCommand; we tolerate
       ±2 bytes of CRLF stripping and re-accumulate split chunks too).
    2. Heuristic fallback: any blob that starts with an RFC822 ``Name:``
       header AND contains a blank line ("\\r\\n\\r\\n") is a full message.
    3. Last resort: the largest candidate blob - never silently skip a
       message because of an unexpected response shape.
    """
    anchor_re = re.compile(
        rb"\d+\s+FETCH\s*\(.*?\{(\d+)\}", re.IGNORECASE | re.DOTALL
    )
    header_re = re.compile(rb"^[A-Za-z][A-Za-z0-9_-]*:")
    blank_line_re = re.compile(rb"\r?\n\r?\n")

    def _is_message_blob(b: bytes) -> bool:
        return bool(header_re.search(b[:200])) and bool(blank_line_re.search(b))

    # --- Pass 1: literal-size anchored extraction -------------------------
    best: bytes | None = None
    pending_size: int | None = None
    accumulated = bytearray()
    for item in resp_lines:
        if not isinstance(item, bytes):
            continue
        if pending_size is None:
            m = anchor_re.search(item)
            if m:
                pending_size = int(m.group(1))
                accumulated = bytearray()
            continue
        # We are expecting literal bytes after a {size} marker.
        accumulated.extend(item)
        if len(accumulated) >= pending_size:
            body = bytes(accumulated[:pending_size])
            if best is None or len(body) > len(best):
                best = body
            pending_size = None
            accumulated = bytearray()
    if best is not None:
        return best

    # --- Pass 2: header + blank-line heuristic ----------------------------
    candidates = [item for item in resp_lines
                  if isinstance(item, bytes) and _is_message_blob(item)]
    if candidates:
        return max(candidates, key=len)

    # --- Pass 3: legacy imaplib-style (meta, body) tuples ------------------
    for item in resp_lines:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            return item[1]

    # --- Pass 4: last resort - biggest blob that isn't the tagged status ---
    blobs = [item for item in resp_lines
             if isinstance(item, bytes) and len(item) > 50
             and not anchor_re.search(item)
             and not re.match(rb"^\w{4}\d+ (OK|NO|BAD)", item)]
    if blobs:
        return max(blobs, key=len)
    return None


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def build_reply_message(
    *,
    original: EmailMessage,
    reply_subject: str,
    reply_body: str,
    from_addr: str,
) -> Message:
    """Build an outbound email with correct threading headers."""
    if reply_subject and not reply_subject.lower().startswith("re:"):
        subject = f"Re: {original.subject}"
    else:
        subject = reply_subject or f"Re: {original.subject}"

    msg = MIMEText(reply_body, _charset="utf-8")
    msg["From"] = from_addr
    msg["To"] = original.from_addr
    msg["Subject"] = subject
    if original.message_id:
        msg["In-Reply-To"] = original.message_id
        msg["References"] = original.message_id
    # Loop protection on our own replies.
    msg["Auto-Submitted"] = "auto-replied"
    return msg


async def send_reply(settings: Settings, msg: Message) -> None:
    """Send ``msg`` over SMTP using the configured TLS mode.

    aiosmtplib semantics (easy to get wrong):
    - ``use_tls=True``   -> implicit TLS from the first byte (SMTPS, port 465)
    - ``start_tls=True`` -> upgrade an existing plain connection (port 587)

    Previously this mapped "ssl" -> use_tls and *everything else* ->
    start_tls, so a misconfigured/empty SMTP_SECURITY silently forced
    STARTTLS against an SSL-only port (and vice versa), making sends hang
    or fail. Now the mode is explicit and defaults to starttls.
    """
    security = getattr(settings.smtp_security, "value", str(settings.smtp_security))
    kwargs: dict[str, Any] = {
        "hostname": settings.smtp_host,
        "port": settings.smtp_port,
        "username": settings.smtp_user,
        "password": settings.smtp_password,
        "timeout": 30,
    }
    if security == "ssl":
        kwargs["use_tls"] = True
    elif security == "starttls":
        kwargs["start_tls"] = True
    else:  # pragma: no cover - enum should prevent this
        raise ValueError(f"unknown SMTP_SECURITY: {security!r}")
    await aiosmtplib.send(msg, **kwargs)


# ---------------------------------------------------------------------------
# In-memory fake mail source (used by tests and `replay` CLI)
# ---------------------------------------------------------------------------

class FakeMailSource:
    """Async iterator yielding parsed :class:`EmailMessage` objects from a
    pre-populated list. Used by tests and the `replay` CLI command."""

    def __init__(self, items: Iterable[EmailMessage]):
        self._items = list(items)

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self) -> EmailMessage:
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


__all__ = [
    "parse_email_bytes",
    "fetch_unseen",
    "mark_seen",
    "append_draft",
    "build_reply_message",
    "send_reply",
    "FakeMailSource",
]
