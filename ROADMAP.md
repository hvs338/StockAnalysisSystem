# Roadmap

v1 ships the daily digest. v1.5 layered Schwab market movers + held-position overlay on top of it. The end goal (v2/v3, now unified) is an **AI "stock broker" digest**: an LLM (Claude **Opus 4.7 via AWS Bedrock**) synthesizes top movers + **Kronos** price forecasts + **internet/news** into per-stock analysis and **option-play ideas**, emailed pre-open. **Architecture decided: run the whole pipeline locally on the always-on RTX 3070 laptop; AWS is used only for Bedrock (called via boto3) — no Lambda/CDK/S3.** Personal research tool — keep the "not investment advice" disclaimer.

| Version | Status | Headline |
| --- | --- | --- |
| v1 | ✅ shipped | Daily digest: watchlist quotes + threshold flags, emailed via Gmail SMTP |
| v1.5 | ✅ shipped | Market Movers section + held-position overlay using Schwab `/movers` and `/accounts` |
| v1.6 | ✅ shipped | Scheduling + auth/token hardening; Kronos **data pull from Schwab** + batch forecaster (`kronos_runner.py`) |
| v2/v3 | 🔨 in progress | Unified AI-broker analysis (Bedrock Opus) — Kronos forecast run, news, option plays |

### Schwab API access — confirmed June 2026

The connected developer app now has access to the full v1 market-data and accounts suites. Tracked here so future slices don't re-litigate availability:

| API group | Endpoints | Used by |
| --- | --- | --- |
| Market data | `/quotes`, `/movers/{index}`, `/markets`, `/markets/{market_id}`, `/instruments`, `/instruments/{cusip_id}`, `/pricehistory`, `/chains`, `/expirationchain` | v1 (`/quotes`), v1.5 (`/movers`), v2 reserved (`/pricehistory`), later (`/markets`, `/instruments`, options) |
| Accounts | `/accounts/accountNumbers`, `/accounts`, `/accounts/{n}`, `/accounts/{n}/orders`, `/accounts/{n}/transactions`, `/userPreference`, `/orders` | v1 (account numbers + watchlist hash), v1.5 (positions), later (orders + transactions report) |

---

## Current status & what's left (June 2026)

### ✅ Done
- **v1 digest** + **v1.5** Market Movers (`/movers`, gainers/decliners split by sign, % ×100 fix) and held-position overlay (`/accounts?fields=positions`).
- **Auth fixed**: switched to schwab-py `client_from_manual_flow`; root cause of the failures was a callback-URL mismatch (registered `https://127.0.0.1`, code sent `:8182`) — now aligned. Token lives in `token/`.
- **Hardening**: project-root-anchored `.env` + `TOKEN_PATH` (CWD-independent for Task Scheduler); UTF-8 stdout; **re-auth reminder email** on token expiry (`email_sender.send_reauth_reminder` + `main._is_auth_failure`).
- **Scheduling**: Tue–Sat **6:00 AM PT** Task Scheduler job (pre-open so quotes/movers reflect the prior session); `Register-ScheduledTask` command + docs in `README.md`.
- **Watchlist**: Schwab has **no watchlists endpoint** (dropped in the TD Ameritrade migration → 404); `watchlist_fallback.yaml` is now the source of truth (YAML syntax repaired, de-duped).
- **Kronos data path**: `schwab_client.get_daily_candles` + cleaned `stock_prediction.py` (pulls daily OHLCV from `/pricehistory`, builds the Kronos input frame) + `kronos_runner.py` (batch top-N movers → `forecasts/*.json`). Data pull verified; the model forecast itself runs on the 3070.

