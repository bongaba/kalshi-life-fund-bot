"""
Trade Performance Analyzer — Self-Learning System
===================================================
Reads trades.db, trade_decisions logs, and configuration to identify:
  • What's working and what isn't
  • Specific parameter tuning recommendations
  • Market categories, price ranges, and directions that over/under-perform
  • Grok accuracy analysis
  • Exit mechanism effectiveness (stop-loss vs trailing TP vs settlement)

Run:  python trade_analyzer.py
      python trade_analyzer.py --json          # machine-readable output
      python trade_analyzer.py --since 7       # only last 7 days
"""

import sqlite3
import re
import json
import os
import glob
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

DB_PATH = Path(__file__).resolve().parent / "trades.db"
LOGS_DIR = Path(__file__).resolve().parent / "logs"
REPORT_DIR = Path(__file__).resolve().parent / "data"


# ── helpers ──────────────────────────────────────────────────────────────────

def _pct(num, denom):
    return (num / denom * 100) if denom else 0.0


def _extract_grok_confidence(reason: str) -> int | None:
    """Extract Grok confidence from the reason string e.g. 'Grok: ...' or 'Grok override: ...'"""
    # The trade decision log has lines like: grok=YES(92) or grok=HOLD(60)
    # But the DB reason field stores the narrative.  We can't extract confidence from
    # the DB reason alone — it's in the log.  Return None here; log parser handles it.
    return None


def _extract_market_category(ticker: str) -> str:
    """Categorize ticker into a market type."""
    t = ticker.upper()
    if "KXBTC" in t:
        return "btc"
    elif "KXETH" in t:
        return "eth"
    elif "KXWTI" in t or "KXOIL" in t:
        return "oil"
    elif "KXGOLD" in t or "KXAU" in t:
        return "gold"
    elif "KXSILVER" in t or "KXAG" in t:
        return "silver"
    elif any(k in t for k in ("KXHIGH", "KXLOW", "KXTEMP", "KXRAIN", "KXSNOW", "KXWIND")):
        return "weather"
    elif any(k in t for k in ("KXSP500", "KXSPY", "KXNAS", "KXNDX", "KXDOW", "KXSPX")):
        return "equities"
    elif "KXAAAGASW" in t or "KXGAS" in t:
        return "gas"
    elif any(k in t for k in ("KXNFL", "KXNBA", "KXMLB", "KXNHL", "KXSOCCER")):
        return "sports"
    elif any(k in t for k in ("KXNETFLIX", "KXSPOTIFY", "KXDHS", "KXDHSFUND")):
        return "events"
    else:
        return "other"


def _extract_exit_type(reason: str) -> str:
    """Infer how a trade was exited from its reason field."""
    r = (reason or "").lower()
    if "stop_loss" in r:
        return "stop_loss"
    elif "trailing" in r or "take_profit" in r:
        return "trailing_tp"
    elif "held_to_settlement" in r:
        return "held_to_settlement"
    elif "auto-settled" in r or "settlement" in r:
        return "settlement"
    elif "reconciled" in r:
        return "reconciled"
    else:
        return "unknown"


def _price_bucket(price: float) -> str:
    if price >= 0.90:
        return "90c+"
    elif price >= 0.80:
        return "80-89c"
    elif price >= 0.70:
        return "70-79c"
    elif price >= 0.60:
        return "60-69c"
    else:
        return "<60c"


# ── database ─────────────────────────────────────────────────────────────────

