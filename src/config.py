import os
from pathlib import Path

from dotenv import load_dotenv

# Everything resolves against the project root, never the current working directory — so the
# app behaves identically whether launched from the repo, from tests/, or by Task Scheduler
# (which starts in system32). The code lives in src/, so the root is this file's parent's parent.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env explicitly from the project root (load_dotenv() alone searches CWD).
load_dotenv(PROJECT_ROOT / ".env")

SCHWAB_CLIENT_ID = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")
SCHWAB_CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")

# Resolve a relative TOKEN_PATH against the project root, and make sure its parent
# directory exists so the first `--auth` on a fresh clone can write the token.
_token_path_raw = os.environ.get("TOKEN_PATH", "token.json")
TOKEN_PATH = str(
    Path(_token_path_raw) if os.path.isabs(_token_path_raw)
    else PROJECT_ROOT / _token_path_raw
)
Path(TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DIGEST_TO = os.environ.get("DIGEST_TO", "")

WATCHLIST_NAME = os.environ.get("WATCHLIST_NAME", "").strip()
WATCHLIST_FALLBACK_PATH = PROJECT_ROOT / "watchlist_fallback.yaml"


def _env_bool(key: str, default: str = "1") -> bool:
    return os.environ.get(key, default).strip().lower() not in {"0", "false", "no", "off", ""}


INCLUDE_MOVERS = _env_bool("INCLUDE_MOVERS", "1")
INCLUDE_POSITIONS = _env_bool("INCLUDE_POSITIONS", "1")
INCLUDE_FORECAST = _env_bool("INCLUDE_FORECAST", "1")
MOVERS_INDEX = os.environ.get("MOVERS_INDEX", "$SPX").strip() or "$SPX"
MOVERS_COUNT_PER_SIDE = int(os.environ.get("MOVERS_COUNT_PER_SIDE", "5"))

# Where kronos_runner writes (and the digest reads) the forecast JSON. Repo-root forecasts/.
FORECASTS_DIR = PROJECT_ROOT / "forecasts"

PCT_CHANGE_ALERT = 3.0
NEAR_52W_HIGH_PCT = 1.0
NEAR_52W_LOW_PCT = 1.0

# Kronos forecasting (stock_prediction.load_kronos_predictor). Both default to "auto":
# device auto → cuda if available else cpu; model auto → Kronos-base on GPU, Kronos-small on CPU.
# Override device with "cuda"/"cpu" or model with an explicit Hugging Face id.
KRONOS_DEVICE = os.environ.get("KRONOS_DEVICE", "auto").strip().lower()
KRONOS_MODEL = os.environ.get("KRONOS_MODEL", "auto").strip()

# Kronos sampling. The forecaster draws KRONOS_SAMPLES independent paths and reports their
# p10/p50/p90 band — averaging out the per-sample noise that makes a single path swing wildly.
# Lower TEMPERATURE / TOP_P make each path more conservative (tighter tails).
KRONOS_SAMPLES = int(os.environ.get("KRONOS_SAMPLES", "25"))
KRONOS_TEMPERATURE = float(os.environ.get("KRONOS_TEMPERATURE", "1.0"))
KRONOS_TOP_P = float(os.environ.get("KRONOS_TOP_P", "0.9"))

# Bedrock AI analysis (stock_analysis.py). Calls Claude on Amazon Bedrock via boto3.
# Bedrock model IDs carry an "anthropic." prefix. Default to the current flagship, Opus 4.8.
# Requires AWS creds (env / shared config / IAM role) and model access enabled in the region.
# ENABLE_ANALYSIS gates the (future) email section; the CLI runs regardless. Off by default
# until the AWS account + Bedrock model access are set up.
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "anthropic.claude-opus-4-8")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ENABLE_ANALYSIS = _env_bool("ENABLE_ANALYSIS", "0")
ANALYSIS_MAX_TOKENS = int(os.environ.get("ANALYSIS_MAX_TOKENS", "4096"))


def require_schwab_creds() -> None:
    missing = [
        k for k, v in {
            "SCHWAB_CLIENT_ID": SCHWAB_CLIENT_ID,
            "SCHWAB_CLIENT_SECRET": SCHWAB_CLIENT_SECRET,
        }.items() if not v
    ]
    if missing:
        raise RuntimeError(
            f"Missing Schwab credentials: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill them in."
        )


def require_email_creds() -> None:
    missing = [
        k for k, v in {
            "GMAIL_USER": GMAIL_USER,
            "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
            "DIGEST_TO": DIGEST_TO,
        }.items() if not v
    ]
    if missing:
        raise RuntimeError(
            f"Missing email credentials: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill them in."
        )
