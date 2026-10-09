# ReplyDesk

Async email auto-reply with **sentiment + product detection**, built on LangChain.

For each new incoming email, ReplyDesk:

1. **Analyzes** it with one LLM call → sentiment, urgency, intent, language, products mentioned.
2. **Matches** those products against `products.yaml` (fuzzy, so "Widgt Pro" still matches "Widget Pro").
3. **Decides** what to do with a small set of plain-Python rules: `send`, `draft`, `escalate` or `ignore`.
4. **Drafts** a reply (second LLM call) using the product info and FAQ text as context.
5. **Sends** the reply over SMTP — or saves it as a draft, or just logs in dry-run — and records it so it's never answered twice.

```
IMAP (new mail) → parse & clean → LangChain analysis → product match → rules → LangChain reply → post-check gate → SMTP send
                                        │                                                          │
                                        └────────────── SQLite (dedupe + audit log) ──────────────┘
```

---

## Quick start

```bash
git clone <this-repo> replydesk
cd replydesk

python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env               # then edit .env with your IMAP/SMTP/LLM keys
cp products.example.yaml products.yaml   # edit your catalog + FAQ snippets

# Offline smoke test (no IMAP / no LLM keys needed):
FAKE_LLM=true python -m replydesk replay tests/fixtures

# Single poll against your real mailbox (reads UNSEEN mail, logs what it would do):
python -m replydesk once

# Process exactly one (the newest) unseen email while testing:
python -m replydesk once --max-emails 1

# Run forever (24/7):
python -m replydesk run
```

Requires Python 3.10+ (tested on 3.10–3.12, Linux and Windows).

---

## CLI

```
python -m replydesk run                      # poll forever (24/7)
python -m replydesk once                     # one pass over UNSEEN
python -m replydesk once --max-emails 1      # cap this run at N emails (0 = no limit)
python -m replydesk replay <dir|file.eml>    # offline: run the pipeline over .eml fixtures
python -m replydesk once --log-level DEBUG   # override LOG_LEVEL for one run
```

`once` and `replay` work without a long-running process. `replay` is the recommended way to sanity-check the pipeline against your catalog before going live.

On startup, `run` and `once` validate your `.env` and print every configuration problem (placeholder hosts, missing passwords when `MODE=live`, missing `products.yaml`, …) instead of failing silently later.

---

## Modes

| `MODE`       | Sends? | Saves drafts? | When to use |
|--------------|:------:|:-------------:|-------------|
| `dry_run`    | no     | no            | First days, demo, replay fixtures, Colab. |
| `draft_only` | no     | yes           | Watch the IMAP Drafts folder for a few days. |
| `live`       | yes    | only downgraded sends (post-check) or reply-generation failures; otherwise replies go straight out over SMTP | Production. |

Switch with `MODE=live` in `.env`. Thresholds come from `.env` too, so you can tune behaviour without code changes.

### Getting replies actually sent (live-mode checklist)

If the log shows `decision=send` but no email arrives, check these in order:

1. **Post-check downgrade** — a line like `post_check downgraded ... reason=needs_human` means the safety
   review rewrote `send` → `draft`; the reply is then appended to Gmail's **Drafts** folder (in any
   non-dry-run mode). Disable with `POST_CHECK_ENABLED=false`, or fix the root cause: add your domain to
   `ALLOWED_DOMAINS` and keep product facts (prices/URLs) in the `products.yaml` FAQ text.
2. **Escalation rules** — `sentiment_score <= -0.6 AND urgency == high` returns `escalate` *before* a
   reply is even generated (nothing to draft/send). A calmer test mail triggers the auto-reply instead.
3. **Dedupe** — a message-id already recorded in SQLite is skipped (`skip dedupe ... already processed`).
   Send genuinely new mail when testing, or reset the DB (see Troubleshooting).
4. **SMTP config** — `MODE=live` requires `SMTP_HOST/PORT/USER/PASSWORD` plus an app password; see
   `SMTP_SECURITY` in the Gmail notes below.

A positive, low-urgency question is the easiest way to force a full `send` path end-to-end, e.g.:

> **Subject:** Quick question about WidgetPro features
> Hi — I've been using WidgetPro for a few weeks and really enjoying it. Does it support exporting
> reports to PDF? No rush. Thanks!

---

## Gmail notes (tested against imap.gmail.com / smtp.gmail.com)

- Use a Google **App Password** (2-step verification required), not your normal password, for
  `IMAP_PASSWORD` / `SMTP_PASSWORD`.