### 🔨 What needs to be done (in order)
1. **Email delivery decision.** The full pipeline works; only the *send* fails — the **corporate network blocks outbound SMTP TLS** (TCP connects, TLS handshake times out on 465 *and* 587; HTTPS/443 is open). Options: (a) run on a home network / the 3070 laptop where SMTP works, or (b) switch delivery to an **HTTPS email API** (Resend/SendGrid now, AWS SES later) so it works anywhere. Also: set a **valid `GMAIL_APP_PASSWORD`** (current value is an invalid placeholder).
2. **Run the Kronos forecast on the 3070.** `pip install -r requirements-kronos.txt` (CUDA wheel for the GPU), then `python kronos_runner.py --top 5` → writes `forecasts/*.json`. `KronosPredictor` auto-selects CUDA.
3. **Minimal AWS for Bedrock.** Create an AWS account; IAM user key with `bedrock:InvokeModel`; **enable model access for Claude Opus 4.7 + Haiku in us-east-1**; `aws configure` on the laptop; set a **$30/mo AWS Budget alert**. (No Lambda/CDK — Bedrock only.)
4. **Bedrock analysis layer** — `bedrock_analyst.py`: boto3 `bedrock-runtime`, model `us.anthropic.claude-opus-4-7`. Feed top-5 movers + flagged watchlist + **trimmed option chains** (`/chains`, near-the-money + nearest 1–2 expiries) → analysis + structured option-play ideas. **Cost-conscious**: prompt-cache the system prompt, Haiku for sub-steps + Opus for final synthesis only. New email section behind `ENABLE_ANALYSIS`.
5. **Internet/news** feeding the prompt (provider TBD: Google CSE vs a finance news API); Haiku-summarize + cache to bound cost.
6. **Wire Kronos forecasts into the analysis** — the prompt reads `forecasts/*.json` so Opus weighs the forecast in its option timing.
7. **Weekly Task Scheduler job** for `kronos_runner.py` (and optional forecast-accuracy reconciliation — the v2 SQLite idea below).
8. **Later/optional**: `/instruments` fundamentals + authoritative names, `/transactions` recap, a `/markets` trading-day guard.

### 💵 Cost (cost-conscious posture chosen)
Only Bedrock Opus costs money (~$15/$75 per 1M in/out tokens) → **~$5–12/mo** with the trimming/caching levers; compute (3070), Schwab, scheduling all $0.

> The detailed v2/v3 designs below predate the unified AI-broker direction; keep them as
> reference for the Kronos mechanics (forecast accuracy, SQLite) and the news/analysis layer.

---

## v2 — Weekly Kronos Recap

### Goal

Friday 5:00 PM ET, separate from the daily digest:

1. Forecast next week's Mon–Fri OHLCV for every watchlist ticker using the [Kronos](https://github.com/shiyu-coder/Kronos) foundation model.
2. Compare last Friday's forecast against the actual closes that materialized this week.
3. Email a recap with per-ticker forecasts + an accuracy table for last week.

### Data flow

1. **Watchlist** — reuse `schwab_client.get_watchlist_symbols(client)` from v1, no changes.
2. **Historical OHLCV** — new function `schwab_client.get_daily_history(client, symbol, years=2)`, wrapping schwab-py's `client.get_price_history_every_day(symbol, start_datetime=..., end_datetime=...)`. The underlying `/pricehistory` endpoint is confirmed accessible on the developer app (June 2026). Returns ~500 trading days of OHLCV, formatted as a pandas DataFrame.
3. **Forecast** — new module `kronos_runner.py`:
   - Loads `KronosPredictor` once per run, reuses across all tickers.
   - `forecast_batch(symbol_to_df, pred_len=5) -> dict[str, DataFrame]` uses `KronosPredictor.predict_batch` for parallel inference across the whole watchlist.
   - Returns predicted OHLCV for the next 5 trading days per symbol.
4. **Persist** — new module `forecast_store.py`. v2 is where SQLite enters the codebase.
5. **Reconcile last week** — read forecasts that were saved last Friday with `target_date` in the past week, fetch actual closes from Schwab, compute error metrics, write to `forecast_accuracy`.
6. **Email** — new module `weekly_recap_email.py` (separate from `email_sender.py` — the layout differs substantially). New entry point `weekly_recap.py` scheduled as a second Windows Task Scheduler job.

### Kronos API details (from upstream README)

| Field | Value |
| --- | --- |
| Hugging Face models | `NeoQuasar/Kronos-Tokenizer-base`, `NeoQuasar/Kronos-small` (24.7 M, CPU-friendly), `NeoQuasar/Kronos-base` (102 M, GPU) |
| Input DataFrame columns | `['open', 'high', 'low', 'close', 'volume', 'amount']` — Schwab gives all except `amount`; derive as `close × volume` |
| Batch entry point | `predictor.predict_batch(df_list, x_timestamp_list, y_timestamp_list, pred_len, T=1.0, top_p=0.9, sample_count=1)` |
| Context window | 512 (small / base), 2048 (mini) |
| Recommended lookback | 400 daily bars (matches upstream example) |

### Compute switching (GPU ↔ CPU)

You have an RTX 3070 on one laptop and CPU-only on the other. The forecasting code auto-detects:

