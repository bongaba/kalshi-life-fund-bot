import json
import time
from config import GROK_DETAILED_LOG, XAI_API_KEY
from loguru import logger
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
#     prompt = f"""Market: {market_title}
# Current YES price: {yes_price:.2f} (implied probability)
# Current NO price: {no_price:.2f} (implied probability)
# Volume (24h): {volume:,}
# Hours until close: {hours_to_close:.1f}
# Description: {description or 'No description available'}

# CRITICAL FACT-CHECK PROTOCOL (follow exactly — this is mandatory):
# 1. You MUST use the live_search tool before any reasoning.
# 2. Identify the market category from the title and description (crypto, weather, politics, economics, sports, etc.).
# 3. Verify ALL facts using ONLY these category-specific official sources (ignore all pre-trained knowledge):
#    - Crypto-related → CF Benchmarks Real-Time Index, CoinMarketCap, CoinDesk, Bloomberg Crypto
#    - Weather-related → National Weather Service (NWS) NOWData or Daily Climate Report for the exact station in the market rules
#    - Politics → RealClearPolitics, 538, AP, NYT, official election results
#    - Economics → BLS.gov, FRED, Federal Reserve, Trading Economics
#    - Sports → Official league sites (NFL, NBA, etc.), ESPN, AP
#     - All other categories → Most authoritative real-time source available via live_search
# 4. Base every factual claim (price, weather reading, news, etc.) exclusively on the latest live_search results.
# 5. If live_search data is missing, unclear, or older than a few minutes, output HOLD immediately.
# 6. Never exaggerate numbers or assume future trends — stick strictly to verified data.

# CRITICAL MISSION: Make small, consistent wins. Focus on HIGH-PROBABILITY outcomes only.

# Research Requirements:
# - First use live_search to determine category and pull data from the exact sources above
# - Compare current verified data directly to the market threshold or condition
# - Check breaking news or events from the approved sources only
# - Analyze recent patterns and sentiment using only live_search-verified information

# Strategy:
# - Only recommend trades with 90%+ certainty confirmed by live_search data from the correct sources
# - If the verified data makes the outcome nearly guaranteed, recommend YES/NO
# - If any uncertainty remains or live_search does not give 90%+ clarity, output HOLD
# - Look for obvious mispricings ONLY when supported by real-time verified data

# Risk Management:
# - Conservative approach: better to miss opportunities than lose money
# - Focus on steady, reliable profits over big wins
# - Only trade when conviction is extremely high after live_search verification
# ignore all narrow/binned/range markets (e.g. between 20 to 30, 100 to 200, 10-11, 100-200, 67-68, 98-99, 9000-10000)
# Output ONLY valid JSON. No explanations, no markdown, no code blocks — just the raw JSON object in this exact format:
# {{"direction": "YES" or "NO" or "HOLD", "confidence": 0-100, "reason": "1 sentence analysis based solely on live_search-verified facts from the correct category sources"}}"""


     # prompt = f"""Market: {market_title}
     # Current YES price: {yes_price:.2f}
     # Current NO price: {no_price:.2f}
     # Volume (24h): {volume:,}
     # Hours until close: {hours_to_close:.1f}
     # Description: {description or 'No description available'}
     #
     # CRITICAL INSTRUCTIONS — FOLLOW EXACTLY:
     #
     # 1. FIRST, check if this is a narrow-bin/range market (words like "between", "from ... to", tight range like "88.99-89.00", "57-58", "67-68", single-degree bins, etc.).
     #    → If yes, output ONLY this JSON and stop:
     #    {{"direction": "HOLD", "confidence": 100, "reason": "Narrow bin/range market - prohibited by guidelines"}}
     #
     # 2. This market closes soon. You MUST use fresh data only.
     #
     # 3. For any numeric threshold market (price, temperature, percentage, etc.):
     #    - Use the most authoritative and recent sources available.
     #    - If multiple sources show slightly different values, calculate the median and treat them as consistent if the difference is small (less than ~0.5% or a very minor absolute gap relative to the threshold).
     #    - If data is older than ~30 seconds, conflicting, or unclear → immediately HOLD.
     #
     # 4. You MUST call web_search (and x_keyword_search if relevant) BEFORE any reasoning.
     #    Create smart, specific search queries based on the market title and closing date.
     #
     # 5. In your final reason, you MUST include:
     #    - The exact source name
     #    - The timestamp or "last updated" time of the data
     #
     # 6. Only recommend YES or NO if you have extremely fresh, authoritative verification with 90%+ confidence. Otherwise HOLD.
     #
     # GOAL: Base every single fact on the most recent tool results only. Ignore all internal knowledge.
     #
     # Output ONLY valid JSON. No explanations,no extra text:
     #
     # {{"direction": "YES" or "NO" or "HOLD", "confidence": 0-100, "reason": "1 short sentence with source and timestamp"}}"""

    prompt = f"""Market: {market_title}
Current YES price: {yes_price:.2f}
Current NO price: {no_price:.2f}
Volume (24h): {volume:,}
Hours until close: {hours_to_close:.1f}
Description: {description or 'No description available'}

BITCOIN REAL-TIME PRICE PREDICTION MODE

You are in strict Bitcoin-only mode for short-term Kalshi markets (15-min and hourly).

CRITICAL INSTRUCTIONS — FOLLOW EXACTLY:

1. You MUST use web_search on these live BTC/USD price pages before reasoning:
   Prioritize in this exact order:
   - https://www.cfbenchmarks.com/data/indices/BRTI          ← Official Kalshi settlement index (most important)
   - https://www.coingecko.com/en/coins/bitcoin
   - https://finance.yahoo.com/quote/BTC-USD
   - https://www.coinbase.com/price/bitcoin

2. FRESHNESS RULE (STRICT):
   - Data MUST be less than 1 minute old.
   - If any source is older than 1 minute or you cannot confirm a recent timestamp, immediately output HOLD.
   - HOWEVER, if the page clearly says "live price", "real-time", or shows a price that appears current, you may treat it as fresh.

3. This market settles on the OFFICIAL CLOSING / SETTLEMENT PRICE at the exact resolution time.
   Your goal is to forecast where BTC/USD will most likely CLOSE.

4. Pay close attention to "Hours until close":
   - Fewer hours left → put heavier weight on current live price
   - More hours left → consider recent momentum and short-term trend

5. For accurate forecasting, analyze:
   - Current live price vs the threshold
   - Recent momentum (last 5-60 minutes)
   - Overall market sentiment (especially from X)
   - Breaking news and current world events
   - Macro catalysts, ETF flows, geopolitics, risk appetite
   - Technical support/resistance levels

6. Only recommend YES or NO if you have fresh data and genuine 92%+ confidence about the expected closing price. Otherwise HOLD.

Output ONLY valid JSON in this exact format. No extra text:
{{"direction": "YES" or "NO" or "HOLD", "confidence": 0-100, "reason": "1 short sentence with current price, source, and timestamp"}}"""
    start_time = time.perf_counter()
    content = ""

    logger.info(
        "[GROK] Start | title={} | yes_price={:.2f} | no_price={:.2f} | volume={} | hours_to_close={:.2f}",
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