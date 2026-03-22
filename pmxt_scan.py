import argparse
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher
import os
import shutil
import re
import time
import traceback

from loguru import logger
import pandas as pd
import pmxt

from logging_setup import setup_log_file


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "pmxt_history"
MARKETS_CSV_PATH = DATA_DIR / "pmxt_cross_exchange_markets_latest.csv"
OPPORTUNITIES_CSV_PATH = DATA_DIR / "pmxt_kalshi_value_opportunities_latest.csv"
DECISION_ENGINE_CSV_PATH = DATA_DIR / "pmxt_kalshi_for_decision_engine.csv"
MATCH_DIAGNOSTICS_CSV_PATH = DATA_DIR / "pmxt_kalshi_match_diagnostics_latest.csv"
SUPPORTED_PLATFORMS = ("kalshi", "polymarket")
DEFAULT_MAX_FETCH_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 5
MATCH_STOPWORDS = {
    "a", "an", "and", "at", "be", "by", "for", "from", "if", "in", "is", "of", "on", "or",
    "the", "to", "will", "with", "market", "event",
}

DECISION_ENGINE_COLUMNS = [
    "ticker",
    "title",
    "subtitle",
    "description",
    "yes_price",
    "no_price",
    "volume",
    "close_time",
    "prefilter_direction",
    "opportunity_side",
    "opportunity_edge",
    "match_score",
    "polymarket_yes_price",
    "polymarket_no_price",
    "matched_polymarket_ticker",
    "matched_polymarket_title",
    "scan_source",
]

OPPORTUNITY_COLUMNS = [
    "ticker",
    "title",
    "subtitle",
    "description",
    "yes_price",
    "no_price",
    "volume",
    "close_time",
    "prefilter_direction",
    "opportunity_side",
    "opportunity_edge",
    "kalshi_contract_price",
    "polymarket_contract_price",
    "polymarket_yes_price",
    "polymarket_no_price",
    "matched_polymarket_ticker",
    "matched_polymarket_title",
    "matched_polymarket_close_time",
    "matched_polymarket_url",
    "match_score",
    "comparison_basis",
    "opportunity_reason",
    "scan_source",
]

MATCH_DIAGNOSTIC_COLUMNS = [
    "ticker",
    "title",
    "yes_price",
    "no_price",
    "volume",
    "close_time",
    "best_match_score",
    "best_match_above_threshold",
    "best_match_polymarket_ticker",
    "best_match_polymarket_title",
    "best_match_polymarket_yes_price",
    "best_match_polymarket_no_price",
    "best_yes_edge",
    "best_no_edge",
    "best_match_threshold",
]


def ensure_output_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    ARCHIVE_DIR.mkdir(exist_ok=True)


def prepend_path_entries(path_entries):
    existing_entries = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    normalized_existing = {entry.lower() for entry in existing_entries if entry}
    new_entries = []
    for entry in path_entries:
        if not entry:
            continue
        entry_path = Path(entry)
        if not entry_path.exists():
            continue
        normalized_entry = str(entry_path).lower()
        if normalized_entry in normalized_existing:
            continue
        new_entries.append(str(entry_path))

    if new_entries:
        os.environ["PATH"] = os.pathsep.join(new_entries + existing_entries)


def configure_pmxt_runtime_environment():
    appdata = os.environ.get("APPDATA", "")
    candidate_paths = [
        r"C:\Program Files\nodejs",
        Path(appdata) / "npm" if appdata else None,
    ]
    prepend_path_entries(candidate_paths)

    node_path = shutil.which("node")
    pmxt_server_path = shutil.which("pmxt-server")
    logger.info(
        "[PMXT_SCAN] Runtime check | node={} | pmxt_server={}",
        node_path or "MISSING",
        pmxt_server_path or "MISSING",
    )


def is_rate_limit_error(error):
    error_text = str(error).lower()
    return "[429]" in error_text or "too many requests" in error_text or "rate limit" in error_text


def compute_retry_delay(attempt_index, base_delay_seconds):
    return max(1.0, base_delay_seconds * (2 ** max(0, attempt_index - 1)))


