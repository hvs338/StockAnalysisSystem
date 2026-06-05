"""Batch Kronos forecaster: forecast the top-N watchlist movers and write the results to
forecasts/<SYMBOL>.json for the (later) Bedrock analysis step to consume.

Runs on the machine with the ML stack (the RTX 3070 laptop):
    pip install -r requirements-kronos.txt
    python kronos_runner.py --top 5

Pick symbols explicitly instead of the top movers:
    python kronos_runner.py --symbols SOFI,TSLA,NVDA

The Kronos model (torch) is imported lazily inside stock_prediction.load_kronos_predictor,
so symbol selection + Schwab history fetch work even without torch installed.
"""

import argparse
import json
import logging
from datetime import datetime, timezone

import src.alert_rules as alert_rules
import src.auth as auth
import src.config as config
import src.schwab_client as schwab_client
import src.stock_prediction as sp

log = logging.getLogger("kronos_runner")


def pick_top_movers(client, top: int) -> list[str]:
    """Return the watchlist's top-N symbols by absolute % change today."""
    symbols = schwab_client.get_watchlist_symbols(client)
    quotes = schwab_client.get_quotes(client, symbols)
    scored = []
    for symbol, data in quotes.items():
        if not isinstance(data, dict):
            continue
        pct = alert_rules.extract_quote_fields(data).get("pct_change")
        if pct is not None:
            scored.append((abs(pct), symbol))
    scored.sort(reverse=True)
    movers = [sym for _, sym in scored[:top]]
    log.info(f"Top {len(movers)} watchlist movers: {', '.join(movers)}")
    return movers


def forecast_symbol(predictor, client, symbol: str, lookback: int, pred_len: int,
                    n_samples: int, T: float, top_p: float) -> dict:
    """Run a Kronos forecast for one symbol and return a JSON-serializable record.

    The forecast carries a per-day p10/p50/p90 band (see stock_prediction.summarize_band)
    rather than a single noisy path, so the downstream analysis can weigh the spread.
    """
    df = sp.load_history(client, symbol, lookback=lookback)
    x_df, x_ts, y_ts = sp.build_inputs(df, pred_len=pred_len)
    samples = sp.predict_samples(predictor, x_df, x_ts, y_ts, pred_len=pred_len,
                                 n_samples=n_samples, T=T, top_p=top_p)
    forecast = sp.summarize_band(samples, y_ts)
    return {
        "symbol": symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_last_date": df["timestamps"].iloc[-1].date().isoformat(),
        "last_close": float(df["close"].iloc[-1]),
        "pred_len": pred_len,
        "n_samples": n_samples,
        "T": T,
        "top_p": top_p,
        "forecast": forecast,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch Kronos forecaster → forecasts/*.json")
    parser.add_argument("--symbols", help="Comma-separated tickers (overrides --top)")
    parser.add_argument("--top", type=int, default=10, help="Forecast the top-N watchlist movers")
    parser.add_argument("--lookback", type=int, default=400, help="Daily bars of context")
    parser.add_argument("--pred-len", type=int, default=5, help="Trading days to forecast")
    parser.add_argument("--samples", type=int, default=config.KRONOS_SAMPLES,
                        help="Independent paths to draw for the p10/p50/p90 band")
    parser.add_argument("--temperature", type=float, default=config.KRONOS_TEMPERATURE,
                        help="Sampling temperature (lower = more conservative)")
    parser.add_argument("--top-p", type=float, default=config.KRONOS_TOP_P,
                        help="Nucleus sampling cutoff (lower trims fat tails)")
    parser.add_argument("--out-dir", default="forecasts", help="Output directory")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    client = auth.get_client()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = pick_top_movers(client, args.top)
    if not symbols:
        raise SystemExit("No symbols to forecast.")

    out_dir = config.PROJECT_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    predictor = sp.load_kronos_predictor()

    written: list[str] = []
    for symbol in symbols:
        try:
            record = forecast_symbol(predictor, client, symbol, args.lookback, args.pred_len,
                                     args.samples, args.temperature, args.top_p)
        except Exception as e:  # one bad ticker shouldn't sink the batch
            log.warning(f"Skipping {symbol}: {type(e).__name__}: {e}")
            continue
        path = out_dir / f"{symbol}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        written.append(symbol)
        fc = record["forecast"][-1]
        log.info(f"Wrote {path.name} (last_close {record['last_close']:.2f} → "
                 f"{fc['close']:.2f} [{fc['close_p10']:.2f}–{fc['close_p90']:.2f}] "
                 f"in {args.pred_len}d)")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pred_len": args.pred_len,
        "n_samples": args.samples,
        "symbols": written,
    }
    (out_dir / "index.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info(f"Done. {len(written)}/{len(symbols)} forecasts in {out_dir}.")


if __name__ == "__main__":
    main()