- `SMTP_PORT=465` + `SMTP_SECURITY=ssl`, **or** `SMTP_PORT=587` + `SMTP_SECURITY=starttls`. The two must
  match: `ssl` = implicit TLS from the first byte, `starttls` = upgrade of a plain connection. A mismatched
  pair makes sends hang or fail.
- Fetching is UID-based end-to-end (`UID SEARCH UNSEEN` / `UID FETCH (BODY.PEEK[])` / `UID STORE \Seen`):
  sequence numbers shift when mail is deleted mid-run and would flag the wrong messages.
- `MAX_EMAILS` keeps the **newest** N unseen messages (UIDs increase monotonically), so a capped run on a
  backlog mailbox works on fresh mail, not years-old unread. Unprocessed mail stays UNSEEN.
- Messages are marked `\Seen` only after the pipeline commits a final outcome, so a crash mid-processing
  re-fetches the same mail next run (and the DB claim prevents double replies).
- aioimaplib 2.x returns `Response(result, lines)` — there is no `.status`/`.data` — and delivers FETCH
  literals as whole `bytearray` entries; `mail_io.py` handles both shapes (covered by regression tests).
- To verify credentials alone (without running the pipeline): `python scripts_imap_login_check.py`.
- If both `GOOGLE_API_KEY` and `GEMINI_API_KEY` are set, the Google SDK warns and prefers
  `GOOGLE_API_KEY`. Harmless — set only one to silence it.

---

## Configuration

All secrets and tunables live in **`.env`** (see `.env.example`). The catalog + FAQ live in
**`products.yaml`** (see `products.example.yaml`). Every setting is loaded once into a typed
`Settings` object (pydantic-settings); no module reads `os.environ` directly.

Key variables:

| Variable | Default | Meaning |
|---|---|---|
| `MODE` | `dry_run` | `dry_run` / `draft_only` / `live` (see Modes above). |
| `LLM_PROVIDER` | `openai` | `openai` \| `azure_openai` \| `anthropic` \| `google_genai` \| `ollama`. |
| `LLM_ANALYSIS_MODEL` | `gpt-4o-mini` | Model for the analysis chain (cheap is fine). |
| `LLM_REPLY_MODEL` | `gpt-4o-mini` | Model for the reply chain (stronger if you want). |
| `LLM_REPLY_FALLBACK_MODEL` | – | Optional fallback used when the primary chain fails after retries. |
| `IMAP_HOST/PORT/USER/PASSWORD/MAILBOX` | – | Incoming mail; port 993 SSL. |
| `SMTP_HOST/PORT/USER/PASSWORD` | – | Outgoing mail (required when `MODE=live`). |
| `SMTP_SECURITY` | `starttls` | `starttls` (587) or `ssl` (465) — must match the port. |
| `POLL_SECONDS` | `30` | Sleep between polls in `run`. |
| `WORKERS` / `MAX_CONCURRENCY` | `4` | Worker tasks / concurrent LLM calls. |
| `MAX_EMAILS` | `0` | Cap per run (0 = unlimited). CLI override: `--max-emails N`. |
| `PRODUCTS_FILE` | `products.yaml` | Catalog + FAQ. |
| `ALLOWED_DOMAINS` | `example.com` | Comma-separated allowlist; URLs outside it downgrade send→draft. |
| `MAX_REPLIES_PER_SENDER` | `3` | Daily cap per sender address. |
| `MIN_CONFIDENCE` | `0.7` | Below this, analysis results go to draft instead of send. |
| `FUZZY_MATCH_THRESHOLD` | `80` | rapidfuzz score under which a product mention is `unknown_product`. |
| `MAX_BODY_CHARS` | `6000` | Hard cap on body length sent to the LLM. |
| `POST_CHECK_ENABLED` | `true` | Safety gate; set `false` to stop send→draft downgrades (see below). |
| `DB_PATH` | `data/replydesk.db` | SQLite file (parent dirs created automatically). |
| `LOG_LEVEL` | `INFO` | `DEBUG` also dumps raw aiosqlite/aioimaplib traffic. |
| `FAKE_LLM` | unset | Set `true` for deterministic stub chains — offline demos/tests, no API keys. |

`LLM_PROVIDER` switches provider without code changes. Install only the matching package, e.g.
`pip install langchain-google-genai google-genai` for Gemini/Gemma models (the extras are listed,
commented out, in `requirements.txt`).

### Post-check safety gate (`POST_CHECK_ENABLED`)

