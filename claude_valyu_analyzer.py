import json
import time

import requests
from loguru import logger

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_VALYU_DETAILED_LOG,
    CLAUDE_MODEL,
    VALYU_API_KEY,
    VALYU_API_URL,
    VALYU_DEEP_RESEARCH,
    VALYU_INCLUDED_SOURCES,
    VALYU_MAX_RESULTS,
    VALYU_SEARCH_TYPE,
    VALYU_TIMEOUT,
)

ALLOWED_DIRECTIONS = {"YES", "NO", "HOLD"}


def log_claude_valyu_detail(message: str, *args) -> None:
    if CLAUDE_VALYU_DETAILED_LOG:
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


def summarize_valyu_results(results: list[dict], max_items: int = 3) -> str:
    preview = []
    for item in results[:max_items]:
        source = item.get("source") or "Unknown source"
        title = item.get("title") or "Untitled source"
        preview.append(f"{source}: {title[:80]}")
    return " | ".join(preview) if preview else "no sources"


def log_valyu_evidence(results: list[dict], max_items: int = 6) -> None:
    if not CLAUDE_VALYU_DETAILED_LOG:
        return
    for index, item in enumerate(results[:max_items], start=1):
        logger.info(
            "[CLAUDE_VALYU][EVIDENCE][{}] source={} | published_at={} | title={} | snippet={} | url={}",
            index,
            item.get("source") or "Unknown source",
            item.get("published_at") or "Unknown time",
            (item.get("title") or "Untitled source")[:160],
            (item.get("snippet") or "")[:300].replace("\n", " "),
            item.get("url") or "N/A",
        )


def build_valyu_query(
    market_title: str,
    description: str,
    hours_to_close: float,
    internal_direction: str,
) -> str:
    return (
        f"Latest verified data for Kalshi market '{market_title}'. "
        f"Internal direction to validate: {internal_direction}. "
        f"Market closes in {hours_to_close:.1f} hours. "
        f"Description: {description or 'No description available'}. "
        "Focus on fresh authoritative sources, exact threshold-relevant facts, and current conditions only."
    )


def normalize_valyu_results(payload: object) -> list[dict]:
    if isinstance(payload, list):
        raw_results = payload
    elif isinstance(payload, dict):
        raw_results = payload.get("results") or payload.get("data") or payload.get("items") or []
    else:
        raw_results = []

    normalized = []
    for item in raw_results[:VALYU_MAX_RESULTS]:
        if not isinstance(item, dict):
            continue

        normalized.append(
            {
                "title": str(item.get("title") or item.get("headline") or "Untitled source"),
                "source": str(item.get("source") or item.get("domain") or item.get("source_type") or "Unknown source"),
                "published_at": str(item.get("publication_date") or item.get("published_at") or item.get("published") or item.get("date") or "Unknown time"),
                "snippet": str(item.get("snippet") or item.get("description") or item.get("content") or item.get("summary") or item.get("text") or ""),
                "url": str(item.get("url") or item.get("link") or ""),
            }
        )

    return normalized


def valyu_research(
    market_title: str,
    description: str,
    hours_to_close: float,
    internal_direction: str,
) -> list[dict]:
    if not VALYU_API_KEY:
        logger.warning("[CLAUDE_VALYU] VALYU_API_KEY is not configured")
        return []

    headers = {
        "X-API-Key": VALYU_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "query": build_valyu_query(market_title, description, hours_to_close, internal_direction),
        "max_num_results": VALYU_MAX_RESULTS,
        "search_type": VALYU_SEARCH_TYPE,
        "instructions": "Prioritize fresh authoritative threshold-relevant sources for real-time market validation.",
        "response_length": "short",
        "fast_mode": not VALYU_DEEP_RESEARCH,
        "is_tool_call": True,
    }

    if VALYU_INCLUDED_SOURCES:
        payload["included_sources"] = VALYU_INCLUDED_SOURCES

    log_claude_valyu_detail(
        "[CLAUDE_VALYU] Valyu request | title={} | direction={} | hours_to_close={:.2f} | search_type={} | max_results={} | deep_research={}",
        market_title[:120],
        internal_direction,
        hours_to_close,
        VALYU_SEARCH_TYPE,
        VALYU_MAX_RESULTS,
        VALYU_DEEP_RESEARCH,
    )

    try:
        response = requests.post(
            VALYU_API_URL,
            headers=headers,
            json=payload,
            timeout=VALYU_TIMEOUT,
        )
        response.raise_for_status()
        normalized = normalize_valyu_results(response.json())
        log_claude_valyu_detail(
            "[CLAUDE_VALYU] Valyu response | status={} | usable_results={} | preview={}",
            response.status_code,
            len(normalized),
            summarize_valyu_results(normalized),
        )
        if not normalized:
            logger.warning(
                "[CLAUDE_VALYU] Valyu returned zero usable results | title={} | direction={}",
                market_title[:120],
                internal_direction,
            )
        return normalized
    except requests.HTTPError as error:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", "unknown")
        body = ""
        if response is not None:
            body = (response.text or "")[:400]
        logger.warning(
            "[CLAUDE_VALYU] Valyu HTTP failure | status={} | error={} | body={}",
            status,
            error,
            body,
        )
        return []
    except Exception as error:
        logger.warning("[CLAUDE_VALYU] Valyu request failed: {}", error)
        return []


