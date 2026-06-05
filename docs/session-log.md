# Session Log

Curated record of design decisions, current state, and pickup points across Claude Code sessions. Newest entries on top.

---

## 2026-06-03 — v1 implementation, deploy to GitHub, email template, roadmap

### TL;DR

Built v1 of the Schwab daily watchlist digest from scratch, deployed it to **hvs338/stock-watchlist-alerts** (private), layered on a Jinja-based newspaper-style email template, and documented a detailed `ROADMAP.md` for v2 (weekly Kronos forecasts) and v3 (top-5 movers analysis with Google Custom Search + Claude). Three commits on `main`.

### Where things stand

- **Repo:** https://github.com/hvs338/stock-watchlist-alerts (private)
- **Branch:** `main`
- **Latest commit:** `98d20bf` — Add ROADMAP, refresh README, ship the Jinja email template
- **v1 code is shipped but not yet running on a real machine** — setup steps in `README.md` still need to be done before the daily digest can fire (Schwab app registration, Gmail app password, `.env`, `--auth` flow, Task Scheduler entry).

### Conversation arc (how we got here)

1. Started from an empty repo (just a stub `README.md`).
2. Planned v1: Schwab API auth + watchlist fetch + Gmail SMTP digest + Windows Task Scheduler.
3. Discovered `schwab-py` doesn't wrap the watchlists endpoint, so we hit `/trader/v1/accounts/{accountHash}/watchlists` through its authenticated `httpx` session, with a `watchlist_fallback.yaml` safety net.
4. Built and smoke-tested all 11 v1 files. Alert logic and email rendering verified with synthetic data.
5. Discovered this directory wasn't its own git repo — it was a subfolder of a parent monorepo at `C:\Users\harsmith\Documents\GitHub\`. Initialized a fresh repo here.
6. Deployed to `hvs338/stock-watchlist-alerts` (private), with local git author pinned to the GitHub no-reply pattern for hvs338. Restored `gh` active account to `harsmith_deloitte` after.
7. Wrote `ROADMAP.md` covering v2 (weekly Kronos recap) and v3 (top-5 movers analysis), with decisions captured from a clarifying-questions round (GPU/CPU switchable, Friday-after-close timing, Google Custom Search, Claude Sonnet 4.6).
8. User provided a hand-designed HTML email mockup (`email_example.html`). Converted it into a Jinja template (`templates/digest.html.j2`), refactored `email_sender.py` to render via Jinja, added company-name extraction to `alert_rules.py`. Smoke-tested with realistic fake data and edge cases.
9. Pushed everything. Harness blocked direct-to-`main` push initially; second attempt with approval succeeded.

### Decisions made (with rationale)

| Area | Decision | Why |
| --- | --- | --- |
| Watchlist source | Schwab Trader API primary, YAML fallback | Real watchlist data + safety net for Schwab outages or app-approval delays |
| Email delivery | Gmail SMTP + app password | Free, no third-party signup, stdlib only |
| Scheduler | Windows Task Scheduler | Solo personal machine; no daemon to babysit |
| SQLite history | Deferred to v2 | v1 ships sooner; v2 introduces SQLite for volume averages + Kronos forecast persistence |
| Email design | Jinja2 template (`templates/digest.html.j2`) | User invested real design effort; Jinja lets them edit HTML without touching Python |
| Kronos compute | Auto-detect GPU vs CPU at runtime | User has two laptops (RTX 3070 on one, CPU on the other); same code works on both |
| Weekly recap timing | Friday after close — week-in-review + week-ahead | Combines forecast accuracy retrospective with forward-looking outlook |
| News source for top-5 | Google Custom Search API | Free 100/day tier covers 5/day with 20× headroom; structured JSON results |
| LLM summarization | Claude API (`claude-sonnet-4-6`) | ~$0.40/year cost; user already on Anthropic ecosystem |

### What to do next to actually start using v1

1. Register Schwab developer app at <https://developer.schwab.com>. Callback URL: `https://127.0.0.1:8182`. Wait for approval (same-day to a few days).
2. Generate a Gmail app password at <https://myaccount.google.com/apppasswords> (requires 2-Step Verification enabled).
3. In a venv inside the repo: `pip install -r requirements.txt`.
4. `Copy-Item .env.example .env`, fill in the 5 secrets.
5. `python main.py --auth` — browser flow opens, writes `token.json`.
6. `python main.py --dry-run` — verify digest looks right in stdout.
7. `python main.py` — sends real digest.
8. Set up Windows Task Scheduler entry (see `README.md` → "Schedule with Windows Task Scheduler").
9. **Every 7 days**, re-run `--auth` when Schwab's refresh token expires.

### What's queued for next session

Per `ROADMAP.md`, recommended order is **v3 first, then v2**:

#### v3 — Top-5 daily movers analysis (do this first)
- Smaller scope, no ML deps. Validates the feature-flag extension pattern in `main.py` and `email_sender.py`.
- Setup needed:
  - Google Cloud project → enable Custom Search API → generate API key
  - Programmable Search Engine at <https://programmablesearchengine.google.com> → get `cx`
  - Anthropic API key from <https://console.anthropic.com>
- New files: `news_scraper.py`, `claude_summarizer.py`
- Modify: `email_sender.py` (add `build_top_movers_html`), `main.py` (feature-flag the path)
- Estimated effort: 2–3 hours