| Env var | Default | Behavior |
| --- | --- | --- |
| `KRONOS_DEVICE` | `auto` | `auto` → `cuda` if `torch.cuda.is_available()` else `cpu`. Explicit values `cuda` / `cpu` override. |
| `KRONOS_MODEL` | `auto` | `auto` → `Kronos-base` on GPU, `Kronos-small` on CPU. Explicit Hugging Face IDs override. |

**RTX 3070 setup (one-time, on the GPU laptop):**

```powershell
# 1. Confirm CUDA visible
nvidia-smi

# 2. Install CUDA-enabled PyTorch (CUDA 12.1 wheels)
pip install torch --index-url https://download.pytorch.org/whl/cu121

# 3. Sanity check
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# Expect: True NVIDIA GeForce RTX 3070
```

**CPU laptop setup:**

```powershell
pip install torch  # CPU wheels, much smaller install
```

### Dependencies (isolated)

New file `requirements-kronos.txt` keeps the heavy ML stack out of the v1 daily run:

```
torch
transformers
huggingface_hub
einops
matplotlib   # for inline forecast sparklines in the email
```

Kronos itself is not on PyPI. Two options:

1. **Preferred** — add `shiyu-coder/Kronos` as a git submodule under `vendor/Kronos`, import via `sys.path.insert(0, "vendor/Kronos")` in `kronos_runner.py`. Versionable, reproducible, survives a fresh clone.
2. **Alternative** — keep the existing standalone clone at `C:\Users\harsmith\Documents\GitHub\Kronos`, add it to `PYTHONPATH` via the Task Scheduler action. Faster to start, doesn't survive a clone on a new machine.

### Email content (HTML)

- **Header:** "Week Ahead: \<Mon date\> – \<Fri date\>"
- **Per-ticker block:** current close, forecasted Friday close, % expected move, mini sparkline (matplotlib PNG → inline via cid attachment).
- **"How we did last week" table:** ticker, predicted close (made last Fri), actual close, abs error %, direction correct (✓/✗).
- **Footer:** aggregate accuracy — directional hit rate and mean abs % error across the watchlist.

### SQLite schema (`history.sqlite`)

```sql
CREATE TABLE daily_quotes (
    symbol       TEXT NOT NULL,
    date         DATE NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       INTEGER,
    pct_change   REAL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    forecast_date   DATE NOT NULL,  -- when the forecast was made
    target_date     DATE NOT NULL,  -- the day being predicted
    predicted_close REAL NOT NULL,
    model_version   TEXT,
    generated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol, forecast_date, target_date)
);

CREATE TABLE forecast_accuracy (
    forecast_id        INTEGER PRIMARY KEY REFERENCES forecasts(id),
    actual_close       REAL,
    abs_error          REAL,
    pct_error          REAL,
    direction_correct  INTEGER  -- 0 or 1
);
```

### New file layout

```
stock-watchlist-alerts/
├── weekly_recap.py              # entry point — Friday 5:00 PM ET Task Scheduler job
├── kronos_runner.py             # KronosPredictor wrapper + device selection
├── forecast_store.py            # SQLite persistence (forecasts + reconciliation)
├── weekly_recap_email.py        # HTML email layout for the weekly recap
├── requirements-kronos.txt      # isolated ML dependencies
├── vendor/Kronos/               # git submodule (preferred) or PYTHONPATH-include
└── history.sqlite               # gitignored
```

### Failure modes

- **Kronos load failure** (missing model weights, CUDA OOM) → log + skip recap, send a "weekly recap failed: \<reason\>" alert email instead of the full recap.
- **Schwab history endpoint failure** for some tickers → degrade gracefully, run recap on the subset that succeeded, note skipped tickers in the email.
- **SQLite read failure** on "last week's forecasts" → skip the reconciliation section, still send the forward-looking forecast.

### Verification (when implemented)

1. `python weekly_recap.py --dry-run` produces an HTML file on disk without emailing.
2. Forecast accuracy section is empty on the first run (no prior forecasts in SQLite) — verify it doesn't crash, just renders "First run — no prior forecasts to compare."
3. Second run a week later includes the reconciliation block populated.
4. Replay test: manually set `today` to a past Friday, point at sliced history, validate Kronos produces non-trivial output and the accuracy reconciliation works against known outcomes.

---

## v3 — Daily Top-5 Movers Analysis

