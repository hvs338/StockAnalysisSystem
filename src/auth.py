import os
import logging

from schwab import auth as schwab_auth

import src.config as config

log = logging.getLogger(__name__)


def get_client():
    """Load an authenticated Schwab client from the saved token file.

    Fails fast (no browser) if the token is missing or unrefreshable — the daily
    scheduled run must be non-interactive.
    """
    config.require_schwab_creds()
    if not os.path.exists(config.TOKEN_PATH):
        raise RuntimeError(
            f"No Schwab token at {config.TOKEN_PATH}. Run: python main.py --auth"
        )
    try:
        return schwab_auth.client_from_token_file(
            token_path=config.TOKEN_PATH,
            api_key=config.SCHWAB_CLIENT_ID,
            app_secret=config.SCHWAB_CLIENT_SECRET,
        )
    except Exception as e:
        raise RuntimeError(
            "Failed to load Schwab client. Schwab refresh tokens expire every 7 days — "
            "if it's been a week since you last authenticated, that's why. "
            f"Run: python main.py --auth. Underlying error: {e}"
        ) from e


def run_auth_flow():
    """Interactive OAuth via schwab-py's manual flow. Prints an authorization URL,
    you log in and click Allow, then paste the redirected callback URL back at the
    prompt. Run on day one and every 7 days when Schwab's refresh token expires.

    We use client_from_manual_flow (not easy_client/login_flow) because the
    browser-assisted flow spins up a local HTTPS redirect server in a child process
    (multiprocess + psutil), which is fragile on Windows / Python 3.14 and was failing
    here with RedirectServerExitedError. The manual flow needs no local server.
    """
    config.require_schwab_creds()
    client = schwab_auth.client_from_manual_flow(
        api_key=config.SCHWAB_CLIENT_ID,
        app_secret=config.SCHWAB_CLIENT_SECRET,
        callback_url=config.SCHWAB_CALLBACK_URL,
        token_path=config.TOKEN_PATH,
    )
    print(f"Auth OK. Token saved to {config.TOKEN_PATH}.")
    return client