After the reply is generated, a rule-based check can downgrade `send` → `draft`. The gate is **on by
default**. Set `POST_CHECK_ENABLED=false` in `.env` to disable it and let rule-approved replies go
straight out over SMTP in live mode. Prefer fixing false positives over disabling in production: keep
your domain in `ALLOWED_DOMAINS` and all facts the model may quote (prices, URLs, policies) inside the
`products.yaml` FAQ text. When the gate does downgrade in live mode, the reply is always appended to
the IMAP **Drafts** folder — never silently discarded.

---

## Product catalog (`products.yaml`)

```yaml
products:
  - id: widget_pro
    name: Widget Pro
    aliases: [WidgetPro, Widgt Pro, WP]
    sku_pattern: "WP-\\d{3,4}"
    faq: |
      The Widget Pro ships within 2 business days from our EU warehouse.
      Standard warranty is 24 months.
```

The matcher runs four passes on each email:

1. **LLM mentions** (`analysis.products_mentioned`) — raw product names as the LLM saw them.
2. **SKU regex** — anything matching `sku_pattern` in the email body is force-mapped to its product.
3. **Alias substring** — anything matching an alias in the email body is force-mapped to its product.
4. **Fuzzy fallback** — any mention that didn't match exactly goes through
   `rapidfuzz.token_sort_ratio` against name + aliases. Anything below `FUZZY_MATCH_THRESHOLD`
   (default 80) is flagged `unknown_product` and routed to a human (draft).

The matched products' `faq` text is injected into the reply prompt — this is also what the money-amount
allowlist in the post-check compares against.

---

## Rules engine (`rules.py`)

One readable function decides what to do with each email:

```python
if email.is_automated or email.from_self:                           return IGNORE
if analysis.intent == "spam":                                       return IGNORE
if analysis.sentiment_score <= -0.6 and analysis.urgency == "high": return ESCALATE
if analysis.confidence < MIN_CONFIDENCE:                            return DRAFT
if analysis.intent == "refund":                                     return DRAFT
if matches.has_unknown:                                             return DRAFT
if history.replies_to_sender_today >= MAX_REPLIES_PER_SENDER:       return DRAFT
return SEND
```

Auto/bounce detection (`is_automated`) covers `Auto-Submitted`, `Precedence: bulk/list/junk`,
`List-Unsubscribe`, failure reports, no-reply localparts, and mail from yourself — loop protection
also tags outgoing replies with `Auto-Submitted: auto-replied`.

After the reply is generated, the **post-check** downgrades `SEND` → `DRAFT` if the reply:

- has `needs_human=True`,
- contains a URL outside `ALLOWED_DOMAINS`, or
- mentions a money amount not found verbatim in the catalog/FAQ context.

(Entirely switchable via `POST_CHECK_ENABLED` — see Configuration.)

---

## Crash safety / never-reply-twice

Every processed message-id is written to SQLite with a state column:

1. **`processing`** — claimed by a worker (atomic `INSERT OR IGNORE`). Concurrent workers hitting the
   same message-id skip immediately.
2. **`sending`** — a `send` decision in `live` mode, recorded just before the SMTP send.
3. **`sent`** — confirmed after SMTP success. Also bumps the per-sender daily counter.
4. **`draft` / `escalate` / `ignore` / `review`** — terminal outcomes (`review` = recovered leftovers).

On restart, any rows left in `processing` or `sending` are moved to `review` so the system never
replies twice; they sit there for a human to look at. Note: a row in `review` still counts as
"already processed" for dedupe — delete/reset the DB to reprocess old test mail (see Troubleshooting).

---

## Logging & tracing

- Stdlib `logging`, one INFO line per email: `id, sender (masked), sentiment, urgency, intent,
  products, decision`. Sender addresses are masked (`a***e@example.com`).
- `--log-level DEBUG` additionally dumps raw aiosqlite queries and full aioimaplib wire traffic —
  useful when fetch/parse behaves unexpectedly.
- Set `LANGSMITH_TRACING=true` (and `LANGSMITH_API_KEY`) to inspect every LLM call without code changes.

---

## Running it

