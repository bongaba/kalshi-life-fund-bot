# config.py – updated for reliable .env loading
import os
from pathlib import Path
from dotenv import load_dotenv

# Always load from the directory where config.py lives (your project root)
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / '.env'

# Debug: show where we're looking
print(f"[CONFIG] Loading .env from: {env_path}")
print(f"[CONFIG] .env exists? {env_path.exists()}")

load_dotenv(env_path, override=True)  # override=True is safe & fixes conflicts

TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSY_VALUES = {"0", "false", "no", "off"}


def get_required_env(name: str) -> str:
	value = os.getenv(name)
	if value is None:
		raise ValueError(f"Missing required environment variable: {name}")
	return value.strip()


def get_optional_env(name: str) -> str | None:
	value = os.getenv(name)
	if value is None:
		return None
	value = value.strip()
	return value or None


def get_required_bool_env(name: str) -> bool:
	value = get_required_env(name).lower()
	if value in TRUTHY_VALUES:
		return True
	if value in FALSY_VALUES:
		return False
	raise ValueError(f"Invalid boolean value for {name}: {value}")


def get_required_int_env(name: str) -> int:
	return int(get_required_env(name))


def get_required_float_env(name: str) -> float:
	return float(get_required_env(name))


def get_required_choice_env(name: str, allowed_values: set[str]) -> str:
	value = get_required_env(name).lower()
	if value not in allowed_values:
		raise ValueError(f"Invalid value for {name}: {value}. Allowed values: {sorted(allowed_values)}")
	return value


def get_csv_env(name: str) -> list[str]:
	value = get_required_env(name)
	if value.upper() == "ALL":
		return []
	return [item.strip() for item in value.split(",") if item.strip()]