def parse_args():
    parser = argparse.ArgumentParser(description="Scan Kalshi and Polymarket with pmxt, match equivalent markets, and export Kalshi value opportunities.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum number of events to fetch from each pmxt exchange client.")
    parser.add_argument("--top", type=int, default=15, help="Number of rows to print in the console preview.")
    parser.add_argument("--min-volume", type=float, default=0.0, help="Minimum market volume required for export.")
    parser.add_argument("--min-edge", type=float, default=0.03, help="Minimum price edge required for a Kalshi side to count as cheaper than Polymarket.")
    parser.add_argument("--match-threshold", type=float, default=0.72, help="Minimum title similarity score required to treat a Kalshi and Polymarket market as the same event.")
    parser.add_argument("--max-fetch-retries", type=int, default=DEFAULT_MAX_FETCH_RETRIES, help="Maximum retries per platform when pmxt returns a transient error such as HTTP 429.")
    parser.add_argument("--retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS, help="Base delay in seconds before retrying a rate-limited platform fetch.")
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include rows missing one or more decision-engine fields in the raw market export.",
    )
    return parser.parse_args()


def safe_getattr(obj, *attr_names, default=None):
    for attr_name in attr_names:
        if obj is None:
            return default
        value = getattr(obj, attr_name, None)
        if value not in (None, ""):
            return value
    return default


def safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_market_price(price):
    numeric_price = safe_float(price)
    if numeric_price is None:
        return None
    if numeric_price > 1:
        numeric_price /= 100.0
    if 0.0 <= numeric_price <= 1.0:
        return round(numeric_price, 4)
    return None


def complementary_price(price):
    normalized_price = normalize_market_price(price)
    if normalized_price is None:
        return None
    complement = 1.0 - normalized_price
    if 0.0 <= complement <= 1.0:
        return round(complement, 4)
    return None


def format_close_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt_value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt_value.isoformat()
    return str(value)


def clean_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    return " ".join(text.split())


