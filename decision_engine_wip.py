from grok_analyzer import get_grok_decision
from config import *

def internal_model_decision(yes_price: float) -> str:
    # More conservative thresholds to reduce risk
    if yes_price <= 0.35: return "YES"
    if yes_price >= 0.65: return "NO"
    return "HOLD"

def should_trade(market: dict) -> dict | None:
    yes_price = market.get('yes_price') or (market.get('last_price') or 0.50)
    volume = market.get('volume', 0)
    close_time = market.get('close_time')
    
    # Calculate hours to close
    hours_to_close = 0
    if close_time:
        try:
            import time
            hours_to_close = max(0, (close_time - time.time()) / 3600)
        except:
            hours_to_close = 12  # Default
    
    internal = internal_model_decision(yes_price)
    if internal == "HOLD":
        return None
    
    # Skip if too close to expiration (high risk)
    if hours_to_close < 2:
        return None
    
    # Skip low volume markets
    if volume < VOLUME_THRESHOLD:
        return None

    grok = get_grok_decision(
        market['title'],
        yes_price,
        market.get('subtitle') or market.get('description', ''),
        volume,
        hours_to_close
    )

    # Require agreement and high confidence
    if grok['direction'] == internal and grok['confidence'] >= 85:
        # Size based on confidence (max $3)
        base_size = 3.0
        confidence_multiplier = grok['confidence'] / 100
        size = min(base_size * confidence_multiplier, 3.0)
        size = max(size, 1.0)  # Minimum $1
        
        return {
            "direction": internal,
            "size": round(size, 2),
            "confidence": grok['confidence'],
            "reason": grok['reason']
        }
    return None