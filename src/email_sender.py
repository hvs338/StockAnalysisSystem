import json
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

import config as config


TEMPLATES_DIR = config.PROJECT_ROOT / "templates"
DIGEST_TEMPLATE = "digest.html.j2"


PILL_STYLES = {
    "NEAR_52W_HIGH": {"text": "52-Week High", "bg": "#f2e3b8", "border": "#d8b56a", "color": "#5a4a1f"},
    "NEAR_52W_LOW":  {"text": "Near 52w Low", "bg": "#f2d8d6", "border": "#b4453f", "color": "#6e1f1f"},
    "BIG_MOVE_UP":   {"text": "Big Move ▲", "bg": "#d4ead9", "border": "#2f7d4f", "color": "#1f4d33"},
    "BIG_MOVE_DOWN": {"text": "Big Move ▼", "bg": "#f2d8d6", "border": "#b4453f", "color": "#6e1f1f"},
}

HELD_PILL_STYLE = {"bg": "#e0dccc", "border": "#a8a395", "color": "#3a382f"}

GREEN = "#2f7d4f"
RED = "#b4453f"
NEUTRAL = "#6b6657"


def _fmt(v, spec=".2f", fallback="—"):
    if v is None:
        return fallback
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return fallback


def _color_for_pct(pct):
    if pct is None:
        return NEUTRAL
    if pct > 0.005:
        return GREEN
    if pct < -0.005:
        return RED
    return NEUTRAL


def _arrow_for_pct(pct):
    if pct is None or abs(pct) < 0.005:
        return ""
    return "▲" if pct > 0 else "▼"


def _format_pct(pct):
    """+4.12% / −5.07% / 0.00% / — (uses U+2212 minus, not hyphen)."""
    if pct is None:
        return "—"
    if abs(pct) < 0.005:
        return "0.00%"
    sign = "+" if pct > 0 else "−"
    return f"{sign}{abs(pct):.2f}%"


def _format_price(p):
    if p is None:
        return "—"
    try:
        return f"${p:,.2f}"
    except (TypeError, ValueError):
        return "—"


def _format_range(low, high):
    if low is None or high is None:
        return ""
    try:
        return f"52w range ${low:,.0f}–${high:,.0f}"
    except (TypeError, ValueError):
        return ""


def _distance_text(row):
    """Right-hand secondary line under the price for a flagged row.

    Priority: 52w proximity messages beat threshold messages, since they're
    more informative when both fire on the same ticker.
    """
    last = row.get("last")
    high = row.get("high_52")
    low = row.get("low_52")
    flag_codes = {f.code for f in row.get("flags", [])}

    if "NEAR_52W_HIGH" in flag_codes and last is not None and high:
        try:
            gap = (high - last) / high * 100
            return f"{gap:.1f}% below high"
        except (TypeError, ZeroDivisionError):
            pass
    if "NEAR_52W_LOW" in flag_codes and last is not None and low:
        try:
            gap = (last - low) / low * 100
            return f"{gap:.1f}% above low"
        except (TypeError, ZeroDivisionError):
            pass
    if flag_codes & {"BIG_MOVE_UP", "BIG_MOVE_DOWN"}:
        return f"threshold ±{config.PCT_CHANGE_ALERT:.1f}%"
    return ""


def _held_pill(position):
    """Pill describing the user's holding for this symbol, or None if not held."""
    if not position:
        return None
    qty = position.get("quantity") or 0
    try:
        qty_num = float(qty)
    except (TypeError, ValueError):
        return None
    if qty_num == 0:
        return None
    if qty_num == int(qty_num):
        qty_text = f"{int(qty_num):,}"
    else:
        qty_text = f"{qty_num:,.2f}"
    label = "sh" if abs(qty_num) != 1 else "sh"
    return {"text": f"Held — {qty_text} {label}", **HELD_PILL_STYLE}


def _format_held_shares(position):
    """Compact 'N sh' annotation for the full-watchlist 'Held' column."""
    if not position:
        return ""
    qty = position.get("quantity") or 0
    try:
        qty_num = float(qty)
    except (TypeError, ValueError):
        return ""
    if qty_num == 0:
        return ""
    if qty_num == int(qty_num):
        return f"{int(qty_num):,} sh"
    return f"{qty_num:,.2f} sh"