def truncate_text(text, limit):
    cleaned = clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def parse_close_time(value):
    formatted = format_close_time(value)
    if not formatted:
        return None
    try:
        parsed = datetime.fromisoformat(formatted.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def normalize_match_text(*values):
    text = " ".join(clean_text(value).lower() for value in values if value not in (None, ""))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [token for token in text.split() if token and token not in MATCH_STOPWORDS]
    return " ".join(tokens)


def outcome_price_by_labels(market, labels):
    outcomes = safe_getattr(market, "outcomes", default=[]) or []
    normalized_labels = {label.upper() for label in labels}
    for outcome in outcomes:
        outcome_label = clean_text(safe_getattr(outcome, "label", default="")).upper()
        if outcome_label in normalized_labels:
            return normalize_market_price(safe_getattr(outcome, "price"))
    return None


def extract_yes_price(market):
    yes_side = safe_getattr(market, "yes")
    direct_yes_price = normalize_market_price(
        safe_getattr(
            yes_side,
            "price",
            default=safe_getattr(market, "yes_price", "last_price", "previous_price"),
        )
    )
    if direct_yes_price is not None:
        return direct_yes_price
    return outcome_price_by_labels(market, {"YES"})


def extract_no_price(market, yes_price):
    no_side = safe_getattr(market, "no")
    direct_no_price = normalize_market_price(safe_getattr(no_side, "price", default=safe_getattr(market, "no_price")))
    if direct_no_price is None:
        direct_no_price = outcome_price_by_labels(market, {"NO"})
    if direct_no_price is not None:
        return direct_no_price, "direct_no_price"
    if yes_price is not None:
        return complementary_price(yes_price), "complement_from_yes"
    return None, "missing"


def extract_volume(market):
    volume = safe_float(safe_getattr(market, "volume", "volume_24h", "liquidity"))
    if volume is None:
        return 0.0
    return volume


def extract_description(event, market):
    return clean_text(
        safe_getattr(
            market,
            "description",
            "subtitle",
            default=safe_getattr(event, "description", "subtitle", default=""),
        )
    )


def extract_close_time(event, market):
    return format_close_time(
        safe_getattr(
            market,
            "resolution_date",
            "close_time",
            "end_time",
            "expiration_time",
            default=safe_getattr(event, "close_time", "end_time", "expiration_time"),
        )
    )


def normalize_market(event, market, platform, scan_timestamp):
    normalized_platform = clean_text(platform).lower()
    yes_price = extract_yes_price(market)
    no_price, no_price_source = extract_no_price(market, yes_price)
    volume = extract_volume(market)
    title = clean_text(safe_getattr(market, "title", default=safe_getattr(event, "title", default="")))
    subtitle = clean_text(safe_getattr(market, "subtitle", default=safe_getattr(event, "subtitle", default="")))
    description = extract_description(event, market)
    ticker = clean_text(safe_getattr(market, "ticker", "market_id", "id", default=""))
    close_time = extract_close_time(event, market)
    event_title = truncate_text(safe_getattr(event, "title", default=title), 160)

    if normalized_platform not in SUPPORTED_PLATFORMS:
        return None

    decision_engine_ready = all(
        [
            ticker,
            title,
            yes_price is not None,
            no_price is not None,
            close_time,
        ]
    )

    return {
        "platform": normalized_platform,
        "ticker": ticker,
        "title": truncate_text(title, 160),
        "subtitle": truncate_text(subtitle, 240),
        "description": truncate_text(description, 500),
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_price_cents": round(yes_price * 100) if yes_price is not None else None,
        "no_price_cents": round(no_price * 100) if no_price is not None else None,
        "implied_prob": yes_price,
        "volume": volume,
        "close_time": close_time,
        "event_title": event_title,
        "event_id": clean_text(safe_getattr(event, "id", default="")),
        "market_id": clean_text(safe_getattr(market, "market_id", "id", default="")),
        "market_url": clean_text(safe_getattr(market, "url", default="")),
        "event_url": clean_text(safe_getattr(event, "url", default="")),
        "category": clean_text(safe_getattr(market, "category", default=safe_getattr(event, "category", default=""))),
        "scan_timestamp": scan_timestamp,
        "scan_source": f"pmxt_{normalized_platform}",
        "no_price_source": no_price_source,
        "decision_engine_ready": decision_engine_ready,
        "data_quality_issue": "" if decision_engine_ready else "missing_required_fields",
        "match_text": normalize_match_text(title, subtitle, description, event_title),
    }


def fetch_events_for_platform(platform, limit, max_fetch_retries, retry_delay_seconds):
    if platform == "kalshi":
        exchange = pmxt.Kalshi()
    elif platform == "polymarket":
        exchange = pmxt.Polymarket()
    else:
        raise ValueError(f"Unsupported platform: {platform}")

    logger.info("[PMXT_SCAN] Fetching {} events with limit={}", platform, limit)

    last_error = None
    for attempt_index in range(1, max_fetch_retries + 1):
        try:
            return exchange.fetch_events(limit=limit)
        except Exception as exc:
            last_error = exc
            if not is_rate_limit_error(exc):
                raise

            if attempt_index >= max_fetch_retries:
                break

            delay_seconds = compute_retry_delay(attempt_index, retry_delay_seconds)
            logger.warning(
                "[PMXT_SCAN] Rate limited by {} on attempt {}/{} | sleeping {:.1f}s before retry",
                platform,
                attempt_index,
                max_fetch_retries,
                delay_seconds,
            )
            time.sleep(delay_seconds)

    raise last_error


def collect_market_rows(limit, max_fetch_retries, retry_delay_seconds):
    scan_timestamp = datetime.now(timezone.utc).isoformat()
    rows = []
    stats = {
        "events_seen": 0,
        "markets_seen": 0,
        "rows_by_platform": {platform: 0 for platform in SUPPORTED_PLATFORMS},
        "decision_engine_ready_by_platform": {platform: 0 for platform in SUPPORTED_PLATFORMS},
        "fetch_failures_by_platform": {platform: 0 for platform in SUPPORTED_PLATFORMS},
        "missing_yes_price": 0,
        "missing_no_price": 0,
        "missing_close_time": 0,
        "used_complement_no_price": 0,
    }

    for platform in SUPPORTED_PLATFORMS:
        try:
            events = fetch_events_for_platform(
                platform,
                limit=limit,
                max_fetch_retries=max_fetch_retries,
                retry_delay_seconds=retry_delay_seconds,
            )
        except Exception as exc:
            stats["fetch_failures_by_platform"][platform] += 1
            logger.error("[PMXT_SCAN] Failed to fetch {} events after retries: {}", platform, exc)
            continue

        for event in events:
            stats["events_seen"] += 1
            for market in safe_getattr(event, "markets", default=[]) or []:
                stats["markets_seen"] += 1
                normalized_row = normalize_market(event, market, platform, scan_timestamp)
                if normalized_row is None:
                    continue

                rows.append(normalized_row)
                stats["rows_by_platform"][platform] += 1

                if normalized_row["decision_engine_ready"]:
                    stats["decision_engine_ready_by_platform"][platform] += 1
                if normalized_row["yes_price"] is None:
                    stats["missing_yes_price"] += 1
                if normalized_row["no_price"] is None:
                    stats["missing_no_price"] += 1
                if not normalized_row["close_time"]:
                    stats["missing_close_time"] += 1
                if normalized_row["no_price_source"] == "complement_from_yes":
                    stats["used_complement_no_price"] += 1

    return rows, stats


def title_similarity_score(kalshi_row, polymarket_row):
    left_text = kalshi_row.get("match_text", "")
    right_text = polymarket_row.get("match_text", "")
    if not left_text or not right_text:
        return 0.0

    sequence_score = SequenceMatcher(None, left_text, right_text).ratio()
    left_tokens = set(left_text.split())
    right_tokens = set(right_text.split())
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens) if (left_tokens or right_tokens) else 0.0

    close_score = 0.0
    left_close = parse_close_time(kalshi_row.get("close_time"))
    right_close = parse_close_time(polymarket_row.get("close_time"))
    if left_close and right_close:
        hour_diff = abs((left_close - right_close).total_seconds()) / 3600.0
        if hour_diff <= 12:
            close_score = 1.0
        elif hour_diff <= 48:
            close_score = 0.5

    score = (0.60 * sequence_score) + (0.30 * token_score) + (0.10 * close_score)
    return round(score, 4)


