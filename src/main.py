import argparse
import logging
import logging.handlers
import sys

import config as config
import auth as auth
import schwab_client as schwab_client
import alert_rules as alert_rules
import email_sender as email_sender


def setup_logging() -> None:
    log_path = config.PROJECT_ROOT / "digest.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])


def build_quote_rows(quotes_json: dict) -> list[dict]:
    rows = []
    for symbol, symbol_data in quotes_json.items():
        if not isinstance(symbol_data, dict):
            continue
        fields = alert_rules.extract_quote_fields(symbol_data)
        flags = alert_rules.evaluate(fields)
        rows.append({"symbol": symbol, **fields, "flags": flags})
    return rows


def _merge_positions_into_rows(rows: list[dict], positions: dict[str, dict]) -> None:
    for row in rows:
        row["position"] = positions.get(row["symbol"])


def cmd_auth() -> None:
    auth.run_auth_flow()


def cmd_run(dry_run: bool) -> None:
    log = logging.getLogger("main")
    client = auth.get_client()
    symbols = schwab_client.get_watchlist_symbols(client)
    log.info(f"Fetching quotes for {len(symbols)} symbols.")
    quotes = schwab_client.get_quotes(client, symbols)
    rows = build_quote_rows(quotes)
    flagged = sum(1 for r in rows if r["flags"])
    log.info(f"Evaluated {len(rows)} symbols; {flagged} flagged.")

    if config.INCLUDE_POSITIONS:
        positions = schwab_client.get_positions(client)
        _merge_positions_into_rows(rows, positions)
    else:
        _merge_positions_into_rows(rows, {})

    movers_up: list[dict] = []
    movers_down: list[dict] = []
    if config.INCLUDE_MOVERS:
        movers_up, movers_down = schwab_client.get_movers(
            client, config.MOVERS_INDEX, config.MOVERS_COUNT_PER_SIDE
        )

    forecasts: list[dict] = []
    forecast_meta: dict = {}
    if config.INCLUDE_FORECAST:
        # Read the latest Kronos forecasts written by the separate GPU job (kronos_runner).
        # Pure file read — no torch — so the daily digest stays light and CPU-friendly.
        forecasts, forecast_meta = email_sender.load_forecasts()
        log.info(f"Loaded {len(forecasts)} Kronos forecast(s) for the digest.")

    html = email_sender.build_digest_html(
        rows,
        movers_up=movers_up,
        movers_down=movers_down,
        movers_index_label=config.MOVERS_INDEX,
        forecasts=forecasts,
        forecast_meta=forecast_meta,
    )
    text = email_sender.build_digest_text(
        rows, movers_up=movers_up, movers_down=movers_down, forecasts=forecasts
    )
    subject = f"Schwab Watchlist — {flagged} flagged today"

    if dry_run:
        print(text)
        print()
        print(f"(dry-run: would send subject '{subject}' to {config.DIGEST_TO})")
        return

    email_sender.send_digest(html, text, subject)
    log.info(f"Digest sent to {config.DIGEST_TO}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Schwab daily watchlist digest")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--auth", action="store_true", help="Run interactive OAuth flow")
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print digest without sending email",
    )
    args = parser.parse_args()

    # Windows consoles default to cp1252, which can't encode the digest's true-minus
    # (−, U+2212) or ▲/▼ arrows — printing or logging them would crash. Force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    log = logging.getLogger("main")

    try:
        if args.auth:
            cmd_auth()
        else:
            cmd_run(dry_run=args.dry_run)
    except Exception as e:
        log.exception(f"Run failed: {e}")
        # On a real (non-interactive) run, an expired Schwab refresh token is the
        # expected weekly failure — email a re-auth nudge instead of failing silently.
        if not args.auth and not args.dry_run and _is_auth_failure(e):
            try:
                email_sender.send_reauth_reminder(e)
                log.info("Sent re-auth reminder email.")
            except Exception as mail_err:
                log.error(f"Could not send re-auth reminder email: {mail_err}")
        sys.exit(1)


def _is_auth_failure(e: Exception) -> bool:
    """True if an exception looks like a Schwab auth/token problem (vs. a transient
    network or data error), so we only send the re-auth nudge when it's warranted."""
    msg = str(e).lower()
    keywords = (
        "token", "refresh", "expired", "invalid_grant", "invalid_client",
        "unauthorized", "401",
    )
    if any(k in msg for k in keywords):
        return True
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None) in (401, 403)


if __name__ == "__main__":
    main()
