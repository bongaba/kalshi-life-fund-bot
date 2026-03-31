"""Interactive Discord bot for SL/TP approval via reactions.

Uses Discord REST API directly (no discord.py dependency).
Sends embed messages with ✅/❌ reactions, polls for user response.
"""

import time
import requests
from loguru import logger
from config import (
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    DISCORD_APPROVAL_TIMEOUT_SECONDS,
)

DISCORD_API_BASE = "https://discord.com/api/v10"
APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

# Cache the bot's own user ID to filter out its own reactions
_BOT_USER_ID = None


def _headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _get_bot_user_id():
    """Fetch and cache the bot's own user ID."""
    global _BOT_USER_ID
    if _BOT_USER_ID:
        return _BOT_USER_ID
    try:
        resp = requests.get(f"{DISCORD_API_BASE}/users/@me", headers=_headers(), timeout=10)
        if resp.status_code == 200:
            _BOT_USER_ID = resp.json().get("id")
            return _BOT_USER_ID
    except Exception as e:
        logger.warning(f"[DISCORD_BOT] Failed to get bot user ID: {e}")
    return None


def is_configured():
    """Check if the Discord bot is properly configured."""
    return bool(DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)


def send_approval_request(
    ticker: str,
    trigger: str,
    direction: str,
    contracts: int,
    entry_price: float,
    current_price: float,
    unrealized_pnl: float,
    pnl_pct: float,
    reason: str = "",
    ttc_seconds: int = None,
) -> str:
    """Send a SL/TP approval request to Discord and return the message ID.

    Returns:
        message_id (str) if sent successfully, None otherwise.
    """
    if not is_configured():
        return None

    # Color: red for stop-loss, green for take-profit
    color = 0xFF4444 if trigger == "stop_loss" else 0x44FF44
    trigger_label = "🛑 STOP-LOSS" if trigger == "stop_loss" else "💰 TAKE-PROFIT"

    pnl_sign = "+" if unrealized_pnl >= 0 else ""
    ttc_str = f"{ttc_seconds // 60}m {ttc_seconds % 60}s" if ttc_seconds else "N/A"

    embed = {
        "title": f"{trigger_label} TRIGGER",
        "description": (
            f"**{ticker}**\n"
            f"React ✅ to **execute exit** | ❌ to **hold position**\n"
            f"No response = hold (expires in {DISCORD_APPROVAL_TIMEOUT_SECONDS}s)"
        ),
        "color": color,
        "fields": [
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "Contracts", "value": str(contracts), "inline": True},
            {"name": "Entry", "value": f"${entry_price:.3f}", "inline": True},
            {"name": "Current", "value": f"${current_price:.3f}", "inline": True},
            {"name": "P&L", "value": f"{pnl_sign}${unrealized_pnl:.2f} ({pnl_sign}{pnl_pct:.1f}%)", "inline": True},
            {"name": "Time to Close", "value": ttc_str, "inline": True},
            {"name": "Reason", "value": reason[:200] if reason else "—", "inline": False},
        ],
    }

    payload = {
        "embeds": [embed],
    }

    try:
        resp = requests.post(
            f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            message_id = resp.json().get("id")
            logger.info(f"[DISCORD_BOT] Approval request sent for {ticker} (msg_id={message_id})")
            # Add reaction emojis
            _add_reaction(message_id, APPROVE_EMOJI)
            _add_reaction(message_id, REJECT_EMOJI)
            return message_id
        else:
            logger.error(f"[DISCORD_BOT] Failed to send message: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        logger.error(f"[DISCORD_BOT] Error sending approval request: {e}")
        return None


def _add_reaction(message_id: str, emoji: str):
    """Add a reaction emoji to a message."""
    # URL-encode the emoji for the API
    import urllib.parse
    encoded = urllib.parse.quote(emoji)
    try:
        resp = requests.put(
            f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}/reactions/{encoded}/@me",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            logger.warning(f"[DISCORD_BOT] Failed to add reaction {emoji}: {resp.status_code}")
    except Exception as e:
        logger.warning(f"[DISCORD_BOT] Error adding reaction: {e}")


def wait_for_approval(message_id: str, timeout: int = None) -> str:
    """Poll for user reaction on the approval message.

    Returns:
        "approved"  - user reacted ✅
        "rejected"  - user reacted ❌
        "timeout"   - no response within timeout
        "error"     - API error
    """
    if not message_id:
        return "error"

    timeout = timeout or DISCORD_APPROVAL_TIMEOUT_SECONDS
    bot_user_id = _get_bot_user_id()
    poll_interval = 0.5  # seconds — fast polling for low latency
    elapsed = 0

    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        try:
            # Check ✅ reactions
            approve_users = _get_reaction_users(message_id, APPROVE_EMOJI)
            if approve_users is not None:
                for user in approve_users:
                    if user.get("id") != bot_user_id:
                        _update_message_result(message_id, "approved")
                        return "approved"

            # Check ❌ reactions
            reject_users = _get_reaction_users(message_id, REJECT_EMOJI)
            if reject_users is not None:
                for user in reject_users:
                    if user.get("id") != bot_user_id:
                        _update_message_result(message_id, "rejected")
                        return "rejected"

        except Exception as e:
            logger.warning(f"[DISCORD_BOT] Error polling reactions: {e}")

    _update_message_result(message_id, "timeout")
    return "timeout"


def _get_reaction_users(message_id: str, emoji: str):
    """Get list of users who reacted with a specific emoji."""
    import urllib.parse
    encoded = urllib.parse.quote(emoji)
    try:
        resp = requests.get(
            f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}/reactions/{encoded}",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            return None
    except Exception:
        return None


def _update_message_result(message_id: str, result: str):
    """Edit the original message to show the outcome."""
    if result == "approved":
        footer_text = "✅ EXIT APPROVED — executing..."
        color = 0x44FF44
    elif result == "rejected":
        footer_text = "❌ HOLD — exit cancelled by user"
        color = 0x4488FF
    else:
        footer_text = "⏰ TIMEOUT — no response, holding position"
        color = 0xFFAA00

    try:
        # Get original message to preserve embed content
        resp = requests.get(
            f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return

        msg = resp.json()
        embeds = msg.get("embeds", [])
        if embeds:
            embeds[0]["color"] = color
            embeds[0]["footer"] = {"text": footer_text}
            # Remove the "React ✅..." instruction from description
            desc = embeds[0].get("description", "")
            lines = desc.split("\n")
            embeds[0]["description"] = lines[0] if lines else desc

        requests.patch(
            f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}",
            headers=_headers(),
            json={"embeds": embeds},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"[DISCORD_BOT] Failed to update message: {e}")


def send_exit_result(
    ticker: str,
    trigger: str,
    direction: str,
    contracts: int,
    entry_price: float,
    exit_price: float,
    pnl: float,
    status: str,
):
    """Send a post-exit result notification (after execution completes)."""
    if not is_configured():
        return

    pnl_sign = "+" if pnl >= 0 else ""
    color = 0x44FF44 if pnl >= 0 else 0xFF4444
    status_emoji = "✅" if status == "WON" else "❌" if status == "LOST" else "🔄"

    embed = {
        "title": f"{status_emoji} EXIT EXECUTED",
        "color": color,
        "fields": [
            {"name": "Ticker", "value": ticker, "inline": True},
            {"name": "Trigger", "value": trigger, "inline": True},
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "Entry", "value": f"${entry_price:.3f}", "inline": True},
            {"name": "Exit", "value": f"${exit_price:.3f}", "inline": True},
            {"name": "P&L", "value": f"{pnl_sign}${pnl:.2f}", "inline": True},
        ],
    }

    try:
        requests.post(
            f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=_headers(),
            json={"embeds": [embed]},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"[DISCORD_BOT] Failed to send exit result: {e}")
