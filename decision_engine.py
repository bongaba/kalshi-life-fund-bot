from grok_analyzer import get_grok_decision
from config import *
from loguru import logger
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# Load the trained historical model
# try:
#     historical_model = joblib.load('historical_model.pkl')
#     print("[DECISION_ENGINE] Historical ML model loaded successfully")
# except Exception as e:
#     print(f"[DECISION_ENGINE] Failed to load historical model: {e}")
#     historical_model = None

# Feature encoding mappings (from training)
# market_category_mapping = {'other': 0, 'basketball': 1, 'football': 2, 'soccer': 3, 'baseball': 4, 'hockey': 5, 'tennis': 6, 'golf': 7}
# market_type_mapping = {'binary': 0}


def calculate_undervalued_market(yes_price: float, no_price: float) -> dict:
    dominant_direction = "YES" if yes_price >= no_price else "NO"
    dominant_price = yes_price if dominant_direction == "YES" else no_price
    is_undervalued = UNDERVALUED_MIN_PROBABILITY <= dominant_price < INTERNAL_HIGH_PROBABILITY_THRESHOLD

    return {
        "is_undervalued": is_undervalued,
        "direction": dominant_direction if is_undervalued else "HOLD",
        "dominant_price": dominant_price,
        "reason": (
            f"near_threshold_value({dominant_price:.3f} in [{UNDERVALUED_MIN_PROBABILITY:.2f}, {INTERNAL_HIGH_PROBABILITY_THRESHOLD:.2f}))"
            if is_undervalued
            else "not_undervalued"
        ),
    }


def internal_model_decision(yes_price: float, no_price: float) -> dict:
    """
    Pre-filter for high-probability markets.
    By default only considers markets where implied probability is above the
    strict high-probability threshold. When undervalued markets are enabled,
    it can also surface near-threshold dominant sides as value opportunities.
    """
    logger.info(
        f"[DECISION_ENGINE] internal_model_decision starting | yes_price={yes_price:.3f} | no_price={no_price:.3f} | "
        f"high_probability_threshold={INTERNAL_HIGH_PROBABILITY_THRESHOLD:.2f} | "
        f"high_probability_upper_limit={INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT:.2f} | "
        f"use_undervalued_markets={USE_UNDERVALUED_MARKETS}"
    )

    if INTERNAL_HIGH_PROBABILITY_THRESHOLD <= yes_price <= INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT:
        return {"direction": "YES", "is_undervalued": False, "reason": "high_probability_yes"}
    if INTERNAL_HIGH_PROBABILITY_THRESHOLD <= no_price <= INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT:
        return {"direction": "NO", "is_undervalued": False, "reason": "high_probability_no"}

    # Ultra-high probability: above upper limit but below 1.0 — allocate 50% of cash
    if INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT < yes_price < 1.0:
        return {"direction": "YES", "is_undervalued": False, "reason": "ultra_high_probability_yes", "half_cash_sizing": True}
    if INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT < no_price < 1.0:
        return {"direction": "NO", "is_undervalued": False, "reason": "ultra_high_probability_no", "half_cash_sizing": True}

    if USE_UNDERVALUED_MARKETS:
        undervalued_result = calculate_undervalued_market(yes_price, no_price)
        if undervalued_result["is_undervalued"]:
            logger.info(
                "[DECISION_ENGINE] internal_model_decision undervalued match | direction={} | dominant_price={:.3f} | reason={}",
                undervalued_result["direction"],
                undervalued_result["dominant_price"],
                undervalued_result["reason"],
            )
            return {
                "direction": undervalued_result["direction"],
                "is_undervalued": True,
                "reason": undervalued_result["reason"],
            }

    return {"direction": "HOLD", "is_undervalued": False, "reason": "not_high_probability_enough"}

def calculate_hours_to_close(close_time) -> float | None:
    if close_time in (None, ""):
        return None

    close_timestamp = None

    try:
        if isinstance(close_time, str):
            close_time = close_time.strip()
            if not close_time:
                return None

            if close_time.isdigit():
                close_timestamp = float(close_time)
            else:
                parsed_datetime = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                if parsed_datetime.tzinfo is None:
                    parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
                close_timestamp = parsed_datetime.timestamp()
        elif isinstance(close_time, (int, float)):
            close_timestamp = float(close_time)
        else:
            return None

        if close_timestamp > 100000000000:
            close_timestamp /= 1000.0

        current_timestamp = datetime.now(timezone.utc).timestamp()
        return max(0.0, (close_timestamp - current_timestamp) / 3600.0)
    except (TypeError, ValueError, OverflowError):
        return None


