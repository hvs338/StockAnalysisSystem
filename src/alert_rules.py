from dataclasses import dataclass

import src.config as config


@dataclass
class Flag:
    code: str
    message: str


def extract_quote_fields(symbol_data: dict) -> dict:
    """Normalize Schwab's quote shape to the fields the alert rules need.

    Schwab's response structure differs across equity / option / mutual fund.
    Read defensively and fall back through several known paths.
    """
    quote = symbol_data.get("quote") or {}
    regular = symbol_data.get("regular") or {}
    reference = symbol_data.get("reference") or {}

    # Prefer the REGULAR-session fields over the realtime ones. The digest runs
    # pre-market (≈6 AM PT), where the realtime quote can carry pre-market trades or a
    # 0% change; the `regular` block holds the last completed regular session — i.e.
    # yesterday's close + its full-day % move, which is what this digest reports.
    last = (
        regular.get("regularMarketLastPrice")
        or quote.get("lastPrice")
        or quote.get("closePrice")
        or quote.get("mark")
    )
    pct_change = (
        regular.get("regularMarketPercentChange")
        if regular.get("regularMarketPercentChange") is not None
        else quote.get("netPercentChange")
        if quote.get("netPercentChange") is not None
        else quote.get("netPercentChangeInDouble")
    )
    volume = quote.get("totalVolume")
    high_52 = quote.get("52WeekHigh") or quote.get("highPrice52")
    low_52 = quote.get("52WeekLow") or quote.get("lowPrice52")

    return {
        "last": last,
        "pct_change": pct_change,
        "volume": volume,
        "high_52": high_52,
        "low_52": low_52,
        "name": reference.get("description") or "",
    }


def evaluate(fields: dict) -> list[Flag]:
    flags: list[Flag] = []
    last = fields.get("last")
    pct = fields.get("pct_change")
    high_52 = fields.get("high_52")
    low_52 = fields.get("low_52")

    if pct is not None:
        if pct >= config.PCT_CHANGE_ALERT:
            flags.append(Flag("BIG_MOVE_UP", f"+{pct:.2f}% (>= {config.PCT_CHANGE_ALERT}%)"))
        elif pct <= -config.PCT_CHANGE_ALERT:
            flags.append(Flag("BIG_MOVE_DOWN", f"{pct:.2f}% (<= -{config.PCT_CHANGE_ALERT}%)"))

    if last is not None and high_52:
        gap_pct = (high_52 - last) / high_52 * 100
        if 0 <= gap_pct <= config.NEAR_52W_HIGH_PCT:
            flags.append(
                Flag("NEAR_52W_HIGH", f"within {gap_pct:.2f}% of 52w high {high_52:.2f}")
            )

    if last is not None and low_52:
        gap_pct = (last - low_52) / low_52 * 100
        if 0 <= gap_pct <= config.NEAR_52W_LOW_PCT:
            flags.append(
                Flag("NEAR_52W_LOW", f"within {gap_pct:.2f}% of 52w low {low_52:.2f}")
            )

    return flags
