"""Live account-side API checks: account numbers, watchlists, positions.

These are read-only — no orders are placed.
"""

import pytest

import src.schwab_client as sc


pytestmark = pytest.mark.live


def test_get_account_numbers_returns_at_least_one(schwab_client):
    resp = schwab_client.get_account_numbers()
    assert resp.status_code == 200, (
        f"get_account_numbers returned {resp.status_code}: {resp.text[:300]}"
    )
    accounts = resp.json()
    assert isinstance(accounts, list)
    assert len(accounts) >= 1, "Expected at least one linked Schwab account"
    first = accounts[0]
    assert "hashValue" in first, f"Account record missing hashValue: {first}"
    assert "accountNumber" in first, f"Account record missing accountNumber: {first}"


def test_get_watchlist_symbols_returns_non_empty(schwab_client):
    """Loads symbols via schwab_client.get_watchlist_symbols.

    Schwab has no watchlists endpoint (dropped in the TD Ameritrade migration), so
    this reads watchlist_fallback.yaml — the test passes as long as it returns a
    non-empty list of clean symbol strings.
    """
    symbols = sc.get_watchlist_symbols(schwab_client)
    assert isinstance(symbols, list)
    assert len(symbols) > 0, (
        "Got zero symbols. Either the Schwab watchlist endpoint returned empty AND "
        "the fallback yaml has no symbols. Check WATCHLIST_NAME and watchlist_fallback.yaml."
    )
    for s in symbols:
        assert isinstance(s, str)
        assert s.strip() == s, f"Symbol has leading/trailing whitespace: {s!r}"


def test_get_positions_returns_dict(schwab_client):
    """get_positions can legitimately return {} (no positions, or position fetch
    failed silently). Shape check only — the digest renders fine in either case."""
    positions = sc.get_positions(schwab_client)
    print(f"Positions: {positions}")
    assert isinstance(positions, dict)
    for symbol, pos in positions.items():
        assert isinstance(symbol, str) and symbol, f"Bad symbol key: {symbol!r}"
        assert isinstance(pos, dict)
        # Quantity is the one field we'll always render — verify it's present.
        assert "quantity" in pos, f"{symbol}: position missing 'quantity' field: {pos}"


def test_account_with_positions_field_returns_securities_account(schwab_client):
    """Direct API check on the get_account(..., fields=POSITIONS) path that
    get_positions wraps. Confirms the response shape get_positions relies on."""
    from schwab.client import Client

    acct_resp = schwab_client.get_account_numbers()
    accounts = acct_resp.json()
    print(f"Accounts: {accounts}")
    if not accounts:
        pytest.skip("No accounts linked")

    print(f"Accounts: {accounts}")
    account_hash = accounts[0]["hashValue"]

    resp = schwab_client.get_account(
        account_hash, fields=Client.Account.Fields.POSITIONS
    )
    assert resp.status_code == 200, (
        f"get_account returned {resp.status_code}: {resp.text[:300]}"
    )
    payload = resp.json()
    assert "securitiesAccount" in payload, (
        f"Expected 'securitiesAccount' in response; got keys {list(payload.keys())}"
    )
    sec_account = payload["securitiesAccount"]
    # 'positions' may be missing if the account is empty — that's a valid shape.
    if "positions" in sec_account:
        assert isinstance(sec_account["positions"], list)