> **Scope note (June 2026):** v1.5 already surfaces *market-wide* top movers via Schwab `/movers/{index}`. v3 remains relevant for two distinct things that v1.5 doesn't cover: (a) **watchlist-internal** top-5 (movers among the symbols *you* track, regardless of whether they're S&P leaders), and (b) **news context + Claude-written explanations** for each mover. Schwab `/movers` returns no headlines, so the Google Custom Search + Claude pipeline below stays.

### Goal

After the daily digest evaluates the watchlist, pick the 5 symbols with the largest `|pct_change|`, fetch recent news per symbol via the Google Custom Search API, summarize each move with the Claude API, and append the analysis to the same daily digest email.

### Data flow

1. After `build_quote_rows` in `main.py`, sort by `abs(pct_change)` descending, take top 5.
2. New module `news_scraper.py`:
   - `fetch_news(symbol, days_back=2, max_items=5) -> list[NewsItem]`
   - Calls Google Custom Search: `GET https://www.googleapis.com/customsearch/v1?q={SYMBOL}+stock+news&cx={CSE_ID}&key={API_KEY}&dateRestrict=d2&num=5&sort=date`
   - Returns `[{title, link, snippet, source, published}]`.
3. New module `claude_summarizer.py`:
   - `summarize_mover(symbol, pct_change, news_items) -> str`
   - Anthropic Messages API call to `claude-sonnet-4-6` with a prompt-cached system prompt.
   - Prompt: "Symbol {symbol} moved {pct:+.2f}% today. Here are recent headlines: ... In 2-3 sentences, what is likely driving this move? Be specific. No boilerplate. If the headlines don't explain the move, say so."
4. Modify `email_sender.py`: add `build_top_movers_html(top_movers_with_summaries)` and stitch it into `build_digest_html`.
5. Modify `main.py`: between evaluate and email, if `ENABLE_TOP_MOVERS_ANALYSIS=1`, call `news_scraper` + `claude_summarizer` for the top 5.

### One-time setup the user does

1. Create a Programmable Search Engine at <https://programmablesearchengine.google.com> — scope to news sites or the whole web. Copy the `cx` (search-engine ID).
2. Create a Google Cloud project, enable the Custom Search API, generate an API key.
3. Get an Anthropic API key at <https://console.anthropic.com>.
4. Add to `.env`:

   ```
   GOOGLE_CSE_ID=your_cx_here
   GOOGLE_API_KEY=AIza...
   ANTHROPIC_API_KEY=sk-ant-...
   ENABLE_TOP_MOVERS_ANALYSIS=1
   ```

### Quota math

- **Google CSE free tier:** 100 queries/day. v3 uses 5/day. ~20× headroom — plenty of room to grow to top-10 or top-20.
- **Anthropic Sonnet 4.6:** ~500 input + ~200 output tokens per call × 5 calls/day. ~$0.001/day, ~$0.40/year. Negligible.

### Dependencies (lightweight)

Append to `requirements.txt`:

```
anthropic>=0.40.0
requests>=2.32.0
```

### Email content

New HTML section, appended after the watchlist table:

> **Top 5 Movers — what's going on**
>
> Per ticker:
> - Ticker + % change badge (green ▲ / red ▼)
> - Claude's 2–3 sentence summary
> - Bullet list of up to 3 source headlines with links

### New file layout

```
stock-watchlist-alerts/
├── news_scraper.py              # Google Custom Search wrapper
├── claude_summarizer.py         # Anthropic API wrapper
├── email_sender.py              # modified — adds build_top_movers_html
└── main.py                      # modified — feature-flagged top-5 path
```

### Failure modes

- **Google CSE returns no results** → render "no recent news found" for that ticker, skip the Claude call.
- **Google CSE quota exhausted (HTTP 429)** → render "news quota exhausted today" once at the section header, skip remaining tickers.
- **Anthropic API error** → render the raw headlines without the summary, log the error.
- All failures are caught at the section boundary. The v1 digest table is never blocked by v3 features.

### Verification (when implemented)

1. `python main.py --dry-run --include-news` prints the digest with the news section to stdout.
2. Market-quiet day (synthetic top-5 with `pct=0`) → section either renders sensibly or is skipped.
3. Bad API keys → digest still sends, news section shows error placeholder.
4. Force a 429 from Google → graceful degradation, single header note + no per-ticker calls.

---

## Carried-over from the original v1 roadmap

These were in the original README and remain part of the v2 work since v2 is where SQLite arrives:

- 30-day volume averages → unusual-volume flag.
- Multiple watchlists rolled into one digest.

Pushed further out:

- Intraday triggers (separate streaming script, real-time push notifications).
