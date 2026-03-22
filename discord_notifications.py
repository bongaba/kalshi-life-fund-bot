import requests
import json
from config import DISCORD_WEBHOOK_URL

def send_discord_notification(message: str, embed_data: dict = None):
    """
    Send a notification to Discord via webhook.

    Args:
        message: The main message text
        embed_data: Optional dict with embed fields (title, description, color, etc.)
    """
    if not DISCORD_WEBHOOK_URL:
        print("[DISCORD] Webhook not configured, skipping notification")
        return

    payload = {
        "content": message,
        "username": "Kalshi Trading Bot",
        "avatar_url": "https://i.imgur.com/4M34hi2.png"  # Optional bot avatar
    }

    if embed_data:
        embed = {
            "title": embed_data.get("title", "Trading Update"),
            "description": embed_data.get("description", ""),
            "color": embed_data.get("color", 3447003),  # Blue color
            "fields": embed_data.get("fields", []),
            "timestamp": embed_data.get("timestamp")
        }
        payload["embeds"] = [embed]

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if response.status_code == 204:
            print("[DISCORD] Notification sent successfully")
        else:
            print(f"[DISCORD] Failed to send notification: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[DISCORD] Error sending notification: {e}")

def notify_trade_executed(
    ticker: str,
    market_title: str,
    direction: str,
    confidence: int,
    quantity: int,
    price: float,
    reason: str,
    total_cost: float,
    is_undervalued: bool = False,
    order_status: str = None,
):
    """Send notification when a trade is executed."""
    message = f"🚀 **Trade Executed!**"
    fields = [
        {"name": "Reason", "value": reason, "inline": False},
        {"name": "Undervalued Trade", "value": "Yes" if is_undervalued else "No", "inline": False},
    ]

    if order_status:
        fields.append({"name": "Exchange Status", "value": order_status, "inline": False})

    embed_data = {
        "title": "New Trade",
        "description": (
            f"Event Title: {market_title}\n"
            f"Market: {ticker}\n"
            f"Direction: {direction}\n"
            f"Confidence: {confidence}%\n"
            f"Quantity: {quantity}\n"
            f"Price: ${price:.2f}\n"
            f"Total Cost: ${total_cost:.2f}\n"
            f"Undervalued: {'Yes' if is_undervalued else 'No'}"
        ),
        "color": 3066993,  # Green
        "fields": fields
    }
    send_discord_notification(message, embed_data)

def notify_cycle_summary(total_markets: int, considered: int, trades: int, pnl_today: float, total_order_cost: float):
    """Send daily cycle summary."""
    message = f"📊 **Cycle Summary**"
    embed_data = {
        "title": "Market Scan Complete",
        "description": (
            f"Total Markets: {total_markets}\n"
            f"Considered: {considered}\n"
            f"Trades: {trades}\n"
            f"Total Order Cost: ${total_order_cost:.2f}\n"
            f"PnL Today: ${pnl_today:.2f}"
        ),
        "color": 16776960 if pnl_today >= 0 else 15158332,  # Yellow if positive, red if negative
        "fields": []
    }
    send_discord_notification(message, embed_data)

def notify_account_balance(balance: float, portfolio_value: float, total_value: float):
    """Send current account balance snapshot."""
    message = f"💰 **Account Balance**"
    embed_data = {
        "title": "Account Snapshot",
        "description": (
            f"Cash Balance: ${balance:.2f}\n"
            f"Portfolio Value: ${portfolio_value:.2f}\n"
            f"Total Account Value: ${total_value:.2f}"
        ),
        "color": 3447003,
        "fields": []
    }
    send_discord_notification(message, embed_data)

def notify_error(error_message: str):
    """Send error notification."""
    message = f"❌ **Bot Error**"
    embed_data = {
        "title": "Error Alert",
        "description": error_message,
        "color": 15158332  # Red
    }
    send_discord_notification(message, embed_data)

def notify_startup():
    """Send startup notification."""
    message = f"🤖 **Bot Started**"
    embed_data = {
        "title": "Trading Bot Online",
        "description": "Kalshi trading bot is now active and scanning markets.",
        "color": 5763719  # Purple
    }
    send_discord_notification(message, embed_data)