def should_bypass_volume_gate() -> bool:
    return OVERRIDE_INTERNAL_MODEL_WITH_GROK and OVERRIDE_GROK_IGNORE_VOLUME_GATE


def decide_with_grok_override(
    market_title: str,
    yes_price: float,
    no_price: float,
    description: str,
    volume: int,
    hours_to_close: float,
) -> dict | None:
    logger.info(
        f"[DECISION_ENGINE] Grok override starting | title={market_title[:120]} | threshold={ANALYZER_CONFIDENCE_THRESHOLD}"
    )

    grok_result = get_grok_decision(
        market_title,
        yes_price,
        no_price,
        description,
        volume,
        hours_to_close,
    )

    rejection_reasons = []
    if grok_result['direction'] == 'HOLD':
        rejection_reasons.append("grok_hold")
    if grok_result['confidence'] < ANALYZER_CONFIDENCE_THRESHOLD:
        rejection_reasons.append(
            f"confidence_below_threshold({grok_result['confidence']}<{ANALYZER_CONFIDENCE_THRESHOLD})"
        )

    if rejection_reasons:
        logger.info(
            f"[DECISION_ENGINE] Rejected in Grok override mode | title={market_title[:120]} | "
            f"direction={grok_result['direction']} | confidence={grok_result['confidence']} | "
            f"reasons={', '.join(rejection_reasons)}"
        )
        return None

    final_confidence = grok_result['confidence']
    size = RISK_PER_TRADE * (final_confidence / 100)

    return {
        "direction": grok_result['direction'],
        "size": round(size, 2),
        "confidence": final_confidence,
        "is_undervalued": False,
        "reason": f"Grok override: {grok_result['reason']}",
    }

def analyze_market_with_validators(
    market_title: str,
    yes_price: float,
    no_price: float,
    description: str,
    volume: int,
    hours_to_close: float,
    internal_direction: str,
) -> tuple[dict | None, dict | None]:
    logger.info(
        f"[DECISION_ENGINE] Validation starting | internal_decision={internal_direction} | use_grok={USE_GROK}"
    )

    if not USE_GROK:
        return None, None

    with ThreadPoolExecutor(max_workers=1) as executor:
        grok_future = None
        if USE_GROK:
            grok_future = executor.submit(
                get_grok_decision,
                market_title,
                yes_price,
                no_price,
                description,
                volume,
                hours_to_close,
            )
        if grok_future is not None:
            try:
                grok_result = grok_future.result(timeout=90)
            except Exception as e:
                logger.error(f"[DECISION_ENGINE] Grok validator failed: {e} — falling back to HOLD")
                grok_result = {"direction": "HOLD", "confidence": 0, "reason": f"Grok error: {e}"}
        else:
            grok_result = None
    return None, grok_result