def build_opportunity_row(kalshi_row, polymarket_row, match_score, min_edge):
    yes_edge = None
    no_edge = None
    if kalshi_row.get("yes_price") is not None and polymarket_row.get("yes_price") is not None:
        yes_edge = round(polymarket_row["yes_price"] - kalshi_row["yes_price"], 4)
    if kalshi_row.get("no_price") is not None and polymarket_row.get("no_price") is not None:
        no_edge = round(polymarket_row["no_price"] - kalshi_row["no_price"], 4)

    side = None
    edge = None
    kalshi_contract_price = None
    polymarket_contract_price = None
    if yes_edge is not None and yes_edge >= min_edge:
        side = "YES"
        edge = yes_edge
        kalshi_contract_price = kalshi_row.get("yes_price")
        polymarket_contract_price = polymarket_row.get("yes_price")
    if no_edge is not None and no_edge >= min_edge and (edge is None or no_edge > edge):
        side = "NO"
        edge = no_edge
        kalshi_contract_price = kalshi_row.get("no_price")
        polymarket_contract_price = polymarket_row.get("no_price")

    if side is None:
        return None

    return {
        **kalshi_row,
        "prefilter_direction": side,
        "opportunity_side": side,
        "opportunity_edge": edge,
        "kalshi_contract_price": kalshi_contract_price,
        "polymarket_contract_price": polymarket_contract_price,
        "polymarket_yes_price": polymarket_row.get("yes_price"),
        "polymarket_no_price": polymarket_row.get("no_price"),
        "matched_polymarket_ticker": polymarket_row.get("ticker"),
        "matched_polymarket_title": polymarket_row.get("title"),
        "matched_polymarket_close_time": polymarket_row.get("close_time"),
        "matched_polymarket_url": polymarket_row.get("market_url"),
        "match_score": match_score,
        "comparison_basis": "kalshi_cheaper_than_polymarket",
        "scan_source": "pmxt_cross_exchange",
        "decision_engine_ready": kalshi_row.get("decision_engine_ready", False),
        "data_quality_issue": kalshi_row.get("data_quality_issue", ""),
        "opportunity_reason": f"kalshi_{side.lower()}_cheaper_than_polymarket_by_{edge:.4f}",
    }


