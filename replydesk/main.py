"""Pipeline orchestration: run_once / run_forever / CLI.

Design goals (kept simple per the plan):
- One bounded ``asyncio.Queue`` + N worker tasks for concurrency.
- LLM calls inside each worker are sequential per email; ``max_concurrency``
  is enforced via the worker count when calling ``abatch`` in batch mode
  (we use sequential per-worker ainvoke for clarity).
- ``dry_run`` mode never sends and never saves a draft; ``draft_only``
  appends to IMAP Drafts; ``live`` sends over SMTP.
- ``replay <dir>`` runs the full pipeline over a directory of ``.eml``
  fixtures without touching IMAP/SMTP - useful for tests and Colab demos.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aioimaplib

from .chains import (
    _fake_llm_enabled,
    analysis_inputs,
    build_analysis_chain,
    build_reply_chain,
    reply_inputs,
)
from .config import Mode, Settings, get_settings
from .mail_io import (
    FakeMailSource,
    append_draft,
    build_reply_message,
    fetch_unseen,
    mark_seen,
    parse_email_bytes,
    send_reply,
)
from .models import (
    Analysis,
    Decision,
    EmailMessage,
    PipelineResult,
    ProductMatches,
    Reply,
)
from .products import ProductCatalog
from .rules import SenderHistory, decide, post_check
from .store import Store

log = logging.getLogger("replydesk")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@dataclass
class Pipeline:
    """Holds everything a worker needs. Built once at startup."""

    settings: Settings
    catalog: ProductCatalog
    store: Store
    analysis_chain: Any
    reply_chain: Any

    @classmethod
    def build(cls, settings: Settings) -> Pipeline:
        catalog = ProductCatalog.from_yaml(
            settings.products_file, threshold=settings.fuzzy_match_threshold
        )
        store = Store(str(settings.db_path_resolved))
        analysis_chain = build_analysis_chain(settings)
        reply_chain = build_reply_chain(settings)
        return cls(
            settings=settings,
            catalog=catalog,
            store=store,
            analysis_chain=analysis_chain,
            reply_chain=reply_chain,
        )

    async def __aenter__(self) -> Pipeline:
        await self.store.connect()
        reclaimed = await self.store.reclaim_sending()
        if reclaimed:
            log.warning("Recovered %d unsent messages (marked for review).", reclaimed)
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.store.close()

    # -- per-email processing --------------------------------------------
    async def process_one(self, email: EmailMessage) -> PipelineResult:
        """Process one email end-to-end and return the audit record."""
        # 0) Atomic claim: dedupe + concurrent-worker guard.
        #    If another worker has already claimed this message_id (e.g. the
        #    same mail was delivered twice and fetched twice), we skip.
        try:
            claimed = await self.store.claim(email.message_id)
        except Exception as exc:
            log.error("claim failed id=%s: %s", email.message_id, exc)
            return PipelineResult(
                message_id=email.message_id,
                decision="ignore",
                error=str(exc),
            )
        if not claimed:
            log.info(
                "skip dedupe id=%s sender=%s (already processed)",
                email.message_id, email.sender_safe,
            )
            return PipelineResult(
                message_id=email.message_id,
                decision="ignore",
                skip_reason="already_processed",
            )

        # 1) analyze
        try:
            analysis: Analysis = await self.analysis_chain.ainvoke(
                analysis_inputs(email)
            )
        except Exception as exc:
            log.error("analysis failed id=%s: %s", email.message_id, exc)
            await self.store.record(email.message_id, "ignore", skip_reason=None)
            return PipelineResult(
                message_id=email.message_id,
                decision="ignore",
                error=str(exc),
            )

        # 2) product detection
        matches: ProductMatches = self.catalog.detect(email, analysis)

        # 3) decision (rules engine)
        history = SenderHistory(
            replies_to_sender_today=await self.store.count_replies_today(
                email.from_addr
            )
        )
        decision: Decision = decide(email, analysis, matches, self.settings, history)

        log.info(
            "process id=%s sender=%s sentiment=%s urgency=%s intent=%s "
            "products=%s decision=%s",
            email.message_id, email.sender_safe,
            analysis.sentiment, analysis.urgency, analysis.intent,
            [m.catalog_id or m.raw for m in matches.matches],
            decision,
        )

        # 4) early-out paths that don't need a reply
        if decision in {"ignore", "escalate"}:
            await self.store.record(
                email.message_id,
                "escalate" if decision == "escalate" else "ignore",
                analysis=analysis,
            )
            return PipelineResult(
                message_id=email.message_id,
                decision=decision,
                analysis=analysis,
                matches=matches,
            )

        # 5) generate reply
        catalog_context = self.catalog.faq_for(matches.catalog_ids)
        try:
            reply: Reply = await self.reply_chain.ainvoke(
                reply_inputs(
                    email=email,
                    analysis=analysis,
                    catalog_context=catalog_context,
                    allowed_domains=self.settings.allowed_domains,
                )
            )
        except Exception as exc:
            log.error("reply failed id=%s: %s", email.message_id, exc)
            await self.store.record(email.message_id, "draft", analysis=analysis)
            return PipelineResult(
                message_id=email.message_id,
                decision="draft",
                analysis=analysis,
                matches=matches,
                error=str(exc),
            )

        # 6) post-check (downgrade SEND -> DRAFT if suspicious).
        #    Controlled by POST_CHECK_ENABLED (default true). Set it to false
        #    in .env to stop downgrades and let "send" decisions go straight
        #    out over SMTP in live mode.
        if decision == "send" and self.settings.post_check_enabled:
            reason = post_check(reply, self.settings, catalog_context)
            if reason is not None:
                log.info(
                    "post_check downgraded id=%s reason=%s "
                    "(set POST_CHECK_ENABLED=false to disable)",
                    email.message_id, reason,
                )
                decision = "draft"
        elif decision == "send":
            log.debug("post_check disabled; keeping decision=send id=%s",
                      email.message_id)

        # 7) persist "sending" before any side effect (crash recovery)
        if decision == "send" and self.settings.mode == Mode.live:
            await self.store.record(
                email.message_id, "sending", analysis=analysis, reply=reply
            )

        # 8) dispatch based on mode
        await self._dispatch(decision, email, reply)

        # 9) record final state
        if decision == "send" and self.settings.mode == Mode.live:
            await self.store.mark_sent(email.message_id, email.from_addr)
        else:
            outcome = decision if self.settings.mode != Mode.dry_run else f"dry_{decision}"
            await self.store.record(
                email.message_id, outcome, analysis=analysis, reply=reply
            )

        return PipelineResult(
            message_id=email.message_id,
            decision=decision,
            analysis=analysis,
            matches=matches,
            reply=reply,
        )

    async def _dispatch(self, decision: Decision, email: EmailMessage, reply: Reply) -> None:
        """Actually send / save draft / log, based on mode."""
        if decision == "send" and self.settings.mode == Mode.live:
            smtp_user = self.settings.smtp_user or self.settings.imap_user
            from_addr = (
                f"{self.settings.smtp_from_name} <{smtp_user}>"
                if self.settings.smtp_from_name
                else smtp_user
            )
            msg = build_reply_message(
                original=email,
                reply_subject=reply.subject,
                reply_body=reply.body,
                from_addr=from_addr,
            )
            try:
                await send_reply(self.settings, msg)
                log.info("sent id=%s to=%s", email.message_id, email.sender_safe)
            except Exception as exc:
                log.error("smtp send failed id=%s: %s", email.message_id, exc)
                # leave 'sending' in store -> will be reclaimed as 'review'
                raise

        elif decision == "draft" and self.settings.mode != Mode.dry_run:
            # draft_only mode, OR a live-mode send that post_check downgraded
            # to draft - previously this fell through to the no-op branch and
            # the generated reply was silently discarded ("no side effect").
            smtp_user = self.settings.smtp_user or self.settings.imap_user
            from_addr = (
                f"{self.settings.smtp_from_name} <{smtp_user}>"
                if self.settings.smtp_from_name
                else smtp_user
            )
            msg = build_reply_message(
                original=email,
                reply_subject=reply.subject,
                reply_body=reply.body,
                from_addr=from_addr,
            )
            try:
                await append_draft(self.settings, msg)
                log.info("draft saved id=%s", email.message_id)
            except Exception as exc:
                log.error("imap draft append failed id=%s: %s", email.message_id, exc)

        else:
            # dry_run, ignore, or escalate -> nothing to send / save
            log.info("decision=%s mode=%s id=%s (no side effect)",
                     decision, self.settings.mode, email.message_id)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

async def _imap_source(settings: Settings) -> AsyncIterator[EmailMessage]:
    """Yield unseen emails from IMAP. Mark each as seen only after we've
    finished processing it (the caller does that)."""
    async for em in fetch_unseen(settings):
        yield em


def _replay_source(dir_or_files: str | list[str], settings: Settings) -> FakeMailSource:
    """Build a FakeMailSource from a directory of .eml files or a list."""
    files: list[Path] = []
    if isinstance(dir_or_files, str):
        p = Path(dir_or_files)
        if p.is_dir():
            files = sorted(p.glob("*.eml"))
        elif p.is_file():
            files = [p]
        else:
            raise FileNotFoundError(p)
    else:
        files = [Path(f) for f in dir_or_files]

    items: list[EmailMessage] = []
    for f in files:
        with f.open("rb") as fh:
            items.append(parse_email_bytes(fh.read(), settings))
    return FakeMailSource(items)


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

async def run_once(
    settings: Settings | None = None,
    *,
    source: AsyncIterator[EmailMessage] | None = None,
    pipeline: Pipeline | None = None,
) -> list[PipelineResult]:
    """Process current batch of emails once and return.

    ``source`` lets tests / ``replay`` inject a fake mail source. If not
    given, the real IMAP source is used.
    """
    settings = settings or get_settings()
    own_pipeline = pipeline is None
    if own_pipeline:
        pipeline = Pipeline.build(settings)
        await pipeline.__aenter__()
    assert pipeline is not None

    results: list[PipelineResult] = []
    try:
        src = source or _imap_source(settings)
        async for em in src:
            try:
                res = await pipeline.process_one(em)
            except Exception as exc:
                log.error("pipeline error id=%s: %s", em.message_id, exc)
                res = PipelineResult(
                    message_id=em.message_id,
                    decision="ignore",
                    error=str(exc),
                )
            results.append(res)
            # Mark the message as seen on IMAP only after we've committed
            # to a final outcome, so a crash mid-processing re-fetches it.
            # NOTE: this runs in *every* mode (including dry_run). If we
            # skipped marking in dry_run, every email would be re-fetched
            # forever and - combined with DB dedupe - just logged as
            # "already_processed", which looks exactly like a blank,
            # non-functional run.
            uid = em.raw_headers.get("x-imap-uid")
            if uid:
                try:
                    await mark_seen(settings, uid)
                except Exception as exc:  # pragma: no cover - best effort
                    log.warning("mark_seen failed uid=%s: %s", uid, exc)
    finally:
        if own_pipeline:
            await pipeline.__aexit__(None, None, None)
    return results


async def run_forever(settings: Settings | None = None) -> None:
    """Poll the mailbox forever, with graceful shutdown on SIGINT/SIGTERM."""
    settings = settings or get_settings()
    stop = asyncio.Event()

    def _set_stop(*_: Any) -> None:
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    # add_signal_handler isn't available on Windows; fall back gracefully.
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: _set_stop())  # pragma: no cover

    async with Pipeline.build(settings) as pipeline:
        log.info(
            "ReplyDesk running mode=%s poll=%ss workers=%d",
            settings.mode, settings.poll_seconds, settings.workers,
        )
        while not stop.is_set():
            try:
                await run_once(settings, source=_imap_source(settings), pipeline=pipeline)
            except aioimaplib.IMAP4.error as exc:
                # Most common cause: bad IMAP_HOST/credentials or blocked login.
                log.exception("IMAP error during poll (check IMAP_* in .env): %s", exc)
            except OSError as exc:
                # DNS failure / connection refused (e.g. placeholder host).
                log.exception("Network error during poll (check IMAP_HOST/PORT): %s", exc)
            except Exception as exc:
                log.exception("run_once iteration failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_seconds)
            except asyncio.TimeoutError:
                continue
        log.info("ReplyDesk stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def _validate_settings(settings: Settings) -> list[str]:
    """Return a list of human-readable configuration problems.

    Empty list == config looks good. This exists because every previous
    failure mode was silent: IMAP login errors were swallowed by
    ``run_forever``'s try/except, so the user saw a blank screen with no
    error even though nothing could ever be fetched or sent.
    """
    problems: list[str] = []

    if not settings.imap_host or "example.com" in settings.imap_host:
        problems.append("IMAP_HOST is not set (still the example.com placeholder)")
    if not settings.imap_user:
        problems.append("IMAP_USER is empty")
    if not settings.imap_password:
        problems.append("IMAP_PASSWORD is empty (Gmail requires an App Password)")

    if settings.mode == Mode.live:
        if not settings.smtp_host or "example.com" in settings.smtp_host:
            problems.append("SMTP_HOST is not set (required when MODE=live)")
        if not settings.smtp_password:
            problems.append("SMTP_PASSWORD is empty (required when MODE=live)")
        if (settings.openai_api_key or "").strip() in {"", "changeme"} and \
                settings.llm_provider == "openai" and not _fake_llm_enabled():
            problems.append(
                "OPENAI_API_KEY is missing but MODE=live and FAKE_LLM is off - "
                "every LLM call will fail"
            )

    products_path = Path(settings.products_file)
    if not products_path.exists():
        problems.append(
            f"PRODUCTS_FILE '{settings.products_file}' does not exist "
            "(copy products.example.yaml to products.yaml)"
        )

    return problems


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replydesk",
        description="Async email auto-reply with sentiment + product detection.",
    )
    parser.add_argument(
        "command",
        choices=["run", "once", "replay"],
        help="run = poll forever; once = single poll; replay <path> = process .eml files offline",
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="For 'replay': directory of .eml files, or a single .eml file.",
    )
    parser.add_argument("--log-level", default=None)
    parser.add_argument(
        "--max-emails", type=int, default=None, metavar="N",
        help="Process at most N emails this run (testing helper). "
             "0 = no limit. Overrides MAX_EMAILS from .env.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.max_emails is not None:
        if args.max_emails < 0:
            print("--max-emails must be >= 0", file=sys.stderr)
            return 2
        settings = settings.model_copy(update={"max_emails": args.max_emails})
    _setup_logging(args.log_level or settings.log_level)

    if args.command in ("run", "once"):
        problems = _validate_settings(settings)
        for p in problems:
            log.warning("CONFIG PROBLEM: %s", p)
        if problems:
            print(
                f"\nReplyDesk found {len(problems)} configuration problem(s):\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\nFix your .env file, then re-run. "
                  "(Tip: python -m replydesk once --log-level DEBUG)\n",
                file=sys.stderr,
            )
            return 2

    if args.command == "run":
        asyncio.run(run_forever(settings))
        return 0

    if args.command == "once":
        asyncio.run(run_once(settings))
        return 0

    if args.command == "replay":
        if not args.path:
            print("replay requires a path to a .eml file or directory", file=sys.stderr)
            return 2
        results = asyncio.run(_run_replay(settings, args.path))
        print(f"Processed {len(results)} email(s).")
        for r in results:
            print(
                f"  {r.message_id[:60]} -> {r.decision} "
                f"intent={r.analysis.intent if r.analysis else 'n/a'} "
                f"products={[m.catalog_id or m.raw for m in r.matches.matches] if r.matches else []}"
            )
        return 0

    return 0  # unreachable


async def _run_replay(settings: Settings, path: str) -> list[PipelineResult]:
    src = _replay_source(path, settings)
    return await run_once(settings, source=src.__aiter__())


if __name__ == "__main__":
    sys.exit(_cli())


__all__ = [
    "Pipeline",
    "run_once",
    "run_forever",
]
