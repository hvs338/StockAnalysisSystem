"""AI stock analyst: synthesizes the Kronos forecast + price history (+ optional news) into a
weekly outlook and defined-risk option plays, via Claude on Amazon Bedrock (boto3).

Run one symbol (needs AWS creds + Bedrock model access):
    python src/stock_analysis.py --symbol MU

Build the prompt without calling Bedrock (no AWS needed — useful for iterating on inputs):
    python src/stock_analysis.py --symbol MU --dry-run

Bedrock model IDs carry an "anthropic." prefix (config.BEDROCK_MODEL, default
anthropic.claude-opus-4-8). News is an optional slot — the dedicated news pull is a later
roadmap step, so it defaults to "none provided" and the prompt handles its absence.
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import alert_rules
import auth
import config
import schwab_client

log = logging.getLogger("stock_analysis")

SYSTEM_PROMPT = """You are a disciplined equity and options analyst. Your job is to synthesize three inputs into a single, well-reasoned weekly outlook for one stock, then commit to a grade, a directional verdict, and a set of defined-risk options plays.

Inputs you will receive each run:

- {{TICKER}} — the stock symbol and company name.
- {{NEWS}} — recent news articles relevant to the company, sector, or macro environment.
- {{HISTORICAL}} — past price/volume data and any technical context provided.
- {{KRONOS_FORECAST}} — a one-week price prediction from an AI model called Kronos, including its predicted price path and any confidence/range information available.
- {{CURRENT_PRICE}} and {{AS_OF_DATE}} — the spot price and the date the analysis is anchored to.

How to reason (do this before writing your verdict):

- Weigh each input independently first. Summarize what the news implies (catalyst, sentiment, materiality), what the historical/technical data implies (trend, support/resistance, volatility regime), and what Kronos implies (direction, magnitude, and how much you trust it given its stated confidence and how far the move is from current price).
- Treat Kronos as one analyst, not an oracle. It is a quantitative signal. If news or technicals strongly contradict it, say so and explain which you weight more and why. Never let a single input drive the verdict alone.
- Distinguish signal from noise. Flag stale, low-quality, or non-material news. Note when historical patterns are unreliable (low liquidity, earnings gap, regime change). State your assumptions explicitly.
- Assess volatility. Estimate whether implied/realized volatility is elevated or compressed, since this dictates whether selling premium or buying premium is the smarter structure. If you lack implied volatility data, say so and reason from realized volatility.
- Identify the key risk. What single event or condition would most likely break your thesis this week (earnings, Fed, sector news, technical breakdown)?

Output format — write a narrative report with these sections, in this order:

