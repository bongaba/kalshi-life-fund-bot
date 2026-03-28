import json
import time
from config import GROK_DETAILED_LOG, XAI_API_KEY
from loguru import logger
from prompts import CATEGORY_PROMPTS, detect_category
from xai_sdk import Client
from xai_sdk.chat import user
from xai_sdk.tools import web_search, x_search


def log_grok_detail(message: str, *args) -> None:
    if GROK_DETAILED_LOG:
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



def get_grok_client() -> Client | None:
    if not XAI_API_KEY:
        return None
    return Client(api_key=XAI_API_KEY)



def get_grok_decision(
    market_title: str,
    yes_price: float,
    no_price: float,
    description: str,
    volume: int = 0,
    hours_to_close: float = 0
) -> dict:
    """
    Get a high-probability trade decision from Grok for a Kalshi market.
    Returns {'direction': 'YES'|'NO'|'HOLD', 'confidence': int, 'reason': str}
    """
    category = detect_category(market_title, description)

    _market_header = f"""Market: {market_title}
Current YES price: {yes_price:.2f}
Current NO price: {no_price:.2f}
Volume (24h): {volume:,}
Hours until close: {hours_to_close:.1f}
Description: {description or 'No description available'}"""

    prompt_body = CATEGORY_PROMPTS[category](
        market_title=market_title,
        description=description or '',
        yes_price=yes_price,
        no_price=no_price,
        hours_to_close=hours_to_close,
    )
    prompt = f"{_market_header}\n\n{prompt_body}"

    start_time = time.perf_counter()
    content = ""

    logger.info(
        "[GROK] Start | prompt={} | title={} | yes_price={:.2f} | no_price={:.2f} | volume={} | hours_to_close={:.2f}",
        category,
        market_title[:120],
        yes_price,
        no_price,
        volume,
        hours_to_close,
    )

    try:
        client = get_grok_client()
        if client is None:
            logger.warning("[GROK] XAI_API_KEY is not configured")
            return {"direction": "HOLD", "confidence": 0, "reason": "Grok API key not configured"}

        log_grok_detail("[GROK] Request | model={} | prompt_chars={}", "grok-4-1-fast-reasoning", len(prompt))
        chat = client.chat.create(
            model="grok-4-1-fast-reasoning",
            messages=[user(prompt)],
            tools=[web_search(), x_search()],
            tool_choice="required",
            temperature=0.1,
            max_tokens=300,
            max_turns=4,
        )
        response = chat.sample()

        # Get the content and clean it up
        content = str(response.content).strip()
        log_grok_detail(
            "[GROK] Raw response | chars={} | preview={}",
            len(content),
            content[:300].replace("\n", " "),
        )

        result, trailing_text = parse_first_json_object(content)
        if trailing_text:
            logger.warning(
                "[GROK] Trailing content ignored | chars={} | preview={}",
                len(trailing_text),
                trailing_text[:300].replace("\n", " "),
            )
        log_grok_detail("[GROK] Parsed JSON | payload={}", result)

        # Basic validation / sanitization
        direction = result.get('direction', 'HOLD')
        if direction not in ['YES', 'NO', 'HOLD']:
            direction = 'HOLD'

        confidence = result.get('confidence', 0)
        confidence = max(0, min(100, int(confidence) if isinstance(confidence, (int, float)) else 0))

        elapsed = time.perf_counter() - start_time
        logger.info(
            "[GROK] Completed | elapsed={:.2f}s | direction={} | confidence={} | reason={}",
            elapsed,
            direction,
            confidence,
            result.get('reason', 'No valid reason provided'),
        )

        return {
            "direction": direction,
            "confidence": confidence,
            "reason": result.get('reason', 'No valid reason provided')
        }

    except json.JSONDecodeError as e:
        elapsed = time.perf_counter() - start_time
        logger.warning(
            "[GROK] JSON decode failed | elapsed={:.2f}s | error={} | raw_response={}",
            elapsed,
            e,
            content[:400].replace("\n", " "),
        )
        return {"direction": "HOLD", "confidence": 0, "reason": "JSON parsing failed"}

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.warning("[GROK] Unexpected failure after {:.2f}s: {}: {}", elapsed, type(e).__name__, e)
        return {"direction": "HOLD", "confidence": 0, "reason": "Analysis failed - holding position"}