| Where | How |
|---|---|
| **Linux** | `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m replydesk run` |
| **Windows** | `py -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt ; python -m replydesk run` |
| **Docker** | `docker compose up -d` (see below) |
| **systemd** | `cp deploy/replydesk.service /etc/systemd/system/ ; systemctl enable --now replydesk` |
| **Colab** | Upload the repo, set secrets via Colab secrets, `await run_once()` (Colab can't run 24×7) |

---

## Docker

`Dockerfile` and `docker-compose.yml` are included. The compose file mounts a `./data` volume for the
SQLite DB and restarts automatically:

```bash
docker compose up -d
docker compose logs -f
```

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest              # 53 unit + pipeline tests (incl. IMAP FETCH regression suite), ~2s
ruff check .        # lint
```

No real LLM or mailbox is needed for tests. The pipeline test uses stub analysis/reply chains and a
temp SQLite file. The `mail_io` tests reproduce aioimaplib 2.x's real response shapes
(`Response(result, lines)`, whole-`bytearray` FETCH literals, Gmail `{size}` markers) so protocol
regressions are caught offline.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `AttributeError: 'Response' object has no attribute 'status'` | Old `mail_io.py` (< commit `8bcd0ab`) or aioimaplib < 2.0. Update the file and `pip install -U 'aioimaplib>=2.0'`. |
| `no body bytes in FETCH response uid=...` | Old `mail_io.py` (< commit `391d731`). Update — the extractor now anchors on the `{size}` literal marker and accepts `bytearray` entries. |
| `skip dedupe ... (already processed)` but no reply ever went out | An earlier crashed run left the message-id in SQLite (`processing` → `review`). Delete `data/replydesk.db*` to reprocess, or send new mail. |
| `decision=send` then `post_check downgraded ... reason=needs_human` | Safety gate rewrote send→draft; reply lands in Gmail **Drafts**. Tune `ALLOWED_DOMAINS`/FAQ, or set `POST_CHECK_ENABLED=false`. |
| `decision=draft mode=Mode.live id=... (no side effect)` | Old `main.py` (< commit `5464cd1`) silently discarded downgraded replies. Update — drafts are now appended in any non-dry-run mode. |
| Empty Drafts folder after a downgrade | Same bug as above; fixed in `5464cd1`. Sync `main.py`. |
| Angry/high-urgency complaint gets no reply at all | Working as designed: `sentiment_score <= -0.6 + urgency=high` → `escalate` (human review, no auto-reply). Send a calmer mail to exercise the send path. |
| Gemini/Gemma `503 UNAVAILABLE ... high demand` | Transient provider-side capacity error; the SDK retries automatically. If it persists, switch `LLM_*_MODEL` or provider. |
| Sends hang / TLS errors in live mode | `SMTP_SECURITY` doesn't match `SMTP_PORT`: use `ssl`+465 or `starttls`+587. |
| Both `GOOGLE_API_KEY` and `GEMINI_API_KEY` warnings | Harmless; the SDK prefers `GOOGLE_API_KEY`. Set only one to silence it. |

---

## Architecture / files

```
replydesk/
├── requirements.txt           # runtime deps (aioimaplib>=2.0 required)
├── requirements-dev.txt       # pytest, pytest-asyncio, ruff
├── .env.example               # every setting documented
├── products.example.yaml      # sample catalog + FAQ
├── README.md
├── Dockerfile
├── docker-compose.yml
├── pytest.ini
├── scripts_imap_login_check.py# standalone Gmail login probe
├── replydesk/
│   ├── __init__.py
│   ├── __main__.py            # python -m replydesk
│   ├── config.py              # Settings from .env (pydantic-settings)
│   ├── models.py              # EmailMessage, Analysis, Reply, PipelineResult (Pydantic)
│   ├── mail_io.py             # IMAP fetch (UID-based), parse/clean, SMTP send, draft append
│   ├── chains.py              # LangChain analysis + reply chains (+ FAKE_LLM stubs)
│   ├── products.py            # catalog loading, SKU/alias/fuzzy matching
│   ├── rules.py               # decide(): send/draft/escalate/ignore + post_check()
│   ├── store.py               # SQLite: claims, audit log, per-sender cap, crash reclaim
│   └── main.py                # Pipeline, run_once(), run_forever(), CLI
├── tests/
│   ├── fixtures/*.eml         # complaint/refund/spam/auto-reply/mailing-list/quoted/HTML samples
│   ├── test_parse.py
│   ├── test_products.py
│   ├── test_rules.py
│   └── test_pipeline.py
├── notebooks/colab_demo.ipynb
├── deploy/replydesk.service
└── .github/workflows/ci.yml   # ubuntu/windows × py3.10/3.11/3.12, ruff + pytest
```

---

## Explicitly left out for v1

Multiple mailboxes, OAuth, Postgres, vector search / RAG over a larger FAQ, attachment reading, Prometheus / dashboards, cloud secret managers. Each can be added later without changing the structure above. The first ones we'd add are RAG over a larger FAQ (LangChain makes it a small change) and multi-mailbox support.
