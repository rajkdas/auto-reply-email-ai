# ReplyDesk

Async email auto-reply with **sentiment + product detection**, built on LangChain.

For each new incoming email, ReplyDesk:

1. **Analyzes** it with one LLM call → sentiment, urgency, intent, language, products mentioned.
2. **Matches** those products against `products.yaml` (fuzzy, so "Widgt Pro" still matches "Widget Pro").
3. **Decides** what to do with a small set of plain-Python rules: `send`, `draft`, `escalate` or `ignore`.
4. **Drafts** a reply (second LLM call) using the product info and FAQ text as context.
5. **Sends** the reply over SMTP — or saves it as a draft, or just logs in dry-run — and records it so it's never answered twice.

```
IMAP (new mail) → parse & clean → LangChain analysis → product match → rules → LangChain reply → SMTP send
                                        │                                │
                                        └──────── SQLite (dedupe + audit log) ────────┘
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

# Single poll against your real mailbox (will read UNSEEN mail, log what it would do):
python -m replydesk once

# Run forever (24/7):
python -m replydesk run
```

---

## Modes

| `MODE`         | Sends? | Saves drafts? | When to use                                  |
|----------------|:------:|:--------------:|----------------------------------------------|
| `dry_run`      |   no   |       no       | First days, demo, replay fixtures, Colab.    |
| `draft_only`   |   no   |       yes       | Watch the IMAP Drafts folder for a few days.  |
| `live`         |  yes   |   only downgraded sends (post-check) or reply-generation failures; otherwise replies go straight out over SMTP | Production. |

Switch with `MODE=live` in `.env`. Thresholds come from `.env` so you can tune them without code changes.

### Getting replies actually sent (live mode checklist)

If `decision=send` but no email arrives, check these in order:

1. **Post-check downgrade** — log line `post_check downgraded ... reason=needs_human` means the safety
   review rewrote `send` → `draft`; the reply is then appended to Gmail's **Drafts** folder (any non-
   dry-run mode). Disable with `POST_CHECK_ENABLED=false`, or fix the root cause: add your domain to
   `ALLOWED_DOMAINS` and keep product facts (prices/URLs) in `products.yaml` FAQ text.
2. **Escalation rules** — `sentiment_score <= -0.6 AND urgency == high` returns `escalate` *before* a
   reply is even generated (nothing to draft/send). Calmer test mail triggers auto-reply instead.
3. **Dedupe** — a message-id already recorded in SQLite is skipped (`skip dedupe ... already processed`).
   Send genuinely new mail when testing.
4. **SMTP config** — `MODE=live` requires `SMTP_HOST/PORT/USER/PASSWORD` + an app password; see
   `SMTP_SECURITY` below.

---

## Gmail notes (tested against imap.gmail.com / smtp.gmail.com)

- Use a Google **App Password** (2FA required), not your normal password, for `IMAP_PASSWORD`/`SMTP_PASSWORD`.
- `SMTP_PORT=465` + `SMTP_SECURITY=ssl`, **or** `SMTP_PORT=587` + `SMTP_SECURITY=starttls`. The two must
  match: `ssl` = implicit TLS from the first byte, `starttls` = upgrade of a plain connection. A mismatched
  pair makes sends hang or fail.
- Fetching is UID-based end-to-end (`UID SEARCH UNSEEN` / `UID FETCH (BODY.PEEK[])` / `UID STORE \Seen`):
  sequence numbers shift when mail is deleted mid-run and would flag the wrong messages.
- `MAX_EMAILS` keeps the **newest** N unseen messages (UIDs are monotonically increasing), so a capped run
  on a backlog mailbox works on fresh mail, not years-old unread. Unprocessed mail stays UNSEEN.
- aioimaplib 2.x returns `Response(result, lines)` — there is no `.status`/`.data` — and delivers FETCH
  literals as whole `bytearray` entries; `mail_io.py` handles both shapes (see regression tests).

---

## Configuration

All secrets and tunables live in **`.env`** (see `.env.example`). The catalog + FAQ live in **`products.yaml`** (see `products.example.yaml`).