def should_trade(market: dict) -> dict | None:
    yes_price = market.get('yes_price')
    no_price = market.get('no_price')
    bypass_volume_gate = should_bypass_volume_gate()

    if yes_price is None:
        logger.info(f"[DECISION_ENGINE] Skipping {market.get('ticker', 'UNKNOWN')}: no exact price provided")
        return None

    if no_price is None:
        logger.info(f"[DECISION_ENGINE] Skipping {market.get('ticker', 'UNKNOWN')}: no exact NO price provided")
        return None

    volume = market.get('volume')
    if volume is None and not bypass_volume_gate:
        logger.info(f"[DECISION_ENGINE] Skipping {market.get('ticker', 'UNKNOWN')}: no exact volume provided")
        return None
    if volume is None:
        volume = 0

    close_time = market.get('close_time')

    hours_to_close = calculate_hours_to_close(close_time)
    if hours_to_close is None:
        logger.info(
            f"[DECISION_ENGINE] Skipping {market.get('ticker', 'UNKNOWN')}: "
            f"invalid close_time={close_time!r}"
        )
        return None

    # Skip if too close to expiration (high risk)
    if hours_to_close < MIN_HOURS_TO_CLOSE:
        logger.info(
            f"[DECISION_ENGINE] Skipping {market.get('ticker', 'UNKNOWN')}: "
            f"hours_to_close={hours_to_close:.2f} < min_threshold={MIN_HOURS_TO_CLOSE:.2f}"
        )
        return None

    # Skip low volume markets
    if volume < VOLUME_THRESHOLD and not bypass_volume_gate:
        return None

    if bypass_volume_gate:
        logger.info(
            f"[DECISION_ENGINE] Bypassing volume gate in Grok override mode | ticker={market.get('ticker', 'UNKNOWN')} | "
            f"volume={volume} | hours_to_close={hours_to_close:.2f}"
        )

    market_description = market.get('subtitle') or market.get('description', '')

    if OVERRIDE_INTERNAL_MODEL_WITH_GROK:
        logger.info(
            f"[DECISION_ENGINE] Grok override enabled | ticker={market.get('ticker', 'UNKNOWN')} | internal model bypassed"
        )
        return decide_with_grok_override(
            market['title'],
            yes_price,
            no_price,
            market_description,
            volume,
            hours_to_close,
        )

    # Step 1: Internal pre-filter for high-probability markets
    internal_result = internal_model_decision(yes_price, no_price)
    internal_decision = internal_result["direction"]
    if internal_decision == "HOLD":
        return None  # Not high probability enough, skip

    # Ultra-high probability: above upper limit — skip Grok, execute immediately with 50% cash
    if internal_result.get("half_cash_sizing"):
        logger.info(
            f"[DECISION_ENGINE] Ultra-high probability — skipping Grok | ticker={market.get('ticker', 'UNKNOWN')} | "
            f"direction={internal_decision} | reason={internal_result['reason']}"
        )
        return {
            "direction": internal_decision,
            "size": round(RISK_PER_TRADE, 2),
            "confidence": 100,
            "is_undervalued": False,
            "reason": f"Ultra-high probability: {internal_result['reason']} (Grok bypassed)",
            "half_cash_sizing": True,
        }

    logger.info(
        f"[DECISION_ENGINE] External validation starting | ticker={market.get('ticker', 'UNKNOWN')} | "
        f"internal_decision={internal_decision} | undervalued={internal_result['is_undervalued']}"
    )
    _, grok = analyze_market_with_validators(
        market['title'],
        yes_price,
        no_price,
        market_description,
        volume,
        hours_to_close,
        internal_decision,
    )

    grok_summary = (
        f"{grok['direction']}({grok['confidence']})"
        if grok is not None
        else "DISABLED"
    )

    logger.info(
        f"[DECISION_ENGINE] Validator results | ticker={market.get('ticker', 'UNKNOWN')} | "
        f"internal={internal_decision} | undervalued={internal_result['is_undervalued']} | "
        f"grok={grok_summary}"
    )

    if USE_GROK and grok is not None:
        rejection_reasons = []
        if grok['direction'] != internal_decision:
            rejection_reasons.append(f"direction_mismatch(expected={internal_decision}, got={grok['direction']})")
        if grok['direction'] == 'HOLD':
            rejection_reasons.append("validator_hold")
        if grok['confidence'] < ANALYZER_CONFIDENCE_THRESHOLD:
            rejection_reasons.append(
                f"confidence_below_threshold({grok['confidence']}<{ANALYZER_CONFIDENCE_THRESHOLD})"
            )

        if rejection_reasons:
            logger.info(
                f"[DECISION_ENGINE] Rejected after validation | ticker={market.get('ticker', 'UNKNOWN')} | "
                f"validator=Grok | direction={grok['direction']} | "
                f"confidence={grok['confidence']} | threshold={ANALYZER_CONFIDENCE_THRESHOLD} | "
                f"reasons={', '.join(rejection_reasons)}"
            )
            return None

        base_size = RISK_PER_TRADE
        final_confidence = grok['confidence']
        confidence_multiplier = final_confidence / 100
        size = base_size * confidence_multiplier

        internal_reason = f"Internal: {internal_decision}"
        if internal_result["is_undervalued"]:
            internal_reason += f" (undervalued=true, {internal_result['reason']})"
        reason_parts = [internal_reason, f"Grok: {grok['reason']}"]

        result = {
            "direction": internal_decision,
            "size": round(size, 2),
            "confidence": final_confidence,
            "is_undervalued": internal_result["is_undervalued"],
            "reason": " | ".join(reason_parts),
        }
        if internal_result.get("half_cash_sizing"):
            result["half_cash_sizing"] = True
        return result

    logger.info(
        f"[DECISION_ENGINE] Rejected after validation | ticker={market.get('ticker', 'UNKNOWN')} | no external validators enabled"
    )
    return None