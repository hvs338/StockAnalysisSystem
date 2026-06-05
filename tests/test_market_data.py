"""Live market-data API checks: /quotes and /movers.

The /quotes test pins on known-liquid symbols (AAPL/MSFT/SPY) rather than the
user's actual watchlist, so a misconfigured WATCHLIST_NAME or empty watchlist
doesn't take this suite down.

Module-level imports use `as sc` to avoid colliding with the `schwab_client`
pytest fixture name.
"""

import pytest

import src.alert_rules as alert_rules
import src.schwab_client as sc


pytestmark = pytest.mark.live


# ----------------------------- /quotes ----------------------------------------

def test_get_quotes_returns_dict_keyed_by_symbol(schwab_client, known_liquid_symbols):
    quotes_json = sc.get_quotes(schwab_client, known_liquid_symbols)
    assert isinstance(quotes_json, dict)
    for symbol in known_liquid_symbols:
        assert symbol in quotes_json, (
            f"Expected {symbol} in /quotes response; got keys {list(quotes_json.keys())}"
        )
        assert isinstance(quotes_json[symbol], dict)


def test_extract_quote_fields_finds_required_fields(schwab_client, known_liquid_symbols):
    """Run extract_quote_fields against real Schwab quote responses for at least
    one liquid symbol — confirms the field-path fallbacks in alert_rules still
    match Schwab's current schema."""
    quotes_json = sc.get_quotes(schwab_client, known_liquid_symbols)

    found_clean = False
    for symbol in known_liquid_symbols:
        data = quotes_json.get(symbol)
        if not isinstance(data, dict):
            continue
        fields = alert_rules.extract_quote_fields(data)
        if (
            fields.get("last") is not None
            and fields.get("high_52") is not None
            and fields.get("low_52") is not None
        ):
            found_clean = True
            assert fields["last"] > 0, f"{symbol} last price should be positive"
            assert fields["high_52"] >= fields["low_52"], (
                f"{symbol} 52w high {fields['high_52']} < 52w low {fields['low_52']}"
            )
            break

    assert found_clean, (
        "None of the liquid symbols returned a full set of "
        "(last, 52w high, 52w low) — Schwab's quote schema may have shifted "
        "and alert_rules.extract_quote_fields needs updated field paths."
    )


# ----------------------------- /movers ----------------------------------------

@pytest.mark.parametrize("index_string,expected_value", [
    ("$SPX", "$SPX"),
    ("SPX", "$SPX"),
    ("spx", "$SPX"),
    ("$DJI", "$DJI"),
    ("NASDAQ", "NASDAQ"),
    ("COMPX", "$COMPX"),
    ("EQUITY_ALL", "EQUITY_ALL"),
])
def test_resolve_movers_index_maps_user_strings(index_string, expected_value):
    """Unit-level — doesn't hit the network. Verifies the env-var → enum mapping
    that protects us from schwab-py's enforce_enums=True default."""
    resolved = sc._resolve_movers_index(index_string)
    actual = resolved.value if hasattr(resolved, "value") else resolved
    assert actual == expected_value


def test_get_movers_returns_two_lists(schwab_client):
    """Fetch $SPX top movers. Tolerates empty results (pre-market / closed days
    still respond OK with empty screener)."""
    up, down = sc.get_movers(schwab_client, "$SPX", 5)
    assert isinstance(up, list)
    assert isinstance(down, list)
    assert len(up) <= 5
    assert len(down) <= 5

    for sample in (up + down):
        assert isinstance(sample, dict)
        assert sample.get("symbol"), f"Mover entry missing symbol: {sample}"


def test_get_movers_with_unknown_index_degrades_gracefully(schwab_client):
    """Bad index string should hit our try/except and return ([], []) rather
    than propagating the underlying exception."""
    up, down = sc.get_movers(schwab_client, "BOGUS_INDEX_XYZ", 5)
    assert up == []
    assert down == []
