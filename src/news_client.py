"""Per-symbol financial news via the Finnhub company-news API, normalized for the AI analyst.

Finnhub aggregates many publishers (Yahoo Finance, MarketWatch, Reuters, Barron's, CNBC, …) and
returns a `source` per article, so we rank the user's preferred sources first and keep the rest.
Free tier (60 req/min) is far more than the digest needs (<=5 symbols/day).

Everything degrades gracefully: a missing key, timeout, or bad response yields an empty list — the
analyst simply runs with "None provided." rather than failing.

    GET https://finnhub.io/api/v1/company-news?symbol=MU&from=YYYY-MM-DD&to=YYYY-MM-DD&token=KEY
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx

import config as config

log = logging.getLogger("news_client")

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
_TIMEOUT = 8.0
_SUMMARY_MAX = 300


def _preferred_rank(source: str, preferred: list[str]) -> int:
    """0..N-1 for a preferred source (case-insensitive substring match), else len(preferred)."""
    s = (source or "").lower()
    for i, pref in enumerate(preferred):
        if pref.lower() in s:
            return i
    return len(preferred)


def _normalize(item: dict) -> dict | None:
    """Map a Finnhub company-news item to {title, source, url, summary, published, _ts}, or None.

    `_ts` (unix seconds, 0 if unknown) is kept only for sorting and dropped before returning.
    """
    title = (item.get("headline") or "").strip()
    if not title:
        return None
    try:
        ts = int(item.get("datetime") or 0)
    except (TypeError, ValueError):
        ts = 0
    try:
        published = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ""
    except (ValueError, OSError):
        published = ""
    return {
        "title": title,
        "source": (item.get("source") or "").strip(),
        "url": (item.get("url") or "").strip(),
        "summary": (item.get("summary") or "").strip(),
        "published": published,
        "_ts": ts,
    }


def fetch_news(symbol: str, lookback_days: int | None = None, max_items: int | None = None,
               preferred_sources: list[str] | None = None) -> list[dict]:
    """Recent news for one symbol, preferred sources first then most-recent. Never raises."""
    if not config.FINNHUB_API_KEY:
        log.info("FINNHUB_API_KEY not set — skipping news for %s.", symbol)
        return []

    lookback_days = lookback_days if lookback_days is not None else config.NEWS_LOOKBACK_DAYS
    max_items = max_items if max_items is not None else config.NEWS_MAX_ITEMS
    preferred = preferred_sources if preferred_sources is not None else config.NEWS_PREFERRED_SOURCES

    today = date.today()
    params = {
        "symbol": symbol.upper(),
        "from": (today - timedelta(days=lookback_days)).isoformat(),
        "to": today.isoformat(),
        "token": config.FINNHUB_API_KEY,
    }
    try:
        resp = httpx.get(FINNHUB_URL, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("News fetch failed for %s: %s: %s", symbol, type(e).__name__, e)
        return []
    if not isinstance(raw, list):
        log.warning("Unexpected news payload for %s: %r", symbol, type(raw))
        return []

    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        norm = _normalize(entry)
        if norm is None:
            continue
        key = norm["url"] or norm["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(norm)

    # Preferred sources first, then most-recent (largest timestamp) within each rank.
    items.sort(key=lambda it: (_preferred_rank(it["source"], preferred), -it["_ts"]))
    for it in items:
        it.pop("_ts", None)
    return items[:max_items]


def format_for_prompt(items: list[dict]) -> str:
    """Render the analyst prompt's NEWS slot. 'None provided.' when empty."""
    if not items:
        return "None provided."
    lines = []
    for it in items:
        summary = it["summary"]
        if len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX].rsplit(" ", 1)[0] + "…"
        meta = " ".join(p for p in (it["source"], it["published"]) if p)
        head = f"- [{meta}] {it['title']}" if meta else f"- {it['title']}"
        lines.append(f"{head} — {summary}" if summary else head)
    return "\n".join(lines)
