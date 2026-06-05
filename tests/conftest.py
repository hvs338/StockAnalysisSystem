"""Shared fixtures for the live Schwab integration tests.

These tests hit the real Schwab API. They require:
  * A populated .env with SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET.
  * A valid token.json (refreshed within the last 7 days).

Any of the above missing → the suite skips with an explanatory message instead
of failing loudly, so the test run can be invoked anywhere without pre-flight.

Run only the live suite:
    pytest -m live

Run everything (the default — the whole suite is live right now):
    pytest
"""

import os
import sys
from pathlib import Path

import pytest

# Make project modules importable without installing the package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import src.config as config  # noqa: E402
import src.auth as auth  # noqa: E402


def _missing_creds_reason() -> str | None:
    """Return a skip reason string if we can't authenticate, else None."""
    if not config.SCHWAB_CLIENT_ID or not config.SCHWAB_CLIENT_SECRET:
        return (
            "Schwab API credentials missing. Copy .env.example to .env and fill in "
            "SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET."
        )
    if not os.path.exists(config.TOKEN_PATH):
        return (
            f"No Schwab token at {config.TOKEN_PATH}. Run `python main.py --auth` first."
        )
    return None


@pytest.fixture(scope="session")
def schwab_client():
    """Authenticated schwab-py client. Skips the test if auth isn't usable."""
    skip = _missing_creds_reason()
    if skip:
        pytest.skip(skip)
    try:
        return auth.get_client()
    except RuntimeError as e:
        pytest.skip(f"Could not load Schwab client: {e}")


@pytest.fixture(scope="session")
def known_liquid_symbols() -> list[str]:
    """Symbols that should always return clean quote data — used to avoid coupling
    tests to whatever happens to be in the user's actual watchlist."""
    return ["AAPL", "MSFT", "SPY"]
