import json
import time

import requests
from loguru import logger

from config import GEMINI_API_KEY, GEMINI_DETAILED_LOG, GEMINI_MODEL, GEMINI_TIMEOUT

ALLOWED_DIRECTIONS = {"YES", "NO", "HOLD"}


def log_gemini_detail(message: str, *args) -> None:
    if GEMINI_DETAILED_LOG:
        logger.info(message, *args)


def clean_model_response_text(text: str) -> str:
    content = text.strip()
    if content.startswith("```json"):
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif content.startswith("```"):
        content = content.split("```", 2)[1].strip()
    return content


def parse_first_json_object(text: str) -> tuple[dict, str]:
    content = clean_model_response_text(text)
    first_brace = content.find("{")
    if first_brace == -1:
        raise json.JSONDecodeError("No JSON object found", content, 0)

    decoder = json.JSONDecoder()
    parsed, end_index = decoder.raw_decode(content[first_brace:])
    trailing_text = content[first_brace + end_index:].strip()
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("Top-level JSON value is not an object", content, first_brace)
    return parsed, trailing_text


def normalize_gemini_decision(result: dict) -> dict:
    if not isinstance(result, dict):
        result = {}

    direction = str(result.get("direction", "HOLD")).upper()
    if direction not in ALLOWED_DIRECTIONS:
        direction = "HOLD"

    confidence = result.get("confidence", 0)
    if isinstance(confidence, bool):
        confidence = 0
    elif not isinstance(confidence, (int, float)):
        confidence = 0

    hold_type = result.get("hold_type")
    if direction == "HOLD":
        hold_type = str(hold_type or "model_hold")
    else:
        hold_type = None

    return {
        "direction": direction,
        "confidence": max(0, min(100, int(confidence))),
        "reason": str(result.get("reason", "Gemini returned no reason")),
        "hold_type": hold_type,
        "data_source": "gemini",
    }


def build_gemini_prompt(
    market_title: str,
    yes_price: float,
    no_price: float,
    description: str,
    volume: int,
    hours_to_close: float,
    internal_direction: str,
) -> str:
    return f"""You are validating a Kalshi trade signal.

Market: {market_title}
Current YES price: {yes_price:.2f} (implied probability)
Current NO price: {no_price:.2f} (implied probability)
Volume (24h): {volume:,}
Hours until close: {hours_to_close:.1f}
Internal prefilter direction: {internal_direction}
Description: {description or 'No description available'}

Rules:
- Treat the internal prefilter direction as the only direction worth validating.
- If the market appears to be a narrow-bin or range-style question, return HOLD.
- Approve a trade only when that direction is strongly supported by current evidence.
- If uncertain, contradictory, or stale, return HOLD.
- Keep reason concise and concrete.

Output ONLY valid JSON in this exact format:
{{"direction": "YES" or "NO" or "HOLD", "confidence": 0-100, "reason": "1 short sentence", "hold_type": "optional when HOLD"}}"""


def extract_response_text(response_json: dict) -> str:
    candidates = response_json.get("candidates") or []
    if not candidates:
        return ""

    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []

    text_chunks = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text_chunks.append(part["text"])

    return "\n".join(text_chunks).strip()


def get_gemini_decision(
    market_title: str,
    yes_price: float,
    no_price: float,
    description: str,
    volume: int = 0,
    hours_to_close: float = 0,
    internal_direction: str = "HOLD",
) -> dict:
    start_time = time.perf_counter()

    logger.info(
        "[GEMINI] Start | title={} | internal_direction={} | yes_price={:.2f} | no_price={:.2f} | volume={} | hours_to_close={:.2f}",
        market_title[:120],
        internal_direction,
        yes_price,
        no_price,
        volume,
        hours_to_close,
    )

    if not GEMINI_API_KEY:
        logger.warning("[GEMINI] GEMINI_API_KEY is not configured")
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Gemini API key not configured",
            "hold_type": "missing_gemini_api_key",
        }

    prompt = build_gemini_prompt(
        market_title,
        yes_price,
        no_price,
        description,
        volume,
        hours_to_close,
        internal_direction,
    )

    request_body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 300,
            "responseMimeType": "application/json",
        },
    }

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    params = {"key": GEMINI_API_KEY}

    try:
        log_gemini_detail(
            "[GEMINI] Request | model={} | timeout={}s | prompt_chars={}",
            GEMINI_MODEL,
            GEMINI_TIMEOUT,
            len(prompt),
        )
        response = requests.post(
            endpoint,
            params=params,
            json=request_body,
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
        raw_json = response.json()
        response_text = extract_response_text(raw_json)

        log_gemini_detail(
            "[GEMINI] Raw response text | chars={} | preview={}",
            len(response_text),
            response_text[:300].replace("\n", " "),
        )

        parsed, trailing_text = parse_first_json_object(response_text)
        if trailing_text:
            logger.warning(
                "[GEMINI] Trailing content ignored | chars={} | preview={}",
                len(trailing_text),
                trailing_text[:300].replace("\n", " "),
            )

        normalized = normalize_gemini_decision(parsed)
        elapsed = time.perf_counter() - start_time
        outcome = "model_hold" if normalized["direction"] == "HOLD" else "validated_trade"
        logger.info(
            "[GEMINI] Completed | elapsed={:.2f}s | outcome={} | direction={} | confidence={} | hold_type={} | reason={}",
            elapsed,
            outcome,
            normalized["direction"],
            normalized["confidence"],
            normalized.get("hold_type"),
            normalized["reason"],
        )
        return normalized
    except requests.HTTPError as error:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", "unknown")
        body = ""
        if response is not None:
            body = (response.text or "")[:400]
        elapsed = time.perf_counter() - start_time
        logger.warning(
            "[GEMINI] HTTP failure | elapsed={:.2f}s | status={} | error={} | body={}",
            elapsed,
            status,
            error,
            body,
        )
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Gemini HTTP request failed",
            "hold_type": "gemini_http_failure",
        }
    except json.JSONDecodeError as error:
        elapsed = time.perf_counter() - start_time
        logger.warning(
            "[GEMINI] JSON decode failed | elapsed={:.2f}s | error={}",
            elapsed,
            error,
        )
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Gemini JSON parsing failed",
            "hold_type": "gemini_json_parse_failure",
        }
    except Exception as error:
        elapsed = time.perf_counter() - start_time
        logger.warning("[GEMINI] Unexpected failure after {:.2f}s: {}", elapsed, error)
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Gemini analysis failed",
            "hold_type": "gemini_runtime_failure",
        }