def build_dataframes(rows, min_volume, include_incomplete, min_edge, match_threshold):
    if not rows:
        return pd.DataFrame(), pd.DataFrame(columns=OPPORTUNITY_COLUMNS), pd.DataFrame(columns=DECISION_ENGINE_COLUMNS), pd.DataFrame(columns=MATCH_DIAGNOSTIC_COLUMNS), {
            "matches_considered": 0,
            "matches_above_threshold": 0,
            "opportunities_found": 0,
            "yes_opportunities": 0,
            "no_opportunities": 0,
        }

    markets_df = pd.DataFrame(rows)

    if min_volume > 0:
        markets_df = markets_df[markets_df["volume"] >= min_volume].copy()

    before_dedup = len(markets_df)
    markets_df = markets_df.sort_values(["platform", "decision_engine_ready", "volume", "yes_price"], ascending=[True, False, False, True])
    markets_df = markets_df.drop_duplicates(subset=["platform", "ticker"], keep="first").reset_index(drop=True)
    duplicates_removed = before_dedup - len(markets_df)
    if duplicates_removed:
        logger.info("[PMXT_SCAN] Removed {} duplicate platform/ticker rows", duplicates_removed)

    kalshi_df = markets_df[markets_df["platform"] == "kalshi"].copy()
    polymarket_df = markets_df[markets_df["platform"] == "polymarket"].copy()

    match_stats = {
        "matches_considered": 0,
        "matches_above_threshold": 0,
        "opportunities_found": 0,
        "yes_opportunities": 0,
        "no_opportunities": 0,
    }

    opportunity_rows = []
    diagnostic_rows = []
    polymarket_records = polymarket_df.to_dict("records")
    for kalshi_row in kalshi_df.to_dict("records"):
        best_match = None
        best_score = 0.0
        for polymarket_row in polymarket_records:
            match_stats["matches_considered"] += 1
            score = title_similarity_score(kalshi_row, polymarket_row)
            if score >= match_threshold and score > best_score:
                best_match = polymarket_row
                best_score = score

        best_any_match = None
        best_any_score = -1.0
        for polymarket_row in polymarket_records:
            score = title_similarity_score(kalshi_row, polymarket_row)
            if score > best_any_score:
                best_any_score = score
                best_any_match = polymarket_row

        best_yes_edge = None
        best_no_edge = None
        if best_any_match is not None:
            if kalshi_row.get("yes_price") is not None and best_any_match.get("yes_price") is not None:
                best_yes_edge = round(best_any_match["yes_price"] - kalshi_row["yes_price"], 4)
            if kalshi_row.get("no_price") is not None and best_any_match.get("no_price") is not None:
                best_no_edge = round(best_any_match["no_price"] - kalshi_row["no_price"], 4)

        diagnostic_rows.append({
            "ticker": kalshi_row.get("ticker"),
            "title": kalshi_row.get("title"),
            "yes_price": kalshi_row.get("yes_price"),
            "no_price": kalshi_row.get("no_price"),
            "volume": kalshi_row.get("volume"),
            "close_time": kalshi_row.get("close_time"),
            "best_match_score": round(best_any_score, 4) if best_any_score >= 0 else None,
            "best_match_above_threshold": bool(best_any_score >= match_threshold),
            "best_match_polymarket_ticker": best_any_match.get("ticker") if best_any_match is not None else None,
            "best_match_polymarket_title": best_any_match.get("title") if best_any_match is not None else None,
            "best_match_polymarket_yes_price": best_any_match.get("yes_price") if best_any_match is not None else None,
            "best_match_polymarket_no_price": best_any_match.get("no_price") if best_any_match is not None else None,
            "best_yes_edge": best_yes_edge,
            "best_no_edge": best_no_edge,
            "best_match_threshold": match_threshold,
        })

        if best_match is None:
            continue

        match_stats["matches_above_threshold"] += 1
        opportunity_row = build_opportunity_row(kalshi_row, best_match, best_score, min_edge)
        if opportunity_row is None:
            continue

        opportunity_rows.append(opportunity_row)
        match_stats["opportunities_found"] += 1
        if opportunity_row["opportunity_side"] == "YES":
            match_stats["yes_opportunities"] += 1
        elif opportunity_row["opportunity_side"] == "NO":
            match_stats["no_opportunities"] += 1

    opportunities_df = pd.DataFrame(opportunity_rows, columns=OPPORTUNITY_COLUMNS)
    if not opportunities_df.empty:
        opportunities_df = opportunities_df.sort_values(["opportunity_edge", "match_score", "volume"], ascending=[False, False, False]).reset_index(drop=True)
        if not include_incomplete:
            opportunities_df = opportunities_df[opportunities_df["decision_engine_ready"]].reset_index(drop=True)

    decision_engine_df = opportunities_df[DECISION_ENGINE_COLUMNS].copy() if not opportunities_df.empty else pd.DataFrame(columns=DECISION_ENGINE_COLUMNS)
    decision_engine_df = decision_engine_df.reset_index(drop=True)

    diagnostics_df = pd.DataFrame(diagnostic_rows, columns=MATCH_DIAGNOSTIC_COLUMNS)
    if not diagnostics_df.empty:
        diagnostics_df = diagnostics_df.sort_values(["best_match_score", "best_yes_edge", "best_no_edge"], ascending=[False, False, False]).reset_index(drop=True)

    markets_export_df = markets_df.copy()
    if not include_incomplete:
        markets_export_df = markets_export_df[markets_export_df["decision_engine_ready"]].copy()
    if "implied_prob" not in markets_export_df.columns and "yes_price" in markets_export_df.columns:
        markets_export_df["implied_prob"] = markets_export_df["yes_price"]
    markets_export_df = markets_export_df.sort_values(["platform", "implied_prob", "volume"], ascending=[True, True, False]).reset_index(drop=True)

    return markets_export_df, opportunities_df, decision_engine_df, diagnostics_df, match_stats