# Now read the variables
KALSHI_API_KEY_ID = get_required_env("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = get_required_env("KALSHI_PRIVATE_KEY_PATH")
XAI_API_KEY = get_required_env("XAI_API_KEY")
MODE = get_required_env("ACCOUNT_MODE")
RISK_PER_TRADE = get_required_float_env("RISK_PER_TRADE")
DAILY_LOSS_LIMIT = get_required_float_env("DAILY_LOSS_LIMIT")
MIN_CASH_RATIO = get_required_float_env("MIN_CASH_RATIO")
MAX_TRADES_PER_DAY = get_required_int_env("MAX_TRADES_PER_DAY")
VOLUME_THRESHOLD = get_required_int_env("VOLUME_THRESHOLD")
MARKET_SCAN_HOURS = get_required_int_env("MARKET_SCAN_HOURS")
OPEN_MARKETS_MAX_PAGES = get_required_int_env("OPEN_MARKETS_MAX_PAGES")
CLOSED_MARKETS_MAX_PAGES = get_required_int_env("CLOSED_MARKETS_MAX_PAGES")
BOT_LOOP_SCHEDULE = get_required_env("BOT_LOOP_SCHEDULE")
BOT_RUN_MODE = get_required_choice_env("BOT_RUN_MODE", {"daemon", "single_run"})
MIN_HOURS_TO_CLOSE = get_required_float_env("MIN_HOURS_TO_CLOSE")
INTERNAL_HIGH_PROBABILITY_THRESHOLD = get_required_float_env("INTERNAL_HIGH_PROBABILITY_THRESHOLD")
INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT = get_required_float_env("INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT")
USE_UNDERVALUED_MARKETS = get_required_bool_env("USE_UNDERVALUED_MARKETS")
UNDERVALUED_MIN_PROBABILITY = get_required_float_env("UNDERVALUED_MIN_PROBABILITY")
MARKET_TITLE_CONTAINS = get_optional_env("MARKET_TITLE_CONTAINS")
EXCLUDED_MARKET_TICKERS = [ticker.upper() for ticker in get_csv_env("EXCLUDED_MARKET_TICKERS")] if os.getenv("EXCLUDED_MARKET_TICKERS") else []
DISCORD_WEBHOOK_URL = get_optional_env("DISCORD_WEBHOOK_URL")
USE_GROK = get_required_bool_env("USE_GROK")
OVERRIDE_INTERNAL_MODEL_WITH_GROK = get_required_bool_env("OVERRIDE_INTERNAL_MODEL_WITH_GROK")
OVERRIDE_GROK_IGNORE_VOLUME_GATE = get_required_bool_env("OVERRIDE_GROK_IGNORE_VOLUME_GATE")
USE_GEMINI = get_required_bool_env("USE_GEMINI")
GROK_DETAILED_LOG = get_required_bool_env("GROK_DETAILED_LOG")
GEMINI_DETAILED_LOG = get_required_bool_env("GEMINI_DETAILED_LOG")
GEMINI_API_KEY = get_optional_env("GEMINI_API_KEY")
GEMINI_MODEL = get_required_env("GEMINI_MODEL")
GEMINI_TIMEOUT = get_required_int_env("GEMINI_TIMEOUT")
ANALYZER_CONFIDENCE_THRESHOLD = get_required_int_env("ANALYZER_CONFIDENCE_THRESHOLD")

if OVERRIDE_INTERNAL_MODEL_WITH_GROK and not USE_GROK:
	raise ValueError("OVERRIDE_INTERNAL_MODEL_WITH_GROK=true requires USE_GROK=true")

if OVERRIDE_GROK_IGNORE_VOLUME_GATE and not OVERRIDE_INTERNAL_MODEL_WITH_GROK:
	print("[CONFIG] OVERRIDE_GROK_IGNORE_VOLUME_GATE=true is set, but it only applies when OVERRIDE_INTERNAL_MODEL_WITH_GROK=true")

if INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT < INTERNAL_HIGH_PROBABILITY_THRESHOLD:
	raise ValueError(
		"INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT must be greater than or equal to INTERNAL_HIGH_PROBABILITY_THRESHOLD"
	)

# Debug print to confirm loading
print(f"[CONFIG] MODE: {MODE}")
print(f"[CONFIG] API Key ID: {KALSHI_API_KEY_ID}")
print(f"[CONFIG] Private Key Path: {KALSHI_PRIVATE_KEY_PATH}")
print(f"[CONFIG] RISK_PER_TRADE: {RISK_PER_TRADE}")
print(f"[CONFIG] MIN_CASH_RATIO: {MIN_CASH_RATIO}")
print(f"[CONFIG] VOLUME_THRESHOLD: {VOLUME_THRESHOLD}")
print(f"[CONFIG] MARKET_SCAN_HOURS: {MARKET_SCAN_HOURS}")
print(f"[CONFIG] OPEN_MARKETS_MAX_PAGES: {OPEN_MARKETS_MAX_PAGES}")
print(f"[CONFIG] CLOSED_MARKETS_MAX_PAGES: {CLOSED_MARKETS_MAX_PAGES}")
print(f"[CONFIG] BOT_LOOP_SCHEDULE: {BOT_LOOP_SCHEDULE}")
print(f"[CONFIG] BOT_RUN_MODE: {BOT_RUN_MODE}")
print(f"[CONFIG] MIN_HOURS_TO_CLOSE: {MIN_HOURS_TO_CLOSE}")
print(f"[CONFIG] INTERNAL_HIGH_PROBABILITY_THRESHOLD: {INTERNAL_HIGH_PROBABILITY_THRESHOLD}")
print(f"[CONFIG] INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT: {INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT}")
print(f"[CONFIG] USE_UNDERVALUED_MARKETS: {USE_UNDERVALUED_MARKETS}")
print(f"[CONFIG] UNDERVALUED_MIN_PROBABILITY: {UNDERVALUED_MIN_PROBABILITY}")
print(f"[CONFIG] MARKET_TITLE_CONTAINS: {MARKET_TITLE_CONTAINS or 'NONE'}")
print(f"[CONFIG] EXCLUDED_MARKET_TICKERS: {EXCLUDED_MARKET_TICKERS or 'NONE'}")
print(f"[CONFIG] USE_GROK: {USE_GROK}")
print(f"[CONFIG] OVERRIDE_INTERNAL_MODEL_WITH_GROK: {OVERRIDE_INTERNAL_MODEL_WITH_GROK}")
print(f"[CONFIG] OVERRIDE_GROK_IGNORE_VOLUME_GATE: {OVERRIDE_GROK_IGNORE_VOLUME_GATE}")
print(f"[CONFIG] USE_GEMINI: {USE_GEMINI}")
print(f"[CONFIG] GROK_DETAILED_LOG: {GROK_DETAILED_LOG}")
print(f"[CONFIG] GEMINI_DETAILED_LOG: {GEMINI_DETAILED_LOG}")
print(f"[CONFIG] GEMINI_MODEL: {GEMINI_MODEL}")
print(f"[CONFIG] GEMINI_TIMEOUT: {GEMINI_TIMEOUT}")
print(f"[CONFIG] ANALYZER_CONFIDENCE_THRESHOLD: {ANALYZER_CONFIDENCE_THRESHOLD}")