def build_claude_prompt(
    market_title: str,
    yes_price: float,
    no_price: float,
    description: str,
    volume: int,
    hours_to_close: float,
    internal_direction: str,
    valyu_results: list[dict],
) -> str:
    valyu_text = "\n\n".join(
        (
            f"[{index + 1}] {result['title']}\n"
            f"Source: {result['source']} | {result['published_at']}\n"
            f"Snippet: {result['snippet'][:400]}\n"
            f"URL: {result['url'] or 'N/A'}"
        )
        for index, result in enumerate(valyu_results[:6])
    )

    return f"""Market: {market_title}
Current YES price: {yes_price:.2f} (implied probability)
Current NO price: {no_price:.2f} (implied probability)
Volume (24h): {volume:,}
Hours until close: {hours_to_close:.1f}
Internal prefilter direction: {internal_direction}
Description: {description or 'No description available'}

Fresh Valyu evidence:
{valyu_text or 'No fresh Valyu data returned.'}

MANDATORY:
- Treat the internal prefilter direction as the only direction worth validating.
- Base every factual claim strictly on the Valyu evidence above.
- Only approve a trade if current evidence makes that direction 90%+ likely.
- Ignore narrow bins or range markets.
- If evidence is unclear, stale, contradictory, or not nearly guaranteed, return HOLD.
- Do not use outside knowledge or assumptions.

Output ONLY valid JSON in this exact format:
{{"direction": "YES" or "NO" or "HOLD", "confidence": 0-100, "reason": "1 sentence from Valyu-backed evidence"}}"""


def extract_text_from_claude_response(message: object) -> str:
    blocks = getattr(message, "content", []) or []
    parts = []
    for block in blocks:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def normalize_claude_decision(result: dict) -> dict:
    if not isinstance(result, dict):
        result = {}

    direction = result.get("direction", "HOLD")
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
        "reason": str(result.get("reason", "Claude returned no reason")),
        "hold_type": hold_type,
        "used_valyu": True,
        "data_source": "valyu_api",
    }


def get_claude_valyu_decision(
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
        "[CLAUDE_VALYU] Start | title={} | internal_direction={} | yes_price={:.2f} | no_price={:.2f} | volume={} | hours_to_close={:.2f}",
        market_title[:120],
        internal_direction,
        yes_price,
        no_price,
        volume,
        hours_to_close,
    )

    if not ANTHROPIC_API_KEY:
        logger.warning("[CLAUDE_VALYU] ANTHROPIC_API_KEY is not configured")
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Anthropic API key not configured",
            "hold_type": "missing_anthropic_api_key",
        }

    valyu_results = valyu_research(market_title, description, hours_to_close, internal_direction)
    if not valyu_results:
        elapsed = time.perf_counter() - start_time
        logger.warning(
            "[CLAUDE_VALYU] Aborted before Claude call | elapsed={:.2f}s | reason=Valyu returned no usable evidence",
            elapsed,
        )
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Valyu returned no usable evidence",
            "hold_type": "valyu_no_evidence",
        }

    prompt = build_claude_prompt(
        market_title,
        yes_price,
        no_price,
        description,
        volume,
        hours_to_close,
        internal_direction,
        valyu_results,
    )

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        log_claude_valyu_detail(
            "[CLAUDE_VALYU] Claude request | model={} | evidence_count={} | prompt_chars={}",
            CLAUDE_MODEL,
            len(valyu_results),
            len(prompt),
        )
        log_valyu_evidence(valyu_results)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )

        content = extract_text_from_claude_response(message)
        log_claude_valyu_detail(
            "[CLAUDE_VALYU] Claude raw response | chars={} | preview={}",
            len(content),
            content[:300].replace("\n", " "),
        )
        parsed, trailing_text = parse_first_json_object(content)
        if trailing_text:
            logger.warning(
                "[CLAUDE_VALYU] Claude trailing content ignored | chars={} | preview={}",
                len(trailing_text),
                trailing_text[:300].replace("\n", " "),
            )
        log_claude_valyu_detail("[CLAUDE_VALYU] Claude parsed JSON | payload={}", parsed)
        normalized = normalize_claude_decision(parsed)
        elapsed = time.perf_counter() - start_time
        outcome = "model_hold" if normalized["direction"] == "HOLD" else "validated_trade"
        logger.info(
            "[CLAUDE_VALYU] Completed | elapsed={:.2f}s | outcome={} | direction={} | confidence={} | evidence_count={} | hold_type={} | reason={}",
            elapsed,
            outcome,
            normalized["direction"],
            normalized["confidence"],
            len(valyu_results),
            normalized.get("hold_type"),
            normalized["reason"],
        )
        return normalized
    except ImportError:
        logger.warning("[CLAUDE_VALYU] anthropic package is not installed")
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "anthropic package not installed",
            "hold_type": "missing_anthropic_package",
        }
    except json.JSONDecodeError as error:
        elapsed = time.perf_counter() - start_time
        logger.warning(
            "[CLAUDE_VALYU] JSON decode failed | elapsed={:.2f}s | error={} | raw_response={}",
            elapsed,
            error,
            content[:400].replace("\n", " "),
        )
        logger.warning(
            "[CLAUDE_VALYU][PARSE_ISSUE] model={} | title={} | evidence_count={} | parser_error={}",
            CLAUDE_MODEL,
            market_title[:120],
            len(valyu_results),
            error,
        )
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Claude JSON parsing failed",
            "hold_type": "claude_json_parse_failure",
        }
    except Exception as error:
        elapsed = time.perf_counter() - start_time
        logger.warning("[CLAUDE_VALYU] Unexpected failure after {:.2f}s: {}", elapsed, error)
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "Claude-Valyu analysis failed",
            "hold_type": "claude_runtime_failure",
        }