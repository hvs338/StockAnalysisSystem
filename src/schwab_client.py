import logging

import httpx
import yaml
from schwab.client import Client

import config as config

log = logging.getLogger(__name__)

# Per-call exceptions we expect from any Schwab fetch. Anything broader propagates.
_SCHWAB_FETCH_ERRORS = (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError)


def get_watchlist_symbols(client=None) -> list[str]:
    """Load the watchlist from watchlist_fallback.yaml.

    Schwab did NOT carry watchlists over from the TD Ameritrade API — there is no
    Schwab watchlist endpoint (the old /trader/v1/accounts/{hash}/watchlists path
    404s), so this YAML file is the source of truth. ``client`` is accepted but
    unused, kept so existing callers don't have to change.
    """
    path = config.WATCHLIST_FALLBACK_PATH
    if not path.exists():
        raise RuntimeError(
            f"Watchlist not found at {path}. Create it with a top-level 'symbols:' "
            f"list, e.g.:\n  symbols:\n    - AAPL\n    - MSFT"
        )
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    symbols = data.get("symbols")
    if isinstance(symbols, str):
        raise RuntimeError(
            f"'symbols' in {path} parsed as a string, not a list — each entry needs a "
            f"space after the dash ('- AAPL', not '-AAPL')."
        )
    symbols = [s.strip() for s in (symbols or []) if isinstance(s, str) and s.strip()]
    if not symbols:
        raise RuntimeError(f"Watchlist at {path} has no symbols.")

    # De-dupe while preserving order (the file may list the same ticker twice).
    seen: set[str] = set()
    deduped = [s for s in symbols if not (s in seen or seen.add(s))]
    log.info(f"Loaded {len(deduped)} symbols from {path}.")
    return deduped


def get_quotes(client, symbols: list[str]) -> dict:
    if len(symbols) > 500:
        log.warning(
            f"Watchlist has {len(symbols)} symbols; Schwab caps /quotes at ~500. "
            f"Truncating to first 500."
        )
        symbols = symbols[:500]
    resp = client.get_quotes(symbols=symbols)
    resp.raise_for_status()
    return resp.json()


def get_daily_candles(client, symbol: str, lookback_days: int = 600) -> list[dict]:
    """Fetch daily OHLCV candles for a single symbol from Schwab PriceHistory.

    Returns the raw `candles` list — each dict has open/high/low/close/volume and a
    `datetime` epoch-ms key — oldest first, as Schwab returns them. Kept pandas-free on
    purpose so importing this module for the daily digest pulls in no heavy deps; the
    Kronos pipeline (which already uses pandas) builds the DataFrame itself.

    `lookback_days` is calendar days; ~600 comfortably covers the ~400 trading bars
    Kronos wants. Raises on HTTP error (the caller decides how to handle it).
    """
    from datetime import datetime, timedelta

    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    resp = client.get_price_history_every_day(
        symbol, start_datetime=start, end_datetime=end
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    candles = payload.get("candles") or []
    log.info(f"Loaded {len(candles)} daily candles for {symbol}.")
    return candles


def _resolve_movers_index(index_value: str):
    """Map a user-facing index string (e.g. '$SPX', 'SPX', 'NASDAQ') to the
    Client.Movers.Index enum that schwab-py requires when enforce_enums=True.

    Returns the enum if matched; falls back to the raw string (which will pass
    only if the client was built with enforce_enums=False).
    """
    if not index_value:
        return Client.Movers.Index.SPX
    normalized = index_value.strip().lstrip("$").upper()
    for member in Client.Movers.Index:
        if member.name == normalized:
            return member
    return index_value


def get_movers(
    client, index: str, count_per_side: int = 5
) -> tuple[list[dict], list[dict]]:
    """Return (gainers, decliners) for an index from Schwab's /movers endpoint.

    Schwab's /movers returns the ~10 biggest movers of the day (both directions
    mixed); its ``sort`` param only reorders that same set — it does NOT separate
    gainers from decliners. So we fetch once and split by the sign of
    netPercentChange ourselves. Each side is capped at ``count_per_side`` and
    ordered by magnitude. Any failure logs a warning and returns ([], []) so the
    digest still ships.
    """
    resolved_index = _resolve_movers_index(index)
    try:
        items = _fetch_movers(client, resolved_index)
        gainers = sorted(
            (it for it in items if _mover_pct(it) > 0),
            key=_mover_pct, reverse=True,
        )[:count_per_side]
        decliners = sorted(
            (it for it in items if _mover_pct(it) < 0),
            key=_mover_pct,
        )[:count_per_side]
        log.info(
            f"Movers for {index}: {len(gainers)} gainers, {len(decliners)} decliners "
            f"(from {len(items)} returned)."
        )
        return gainers, decliners
    except _SCHWAB_FETCH_ERRORS as e:
        log.warning(
            f"Failed to load movers for {index} ({type(e).__name__}: {e}); "
            f"digest will render without movers section."
        )
        return [], []


def _mover_pct(item: dict) -> float:
    """netPercentChange as a float; /movers returns it as a fraction (0.0101)."""
    v = item.get("netPercentChange")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fetch_movers(client, index) -> list[dict]:
    resp = client.get_movers(index)
    resp.raise_for_status()
    payload = resp.json() or {}
    # Schwab returns {"screeners": [...]} but older docs use "movers".
    items = payload.get("screeners") or payload.get("movers") or []
    return [item for item in items if isinstance(item, dict) and item.get("symbol")]


def get_positions(client) -> dict[str, dict]:
    """Return {symbol: position_dict} for the first linked account.

    Position dict carries quantity, market value, and day P/L %. Any failure
    logs a warning and returns {} so the digest renders without held overlays.
    """
    try:
        acct_resp = client.get_account_numbers()
        acct_resp.raise_for_status()
        accounts = acct_resp.json() or []
        if not accounts:
            log.warning("No Schwab accounts returned; skipping positions overlay.")
            return {}
        account_hash = accounts[0]["hashValue"]

        resp = client.get_account(
            account_hash, fields=Client.Account.Fields.POSITIONS
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        sec_account = payload.get("securitiesAccount") or {}
        positions = sec_account.get("positions") or []

        out: dict[str, dict] = {}
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            instrument = pos.get("instrument") or {}
            symbol = instrument.get("symbol")
            if not symbol:
                continue
            long_qty = pos.get("longQuantity") or 0
            short_qty = pos.get("shortQuantity") or 0
            net_qty = long_qty - short_qty
            out[symbol] = {
                "quantity": net_qty,
                "long_quantity": long_qty,
                "short_quantity": short_qty,
                "market_value": pos.get("marketValue"),
                "day_pl_pct": pos.get("currentDayProfitLossPercentage"),
                "day_pl": pos.get("currentDayProfitLoss"),
            }
        log.info(f"Loaded {len(out)} positions from account {account_hash[:8]}…")
        return out

    except _SCHWAB_FETCH_ERRORS as e:
        log.warning(
            f"Failed to load positions ({type(e).__name__}: {e}); "
            f"digest will render without held overlay."
        )
        return {}
