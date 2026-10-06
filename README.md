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
| `live`         |  yes   |       n/a       | Production.                                  |

Switch with `MODE=live` in `.env`. Thresholds come from `.env` so you can tune them without code changes.

---

## Configuration

All secrets and tunables live in **`.env`** (see `.env.example`). The catalog + FAQ live in **`products.yaml`** (see `products.example.yaml`).

Two env vars control which LLM model is used:
- `LLM_ANALYSIS_MODEL` — for the analysis chain (use a cheap model, e.g. `gpt-4o-mini`).
- `LLM_REPLY_MODEL`     — for the reply chain (use a stronger model if you want).
- `LLM_REPLY_FALLBACK_MODEL` — optional, used as a fallback if the primary model fails after retries.

`LLM_PROVIDER` switches provider without code changes: `openai | azure_openai | anthropic | google_genai | ollama`. Install only the `langchain-<provider>` package you need.

For offline / demo runs (no API keys), set `FAKE_LLM=true` to use a deterministic stub chain.

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
pytest                  # 45 unit + pipeline tests, ~2s
ruff check .            # lint
```

No real LLM or mailbox is needed for tests. The pipeline test uses a stub analysis/reply chain and a temp SQLite file.

A live sanity check against a real model and real mailbox is opt-in: `pytest -m live`.

CI (GitHub Actions): one workflow, matrix `ubuntu / windows` × Python `3.10 / 3.11 / 3.12`, running `ruff` and `pytest`.

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