def load_trades(since_days: int | None = None) -> list[dict]:
    """Load trades from DB, deduplicating reconciled entries."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    where = "WHERE status IN ('WON','LOST','CLOSED')"
    params = []
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
        where += " AND timestamp >= ?"
        params.append(cutoff)

    # Deduplicate: keep the FIRST (original) entry per market_ticker.
    # Reconciled entries have reason starting with 'reconciled_from_'.
    c.execute(f"""
        SELECT * FROM trades {where} ORDER BY id ASC
    """, params)

    seen_tickers: dict[str, dict] = {}
    all_rows = []
    for row in c.fetchall():
        d = dict(row)
        ticker = d["market_ticker"]
        reason = d.get("reason") or ""

        # If we already have a non-reconciled entry for this ticker, skip reconciled dupes.
        if ticker in seen_tickers:
            if reason.startswith("reconciled"):
                continue
            # If existing was reconciled but this one is original, replace
            if seen_tickers[ticker].get("_is_reconciled") and not reason.startswith("reconciled"):
                # Remove old entry
                all_rows = [r for r in all_rows if r["market_ticker"] != ticker]
                d["_is_reconciled"] = False
                seen_tickers[ticker] = d
                all_rows.append(d)
                continue
            # Multiple separate trades of same ticker — keep all originals
            d["_is_reconciled"] = reason.startswith("reconciled")
            all_rows.append(d)
            continue

        d["_is_reconciled"] = reason.startswith("reconciled")
        seen_tickers[ticker] = d
        all_rows.append(d)

    conn.close()
    return all_rows


# ── log parsing ──────────────────────────────────────────────────────────────

def parse_trade_decision_logs() -> dict:
    """Parse trade_decisions logs for Grok confidence, skip patterns, and timing."""
    log_dir = LOGS_DIR / "trade_decisions"
    if not log_dir.exists():
        return {"grok_calls": [], "skips": defaultdict(int), "grok_accuracy": []}

    grok_calls = []      # {"ticker", "direction", "confidence", "reason", "elapsed_s"}
    skip_reasons = defaultdict(int)
    considered_count = 0

    for log_file in sorted(log_dir.glob("trade_decisions.*.log")):
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # CONSIDERING lines
                    if "CONSIDERING:" in line:
                        considered_count += 1

                    # Grok completion lines: [GROK] Completed | elapsed=32.67s | direction=HOLD | confidence=60
                    m = re.search(
                        r"\[GROK\] Completed \| elapsed=([\d.]+)s \| direction=(\w+) \| confidence=(\d+)",
                        line,
                    )
                    if m:
                        # Try to extract ticker from preceding context — often in the same log block
                        grok_calls.append({
                            "elapsed_s": float(m.group(1)),
                            "direction": m.group(2),
                            "confidence": int(m.group(3)),
                        })

                    # [DECISION_ENGINE] Rejected after validation | ticker=X | validator=Grok | ...
                    m = re.search(
                        r"\[DECISION_ENGINE\] Rejected after validation \| ticker=(\S+) \| .*reasons=(.*)",
                        line,
                    )
                    if m:
                        for reason in m.group(2).split(", "):
                            reason_key = re.sub(r"\(.*\)", "", reason.strip())
                            skip_reasons[reason_key] += 1

                    # [DECISION] SKIP lines
                    if "[DECISION] SKIP" in line:
                        skip_reasons["total_skips"] += 1

        except Exception:
            continue

    return {
        "grok_calls": grok_calls,
        "skips": dict(skip_reasons),
        "considered_count": considered_count,
    }


# ── analysis ─────────────────────────────────────────────────────────────────

def analyze(trades: list[dict], log_data: dict) -> dict:
    """Run all analyses and return a structured report."""
    report = {}

    # ── 1. Overall performance ───────────────────────────────────────────
    total = len(trades)
    wins = [t for t in trades if t["status"] == "WON"]
    losses = [t for t in trades if t["status"] == "LOST"]
    closed = [t for t in trades if t["status"] == "CLOSED"]
    total_pnl = sum(t["pnl"] or 0 for t in trades)
    total_fees = sum(t["fees"] or 0 for t in trades)

    report["overall"] = {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "closed": len(closed),
        "win_rate_pct": round(_pct(len(wins), len(wins) + len(losses)), 1),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "net_pnl": round(total_pnl - total_fees, 2),
        "avg_win_pnl": round(sum(t["pnl"] or 0 for t in wins) / max(len(wins), 1), 4),
        "avg_loss_pnl": round(sum(t["pnl"] or 0 for t in losses) / max(len(losses), 1), 4),
    }

    # ── 2. By direction ──────────────────────────────────────────────────
    direction_stats = {}
    for d_val in ("YES", "NO"):
        d_trades = [t for t in trades if t["direction"] == d_val]
        d_wins = [t for t in d_trades if t["status"] == "WON"]
        d_losses = [t for t in d_trades if t["status"] == "LOST"]
        direction_stats[d_val] = {
            "count": len(d_trades),
            "wins": len(d_wins),
            "losses": len(d_losses),
            "win_rate_pct": round(_pct(len(d_wins), len(d_wins) + len(d_losses)), 1),
            "total_pnl": round(sum(t["pnl"] or 0 for t in d_trades), 2),
            "avg_pnl": round(sum(t["pnl"] or 0 for t in d_trades) / max(len(d_trades), 1), 4),
        }
    report["by_direction"] = direction_stats

    # ── 3. By entry price bucket ─────────────────────────────────────────
    bucket_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        b = _price_bucket(t["price"])
        bucket_stats[b]["count"] += 1
        bucket_stats[b]["pnl"] += t["pnl"] or 0
        if t["status"] == "WON":
            bucket_stats[b]["wins"] += 1
        elif t["status"] == "LOST":
            bucket_stats[b]["losses"] += 1

    for b in bucket_stats:
        s = bucket_stats[b]
        s["win_rate_pct"] = round(_pct(s["wins"], s["wins"] + s["losses"]), 1)
        s["avg_pnl"] = round(s["pnl"] / max(s["count"], 1), 4)
        s["pnl"] = round(s["pnl"], 2)

    report["by_entry_price"] = dict(bucket_stats)

    # ── 4. By market category ────────────────────────────────────────────
    cat_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        cat = _extract_market_category(t["market_ticker"])
        cat_stats[cat]["count"] += 1
        cat_stats[cat]["pnl"] += t["pnl"] or 0
        if t["status"] == "WON":
            cat_stats[cat]["wins"] += 1
        elif t["status"] == "LOST":
            cat_stats[cat]["losses"] += 1

    for cat in cat_stats:
        s = cat_stats[cat]
        s["win_rate_pct"] = round(_pct(s["wins"], s["wins"] + s["losses"]), 1)
        s["avg_pnl"] = round(s["pnl"] / max(s["count"], 1), 4)
        s["pnl"] = round(s["pnl"], 2)

    report["by_category"] = dict(cat_stats)

    # ── 5. By exit type ──────────────────────────────────────────────────
    exit_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        exit_type = _extract_exit_type(t.get("reason", ""))
        exit_stats[exit_type]["count"] += 1
        exit_stats[exit_type]["pnl"] += t["pnl"] or 0
        if t["status"] == "WON":
            exit_stats[exit_type]["wins"] += 1
        elif t["status"] == "LOST":
            exit_stats[exit_type]["losses"] += 1

    for ex in exit_stats:
        s = exit_stats[ex]
        s["win_rate_pct"] = round(_pct(s["wins"], s["wins"] + s["losses"]), 1)
        s["avg_pnl"] = round(s["pnl"] / max(s["count"], 1), 4)
        s["pnl"] = round(s["pnl"], 2)

    report["by_exit_type"] = dict(exit_stats)

    # ── 6. By hour of day ────────────────────────────────────────────────
    hour_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        try:
            ts = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
            h = ts.hour
        except Exception:
            h = -1
        hour_stats[h]["count"] += 1
        hour_stats[h]["pnl"] += t["pnl"] or 0
        if t["status"] == "WON":
            hour_stats[h]["wins"] += 1
        elif t["status"] == "LOST":
            hour_stats[h]["losses"] += 1

    for h_val in hour_stats:
        s = hour_stats[h_val]
        s["win_rate_pct"] = round(_pct(s["wins"], s["wins"] + s["losses"]), 1)
        s["avg_pnl"] = round(s["pnl"] / max(s["count"], 1), 4)
        s["pnl"] = round(s["pnl"], 2)

    report["by_hour"] = {str(k): v for k, v in sorted(hour_stats.items())}

    # ── 7. Biggest losses ────────────────────────────────────────────────
    worst = sorted(trades, key=lambda t: t["pnl"] or 0)[:10]
    report["worst_trades"] = [
        {
            "ticker": t["market_ticker"],
            "direction": t["direction"],
            "entry_price": t["price"],
            "pnl": t["pnl"],
            "status": t["status"],
            "exit_type": _extract_exit_type(t.get("reason", "")),
            "reason_preview": (t.get("reason") or "")[:120],
        }
        for t in worst
    ]

    # ── 8. Grok analysis (from logs) ─────────────────────────────────────
    grok_calls = log_data.get("grok_calls", [])
    if grok_calls:
        confidences = [g["confidence"] for g in grok_calls]
        holds = [g for g in grok_calls if g["direction"] == "HOLD"]
        elapsed = [g["elapsed_s"] for g in grok_calls]
        report["grok_analysis"] = {
            "total_calls": len(grok_calls),
            "avg_confidence": round(sum(confidences) / len(confidences), 1),
            "hold_rate_pct": round(_pct(len(holds), len(grok_calls)), 1),
            "avg_elapsed_s": round(sum(elapsed) / len(elapsed), 1),
            "confidence_distribution": {
                "90+": len([c for c in confidences if c >= 90]),
                "80-89": len([c for c in confidences if 80 <= c < 90]),
                "70-79": len([c for c in confidences if 70 <= c < 80]),
                "<70": len([c for c in confidences if c < 70]),
            },
        }

    report["skip_reasons"] = log_data.get("skips", {})
    report["considered_count"] = log_data.get("considered_count", 0)

    # ── 9. Streaks & variance ────────────────────────────────────────────
    sorted_trades = sorted(trades, key=lambda t: t["id"])
    max_win_streak = max_loss_streak = cur_win = cur_loss = 0
    for t in sorted_trades:
        if t["status"] == "WON":
            cur_win += 1
            cur_loss = 0
        elif t["status"] == "LOST":
            cur_loss += 1
            cur_win = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    report["streaks"] = {
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
    }

    # ── 10. Daily P&L ────────────────────────────────────────────────────
    daily_pnl = defaultdict(float)
    daily_count = defaultdict(int)
    for t in trades:
        try:
            day = t["timestamp"][:10]
        except Exception:
            continue
        daily_pnl[day] += t["pnl"] or 0
        daily_count[day] += 1

    report["daily_pnl"] = {
        day: {"pnl": round(pnl, 2), "trades": daily_count[day]}
        for day, pnl in sorted(daily_pnl.items())
    }

    return report


# ── recommendations ──────────────────────────────────────────────────────────

def generate_recommendations(report: dict) -> list[str]:
    """Generate actionable parameter tuning recommendations."""
    recs = []

    # 1. Entry price bucket recommendations
    by_price = report.get("by_entry_price", {})
    for bucket, stats in by_price.items():
        if stats["count"] >= 10 and stats["win_rate_pct"] < 60:
            recs.append(
                f"LOW WIN RATE in {bucket} bucket: {stats['win_rate_pct']}% win rate over "
                f"{stats['count']} trades (PnL ${stats['pnl']}). Consider raising "
                f"INTERNAL_HIGH_PROBABILITY_THRESHOLD to avoid these entry prices."
            )
        if stats["count"] >= 10 and stats["avg_pnl"] < 0:
            recs.append(
                f"NEGATIVE AVG PnL in {bucket} bucket: ${stats['avg_pnl']:.4f}/trade. "
                f"These trades are net destroyers of capital."
            )

    # 2. Direction imbalance
    by_dir = report.get("by_direction", {})
    yes_wr = by_dir.get("YES", {}).get("win_rate_pct", 0)
    no_wr = by_dir.get("NO", {}).get("win_rate_pct", 0)
    if abs(yes_wr - no_wr) > 10:
        weaker = "YES" if yes_wr < no_wr else "NO"
        stronger = "NO" if weaker == "YES" else "YES"
        recs.append(
            f"DIRECTION IMBALANCE: {stronger} trades win at {max(yes_wr, no_wr):.1f}% vs "
            f"{weaker} at {min(yes_wr, no_wr):.1f}%. Investigate why {weaker} trades "
            f"underperform — may need higher confidence threshold for {weaker} direction."
        )

    # 3. Category recommendations
    by_cat = report.get("by_category", {})
    for cat, stats in by_cat.items():
        if stats["count"] >= 5 and stats["pnl"] < -5:
            recs.append(
                f"LOSING CATEGORY '{cat}': {stats['count']} trades, PnL ${stats['pnl']}, "
                f"win rate {stats['win_rate_pct']}%. Consider adding to EXCLUDED_MARKET_TICKERS "
                f"or tightening filters for this category."
            )
        if stats["count"] >= 20 and stats["win_rate_pct"] > 85 and stats["pnl"] > 10:
            recs.append(
                f"STRONG CATEGORY '{cat}': {stats['count']} trades, PnL ${stats['pnl']}, "
                f"win rate {stats['win_rate_pct']}%. Consider increasing allocation here."
            )

    # 4. Exit type analysis
    by_exit = report.get("by_exit_type", {})
    stop_loss = by_exit.get("stop_loss", {})
    if stop_loss.get("count", 0) > 5:
        sl_loss_pct = _pct(stop_loss.get("losses", 0), stop_loss["count"])
        recs.append(
            f"STOP-LOSS EXITS: {stop_loss['count']} trades hit stop-loss, "
            f"{sl_loss_pct:.0f}% were losses. Total PnL from SL exits: ${stop_loss.get('pnl', 0):.2f}. "
            f"If many SL exits later resolved as wins, consider widening stop-loss tiers."
        )

    # 5. Grok confidence analysis
    grok = report.get("grok_analysis", {})
    if grok:
        if grok.get("hold_rate_pct", 0) > 50:
            recs.append(
                f"GROK HOLD RATE HIGH: {grok['hold_rate_pct']}% of Grok calls return HOLD. "
                f"This wastes API calls. Consider pre-filtering more aggressively with the "
                f"internal model before sending to Grok."
            )
        conf_dist = grok.get("confidence_distribution", {})
        low_conf = conf_dist.get("<70", 0)
        total_calls = grok.get("total_calls", 1)
        if _pct(low_conf, total_calls) > 30:
            recs.append(
                f"GROK LOW CONFIDENCE: {_pct(low_conf, total_calls):.0f}% of Grok calls "
                f"return confidence <70. May indicate prompt needs sharpening or markets "
                f"being sent to Grok are genuinely uncertain."
            )

    # 6. Hour-of-day recommendations
    by_hour = report.get("by_hour", {})
    bad_hours = []
    for h, stats in by_hour.items():
        if stats["count"] >= 10 and stats["win_rate_pct"] < 60:
            bad_hours.append((h, stats["win_rate_pct"], stats["count"]))
    if bad_hours:
        hours_str = ", ".join(f"{h}:00 ({wr:.0f}% over {n})" for h, wr, n in bad_hours)
        recs.append(
            f"WEAK HOURS: Low win rates at {hours_str}. "
            f"Consider pausing the bot during these hours or increasing confidence threshold."
        )

    # 7. Overall health
    overall = report.get("overall", {})
    if overall.get("win_rate_pct", 0) > 80 and overall.get("net_pnl", 0) > 0:
        recs.append(
            f"HEALTHY SYSTEM: {overall['win_rate_pct']}% win rate, ${overall['net_pnl']} net PnL. "
            f"Focus on eliminating losing buckets rather than changing winning formula."
        )
    if overall.get("avg_loss_pnl", 0) != 0 and overall.get("avg_win_pnl", 0) != 0:
        ratio = abs(overall["avg_win_pnl"] / overall["avg_loss_pnl"]) if overall["avg_loss_pnl"] != 0 else 0
        if ratio < 0.5:
            recs.append(
                f"RISK/REWARD CONCERN: Avg win (${overall['avg_win_pnl']:.4f}) vs avg loss "
                f"(${overall['avg_loss_pnl']:.4f}), ratio={ratio:.2f}. Losses are much larger "
                f"than wins. Consider tightening stop-loss or increasing position size selectivity."
            )

    # 8. Loss streak warning
    streaks = report.get("streaks", {})
    if streaks.get("max_loss_streak", 0) >= 5:
        recs.append(
            f"DRAWDOWN RISK: Max loss streak of {streaks['max_loss_streak']}. "
            f"Consider implementing a circuit breaker that pauses trading after N consecutive losses."
        )

    return recs


# ── pretty print ─────────────────────────────────────────────────────────────

def print_report(report: dict, recs: list[str]):
    """Print a human-readable report to stdout."""
    print("=" * 80)
    print("  TRADE PERFORMANCE ANALYSIS REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # Overall
    o = report["overall"]
    print(f"\n{'─'*40}")
    print(f"  OVERALL PERFORMANCE")
    print(f"{'─'*40}")
    print(f"  Total trades (deduplicated): {o['total_trades']}")
    print(f"  Wins: {o['wins']}  |  Losses: {o['losses']}  |  Closed: {o['closed']}")
    print(f"  Win rate: {o['win_rate_pct']}%")
    print(f"  Total PnL: ${o['total_pnl']}  |  Fees: ${o['total_fees']}  |  Net: ${o['net_pnl']}")
    print(f"  Avg win: ${o['avg_win_pnl']:.4f}  |  Avg loss: ${o['avg_loss_pnl']:.4f}")

    # Direction
    print(f"\n{'─'*40}")
    print(f"  BY DIRECTION")
    print(f"{'─'*40}")
    for d_val, s in report["by_direction"].items():
        print(f"  {d_val:4s}: {s['count']:4d} trades | WR {s['win_rate_pct']:5.1f}% | PnL ${s['total_pnl']:8.2f} | avg ${s['avg_pnl']:.4f}")

    # Entry price
    print(f"\n{'─'*40}")
    print(f"  BY ENTRY PRICE")
    print(f"{'─'*40}")
    for bucket in ["90c+", "80-89c", "70-79c", "60-69c", "<60c"]:
        s = report["by_entry_price"].get(bucket, {})
        if s:
            print(f"  {bucket:7s}: {s['count']:4d} trades | WR {s['win_rate_pct']:5.1f}% | PnL ${s['pnl']:8.2f} | avg ${s['avg_pnl']:.4f}")

    # Category
    print(f"\n{'─'*40}")
    print(f"  BY MARKET CATEGORY")
    print(f"{'─'*40}")
    sorted_cats = sorted(report["by_category"].items(), key=lambda x: x[1]["pnl"], reverse=True)
    for cat, s in sorted_cats:
        print(f"  {cat:12s}: {s['count']:4d} trades | WR {s['win_rate_pct']:5.1f}% | PnL ${s['pnl']:8.2f} | avg ${s['avg_pnl']:.4f}")

    # Exit type
    print(f"\n{'─'*40}")
    print(f"  BY EXIT TYPE")
    print(f"{'─'*40}")
    for ex, s in sorted(report["by_exit_type"].items(), key=lambda x: x[1]["count"], reverse=True):
        print(f"  {ex:15s}: {s['count']:4d} trades | WR {s['win_rate_pct']:5.1f}% | PnL ${s['pnl']:8.2f}")

    # Hour of day
    print(f"\n{'─'*40}")
    print(f"  BY HOUR (UTC)")
    print(f"{'─'*40}")
    for h, s in sorted(report["by_hour"].items(), key=lambda x: int(x[0]) if x[0].lstrip('-').isdigit() else 99):
        bar = "█" * max(1, int(s["count"] / 5))
        print(f"  {h:>2s}:00  {s['count']:4d} trades | WR {s['win_rate_pct']:5.1f}% | PnL ${s['pnl']:8.2f}  {bar}")

    # Grok
    grok = report.get("grok_analysis")
    if grok:
        print(f"\n{'─'*40}")
        print(f"  GROK VALIDATOR ANALYSIS")
        print(f"{'─'*40}")
        print(f"  Total calls: {grok['total_calls']}")
        print(f"  Avg confidence: {grok['avg_confidence']}")
        print(f"  Hold rate: {grok['hold_rate_pct']}%")
        print(f"  Avg latency: {grok['avg_elapsed_s']}s")
        cd = grok["confidence_distribution"]
        print(f"  Confidence: 90+={cd['90+']}  80-89={cd['80-89']}  70-79={cd['70-79']}  <70={cd['<70']}")

    # Skip reasons
    skips = report.get("skip_reasons", {})
    if skips:
        print(f"\n{'─'*40}")
        print(f"  DECISION SKIP REASONS (from logs)")
        print(f"{'─'*40}")
        for reason, count in sorted(skips.items(), key=lambda x: x[1], reverse=True):
            print(f"  {reason:40s}: {count}")

    # Worst trades
    print(f"\n{'─'*40}")
    print(f"  TOP 10 WORST TRADES")
    print(f"{'─'*40}")
    for i, t in enumerate(report["worst_trades"], 1):
        print(f"  {i:2d}. {t['ticker'][:40]:40s} {t['direction']:3s} @ ${t['entry_price']:.3f} → PnL ${t['pnl']:.3f} ({t['exit_type']})")

    # Daily P&L
    print(f"\n{'─'*40}")
    print(f"  DAILY P&L")
    print(f"{'─'*40}")
    cumulative = 0
    for day, s in report["daily_pnl"].items():
        cumulative += s["pnl"]
        bar_char = "█" if s["pnl"] >= 0 else "░"
        bar = bar_char * max(1, int(abs(s["pnl"]) / 2))
        sign = "+" if s["pnl"] >= 0 else ""
        print(f"  {day}  {sign}${s['pnl']:8.2f}  ({s['trades']:3d} trades)  cum=${cumulative:8.2f}  {bar}")

    # Streaks
    streaks = report.get("streaks", {})
    print(f"\n{'─'*40}")
    print(f"  STREAKS")
    print(f"{'─'*40}")
    print(f"  Max win streak:  {streaks.get('max_win_streak', 0)}")
    print(f"  Max loss streak: {streaks.get('max_loss_streak', 0)}")

    # Recommendations
    print(f"\n{'='*80}")
    print(f"  RECOMMENDATIONS ({len(recs)})")
    print(f"{'='*80}")
    for i, rec in enumerate(recs, 1):
        print(f"\n  {i}. {rec}")

    print(f"\n{'='*80}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trade Performance Analyzer")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable")
    parser.add_argument("--since", type=int, default=None, help="Only analyze trades from last N days")
    parser.add_argument("--save", action="store_true", help="Save report to data/ directory")
    args = parser.parse_args()

    print(f"Loading trades from {DB_PATH}...")
    trades = load_trades(since_days=args.since)
    print(f"Loaded {len(trades)} deduplicated trades")

    print("Parsing trade decision logs...")
    log_data = parse_trade_decision_logs()
    print(f"Found {len(log_data['grok_calls'])} Grok calls, {log_data['considered_count']} markets considered")

    report = analyze(trades, log_data)
    recs = generate_recommendations(report)

    if args.json:
        output = {"report": report, "recommendations": recs}
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(report, recs)

    if args.save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORT_DIR / f"analysis_report_{ts}.json"
        with open(report_path, "w") as f:
            json.dump({"report": report, "recommendations": recs}, f, indent=2, default=str)
        print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