Two env vars control which LLM model is used:
- `LLM_ANALYSIS_MODEL` — for the analysis chain (use a cheap model, e.g. `gpt-4o-mini`).
- `LLM_REPLY_MODEL`     — for the reply chain (use a stronger model if you want).
- `LLM_REPLY_FALLBACK_MODEL` — optional, used as a fallback if the primary model fails after retries.

`LLM_PROVIDER` switches provider without code changes: `openai | azure_openai | anthropic | google_genai | ollama`. Install only the `langchain-<provider>` package you need (e.g. `pip install langchain-google-genai google-genai` for Gemini/Gemma models).

For offline / demo runs (no API keys), set `FAKE_LLM=true` to use a deterministic stub chain.

### Post-check safety gate (`POST_CHECK_ENABLED`)

After the reply is generated, an extra LLM review can downgrade `send` → `draft` (see Rules engine
below). The gate is on by default. Set `POST_CHECK_ENABLED=false` in `.env` to disable it and let
rule-approved replies go straight out over SMTP in live mode. Prefer fixing false positives instead of
disabling in production: keep your domain in `ALLOWED_DOMAINS` and all facts the model may quote
(prices, URLs, policies) inside `products.yaml` FAQ text.

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

The matcher runs three passes on each email:
1. **LLM mentions** (`analysis.products_mentioned`) — raw product names as the LLM saw them.
2. **SKU regex** — anything matching `sku_pattern` in the email body is force-mapped to its product.
3. **Alias substring** — anything matching an alias in the email body is force-mapped to its product.
4. **Fuzzy fallback** — any LLM mention that didn't match exactly goes through `rapidfuzz.token_sort_ratio` against name + aliases. Anything below `FUZZY_MATCH_THRESHOLD` (default 80) is flagged `unknown_product` and routed to a human.

---

## Rules engine (`rules.py`)

One readable function decides what to do with each email:

```python
if email.is_automated or email.from_self:                          return IGNORE
if analysis.intent == "spam":                                       return IGNORE
if analysis.sentiment_score <= -0.6 and analysis.urgency == "high": return ESCALATE
if analysis.confidence < MIN_CONFIDENCE:                            return DRAFT
if analysis.intent == "refund":                                     return DRAFT
if matches.has_unknown:                                              return DRAFT
if history.replies_to_sender_today >= MAX_REPLIES_PER_SENDER:        return DRAFT
return SEND
```

After the reply is generated, a small **post-check** downgrades `SEND` → `DRAFT` if the reply:
- Contains a URL outside `ALLOWED_DOMAINS`.
- Mentions a money amount not found in the catalog/FAQ context.
- Has `needs_human=True`.

This gate can be switched off entirely with `POST_CHECK_ENABLED=false` in `.env` (see above). When it
downgrades a decision in `live` mode, the reply is appended to the IMAP **Drafts** folder — it is never
silently discarded.

---

## Crash safety / never-reply-twice

Every processed message-id is written to SQLite with a state column:

1. **`processing`** — claimed by a worker (atomic `INSERT OR IGNORE`). Concurrent workers on the same message-id skip immediately.
2. **`sending`** — a `send` decision in `live` mode, just before the SMTP send.
3. **`sent`** — confirmed after SMTP success. Also bumps the per-sender daily cap.
4. **`draft` / `escalate` / `ignore` / `review`** — terminal outcomes.

On restart, any rows left in `processing` or `sending` are moved to `review` so the system never replies twice. They sit there for a human to look at.

---

## Logging & tracing

- Stdlib `logging`, one line per email: `id, sentiment, urgency, intent, products, decision`. Sender addresses are masked (`a***e@example.com`).
- Set `LANGSMITH_TRACING=true` (and `LANGSMITH_API_KEY`) to inspect every LLM call without code changes.

---

## Running it