def archive_output_path(prefix):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ARCHIVE_DIR / f"{prefix}_{timestamp}.csv"


def save_outputs(markets_export_df, opportunities_df, decision_engine_df, diagnostics_df):
    markets_archive_path = archive_output_path("pmxt_cross_exchange_markets")
    opportunities_archive_path = archive_output_path("pmxt_kalshi_value_opportunities")
    decision_archive_path = archive_output_path("pmxt_kalshi_for_decision_engine")
    diagnostics_archive_path = archive_output_path("pmxt_kalshi_match_diagnostics")

    markets_export_df.to_csv(MARKETS_CSV_PATH, index=False)
    markets_export_df.to_csv(markets_archive_path, index=False)
    opportunities_df.to_csv(OPPORTUNITIES_CSV_PATH, index=False)
    opportunities_df.to_csv(opportunities_archive_path, index=False)
    decision_engine_df.to_csv(DECISION_ENGINE_CSV_PATH, index=False)
    decision_engine_df.to_csv(decision_archive_path, index=False)
    diagnostics_df.to_csv(MATCH_DIAGNOSTICS_CSV_PATH, index=False)
    diagnostics_df.to_csv(diagnostics_archive_path, index=False)

    logger.info("[PMXT_SCAN] Saved latest market export to {}", MARKETS_CSV_PATH)
    logger.info("[PMXT_SCAN] Saved archived market export to {}", markets_archive_path)
    logger.info("[PMXT_SCAN] Saved latest opportunity export to {}", OPPORTUNITIES_CSV_PATH)
    logger.info("[PMXT_SCAN] Saved archived opportunity export to {}", opportunities_archive_path)
    logger.info("[PMXT_SCAN] Saved latest decision-engine export to {}", DECISION_ENGINE_CSV_PATH)
    logger.info("[PMXT_SCAN] Saved archived decision-engine export to {}", decision_archive_path)
    logger.info("[PMXT_SCAN] Saved latest match diagnostics export to {}", MATCH_DIAGNOSTICS_CSV_PATH)
    logger.info("[PMXT_SCAN] Saved archived match diagnostics export to {}", diagnostics_archive_path)

    return {
        "latest_markets": MARKETS_CSV_PATH,
        "archive_markets": markets_archive_path,
        "latest_opportunities": OPPORTUNITIES_CSV_PATH,
        "archive_opportunities": opportunities_archive_path,
        "latest_decision": DECISION_ENGINE_CSV_PATH,
        "archive_decision": decision_archive_path,
        "latest_diagnostics": MATCH_DIAGNOSTICS_CSV_PATH,
        "archive_diagnostics": diagnostics_archive_path,
    }


def log_summary(stats, match_stats, markets_export_df, opportunities_df, decision_engine_df):
    logger.info(
        "[PMXT_SCAN] Summary | events_seen={} | markets_seen={} | kalshi_rows={} | polymarket_rows={} | kalshi_ready={} | polymarket_ready={} | kalshi_fetch_failures={} | polymarket_fetch_failures={} | missing_yes_price={} | missing_no_price={} | missing_close_time={} | complement_no_price={} | matches_above_threshold={} | opportunities_found={} | yes_opportunities={} | no_opportunities={} | exported_markets={} | exported_opportunities={} | exported_decision={}",
        stats["events_seen"],
        stats["markets_seen"],
        stats["rows_by_platform"]["kalshi"],
        stats["rows_by_platform"]["polymarket"],
        stats["decision_engine_ready_by_platform"]["kalshi"],
        stats["decision_engine_ready_by_platform"]["polymarket"],
        stats["fetch_failures_by_platform"]["kalshi"],
        stats["fetch_failures_by_platform"]["polymarket"],
        stats["missing_yes_price"],
        stats["missing_no_price"],
        stats["missing_close_time"],
        stats["used_complement_no_price"],
        match_stats["matches_above_threshold"],
        match_stats["opportunities_found"],
        match_stats["yes_opportunities"],
        match_stats["no_opportunities"],
        len(markets_export_df),
        len(opportunities_df),
        len(decision_engine_df),
    )


