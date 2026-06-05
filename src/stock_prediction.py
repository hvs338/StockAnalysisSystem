"""Kronos daily-OHLCV forecasting, sourced from the Schwab API.

Pulls ~400 daily bars for a symbol from Schwab PriceHistory, feeds them to the Kronos
foundation model, and forecasts the next few trading days. Reuses the project's existing
Schwab auth (`auth.get_client`) and client wrapper (`schwab_client.get_daily_candles`).

Run the data pull alone (no torch / model download needed):
    python stock_prediction.py --symbol SOFI --data-only

Full forecast (requires `pip install -r requirements-kronos.txt`; first run downloads
the Kronos weights from Hugging Face; auto-uses CUDA on the RTX 3070):
    python stock_prediction.py --symbol SOFI --plot

Heavy deps (torch, Kronos, matplotlib) are imported lazily so --data-only stays light.
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import src.auth as auth
import src.config as config
import src.schwab_client as schwab_client

log = logging.getLogger("stock_prediction")

# Columns Kronos expects as model input.
FEATURE_COLS = ["open", "high", "low", "close", "volume", "amount"]

# Kronos Hugging Face model ids, picked by device when KRONOS_MODEL=auto.
_GPU_MODEL = "NeoQuasar/Kronos-base"    # 102M params — higher quality, needs a GPU
_CPU_MODEL = "NeoQuasar/Kronos-small"   # 24.7M params — CPU-friendly fallback
_TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"  # shared by both models


def _resolve_device(setting: str) -> str:
    """Map KRONOS_DEVICE (auto|cuda|cpu) to a concrete torch device string."""
    import torch

    setting = (setting or "auto").strip().lower()
    if setting == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    if setting == "cuda":
        return "cuda:0"
    return setting  # "cpu" or an explicit "cuda:N"


def load_kronos_predictor(
    model_name: str | None = None,
    tokenizer_name: str = _TOKENIZER,
    max_context: int = 512,
):
    """Load the Kronos tokenizer + model and wrap them in a predictor.

    Device and model are chosen from config (KRONOS_DEVICE / KRONOS_MODEL), which both
    default to ``auto``: CUDA → Kronos-base (uses the RTX 3070), CPU → Kronos-small.
    An explicit ``model_name`` arg or a non-auto KRONOS_MODEL overrides the auto pick.
    Imported lazily so the --data-only path doesn't require torch.
    """
    # Ensure both the repo root AND the vendored Kronos/ dir are importable: the package's
    # internal `from model.module import *` needs Kronos/ itself on sys.path.
    root = Path(__file__).resolve().parent
    for p in (str(root), str(root / "Kronos")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from Kronos.model import Kronos, KronosTokenizer, KronosPredictor

    device = _resolve_device(config.KRONOS_DEVICE)
    on_gpu = device.startswith("cuda")

    # Precedence: explicit arg → KRONOS_MODEL env (if not "auto") → device-based default.
    if model_name is None:
        env_model = config.KRONOS_MODEL
        if env_model and env_model.lower() != "auto":
            model_name = env_model
        else:
            model_name = _GPU_MODEL if on_gpu else _CPU_MODEL

    log.info(f"Loading Kronos tokenizer={tokenizer_name} model={model_name} on {device} …")
    tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
    model = Kronos.from_pretrained(model_name)
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=max_context)
    log.info(f"Kronos ready on device: {predictor.device}")
    return predictor


def load_history(client, symbol: str, lookback: int = 400) -> pd.DataFrame:
    """Fetch daily OHLCV from Schwab and return the last `lookback` bars as a DataFrame
    with columns [timestamps, open, high, low, close, volume, amount], oldest first."""
    candles = schwab_client.get_daily_candles(client, symbol)
    if not candles:
        raise RuntimeError(
            f"Schwab returned no daily candles for {symbol!r}. Check the symbol and that "
            f"the token is valid (python main.py --auth)."
        )
    df = pd.DataFrame(candles).sort_values("datetime").reset_index(drop=True)
    # Schwab gives OHLCV but not 'amount' (dollar volume); Kronos uses it. .assign returns
    # a fresh frame, sidestepping chained-assignment warnings.
    df = df.assign(
        timestamps=pd.to_datetime(df["datetime"], unit="ms"),
        amount=df["close"].astype(float) * df["volume"].astype(float),
    )
    df = df[["timestamps", *FEATURE_COLS]]
    if len(df) > lookback:
        df = df.tail(lookback).reset_index(drop=True)
    log.info(f"{symbol}: using {len(df)} daily bars "
             f"({df['timestamps'].iloc[0].date()} → {df['timestamps'].iloc[-1].date()}).")
    return df


def build_inputs(df: pd.DataFrame, pred_len: int = 5):
    """Split a history DataFrame into Kronos predict() inputs.

    Returns (x_df, x_timestamp, y_timestamp) where y_timestamp is the next `pred_len`
    business days after the last historical bar.
    """
    x_df = df[FEATURE_COLS].copy()
    x_timestamp = df["timestamps"]
    last_date = x_timestamp.iloc[-1]
    y_timestamp = pd.Series(pd.bdate_range(start=last_date, periods=pred_len + 1)[1:])
    return x_df, x_timestamp, y_timestamp


def predict_samples(predictor, x_df, x_timestamp, y_timestamp, pred_len: int = 5,
                    n_samples: int | None = None, T: float | None = None,
                    top_p: float | None = None) -> np.ndarray:
    """Draw ``n_samples`` independent Kronos forecast paths.

    Kronos is generative, so a single path swings wildly over a multi-day horizon. We run
    the paths as **one batched GPU pass**: predict_batch replicates the series across the
    batch dimension with ``sample_count=1``, so each batch row samples its own stochastic
    path (no internal averaging). Because the autoregressive loop is parallel across the
    batch, N paths cost roughly the same wall time as one.

    Returns an array shaped ``(n_samples, pred_len, len(FEATURE_COLS))``.
    """
    n = config.KRONOS_SAMPLES if n_samples is None else n_samples
    pred_dfs = predictor.predict_batch(
        df_list=[x_df] * n,
        x_timestamp_list=[x_timestamp] * n,
        y_timestamp_list=[y_timestamp] * n,
        pred_len=pred_len,
        T=config.KRONOS_TEMPERATURE if T is None else T,
        top_p=config.KRONOS_TOP_P if top_p is None else top_p,
        sample_count=1,
        verbose=True,
    )
    return np.stack([d[FEATURE_COLS].values for d in pred_dfs])


def summarize_band(samples: np.ndarray, y_timestamp) -> list[dict]:
    """Collapse sampled paths into a per-day p10/p50/p90 band.

    ``samples`` is ``(n_samples, pred_len, len(FEATURE_COLS))``. The p50 (median) is the
    central OHLCV estimate; ``close_p10``/``close_p90`` bound the close so downstream
    consumers can reason about the forecast's spread instead of a single noisy point.
    """
    p10, p50, p90 = (np.percentile(samples, q, axis=0) for q in (10, 50, 90))
    ci = FEATURE_COLS.index("close")
    rows = []
    for t, ts in enumerate(y_timestamp):
        rows.append({
            "date": ts.date().isoformat(),
            "open": float(p50[t, 0]),
            "high": float(p50[t, 1]),
            "low": float(p50[t, 2]),
            "close": float(p50[t, 3]),
            "volume": float(p50[t, 4]),
            "close_p10": float(p10[t, ci]),
            "close_p90": float(p90[t, ci]),
        })
    return rows


def plot_prediction(history_df: pd.DataFrame, pred_df: pd.DataFrame, out_path: str) -> None:
    """Save a close+volume chart of history vs. forecast to `out_path` (PNG)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hist = history_df.set_index("timestamps")
    pred = pred_df.copy()
    pred.index = pd.Index(
        pd.bdate_range(start=hist.index[-1], periods=len(pred) + 1)[1:], name="timestamps"
    )

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    ax1.plot(hist.index, hist["close"], label="History", color="#1f4d33", linewidth=1.4)
    ax1.plot(pred.index, pred["close"], label="Forecast", color="#b4453f", linewidth=1.8)
    ax1.set_ylabel("Close")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    ax2.plot(hist.index, hist["volume"], label="History", color="#1f4d33", linewidth=1.0)
    ax2.plot(pred.index, pred["volume"], label="Forecast", color="#b4453f", linewidth=1.4)
    ax2.set_ylabel("Volume")
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved forecast chart → {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kronos daily forecast from Schwab data")
    parser.add_argument("--symbol", default="SOFI", help="Ticker to forecast")
    parser.add_argument("--lookback", type=int, default=400, help="Daily bars of context")
    parser.add_argument("--pred-len", type=int, default=5, help="Trading days to forecast")
    parser.add_argument(
        "--data-only", action="store_true",
        help="Fetch + print the Schwab history and exit (skips Kronos / no torch needed)",
    )
    parser.add_argument("--samples", type=int, default=config.KRONOS_SAMPLES,
                        help="Independent paths to draw for the p10/p50/p90 band")
    parser.add_argument("--temperature", type=float, default=config.KRONOS_TEMPERATURE,
                        help="Sampling temperature (lower = more conservative)")
    parser.add_argument("--top-p", type=float, default=config.KRONOS_TOP_P,
                        help="Nucleus sampling cutoff (lower trims fat tails)")
    parser.add_argument("--plot", action="store_true", help="Save forecast.png")
    parser.add_argument("--out", default="forecast.png", help="Chart output path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    client = auth.get_client()
    df = load_history(client, args.symbol, lookback=args.lookback)

    if args.data_only:
        print(df.tail(10).to_string(index=False))
        print(f"\n{len(df)} bars fetched for {args.symbol}. (--data-only: skipping Kronos)")
        return

    predictor = load_kronos_predictor(max_context=512)
    x_df, x_timestamp, y_timestamp = build_inputs(df, pred_len=args.pred_len)
    samples = predict_samples(
        predictor, x_df, x_timestamp, y_timestamp, pred_len=args.pred_len,
        n_samples=args.samples, T=args.temperature, top_p=args.top_p,
    )
    band = summarize_band(samples, y_timestamp)
    band_df = pd.DataFrame(band)

    last_close = float(df["close"].iloc[-1])
    print(f"\n{args.symbol} — forecast next {args.pred_len} trading days "
          f"({args.samples} samples, last close {last_close:.2f}):")
    table = band_df[["date", "close_p10", "close", "close_p90", "volume"]].rename(
        columns={"close": "close_p50"})
    print(table.to_string(index=False))

    if args.plot:
        plot_prediction(df, band_df, args.out)


if __name__ == "__main__":
    main()
