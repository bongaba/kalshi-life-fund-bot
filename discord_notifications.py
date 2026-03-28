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
                COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(pnl), 0.0) AS pnl_total,
                COALESCE(SUM(size * price), 0.0) AS total_cost
            FROM trades
            WHERE status IN ('WON', 'LOST', 'CLOSED', 'SETTLED')
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
                COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(pnl), 0.0) AS pnl_total,
                COALESCE(SUM(size * price), 0.0) AS total_cost
            FROM trades
            WHERE status IN ('WON', 'LOST', 'CLOSED', 'SETTLED')
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
    fees: float = 0.0,
):
    """Send notification when a trade is executed."""
    message = f"🚀 **Trade Executed!**"
    fields = [
        {"name": "Reason", "value": reason, "inline": False},
        {"name": "Undervalued Trade", "value": "Yes" if is_undervalued else "No", "inline": False},
    ]

    if order_status:
        fields.append({"name": "Exchange Status", "value": order_status, "inline": False})

    total_with_fees = total_cost + fees

    embed_data = {
        "title": "New Trade",
        "description": (
            f"**Event:** {market_title}\n"
            f"**Market:** {ticker}\n"
            f"**Direction:** {direction}\n"
            f"**Confidence:** {confidence}%\n\n"
            f"**Bought:** {quantity} × ${price:.4f} = **${total_with_fees:.2f}**"
        ),
        "color": 3066993,  # Green
        "fields": fields
    }
    send_discord_notification(message, embed_data)


def notify_position_closed(
    ticker: str,
    direction: str,
    quantity: int,
    entry_price: float,
    exit_price: float,
    pnl_dollars: float,
    pnl_percent: float,
    trigger: str,
    order_status: str | None = None,
    entry_fees: float = 0.0,
    exit_fees: float = 0.0,
):
    """Send notification when a position close order is submitted."""
    message = "📉 **Position Closed**"
    trigger_label = "Take Profit" if trigger == "take_profit" else "Stop Loss"

    total_cost = entry_price * quantity + entry_fees
    total_exit = exit_price * quantity - exit_fees
    net_pnl = total_exit - total_cost

    fields = [
        {"name": "Trigger", "value": trigger_label, "inline": True},
    ]

    if order_status:
        fields.append({"name": "Exchange Status", "value": order_status, "inline": True})

    embed_data = {
        "title": "Exit Order Submitted",
        "description": (
            f"**Market:** {ticker}\n"
            f"**Direction:** {direction}\n"
            f"**Quantity:** {quantity}\n\n"
            f"**Bought:** ${total_cost:.2f}\n"
            f"**Sold:** ${total_exit:.2f}\n\n"
            f"**P&L:** ${net_pnl:+.2f}"
        ),
        "color": 3066993 if net_pnl >= 0 else 15158332,
        "fields": fields,
    }
    send_discord_notification(message, embed_data)

def notify_cycle_summary(total_markets: int, considered: int, trades: int, pnl_today: float, total_order_cost: float,
                         balance: float = None, portfolio_value: float = None):
    """Send a single consolidated cycle summary with optional balance and 24h performance."""
    fields = []

    # Account balance section (if provided)
    if balance is not None and portfolio_value is not None:
        total_value = balance + portfolio_value
        fields.append({
            "name": "💰 Account",
            "value": f"Cash: ${balance:.2f} | Portfolio: ${portfolio_value:.2f} | Total: ${total_value:.2f}",
            "inline": False,
        })

    # Rolling 24h performance (if enabled)
    if DISCORD_INCLUDE_ROLLING24H:
        perf = get_rolling_24h_performance()
        if perf["resolved"] > 0:
            pnl_emoji = "📈" if perf["pnl"] >= 0 else "📉"
            fields.append({
                "name": f"{pnl_emoji} Rolling 24h",
                "value": f"W/L: {perf['wins']}/{perf['losses']} | P&L: ${perf['pnl']:.2f} ({perf['pnl_pct']:+.1f}%)",
                "inline": False,
            })

    # All-time performance (if enabled)
    if DISCORD_INCLUDE_ALL_TIME_PERFORMANCE:
        perf_at = get_all_time_performance()
        if perf_at["resolved"] > 0:
            fields.append({
                "name": "🧾 All-Time",
                "value": f"W/L: {perf_at['wins']}/{perf_at['losses']} | P&L: ${perf_at['pnl']:.2f} ({perf_at['pnl_pct']:+.1f}%)",
                "inline": False,
            })

    message = f"📊 **Cycle Complete** — {trades} trade{'s' if trades != 1 else ''} | {considered}/{total_markets} markets"
    embed_data = {
        "title": "Cycle Summary",
        "description": (
            f"Markets Scanned: {total_markets}\n"
            f"Considered: {considered}\n"
            f"Trades: {trades}\n"
            f"Cycle Cost: ${total_order_cost:.2f}"
        ),
        "color": 3066993 if trades > 0 else 3447003,
        "fields": fields,
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