def print_console_preview(opportunities_df, top_n):
    if opportunities_df.empty:
        print("No Kalshi opportunities cheaper than Polymarket were found.")
        return

    preview_columns = [
        "ticker",
        "title",
        "opportunity_side",
        "yes_price",
        "no_price",
        "polymarket_yes_price",
        "polymarket_no_price",
        "opportunity_edge",
        "match_score",
        "volume",
        "close_time",
    ]
    available_columns = [column for column in preview_columns if column in opportunities_df.columns]
    print("\n=== KALSHI OPPORTUNITIES CHEAPER THAN POLYMARKET ===")
    print(opportunities_df[available_columns].head(top_n).to_string(index=False))


def scan_with_pmxt(
    limit=100,
    min_volume=0.0,
    include_incomplete=False,
    min_edge=0.03,
    match_threshold=0.72,
    max_fetch_retries=DEFAULT_MAX_FETCH_RETRIES,
    retry_delay_seconds=DEFAULT_RETRY_DELAY_SECONDS,
):
    rows, stats = collect_market_rows(
        limit=limit,
        max_fetch_retries=max_fetch_retries,
        retry_delay_seconds=retry_delay_seconds,
    )
    markets_export_df, opportunities_df, decision_engine_df, diagnostics_df, match_stats = build_dataframes(
        rows,
        min_volume=min_volume,
        include_incomplete=include_incomplete,
        min_edge=min_edge,
        match_threshold=match_threshold,
    )
    return markets_export_df, opportunities_df, decision_engine_df, diagnostics_df, stats, match_stats


def main():
    args = parse_args()
    ensure_output_dirs()
    setup_log_file("pmxt_scan.log")
    configure_pmxt_runtime_environment()
    logger.info(
        "[PMXT_SCAN] Starting cross-exchange scan | limit={} | min_volume={} | include_incomplete={} | min_edge={} | match_threshold={} | max_fetch_retries={} | retry_delay_seconds={}",
        args.limit,
        args.min_volume,
        args.include_incomplete,
        args.min_edge,
        args.match_threshold,
        args.max_fetch_retries,
        args.retry_delay_seconds,
    )

    try:
        markets_export_df, opportunities_df, decision_engine_df, diagnostics_df, stats, match_stats = scan_with_pmxt(
            limit=args.limit,
            min_volume=args.min_volume,
            include_incomplete=args.include_incomplete,
            min_edge=args.min_edge,
            match_threshold=args.match_threshold,
            max_fetch_retries=args.max_fetch_retries,
            retry_delay_seconds=args.retry_delay_seconds,
        )
        if markets_export_df.empty:
            logger.warning("[PMXT_SCAN] No cross-exchange markets returned - check pmxt installation, Node.js, or API status.")
            print("No markets returned.")
            return

        output_paths = save_outputs(markets_export_df, opportunities_df, decision_engine_df, diagnostics_df)
        log_summary(stats, match_stats, markets_export_df, opportunities_df, decision_engine_df)
        print_console_preview(opportunities_df, args.top)
        print(f"\nMarket export rows: {len(markets_export_df)}")
        print(f"Opportunity rows: {len(opportunities_df)}")
        print(f"Decision-engine-ready rows: {len(decision_engine_df)}")
        print(f"Latest decision engine file: {output_paths['latest_decision']}")
        if opportunities_df.empty:
            print(f"No matched Kalshi opportunities passed the current thresholds. Diagnostics file: {output_paths['latest_diagnostics']}")
    except Exception as exc:
        logger.exception("[PMXT_SCAN] Unhandled scan failure: {}", exc)
        print("Main error:")
        print(traceback.format_exc())
        raise
    finally:
        logger.info("[PMXT_SCAN] Scan finished")


if __name__ == "__main__":
    main()