"""Live auth checks. Verify the saved token loads into a usable schwab-py client.

Failure modes these catch:
  * Token file missing / unparseable.
  * Refresh token expired (Schwab refresh tokens die after 7 days).
  * Credentials in .env don't match the developer-app registration.
"""

import os

import pytest

import config as config


pytestmark = pytest.mark.live


def test_token_file_present():
    """Sanity precondition for every other live test."""
    assert os.path.exists(config.TOKEN_PATH), (
        f"No Schwab token at {config.TOKEN_PATH}. Run `python main.py --auth`."
    )


def test_get_client_returns_object(schwab_client):
    """auth.get_client returned something with the schwab-py client surface."""
    assert schwab_client is not None
    # schwab-py exposes get_quotes / get_account_numbers — minimal surface check.
    assert hasattr(schwab_client, "get_quotes")
    assert hasattr(schwab_client, "get_account_numbers")
    assert hasattr(schwab_client, "get_account")
    assert hasattr(schwab_client, "get_movers")


def test_client_can_make_authenticated_call(schwab_client):
    """Smallest possible end-to-end check: an authenticated GET that returns 200."""
    resp = schwab_client.get_account_numbers()
    assert resp.status_code == 200, (
        f"get_account_numbers returned {resp.status_code}. "
        f"If 401, the refresh token has expired — re-run `python main.py --auth`. "
        f"Body: {resp.text[:300]}"
    )
    payload = resp.json()
    assert isinstance(payload, list), f"Expected a list of accounts, got {type(payload).__name__}"