def _build_flagged_row_context(row):
    flags = row.get("flags", [])
    pills = [PILL_STYLES[f.code] for f in flags if f.code in PILL_STYLES]
    pct = row.get("pct_change")
    return {
        "symbol": row["symbol"],
        "name": row.get("name") or row["symbol"],
        "last_price": _format_price(row.get("last")),
        "pct_change_text": _format_pct(pct),
        "pct_color": _color_for_pct(pct),
        "pct_arrow": _arrow_for_pct(pct),
        "pills": pills,
        "held_pill": _held_pill(row.get("position")),
        "range_text": _format_range(row.get("low_52"), row.get("high_52")),
        "distance_text": _distance_text(row),
    }


def _build_all_row_context(row):
    pct = row.get("pct_change")
    return {
        "symbol": row["symbol"],
        "last_price": _format_price(row.get("last")),
        "pct_change_text": _format_pct(pct),
        "pct_color": _color_for_pct(pct),
        "held_shares": _format_held_shares(row.get("position")),
    }


def _build_mover_context(item):
    """Map a Schwab /movers screener item into the shape the template expects."""
    pct_raw = item.get("netPercentChange")
    if pct_raw is None:
        pct_raw = item.get("netPercentChangeInDouble")
    last_raw = item.get("lastPrice") or item.get("last")
    pct = None
    if pct_raw is not None:
        try:
            # The /movers endpoint returns netPercentChange as a fraction
            # (0.0101 = 1.01%), unlike /quotes which is already scaled. Always ×100.
            pct = float(pct_raw) * 100
        except (TypeError, ValueError):
            pct = None
    return {
        "symbol": item.get("symbol", ""),
        "name": item.get("description") or item.get("symbol", ""),
        "last_price": _format_price(last_raw),
        "pct_change_text": _format_pct(pct),
        "pct_color": _color_for_pct(pct),
        "pct_arrow": _arrow_for_pct(pct),
    }


