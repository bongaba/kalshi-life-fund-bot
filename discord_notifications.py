import requests
import json
import sqlite3
import re
from config import DISCORD_WEBHOOK_URL, DISCORD_INCLUDE_ROLLING24H, DISCORD_INCLUDE_ALL_TIME_PERFORMANCE


def sanitize_discord_text(value: str | None) -> str | None:
    if value is None:
        return None

    sanitized = re.sub(r"(?i)grok[_ ]override", "AI override", value)
    sanitized = re.sub(r"(?i)\bgrok\b", "AI", sanitized)
    return sanitized


def get_rolling_24h_performance(db_path: str = "trades.db") -> dict:
    """Return rolling 24-hour win/loss and PnL metrics from local trades DB."""
    metrics = {
        "wins": 0,
        "losses": 0,
        "resolved": 0,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "window_hours": 24,
    }

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        time_field = "resolved_timestamp" if "resolved_timestamp" in columns else "timestamp"

        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN status = 'WON' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN status = 'LOST' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN status IN ('WON', 'LOST') THEN pnl ELSE 0 END), 0.0) AS pnl_total,
                COALESCE(SUM(CASE WHEN status IN ('WON', 'LOST') THEN size * price ELSE 0 END), 0.0) AS total_cost
            FROM trades
            WHERE status IN ('WON', 'LOST')
              AND COALESCE({time_field}, timestamp) >= datetime('now', '-24 hours')
            """
        )
        wins, losses, pnl_total, total_cost = cursor.fetchone()
        conn.close()

        wins = int(wins or 0)
        losses = int(losses or 0)
        resolved = wins + losses
        pnl_total = float(pnl_total or 0.0)
        total_cost = float(total_cost or 0.0)
        pnl_pct = (pnl_total / total_cost * 100.0) if total_cost > 0 else 0.0

        metrics.update({
            "wins": wins,
            "losses": losses,
            "resolved": resolved,
            "pnl": pnl_total,
            "pnl_pct": pnl_pct,
        })
    except Exception as e:
        print(f"[DISCORD] Failed to compute rolling 24h performance: {e}")

    return metrics


def get_all_time_performance(db_path: str = "trades.db") -> dict:
    """Return all-time win/loss and PnL metrics from local trades DB."""
    metrics = {
        "wins": 0,
        "losses": 0,
        "resolved": 0,
        "pnl": 0.0,
        "pnl_pct": 0.0,
    }

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'WON' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN status = 'LOST' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN status IN ('WON', 'LOST') THEN pnl ELSE 0 END), 0.0) AS pnl_total,
                COALESCE(SUM(CASE WHEN status IN ('WON', 'LOST') THEN size * price ELSE 0 END), 0.0) AS total_cost
            FROM trades
            WHERE status IN ('WON', 'LOST')
            """
        )
        wins, losses, pnl_total, total_cost = cursor.fetchone()
        conn.close()

        wins = int(wins or 0)
        losses = int(losses or 0)
        resolved = wins + losses
        pnl_total = float(pnl_total or 0.0)
        total_cost = float(total_cost or 0.0)
        pnl_pct = (pnl_total / total_cost * 100.0) if total_cost > 0 else 0.0

        metrics.update({
            "wins": wins,
            "losses": losses,
            "resolved": resolved,
            "pnl": pnl_total,
            "pnl_pct": pnl_pct,
        })
    except Exception as e:
        print(f"[DISCORD] Failed to compute all-time performance: {e}")

    return metrics


def send_discord_notification(message: str, embed_data: dict = None):
    """
    Send a notification to Discord via webhook.

    Args:
        message: The main message text
        embed_data: Optional dict with embed fields (title, description, color, etc.)
    """
    if not DISCORD_WEBHOOK_URL:
        print("[DISCORD] Webhook not configured, skipping notification")
        return False

    payload = {
        "content": sanitize_discord_text(message),
        "username": "Kalshi Trading Bot",
        "avatar_url": "https://i.imgur.com/4M34hi2.png"  # Optional bot avatar
    }

    if embed_data:
        fields = []
        for field in embed_data.get("fields", []):
            fields.append(
                {
                    "name": sanitize_discord_text(field.get("name")),
                    "value": sanitize_discord_text(field.get("value")),
                    "inline": field.get("inline", False),
                }
            )
        embed = {
            "title": sanitize_discord_text(embed_data.get("title", "Trading Update")),
            "description": sanitize_discord_text(embed_data.get("description", "")),
            "color": embed_data.get("color", 3447003),  # Blue color
            "fields": fields,
            "timestamp": embed_data.get("timestamp")
        }
        payload["embeds"] = [embed]

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if response.status_code == 204:
            print("[DISCORD] Notification sent successfully")
            return True
        else:
            print(f"[DISCORD] Failed to send notification: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"[DISCORD] Error sending notification: {e}")
        return False


def notify_rolling_24h_performance():
    """Send standalone rolling 24-hour performance notification."""
    if not DISCORD_INCLUDE_ROLLING24H:
        return

    perf = get_rolling_24h_performance()
    wins = perf["wins"]
    losses = perf["losses"]
    resolved = perf["resolved"]
    pnl = perf["pnl"]
    pnl_pct = perf["pnl_pct"]

    message = "📈 **Rolling 24h Performance**"
    embed_data = {
        "title": "Rolling 24h Performance",
        "description": (
            f"Resolved: {resolved}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"P&L: ${pnl:.2f}\n"
            f"P&L %: {pnl_pct:.2f}%"
        ),
        "color": 3066993 if pnl >= 0 else 15158332,
        "fields": [],
    }
    send_discord_notification(message, embed_data)


def notify_all_time_performance():
    """Send standalone all-time performance notification (since account history in local DB)."""
    if not DISCORD_INCLUDE_ALL_TIME_PERFORMANCE:
        return

    perf = get_all_time_performance()
    wins = perf["wins"]
    losses = perf["losses"]
    resolved = perf["resolved"]
    pnl = perf["pnl"]
    pnl_pct = perf["pnl_pct"]

    message = "🧾 **All-Time Performance**"
    embed_data = {
        "title": "All-Time Performance",
        "description": (
            f"Resolved: {resolved}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"P&L: ${pnl:.2f}\n"
            f"P&L %: {pnl_pct:.2f}%"
        ),
        "color": 3066993 if pnl >= 0 else 15158332,
        "fields": [],
    }
    send_discord_notification(message, embed_data)

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