| Where | How |
|---|---|
| **Linux** | `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m replydesk` |
| **Windows** | `py -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt ; python -m replydesk` |
| **Docker** | `docker compose up -d` (see below) |
| **systemd** | `cp deploy/replydesk.service /etc/systemd/system/ ; systemctl enable --now replydesk` |
| **Colab** | Upload the repo, set secrets via Colab secrets, `await run_once()` (Colab can't run 24×7) |

CLI:

```
python -m replydesk run                          # poll forever (24/7)
python -m replydesk once                         # one pass over UNSEEN
python -m replydesk replay tests/fixtures        # offline: run the pipeline over .eml files
```

`once` and `replay` work without a mailbox. `replay` is the recommended way to sanity-check the pipeline against your catalog before going live.

---

## Docker

`Dockerfile` and `docker-compose.yml` are included. The compose file mounts a `./data` volume for the SQLite DB and restarts automatically:

```bash
docker compose up -d
docker compose logs -f
```

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                  # 53 unit + pipeline tests (incl. IMAP FETCH regression suite), ~2s
ruff check .            # lint
```

No real LLM or mailbox is needed for tests. The pipeline test uses a stub analysis/reply chain and a temp SQLite file. The `mail_io` tests reproduce aioimaplib 2.x's real response shapes (`Response(result, lines)`, whole-`bytearray` FETCH literals, Gmail `{size}` markers) so protocol regressions are caught offline.

A live sanity check against a real model and real mailbox is opt-in: `pytest -m live`.

CI (GitHub Actions): one workflow, matrix `ubuntu / windows` × Python `3.10 / 3.11 / 3.12`, running `ruff` and `pytest`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no body bytes in FETCH response uid=...` | Old `mail_io.py` (< commit `391d731`). Update — the extractor now anchors on the `{size}` literal marker and accepts `bytearray` entries. |
| `skip dedupe ... (already processed)` but no reply ever went out | Earlier crashed run left the message-id in SQLite (`processing` → `review`). Delete `data/replydesk.db*` to reprocess, or send new mail. |
| `decision=send` then `post_check downgraded ... reason=needs_human` | Safety gate rewrote send→draft; reply lands in Gmail **Drafts**. Tune `ALLOWED_DOMAINS`/FAQ, or set `POST_CHECK_ENABLED=false`. |
| `decision=draft mode=Mode.live id=... (no side effect)` | Old `main.py` (< commit `5464cd1`) silently discarded downgraded replies. Update — drafts are now appended in any non-dry-run mode. |
| Gemini/Gemma `503 UNAVAILABLE ... high demand` | Transient provider-side capacity error; the SDK retries automatically. If it persists, switch `LLM_*_MODEL` or provider. |
| Both `GOOGLE_API_KEY` and `GEMINI_API_KEY` warnings | Harmless; the SDK prefers `GOOGLE_API_KEY`. Set only one to silence it. |
| Sends hang / TLS errors in live mode | `SMTP_SECURITY` doesn't match `SMTP_PORT`: use `ssl`+465 or `starttls`+587. |

---

## Architecture / files

```
replydesk/
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── products.example.yaml
├── README.md
├── Dockerfile
├── docker-compose.yml
├── pytest.ini
├── replydesk/
│   ├── __init__.py
│   ├── __main__.py            # python -m replydesk
│   ├── config.py              # settings from .env (pydantic-settings)
│   ├── models.py              # Email, Analysis, Reply (Pydantic)
│   ├── mail_io.py             # fetch (IMAP), parse/clean, send (SMTP), append draft
│   ├── chains.py              # LangChain: analysis chain + reply chain (+ fakes)
│   ├── products.py            # load catalog, fuzzy-match LLM output to catalog
│   ├── rules.py               # decide(): send / draft / escalate / ignore
│   ├── store.py              # SQLite: processed ids, audit log, per-sender cap
│   └── main.py                # async loop: run_forever() and run_once()
├── tests/
│   ├── fixtures/*.eml
│   ├── test_parse.py
│   ├── test_products.py
│   ├── test_rules.py
│   └── test_pipeline.py
├── notebooks/colab_demo.ipynb
└── deploy/
    ├── replydesk.service
    └── ci.yml
```

---

## Explicitly left out for v1

Multiple mailboxes, OAuth, Postgres, vector search / RAG over a larger FAQ, attachment reading, Prometheus / dashboards, cloud secret managers. Each can be added later without changing the structure above. The first ones we'd add are RAG over a larger FAQ (LangChain makes it a small change) and multi-mailbox support.