def load_forecasts(forecasts_dir=None) -> tuple[list[dict], dict]:
    """Read the latest Kronos forecasts from disk for the digest's Week Ahead section.

    Pure file IO (json + pathlib) — deliberately no torch / stock_prediction import, so the
    daily digest stays lightweight and runs on a CPU-only machine. The GPU forecast is a
    separate scheduled job (kronos_runner) that writes these files.

    Returns ``(records, meta)`` where ``meta`` carries generated_at / n_samples from
    index.json and ``records`` are the per-symbol JSON dicts in the index's order. A missing
    directory or index, or any unreadable/garbled file, degrades to ``([], {})`` / skips —
    the digest never breaks because a forecast is absent.
    """
    forecasts_dir = Path(forecasts_dir) if forecasts_dir else config.FORECASTS_DIR
    try:
        meta = json.loads((forecasts_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}
    records = []
    for symbol in meta.get("symbols", []):
        try:
            rec = json.loads((forecasts_dir / f"{symbol}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("forecast"):
            records.append(rec)
    return records, meta


def _build_forecast_context(record):
    """Map a forecast JSON record into the card fields the template expects.

    The final forecast day is the headline target (p50 close); close_p10/close_p90 give the
    band. The expected % move is target-vs-last_close, formatted like the rest of the digest.
    """
    last_close = record.get("last_close")
    last_day = record["forecast"][-1]
    target = last_day.get("close")
    pct = None
    if last_close and target is not None:
        try:
            pct = (target - last_close) / last_close * 100
        except (TypeError, ZeroDivisionError):
            pct = None
    return {
        "symbol": record.get("symbol", ""),
        "horizon": record.get("pred_len") or len(record["forecast"]),
        "last_price": _format_price(last_close),
        "target_price": _format_price(target),
        "pct_change_text": _format_pct(pct),
        "pct_color": _color_for_pct(pct),
        "pct_arrow": _arrow_for_pct(pct),
        "band_text": f"{_format_price(last_day.get('close_p10'))} – {_format_price(last_day.get('close_p90'))}",
    }


def _forecast_asof_text(generated_at):
    """Friendly 'Mon D' date from index.json's ISO generated_at, or '' if unparseable."""
    if not generated_at:
        return ""
    try:
        dt = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return ""
    return f"{dt.strftime('%b')} {dt.day}"


def _render_report_html(text: str) -> str:
    """Render the AI analyst's markdown-ish report as email-safe HTML.

    The text is model output, so escape it first (neutralizes any HTML/injection), then re-apply
    only **bold** and line breaks. Nothing the model emits can inject markup.
    """
    rendered = []
    for raw in text.split("\n"):
        line = str(escape(raw))
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        rendered.append(line)
    return "<br>".join(rendered)


def _build_analysis_context(analysis: dict) -> dict:
    return {
        "symbol": analysis.get("symbol", ""),
        "name": analysis.get("name") or analysis.get("symbol", ""),
        "report_html": _render_report_html(analysis["report"]) if analysis.get("report") else None,
        "error": analysis.get("error"),
    }


def _flagged_sort_key(row):
    """Order flagged rows by alert severity, then symbol alphabetically.

    Severity = max |pct_change| or proximity to a 52w extreme; we approximate
    with abs(pct_change) since both kinds of flag correlate with it well enough.
    """
    pct = row.get("pct_change") or 0
    return (-abs(pct), row["symbol"])


def _jinja_env():
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "htm", "j2"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )


def build_digest_html(
    quote_rows: list[dict],
    movers_up: list[dict] | None = None,
    movers_down: list[dict] | None = None,
    movers_index_label: str = "",
    forecasts: list[dict] | None = None,
    forecast_meta: dict | None = None,
    analyses: list[dict] | None = None,
    analysis_model: str = "",
) -> str:
    env = _jinja_env()
    template = env.get_template(DIGEST_TEMPLATE)

    valid_pcts = [r["pct_change"] for r in quote_rows if r.get("pct_change") is not None]
    avg_pct = sum(valid_pcts) / len(valid_pcts) if valid_pcts else None

    flagged = sorted(
        [r for r in quote_rows if r.get("flags")],
        key=_flagged_sort_key,
    )
    flagged_rows = [_build_flagged_row_context(r) for r in flagged]
    all_rows = [_build_all_row_context(r) for r in sorted(quote_rows, key=lambda r: r["symbol"])]

    movers_up_ctx = [_build_mover_context(m) for m in (movers_up or [])]
    movers_down_ctx = [_build_mover_context(m) for m in (movers_down or [])]
    has_movers = bool(movers_up_ctx or movers_down_ctx)

    forecast_ctx = [_build_forecast_context(r) for r in (forecasts or [])]
    meta = forecast_meta or {}

    analysis_ctx = [_build_analysis_context(a) for a in (analyses or [])]

    now = datetime.now()
    date_full = f"{now.strftime('%B')} {now.day}, {now.year}"
    date_full_nbsp = date_full.replace(" ", "&nbsp;")

    held_count = sum(1 for r in all_rows if r.get("held_shares"))

    return template.render(
        weekday=now.strftime("%A"),
        date_full=date_full,
        date_full_nbsp=date_full_nbsp,
        alert_count=len(flagged_rows),
        avg_pct_text=_format_pct(avg_pct),
        avg_pct_color=_color_for_pct(avg_pct),
        symbol_count=len(quote_rows),
        held_count=held_count,
        flagged_rows=flagged_rows,
        all_rows=all_rows,
        movers_up=movers_up_ctx,
        movers_down=movers_down_ctx,
        movers_index_label=movers_index_label,
        has_movers=has_movers,
        forecasts=forecast_ctx,
        has_forecast=bool(forecast_ctx),
        forecast_asof=_forecast_asof_text(meta.get("generated_at")),
        forecast_samples=meta.get("n_samples"),
        analyses=analysis_ctx,
        has_analysis=bool(analysis_ctx),
        analysis_model=analysis_model,
        pct_change_threshold=config.PCT_CHANGE_ALERT,
        near_52w_pct=config.NEAR_52W_HIGH_PCT,
    )


def build_digest_text(
    quote_rows: list[dict],
    movers_up: list[dict] | None = None,
    movers_down: list[dict] | None = None,
    forecasts: list[dict] | None = None,
    analyses: list[dict] | None = None,
) -> str:
    lines = [
        f"The Daily Run — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"{'Symbol':<8} {'Last':>10} {'% Chg':>8}   {'Held':>10}   Flags",
        "-" * 78,
    ]
    for row in sorted(quote_rows, key=lambda r: (not bool(r.get("flags")), r["symbol"])):
        last = _fmt(row.get("last"))
        pct = _fmt(row.get("pct_change"), "+.2f")
        held = _format_held_shares(row.get("position")) or "—"
        flag_text = "; ".join(f.message for f in row.get("flags", [])) or "—"
        lines.append(f"{row['symbol']:<8} {last:>10} {pct:>7}%   {held:>10}   {flag_text}")

    def _movers_block(label, items):
        if not items:
            return []
        block = ["", label, "-" * len(label)]
        for item in items:
            ctx = _build_mover_context(item)
            block.append(
                f"{ctx['symbol']:<8} {ctx['last_price']:>10} {ctx['pct_change_text']:>8}  {ctx['name']}"
            )
        return block

    lines.extend(_movers_block("Top Gainers", movers_up or []))
    lines.extend(_movers_block("Top Decliners", movers_down or []))

    if forecasts:
        lines.extend(["", "Week Ahead — Kronos Forecast", "-" * 28])
        for record in forecasts:
            ctx = _build_forecast_context(record)
            lines.append(
                f"{ctx['symbol']:<8} {ctx['last_price']:>10} -> {ctx['target_price']:>10}  "
                f"{ctx['pct_change_text']:>8}   band {ctx['band_text']}"
            )

    if analyses:
        lines.extend(["", "AI Analysis", "=" * 40])
        for a in analyses:
            header = f"{a.get('symbol', '')} — {a.get('name') or a.get('symbol', '')}"
            lines.extend(["", header, "-" * len(header)])
            lines.append(a.get("report") or f"(analysis unavailable: {a.get('error', 'unknown error')})")

    return "\n".join(lines)


def _send_email(subject: str, text: str, html: str | None = None) -> None:
    """Send a UTF-8 multipart email via Gmail SMTP. html is optional."""
    config.require_email_creds()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_USER
    msg["To"] = config.DIGEST_TO
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html is not None:
        msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        server.send_message(msg)


def send_digest(html: str, text: str, subject: str) -> None:
    _send_email(subject, text, html)


def send_reauth_reminder(error: object = "") -> None:
    """Email a nudge that the Schwab token has lapsed and needs a manual re-auth.

    Schwab refresh tokens expire after 7 days; the daily run can't refresh past that
    and there's no way to automate the interactive login. This turns the silent
    weekly failure into an actionable message.
    """
    subject = "Schwab Watchlist — action needed: re-authenticate"
    text = (
        "The daily Schwab digest could not run because the API token has expired or "
        "is invalid.\n\n"
        "Schwab refresh tokens last only 7 days, so a manual re-auth is needed:\n\n"
        "    python main.py --auth\n\n"
        "Then the daily digest will resume on its own.\n"
        f"\nDetails: {error}\n"
    )
    html = (
        "<div style=\"font-family:Georgia,serif;font-size:15px;color:#1a1a17;\">"
        "<p>The daily Schwab digest could not run — the API token has expired or is "
        "invalid.</p>"
        "<p>Schwab refresh tokens last only <b>7 days</b>, so a manual re-auth is needed:</p>"
        "<pre style=\"background:#f2efe6;padding:10px 14px;\">python main.py --auth</pre>"
        "<p>Then the daily digest resumes automatically.</p>"
        f"<p style=\"color:#8a8474;font-size:12px;\">Details: {error}</p>"
        "</div>"
    )
    _send_email(subject, text, html)