#### v2 — Weekly Kronos recap (do this second)
- Larger lift. Introduces SQLite, torch, the Kronos vendor tree, a second entry point + Task Scheduler job.
- Setup needed:
  - On RTX 3070 laptop: `pip install torch --index-url https://download.pytorch.org/whl/cu121`
  - On CPU laptop: `pip install torch` (CPU wheels)
  - Decide: git-submodule the Kronos repo under `vendor/Kronos`, or PYTHONPATH-include the existing clone at `C:\Users\harsmith\Documents\GitHub\Kronos`
- SQLite schema already designed in `ROADMAP.md`
- Estimated effort: full day

### Gotchas to remember

- **Schwab refresh tokens expire every 7 days.** The daily run will fail loudly until `python main.py --auth` is re-run. The error message in `digest.log` points to this.
- **Two GitHub accounts on this machine.** `harsmith_deloitte` (work, default) and `hvs338` (personal). Local git author for this repo is pinned to `hvs338 <79503690+hvs338@users.noreply.github.com>` (no-reply pattern keeps personal email private). To push: `gh auth switch --user hvs338` → push → `gh auth switch --user harsmith_deloitte`.
- **The Claude Code harness blocks direct pushes to `main`** even on personal repos. Approval prompt fires; just approve. Can be allowlisted permanently via `.claude/settings.json` if it becomes annoying.
- **This directory is its own git repo** despite being inside the parent `C:\Users\harsmith\Documents\GitHub\` which is also a git repo. The inner `.git` wins for git operations run from this folder. Confirmed with `git rev-parse --show-toplevel`.
- **Kronos isn't on PyPI.** When implementing v2, plan to either git-submodule it under `vendor/Kronos` (preferred — versionable, survives fresh clones) or add `C:\Users\harsmith\Documents\GitHub\Kronos` to PYTHONPATH (faster start, less portable).
- **`schwab-py` doesn't wrap the watchlists endpoint.** We hit `/trader/v1/accounts/{accountHash}/watchlists` through its authenticated `httpx` session directly. Documented in `schwab_client.py`.
- **Quote field names from Schwab aren't 100% guaranteed stable across asset types.** `alert_rules.extract_quote_fields` reads them defensively with `.get()` and multiple fallback paths.
- **Email rendering uses UTF-8 explicitly** (`MIMEText(..., "utf-8")`) — required because the design uses em-dashes, true minus signs (U+2212), and arrows (▲ ▼).

### File tour

| Path | Purpose |
| --- | --- |
| `main.py` | Entry point: `--auth` / `--dry-run` / default (send) |
| `auth.py` | Schwab OAuth via schwab-py — interactive flow and token-file load |
| `schwab_client.py` | Watchlist fetch (raw HTTP for `/watchlists`), quote fetch via `get_quotes` |
| `alert_rules.py` | `Flag` dataclass, `extract_quote_fields`, `evaluate` |
| `email_sender.py` | Jinja-based renderer, pill/color/distance helpers, SMTP send |
| `templates/digest.html.j2` | Newspaper-style HTML template (Georgia serif, masthead, summary strip, flagged cards, full watchlist, dark mode, mobile-responsive) |
| `email_example.html` | Static design reference (frozen mockup; do not modify) |
| `config.py` | `.env` loader + thresholds + cred validators |
| `watchlist_fallback.yaml` | Used if Schwab watchlist endpoint fails |
| `ROADMAP.md` | v2 + v3 plans, in execution detail |
| `README.md` | Setup + Task Scheduler instructions |
| `requirements.txt` | schwab-py, python-dotenv, pyyaml, jinja2 |
| `.env.example` | Template — copy to `.env` and fill in |
| `.gitignore` | token.json, .env, digest.log, rendered_preview.html, `__pycache__` |
| `docs/session-log.md` | This file |

### Quick commands

```powershell
# Preview the email template with sample data
# (build a small fake-data script that calls email_sender.build_digest_html)

# Push to GitHub (must switch accounts first; switch back after)
gh auth switch --user hvs338
git push origin main
gh auth switch --user harsmith_deloitte

# Re-authenticate Schwab (every 7 days)
python main.py --auth

# Daily smoke test (no email sent)
python main.py --dry-run

# Send the real digest
python main.py
```

### Notes on the email design

Your `email_example.html` is preserved as a frozen visual reference. The live template at `templates/digest.html.j2` is derived from it with these substitutions:

| Region | Jinja variables |
| --- | --- |
| Masthead date | `{{ weekday }}`, `{{ date_full_nbsp }}` |
| "Alerts Today" | `{{ alert_count }}` |
| "Watchlist Avg" | `{{ avg_pct_text }}` colored by `{{ avg_pct_color }}` |
| "Symbols Tracked" | `{{ symbol_count }}` |
| Flagged rows | `{% for row in flagged_rows %}` — each card has symbol/name/pills/price/pct/range/distance |
| Pills | `{% for pill in row.pills %}` — color/bg/border/text mapped from `Flag.code` via `PILL_STYLES` in `email_sender.py` |
| Right-side distance line | "X.X% below high" / "X.X% above low" / "threshold ±N.N%" — priority order in `_distance_text()` |
| Full watchlist | `{% for row in all_rows %}` |
| Threshold note | reads `config.PCT_CHANGE_ALERT` and `config.NEAR_52W_HIGH_PCT` live |

If the design needs tweaks, edit the `.j2` template directly. No Python changes needed unless surfacing a new data field.

---

## Template for future entries

When picking back up, add a new entry at the top following this skeleton:

```markdown
## YYYY-MM-DD — <one-line summary>

### TL;DR
<one paragraph>

### What changed
<bullet list — commits, files, decisions>

### Decisions made
<table or list with rationale>

### Next session
<what to do next>

### Gotchas added
<anything new to remember>
```