- Snapshot — Ticker, current price, as-of date, and a one-sentence thesis.
- News Read — What the news supports, with materiality called out.
- Technical/Historical Read — Trend, key levels, volatility regime.
- Kronos Read — What the forecast says and how much you trust it this week, with reasoning.
- Synthesis — How you reconciled the three inputs, including any conflicts and how you resolved them.
- Grade (A-F) — A single letter for the attractiveness of the setup this week, where A = high-conviction, favorable risk/reward and F = avoid. Briefly justify the grade.
- Verdict — BUY or SELL (you must pick one; no "hold"), plus a confidence level (Low / Medium / High) and a one-week price expectation or range.
- Options Plays — 2 to 3 defined-risk ideas suited to the setup and volatility regime. Allowed structures: covered calls, cash-secured puts, vertical credit spreads, vertical debit spreads, and longer-dated (30+ DTE) directional debit spreads. Every play must have capped, known maximum loss — no naked short options, no undefined-risk positions, and avoid same-week weeklies as the primary play. For each play, specify: structure, rough strikes (as % from spot or delta if exact prices aren't derivable), target expiration window, the thesis it expresses, max risk vs. max reward, and what invalidates it.
- Key Risk — The one thing most likely to break the thesis this week.

Rules:

- Be decisive but calibrated. Match your confidence and grade to the actual strength of the evidence — don't manufacture conviction.
- If inputs are missing, low-quality, or contradictory, say so plainly and lower your confidence accordingly rather than guessing.
- Never invent data, prices, news, or Kronos values that weren't provided. If you need a number you don't have, state the assumption you're making.
- Keep position sizing out of scope unless asked; focus on structure and risk definition.
- End with this disclaimer: "This is AI-generated analysis for research purposes only, not financial advice. Options involve substantial risk of loss."
"""


# --------------------------------------------------------------------------- data gathering

def get_quote_fields(client, symbol: str) -> dict:
    """Current price + 52-week context for one symbol, via the shared quote helper."""
    quotes = schwab_client.get_quotes(client, [symbol])
    data = quotes.get(symbol)
    if not isinstance(data, dict):
        return {}
    return alert_rules.extract_quote_fields(data)


def build_historical_summary(client, symbol: str, recent: int = 20) -> str:
    """Compact technical context from Schwab daily candles: trend stats + the last `recent`
    bars. Kept bounded so the prompt stays cheap — full history would bloat token cost."""
    candles = schwab_client.get_daily_candles(client, symbol)
    if not candles:
        return "No historical price data available."
    candles = sorted(candles, key=lambda c: c["datetime"])
    # Schwab can return the current day twice (an in-progress bar + a snapshot) with the same
    # date but a slightly different ms timestamp. Keep the last bar per calendar date.
    by_date = {datetime.fromtimestamp(c["datetime"] / 1000, tz=timezone.utc).date(): c
               for c in candles}
    candles = [by_date[d] for d in sorted(by_date)]
    closes = [float(c["close"]) for c in candles]

    def sma(n: int):
        return sum(closes[-n:]) / n if len(closes) >= n else None

    last = closes[-1]
    sma20, sma50, sma200 = sma(20), sma(50), sma(200)
    hi = max(closes[-252:]) if len(closes) >= 1 else last
    lo = min(closes[-252:]) if len(closes) >= 1 else last

    lines = [
        f"Bars available: {len(candles)} daily.",
        f"Last close: {last:.2f}",
        f"SMA20: {sma20:.2f}" if sma20 else "SMA20: n/a",
        f"SMA50: {sma50:.2f}" if sma50 else "SMA50: n/a",
        f"SMA200: {sma200:.2f}" if sma200 else "SMA200: n/a",
        f"~52-week range (close): {lo:.2f} - {hi:.2f}",
        "",
        f"Last {recent} daily bars (date  open  high  low  close  volume):",
    ]
    for c in candles[-recent:]:
        d = datetime.fromtimestamp(c["datetime"] / 1000, tz=timezone.utc).date().isoformat()
        lines.append(
            f"{d}  {float(c['open']):.2f}  {float(c['high']):.2f}  "
            f"{float(c['low']):.2f}  {float(c['close']):.2f}  {int(c['volume'])}"
        )
    return "\n".join(lines)


def load_kronos_forecast(symbol: str) -> str:
    """Render the symbol's Kronos forecast JSON (p50 path + p10/p90 band) as prompt text,
    or a clear 'not available' note. Reads the same forecasts/*.json the digest uses."""
    path = config.FORECASTS_DIR / f"{symbol}.json"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "No Kronos forecast available for this symbol."

    lines = [
        f"Forecast generated: {rec.get('generated_at', 'unknown')}",
        f"History last date: {rec.get('history_last_date', 'unknown')}",
        f"Last close at forecast time: {rec.get('last_close')}",
        f"Sampling: {rec.get('n_samples')} paths, T={rec.get('T')}, top_p={rec.get('top_p')}",
        "",
        "Predicted daily closes (date: p50 [p10-p90]):",
    ]
    for day in rec.get("forecast", []):
        lines.append(
            f"  {day['date']}: {day['close']:.2f} "
            f"[{day.get('close_p10', float('nan')):.2f}-{day.get('close_p90', float('nan')):.2f}]"
        )
    return "\n".join(lines)


def build_user_prompt(symbol: str, name: str, current_price, as_of: str,
                      kronos_forecast: str, historical: str, news: str) -> str:
    """Fill the system prompt's labeled input slots in a single user message."""
    price_text = f"${current_price:.2f}" if isinstance(current_price, (int, float)) else "unknown"
    ticker = f"{symbol}" + (f" ({name})" if name else "")
    return (
        f"TICKER: {ticker}\n"
        f"CURRENT_PRICE: {price_text}\n"
        f"AS_OF_DATE: {as_of}\n\n"
        f"NEWS:\n{news or 'None provided.'}\n\n"
        f"HISTORICAL:\n{historical}\n\n"
        f"KRONOS_FORECAST:\n{kronos_forecast}\n"
    )


# --------------------------------------------------------------------------- Bedrock client

class BedrockClient:
    """Thin wrapper over the Bedrock runtime for the stock-analysis system prompt."""

    def __init__(self, model_id: str | None = None, region: str | None = None,
                 max_tokens: int | None = None):
        self.model_id = model_id or config.BEDROCK_MODEL
        self.region = region or config.AWS_REGION
        self.max_tokens = max_tokens or config.ANALYSIS_MAX_TOKENS
        self.system_prompt = SYSTEM_PROMPT
        # Lazily created so --dry-run and imports don't require AWS creds.
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def invoke(self, user_text: str) -> str:
        """Single-shot completion via the model-agnostic Converse API, so the same code path
        works across Bedrock models (Claude, Amazon Nova, …) — only the modelId changes."""
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": self.system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": self.max_tokens},
            )
        except (BotoCoreError, ClientError) as e:
            raise RuntimeError(
                f"Bedrock Converse failed ({type(e).__name__}: {e}). Check that AWS credentials "
                f"are configured and that model access for {self.model_id!r} is enabled in "
                f"{self.region}."
            ) from e
        blocks = resp["output"]["message"]["content"]
        return "".join(b.get("text", "") for b in blocks if "text" in b)

    def analyze(self, user_text: str) -> str:
        return self.invoke(user_text)


# --------------------------------------------------------------------------- orchestration

def gather_inputs(client, symbol: str, news: str = "") -> dict:
    """Pull the current price, history, and Kronos forecast for one symbol."""
    fields = get_quote_fields(client, symbol)
    return {
        "symbol": symbol,
        "name": fields.get("name", ""),
        "current_price": fields.get("last"),
        "as_of": date.today().isoformat(),
        "historical": build_historical_summary(client, symbol),
        "kronos_forecast": load_kronos_forecast(symbol),
        "news": news,
    }


def analyze_symbols(client, symbols: list[str], max_tokens: int | None = None) -> list[dict]:
    """Run the Bedrock analyst for each symbol; reuses one client. Per-symbol failures degrade
    gracefully (recorded as `error`) so one bad ticker never sinks the digest.

    Returns ``[{symbol, name, report}]`` (or ``{symbol, error}`` on failure).
    """
    analyst = BedrockClient(max_tokens=max_tokens)
    results: list[dict] = []
    for symbol in symbols:
        rec: dict = {"symbol": symbol}
        try:
            inputs = gather_inputs(client, symbol)
            rec["name"] = inputs["name"]
            user_text = build_user_prompt(
                inputs["symbol"], inputs["name"], inputs["current_price"], inputs["as_of"],
                inputs["kronos_forecast"], inputs["historical"], inputs["news"],
            )
            rec["report"] = analyst.analyze(user_text)
            log.info(f"Analyzed {symbol} via {analyst.model_id}.")
        except Exception as e:  # one bad ticker shouldn't sink the batch
            log.warning(f"Analysis failed for {symbol}: {type(e).__name__}: {e}")
            rec["error"] = f"{type(e).__name__}: {e}"
        results.append(rec)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="AI stock analyst via Claude on Bedrock")
    parser.add_argument("--symbol", required=True, help="Ticker to analyze")
    parser.add_argument("--news", default="", help="Inline news text to feed the analysis")
    parser.add_argument("--news-file", help="Read news text from a file")
    parser.add_argument("--model", help="Override Bedrock model id (default config.BEDROCK_MODEL)")
    parser.add_argument("--region", help="Override AWS region (default config.AWS_REGION)")
    parser.add_argument("--max-tokens", type=int, help="Override response max tokens")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build and print the prompt without calling Bedrock (no AWS creds needed)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Windows consoles default to cp1252 and can't encode the report's dashes/arrows — force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    symbol = args.symbol.strip().upper()
    news = args.news
    if args.news_file:
        news = open(args.news_file, encoding="utf-8").read()

    client = auth.get_client()
    inputs = gather_inputs(client, symbol, news=news)
    user_text = build_user_prompt(
        inputs["symbol"], inputs["name"], inputs["current_price"], inputs["as_of"],
        inputs["kronos_forecast"], inputs["historical"], inputs["news"],
    )

    if args.dry_run:
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT)
        print("\n=== USER PROMPT ===")
        print(user_text)
        print(f"\n(dry-run: skipped Bedrock call to {args.model or config.BEDROCK_MODEL})")
        return

    analyst = BedrockClient(model_id=args.model, region=args.region, max_tokens=args.max_tokens)
    log.info(f"Analyzing {symbol} via {analyst.model_id} in {analyst.region} …")
    report = analyst.analyze(user_text)
    print(report)


if __name__ == "__main__":
    main()
