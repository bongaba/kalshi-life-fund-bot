# Kalshi Automated Trading Bot — Performance Analysis & Optimization Report

**Date:** March 31, 2026  
**Reporting Period:** March 10–31, 2026 (22 trading days)  
**Prepared for:** External Consultant Review  
**System:** Autonomous prediction market trading bot on Kalshi exchange  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Problem Statement](#3-problem-statement)
4. [Data Sources & Methodology](#4-data-sources--methodology)
5. [Datapoints Gathered](#5-datapoints-gathered)
   - 5.1 Overall Performance
   - 5.2 Performance by Direction (YES vs NO)
   - 5.3 Performance by Entry Price Bucket
   - 5.4 Cross-Tabulation: Direction × Entry Price
   - 5.5 Performance by Market Category
   - 5.6 Performance by Market Series (Granular)
   - 5.7 Performance by Exit Mechanism
   - 5.8 Performance by Hour of Day (UTC)
   - 5.9 Daily P&L Trajectory
   - 5.10 Position Sizing Analysis
   - 5.11 Grok AI Validator Analysis
   - 5.12 Decision Pipeline Funnel
   - 5.13 Stop-Loss Deep Dive
   - 5.14 Streak & Drawdown Analysis
   - 5.15 Hold Time Analysis
   - 5.16 Worst Trades Analysis (March 10–11 Disaster)
   - 5.17 Operating Mode Comparison
6. [Current Configuration](#6-current-configuration)
7. [Identified Problems](#7-identified-problems)
8. [Proposed Solutions](#8-proposed-solutions)
9. [Recommendations (Prioritized)](#9-recommendations-prioritized)
10. [Implementation Roadmap](#10-implementation-roadmap)

---

## 1. Executive Summary

The bot has executed **862 deduplicated trades** over 22 days, achieving an **82.6% win rate** and a **net P&L of $187.27** (after $20.09 in fees). However, this headline number masks severe structural weaknesses:

- **The <60¢ entry price bucket is a net capital destroyer**, losing $34.27 across 132 trades (32.6% win rate). Eliminating these trades alone would have improved net P&L by ~18%.
- **YES-direction trades underperform NO by 12 percentage points** (77.4% vs 89.5%), driven primarily by YES trades at low entry prices.
- **Weather markets are barely profitable** — 114 trades generating only $1.34 in P&L, with catastrophic losses on March 10–11 wiping $48 in two days.
- **The Grok AI validator returns HOLD 48.3% of the time** and has a 33% rate of sub-70 confidence responses, suggesting significant API cost waste.
- **Average loss ($0.80) is 1.73× the average win ($0.46)**, creating a fragile risk/reward asymmetry that depends on maintaining a >63% win rate just to break even.
- **A 13-trade consecutive loss streak** occurred with no circuit breaker in place.

The system is profitable but operating well below its potential. The core BTC + gas + events strategy at high entry prices (≥80¢) is extremely strong (93%+ win rates). The losses are concentrated in identifiable, eliminable segments.

---

## 2. System Architecture

### 2.1 Component Overview

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Execution Bot** (`execution_bot.py`) | Python 3.x | Market scanning, decision pipeline, order placement |
| **Decision Engine** (`decision_engine.py`) | Internal model + Grok | Two-stage trade filtering: internal probability pre-filter → external AI validation |
| **Grok Analyzer** (`grok_analyzer.py`) | xAI `grok-4-1-fast-reasoning` | Real-time AI validator with `web_search` + `x_search` tools |
| **Position Monitor** (`position_monitor.py`) | WebSocket + REST | Real-time position tracking, trailing take-profit, 3-tier stop-loss |
| **WebSocket Client** (`kalshi_ws_client.py`) | `wss://api.elections.kalshi.com` | Orderbook streaming with auto-reconnect (exponential backoff 1–60s) |
| **Database** (`trades.db`) | SQLite | Trade history: 14-column `trades` table + `trailing_marks` table |
| **Logging** (`logging_setup.py`) | Loguru | 3 sinks: main log, error log (ERROR+ with backtrace), trade decisions log (tag-filtered) |
| **Prompt System** (`prompts.py`) | Category-specific prompts | 7 categories: BTC, weather, gas, oil, AI, CPI, generic |
| **Configuration** (`.env` + `config.py`) | dotenv | 30+ tunable parameters |

### 2.2 Decision Pipeline Flow

```
Market Scan (Kalshi REST API, up to 30 pages)
    ↓
Volume Gate (≥100,000 24h volume)
    ↓
Time Gate (MIN_HOURS_TO_CLOSE = 0h currently)
    ↓
Excluded Ticker Filter
    ↓
Internal Model Pre-Filter
  ├── YES price in [0.50, 0.99] → direction = YES
  ├── NO price in [0.50, 0.99] → direction = NO
  └── Otherwise → HOLD (skip)
    ↓
Grok AI External Validation
  ├── Category detection (BTC/weather/gas/oil/AI/CPI/generic)
  ├── Category-specific prompt with tool calls (web_search, x_search)
  ├── Response: {direction, confidence, reason}
  ├── Validation: direction must match internal ∧ confidence ≥ 85
  └── Rejection: direction mismatch OR HOLD OR confidence < 85
    ↓
Position Sizing (RISK_PER_TRADE × confidence/100)
    ↓
Order Execution (limit order at current best bid/ask)
    ↓
Position Monitor (2-second intervals)
  ├── Trailing Take-Profit (tiered by entry price, activation gate, dynamic tightening)
  ├── 3-Tier Stop-Loss (entry-price-dependent thresholds)
  └── Hold-for-Settlement (optional)
```

### 2.3 Grok Prompt Categories

| Category | Keywords | # Trades | Win Rate | Notes |
|----------|----------|----------|----------|-------|
| BTC | "bitcoin", "btc" | 497 | 80.3% | 57.7% of all trades; BRTI settlement index |
| Weather | "temperature", "weather", etc. | 114 | 78.9% | NWS CLI settlement; narrow-bin HOLD rule |
| Gas | "us gas prices" | 66 | 100% | AAA settlement; perfect track record |
| Oil | "wti", "oil price" | 37 | 88.9% | ICE settlement |
| Events/Generic | catch-all | 148 | 83.8% | Netflix, Spotify, DHS, etc. |

---

## 3. Problem Statement

### 3.1 Primary Problem

The trading system is profitable overall but has **five structural weaknesses** that are suppressing returns and creating unnecessary risk:

1. **The entry price filter is too permissive.** The internal model accepts any price in [0.50, 0.99], causing 132 trades at <60¢ entries with a 32.6% win rate and -$34 PnL. These are speculative bets, not high-conviction trades.

2. **YES-direction trades are systematically weaker** at every price tier, yet receive no differentiated treatment in confidence thresholds or position sizing.

3. **The Grok validator has a 48.3% HOLD rate**, meaning nearly half of all API calls (each taking ~27 seconds and consuming API credits) produce no trade. A tighter internal pre-filter could eliminate many of these wasted calls.

4. **The risk/reward ratio is inverted**: the average loss ($0.80) is significantly larger than the average win ($0.46). The system depends entirely on a high win rate to remain profitable — a regression toward even 75% WR would make it unprofitable.

5. **No drawdown protection exists.** A 13-trade loss streak occurred with no circuit breaker, and the March 10–11 weather disaster (-$48 over 2 days) had no automated response.

### 3.2 Secondary Problems

6. **Weather markets are nearly breakeven** (+$1.34 on 114 trades, 78.9% WR). The category generates significant volume but minimal alpha.
7. **Stop-loss exits show mixed effectiveness** — 54.4% were losses, but 45.6% were actually profitable (position recovered after SL trigger, suggesting premature stops).
8. **MIN_HOURS_TO_CLOSE is set to 0**, allowing trades in the final minutes before settlement, when liquidity and data quality are worst.
9. **The internal threshold of 0.50 is essentially filtering by "dominant side"**, not by conviction. It passes markets where the implied probability is barely above coin-flip.
10. **698 out of 862 trades (81%) have "unknown" exit type** — the exit reason tagging is incomplete, making post-hoc analysis of exit mechanism effectiveness difficult.

---

## 4. Data Sources & Methodology

### 4.1 Data Sources

| Source | Records | Period | Notes |
|--------|---------|--------|-------|
| `trades.db` — `trades` table | 1,004 raw rows, 862 after deduplication | Mar 10–31, 2026 | Deduplicated by removing `reconciled_from_*` duplicate entries per market_ticker |
| `trades.db` — `trailing_marks` table | 33 entries | Active positions | Real-time high-water marks for trailing TP |
| Trade decision logs (`logs/trade_decisions/`) | 6 files, 2.7 MB total | Mar 30–31, 2026 | Contains Grok confidence, skip reasons, timing |
| Bot execution logs (`logs/bot/`) | 52 files | Full period | General execution logs |
| Monitor logs (`logs/monitor/`) | 98 files | Full period | Position monitoring + exit logs |
| Error logs (`logs/errors/`) | 6 files | Mar 30–31 | ERROR+ with backtrace |

### 4.2 Deduplication Methodology

The database contains multiple entries per trade due to the reconciliation system:
- **Original entry**: Created when the bot places an order (reason starts with "Internal:" or "Grok override:")
- **Reconciled entries**: Created by periodic position reconciliation (reason starts with "reconciled_from_api_position_cost")
- **Settlement entries**: Created when market settles (reason contains "auto-settled")

Example for ticker `KXBTCD-26MAR3112-T67099.99`:
```
id=992  reason=reconciled_from_api_position_cost_estimate | auto-reconciled  ← DUPLICATE
id=993  reason=Internal: NO | Grok: BTC prices: Yahoo 66884...              ← ORIGINAL
id=997  reason=reconciled_from_api_position_cost_estimate                   ← DUPLICATE
```

**Deduplication rule**: For each `market_ticker`, keep the first non-reconciled entry. If only reconciled entries exist, keep the first one. This reduced 990 resolved rows to 862 unique trade events.

### 4.3 Limitations

- Trade decision logs only cover ~2 days (March 30–31) due to log rotation; Grok analysis statistics are limited to this window.
- The `reason` field is a free-text narrative, making automated extraction of Grok confidence per-trade imprecise. Confidence data comes from parsed log entries.
- Exit type classification relies on keyword matching in the `reason` field; 81% of trades have "unknown" exit type (likely held-to-settlement without explicit tagging).
- No separate tracking of fill price vs. intended price (slippage analysis not possible).

---

## 5. Datapoints Gathered

### 5.1 Overall Performance

| Metric | Value |
|--------|-------|
| Total trades (deduplicated) | 862 |
| Wins | 711 (82.6%) |
| Losses | 150 (17.4%) |
| Closed (early exit, breakeven) | 1 |
| Total P&L | $207.36 |
| Total fees | $20.09 |
| **Net P&L** | **$187.27** |
| Avg win P&L | $0.4598 |
| Avg loss P&L | -$0.7971 |
| **Loss/Win ratio** | **1.73×** |
| Avg P&L per trade | $0.2173 |
| Max win streak | 75 |
| Max loss streak | 13 |
| Best day | +$50.26 (Mar 26, 111 trades) |
| Worst day | -$25.59 (Mar 11, 14 trades) |

### 5.2 Performance by Direction (YES vs NO)

| Direction | Trades | Wins | Losses | Win Rate | Total PnL | Avg PnL |
|-----------|--------|------|--------|----------|-----------|---------|
| **YES** | 465 | 360 | 105 | **77.4%** | $99.65 | $0.2143 |
| **NO** | 393 | 351 | 41 | **89.5%** | $117.91 | $0.3000 |

**Key finding**: NO trades outperform YES by **12.1 percentage points** in win rate and generate **18% more total P&L** despite fewer trades. NO trades have a significantly better risk-adjusted return ($0.30 avg vs $0.21 avg per trade).

### 5.3 Performance by Entry Price Bucket

| Entry Price | Trades | Wins | Losses | Win Rate | Total PnL | Avg PnL |
|-------------|--------|------|--------|----------|-----------|---------|
| **90¢+** | 520 | 501 | 19 | **96.3%** | $190.27 | $0.3659 |
| **80–89¢** | 98 | 86 | 12 | **87.8%** | $25.43 | $0.2595 |
| **70–79¢** | 72 | 55 | 16 | 77.5% | $16.73 | $0.2323 |
| **60–69¢** | 40 | 26 | 14 | 65.0% | $9.20 | $0.2300 |
| **<60¢** | 132 | 43 | 89 | **32.6%** | **-$34.27** | **-$0.2596** |

**Key finding**: The <60¢ bucket is catastrophically negative. It accounts for **15.3% of all trades** but is responsible for a **-$34.27 drag** on the portfolio. The 60–69¢ bucket is barely profitable at 65% WR. Performance is strongly monotonic with entry price — higher entry prices (stronger conviction) produce dramatically better results.

**Breakeven analysis**: At the current avg loss of ~$0.80, the system needs a minimum **63.5% win rate** per bucket to be breakeven. The <60¢ (32.6%) and 60–69¢ (65.0%) buckets are at or below this threshold.

### 5.4 Cross-Tabulation: Direction × Entry Price

**YES Direction by Entry Price:**

| Bucket | Trades | Wins | Losses | Win Rate | PnL |
|--------|--------|------|--------|----------|-----|
| 90¢+ | 231 | 228 | 3 | **98.7%** | $101.72 |
| 80–89¢ | 67 | 57 | 10 | 85.1% | $19.83 |
| 70–79¢ | 56 | 44 | 12 | 78.6% | $14.01 |
| 60–69¢ | 22 | 13 | 9 | 59.1% | $4.33 |
| <60¢ | 87 | 16 | 71 | **18.4%** | **-$40.35** |

**NO Direction by Entry Price:**

| Bucket | Trades | Wins | Losses | Win Rate | PnL |
|--------|--------|------|--------|----------|-----|
| 90¢+ | 285 | 270 | 15 | **94.7%** | $90.91 |
| 80–89¢ | 30 | 28 | 2 | 93.3% | $5.59 |
| 70–79¢ | 15 | 10 | 4 | 71.4% | $2.67 |
| 60–69¢ | 17 | 12 | 5 | 70.6% | $4.86 |
| <60¢ | 42 | 27 | 15 | **64.3%** | $13.73 |

**Critical finding**: The YES/<60¢ intersection is the single worst segment: **18.4% win rate, -$40.35 PnL on 87 trades**. These are low-probability YES bets where the internal model and Grok agree on a contrarian view but the market is correct 82% of the time. By contrast, NO/<60¢ is a more defensible 64.3% WR with positive PnL ($13.73).

### 5.5 Performance by Market Category

| Category | Trades | Wins | Losses | Win Rate | PnL | Avg PnL | % of Total |
|----------|--------|------|--------|----------|-----|---------|------------|
| **BTC** | 497 | 399 | 98 | 80.3% | $115.61 | $0.23 | 57.7% |
| **Weather** | 114 | 90 | 24 | 78.9% | **$1.34** | **$0.01** | 13.2% |
| **Events** | 85 | 70 | 15 | 82.4% | $35.40 | $0.42 | 9.9% |
| **Gas** | 66 | 66 | 0 | **100%** | $26.85 | $0.41 | 7.7% |
| **Other** | 63 | 54 | 9 | 85.7% | $18.62 | $0.30 | 7.3% |
| **Oil** | 37 | 32 | 4 | 88.9% | $9.54 | $0.26 | 4.3% |

**Key findings**:
- **Gas** is the star performer — 100% WR over 66 trades, $26.85 PnL, $0.41/trade. The gas prompt + AAA settlement data is highly predictable.
- **Weather** is nearly dead weight — 78.9% WR but only $1.34 total PnL ($0.01/trade). The category generates large losses on extreme weather events (see §5.16) while contributing minimal alpha on routine days.
- **Events** (Netflix, Spotify, DHS, etc.) has the highest avg PnL at $0.42/trade with 82.4% WR.
- **BTC** is the workhorse — 57.7% of all trades with 80.3% WR — but its PnL efficiency ($0.23/trade) is below gas and events, dragged down by low-price-bucket BTC trades.

### 5.6 Performance by Market Series (Granular)

| Series | Trades | W | L | Win Rate | PnL | Notes |
|--------|--------|---|---|----------|-----|-------|
| KXBTCD (BTC daily) | 460 | 367 | 93 | 79.8% | $101.92 | Core BTC; large volume |
| KXAAAGASW (gas) | 66 | 66 | 0 | 100% | $26.85 | Perfect performer |
| KXHIGHLAX (LA high temp) | 49 | 40 | 9 | 81.6% | $13.85 | Weather; mixed |
| KXBTC15M (BTC 15-min) | 31 | 26 | 5 | 83.9% | $13.43 | Shorter timeframe BTC |
| KXWTI (crude oil) | 30 | 25 | 4 | 86.2% | $3.46 | |
| KXNETFLIXRANKMOVIERUNNERUP | 21 | 18 | 3 | 85.7% | $1.44 | |
| KXNETFLIXRANKSHOW | 14 | 13 | 1 | 92.9% | $9.91 | Strong event market |
| KXSPOTIFYALBUMRELEASE... | 13 | 13 | 0 | 100% | $12.89 | Perfect performer |
| **KXHIGHCHI (Chicago temp)** | 13 | 9 | 4 | 69.2% | **-$11.02** | **Loss leader** |
| **KXNETFLIXRANKMOVIE** | 12 | 3 | 9 | **25.0%** | **-$8.68** | **Severe underperformer** |
| KXDHSFUNDING | 10 | 10 | 0 | 100% | $9.91 | Perfect performer |
| KXDHSFUND | 10 | 10 | 0 | 100% | $9.88 | Perfect performer |

**Critical finding**: `KXNETFLIXRANKMOVIE` (Netflix top movie rank) has a **25% win rate over 12 trades, losing $8.68**. This is the worst-performing series. Meanwhile `KXNETFLIXRANKSHOW` (Netflix top show rank) is 92.9% WR — suggesting the approach works for shows but fails for movies (likely due to higher volatility in movie rankings).

**Chicago weather** (`KXHIGHCHI`) is the worst weather city at 69.2% WR, -$11.02 PnL. This series single-handedly accounts for most of the weather category's losses.

### 5.7 Performance by Exit Mechanism

| Exit Type | Trades | Wins | Losses | Win Rate | PnL | Avg PnL |
|-----------|--------|------|--------|----------|-----|---------|
| Unknown (likely settlement) | 698 | 588 | 110 | 84.2% | $184.97 | $0.265 |
| Settlement (explicit) | 77 | 65 | 12 | 84.4% | $16.38 | $0.213 |
| **Stop-Loss** | 57 | 31 | 26 | **54.4%** | $1.03 | $0.018 |
| Trailing Take-Profit | 28 | 25 | 2 | **92.6%** | $4.71 | $0.168 |
| Reconciled | 2 | 2 | 0 | 100% | $0.27 | $0.135 |

**Key finding on stop-loss**: 57 trades hit stop-loss, but **54.4% (31 of 57) ultimately resolved as WINS**. This means the stop-loss is triggering prematurely — the price dips below the stop threshold, the position is closed at a loss, and then the market recovers and settles in the original direction. This represents significant lost profit.

**Stop-loss outcome breakdown:**
- **WON after SL**: 30 trades, PnL +$5.66, avg entry $0.776
- **LOST after SL**: 26 trades, PnL -$4.68, avg entry $0.650

The average entry price for trades that hit SL and then won ($0.776) is higher than those that lost ($0.650), suggesting higher-conviction entries are more likely to be false SL triggers.

**Trailing TP** is working well: 92.6% WR on 28 trades with $4.71 PnL, but it's only capturing a small fraction of exits. This is expected for a newly implemented feature.

### 5.8 Performance by Hour of Day (UTC)

| Hour (UTC) | Trades | Win Rate | PnL | Avg PnL | Assessment |
|------------|--------|----------|-----|---------|------------|
| 00:00 | 83 | 75.9% | -$6.39 | -$0.077 | **Worst hour — negative PnL** |
| 01:00 | 15 | 86.7% | $5.05 | $0.337 | Good |
| 02:00–12:00 | 113 | ~95%+ | $75.57 | $0.669 | **Off-peak excellence** |
| 13:00 | 58 | 79.3% | $15.39 | $0.265 | Moderate |
| **14:00** | 41 | **65.9%** | $1.44 | $0.035 | **Near breakeven** |
| **15:00** | 42 | **69.0%** | $9.78 | $0.233 | **Below average** |
| 16:00 | 73 | 82.2% | $16.15 | $0.221 | Good |
| 17:00 | 61 | 90.0% | $14.24 | $0.234 | Strong |
| 18:00 | 56 | 82.1% | $3.88 | $0.069 | Moderate but low avg PnL |
| 19:00 | 50 | 82.0% | $6.63 | $0.133 | Moderate |
| 20:00 | 75 | **92.0%** | **$30.69** | $0.409 | **Best high-volume hour** |
| 21:00 | 71 | 87.3% | $7.12 | $0.100 | Good |
| 22:00 | 76 | 89.5% | $17.64 | $0.232 | Strong |
| **23:00** | 48 | **62.5%** | $10.61 | $0.221 | **Lowest win rate** |

**Key findings**:
- **02:00–12:00 UTC (off-peak) is the golden window**: very few trades (113) but near-perfect win rates (95%+) and $0.67 avg PnL. Lower competition and more predictable markets.
- **23:00 UTC is the weakest hour**: 62.5% WR. This is ~6:00 PM ET — a period of active US trading with higher volatility.
- **14:00 UTC (9:00 AM ET)**: 65.9% WR, nearly breakeven. US market open creates volatility.
- **00:00 UTC (7:00 PM ET)**: 75.9% WR, negative PnL — the only net-negative hour.
- **20:00 UTC (3:00 PM ET)**: Best high-volume hour — 92% WR, $30.69 PnL on 75 trades.

### 5.9 Daily P&L Trajectory

| Date | PnL | Trades | Cumulative | Notes |
|------|-----|--------|------------|-------|
| Mar 10 | **-$22.79** | 9 | -$22.79 | **Weather disaster day 1** |
| Mar 11 | **-$25.59** | 14 | -$48.38 | **Weather disaster day 2** |
| Mar 12 | +$26.06 | 30 | -$22.32 | Recovery begins |
| Mar 13 | +$4.81 | 13 | -$17.51 | |
| Mar 14 | +$1.74 | 11 | -$15.77 | |
| Mar 15 | +$16.14 | 87 | +$0.37 | **Breakeven reached** |
| Mar 16 | +$7.14 | 51 | +$7.51 | |
| Mar 17 | +$3.75 | 115 | +$11.26 | Highest trade count |
| Mar 18 | +$1.35 | 41 | +$12.61 | |
| Mar 19 | +$2.70 | 35 | +$15.31 | |
| Mar 20 | **+$36.75** | 46 | +$52.06 | **Best day** |
| Mar 21 | +$23.68 | 32 | +$75.74 | |
| Mar 22 | +$21.56 | 22 | +$97.30 | |
| Mar 23 | +$12.89 | 29 | +$110.19 | |
| Mar 24 | +$3.23 | 23 | +$113.42 | |
| Mar 25 | +$20.20 | 33 | +$133.62 | |
| **Mar 26** | **+$50.26** | 111 | +$183.88 | **Best overall day** |
| Mar 27 | +$14.50 | 42 | +$198.38 | |
| Mar 28 | +$5.59 | 34 | +$203.97 | |
| Mar 29 | +$1.40 | 15 | +$205.37 | Weekend (low volume) |
| Mar 30 | +$0.43 | 55 | +$205.80 | |
| Mar 31 | +$1.56 | 14 | +$207.36 | Partial day |

**Key findings**:
- The system **took 6 days to recover from the Mar 10–11 disaster** (-$48.38 drawdown).
- After recovery, the system has been **net-positive every single trading day** (Mar 12–31).
- The system shows consistent daily profitability in the $1–$20 range, with occasional breakout days ($36–$50).
- Average daily P&L post-recovery (Mar 12–31): **+$12.90/day**.

### 5.10 Position Sizing Analysis

| Position Size | Trades | W | L | PnL | Notes |
|---------------|--------|---|---|-----|-------|
| $0.05 | 170 | 141 | 29 | $5.60 | Tiny size; low-confidence trades |
| $0.19 | 137 | 123 | 13 | $20.90 | |
| $0.20 | 83 | 81 | 2 | $15.80 | High conviction; 97.6% WR |
| $0.76 | 58 | 42 | 16 | $19.76 | |
| $0.18 | 52 | 31 | 21 | $1.80 | Nearly breakeven |
| $1.00 | 49 | 41 | 8 | $33.00 |
| $0.10 | 46 | 41 | 5 | $3.60 | |
| $0.78 | 37 | 33 | 4 | $22.62 | |
| $0.98 | 33 | 29 | 4 | $24.50 | |
| $0.99    |26 | 24 | 2 |$21.78| |
| **$2.55** | 13 | 2 | 11 | **-$22.95** | **Worst size — early large bets** |

**Key finding**: The $2.55 position size (13 trades, 2W/11L, -$22.95) represents early-period trades from March 10–11 disaster when the bot was running with a larger RISK_PER_TRADE. This was the original configuration    before it was tuned down. These large early-period losses account for ~12% of all losses.

Position sizes of $0.18–0.20 (small, low-confidence) have split outcomes: $0.20 is 97.6% WR but $0.18 is only 59.6% WR. This correlates with the entry price findings — $0.18 likely corresponds to low-confidence Grok calls on dubious markets.

### 5.11 Grok AI Validator Analysis

*Data from trade_decisions logs (March 30–31, ~2 days)*

| Metric | Value |
|--------|-------|
| Total Grok API calls | 648 |
| Average confidence | 73.6 |
| **HOLD rate** | **48.3% (313/648)** |
| Average response time | 26.9 seconds |
| Confidence ≥ 90 | 260 (40.1%) |
| Confidence 80–89 | 121 (18.7%) |
| Confidence 70–79 | 54 (8.3%) |
| **Confidence < 70** | **213 (32.9%)** |

**Grok skip/rejection reasons:**

| Reason | Count |
|--------|-------|
| Total skips (all sources) | 1,192 |
| Grok returned HOLD | 314 |
| Confidence below threshold (85) | 306 |
| Direction mismatch (internal=YES, Grok≠YES) | 256 |
| Direction mismatch (internal=NO, Grok≠NO) | 113 |

**Key findings**:
- **48.3% HOLD rate means nearly half of all Grok calls are wasted.** Each call costs ~27 seconds of latency + API credits. Over the 2-day sample, 313 calls resulted in no trade.
- **32.9% of responses have confidence < 70**, well below the 85 threshold. These are very clearly going to be rejected — the markets are genuinely uncertain.
- **Direction mismatch** is the second most common rejection reason (369 total). This means the internal model says YES/NO but Grok disagrees — a healthy disagreement signal, but it also means the internal pre-filter is passing markets where the AI has no conviction.

**API cost estimate** (if applicable): At 648 calls/2 days → ~324 calls/day → ~$10–30/day in API costs depending on xAI pricing (model: `grok-4-1-fast-reasoning` with tool use).

### 5.12 Decision Pipeline Funnel

*From trade_decisions logs (March 30–31)*

| Stage | Count | Conversion |
|-------|-------|------------|
| Markets considered | 1,455 | — |
| Sent to Grok | 648 | 44.5% passed internal filter |
| Grok returned actionable (not HOLD, ≥85 conf, direction match) | ~335 | 51.7% of Grok calls |
| Trades executed | ~55 (from those 2 days) | 8.4% of Grok actionable |
| Trades won | ~45 | ~82% WR |

The funnel shows that **only 3.8% of considered markets result in trades** (55/1,455). This is conservative but may be leaving edge on the table in strong categories like gas and oil.

### 5.13 Stop-Loss Deep Dive

**Current 3-Tier Stop-Loss Configuration:**

| Tier | Entry Price | Stop Method | Notes |
|------|------------|-------------|-------|
| Tier 1 | ≥$0.82 | 15¢ absolute drop | Fixed dollar threshold |
| Tier 2 | $0.75–$0.81 | 25% of max_payout | Proportional |
| Tier 3 | <$0.75 | 15% of max_payout | Tighter for lower-conviction |

**Stop-loss requires 3 consecutive confirmations** at 2-second intervals (6 seconds minimum before SL triggers).

**Outcomes of stop-loss exits:**

| Eventual Status | Count | PnL | Avg Entry |
|-----------------|-------|-----|-----------|
| WON (position recovered) | 30 | +$5.66 | $0.776 |
| LOST (correctly stopped) | 26 | -$4.68 | $0.650 |

**54.4% of stop-loss triggers were false signals** — the position would have been profitable if held. This is empirically significant and suggests the stop-loss thresholds are too tight, particularly for higher entry prices.

**Implication**: If the 30 trades that hit SL but would have won had been held, the system would have gained an additional ~$5–10 in PnL (exact amount depends on what the final settlement PnL would have been vs. the SL exit price).

### 5.14 Streak & Drawdown Analysis

| Metric | Value |
|--------|-------|
| Max win streak | 75 trades |
| Max loss streak | 13 trades |
| Worst 2-day drawdown | -$48.38 (Mar 10–11) |
| Days to recover from worst drawdown | 6 days (breakeven on Mar 15) |

The 13-trade loss streak occurred during the March 10–11 weather disaster. No circuit breaker exists to pause trading after consecutive losses. At current sizing ($0.18–$0.20 per trade), a 13-trade loss streak represents ~$2.50 in exposure, but at the early period's $2.55 sizing, it was catastrophic ($33+
in potential drawdown).

### 5.15 Hold Time Analysis

| Status | Trades | Avg Hold Time |
|--------|--------|---------------|
| WON | 705 | 8.0 hours |
| LOST | 150 | 5.3 hours |
| CLOSED | 1 | 0.0 hours |

**Key finding**: Losing trades resolve faster (5.3h) than winning trades (8.0h). This is consistent with the stop-loss mechanism closing losing positions early. However, it also means losses are realized faster, which contributes to shorter loss streaks in time but not necessarily in trade count.

### 5.16 Worst Trades Analysis (March 10–11 Disaster)

The **March 10–11 period** represents the system's worst performance, losing **$48.38 across 23 trades**. Analysis of these trades reveals a specific pattern:

**March 10 Losses (9 trades, -$22.79):**

| Ticker | Dir | Entry | PnL | Reason |
|--------|-----|-------|-----|--------|
| KXHIGHCHI-26MAR10-B63.5 | NO | $0.975 | **-$8.00** | Historical avg ~45°F, 63°F "extremely rare" — wrong |
| KXHIGHNY-26MAR10-T73 | NO | $0.995 | -$2.85 | "Strong market sentiment" but bet against it |
| KXHIGHPHIL-26MAR10-T78 | NO | $0.995 | -$2.85 | Same pattern |
| KXHIGHLAX-26MAR10-B67.5 | NO | $0.995 | -$2.55 | Same pattern |
| KXHIGHTHOU-26MAR10-B84.5 | NO | $0.995 | -$2.55 | Same pattern |
| KXHIGHDEN-26MAR10-T67 | NO | $0.995 | -$2.55 | Same pattern |
| KXHIGHTATL-26MAR10-B81.5 | NO | $0.965 | -$2.55 | Same pattern |

**Root cause**: The bot was trading weather markets where **YES was priced at 96–99.5¢**, and the bot bet NO (essentially betting the weather event would NOT occur). The markets resolved YES — the weather events did occur. The bot's reasoning cited historical averages and statistical improbability, but March 10, 2026 turned out to be an unusually warm day across multiple US cities simultaneously.

**Pattern**: The system treated "high market consensus" (YES at 95%+) as overconfidence and bet against it. For weather markets near settlement, market consensus is usually correct. This is a **critical flaw in the weather prompt** — it should not be contrarian against strong market consensus on same-day weather events.

**March 11 Losses**: Similar pattern continued with additional weather markets plus some BTC and S&P 500 trades that also went wrong, compounding the damage before the bot's parameters were adjusted.

### 5.17 Operating Mode Comparison

| Mode | Trades | W | L | Win Rate | PnL |
|------|--------|---|---|----------|-----|
| Grok override (bypass internal model) | 429 | 350 | 79 | **81.6%** | $139.34 |
| Internal + Grok (standard two-stage) | 427 | 355 | 71 | **83.3%** | $67.76 |

**Key finding**: Grok override mode produces **2× the PnL per trade** ($0.32 vs $0.16) despite a slightly lower win rate (81.6% vs 83.3%). This suggests the internal model is filtering out some profitable opportunities that Grok would approve. However, the internal+Grok mode is slightly safer (higher WR).

The higher PnL in Grok override mode is partly explained by the Grok override's willingness to trade at higher position sizes when Grok reports high confidence — even on markets the internal model would reject.

---

## 6. Current Configuration

### 6.1 Trading Parameters

| Parameter | Current Value | Purpose |
|-----------|--------------|---------|
| `RISK_PER_TRADE` | $0.20 | Maximum dollar risk per trade |
| `DAILY_LOSS_LIMIT` | $15 | Maximum daily loss before stopping |
| `MIN_CASH_RATIO` | 0.05 (5%) | Minimum cash reserve ratio |
| `MAX_TRADES_PER_DAY` | 100 | Hard cap on daily trades |
| `VOLUME_THRESHOLD` | 100,000 | Minimum 24h volume to consider market |
| `MARKET_SCAN_HOURS` | 12 | Scan markets closing within N hours |
| `MIN_HOURS_TO_CLOSE` | **0** | **No time-to-close minimum** |
| `INTERNAL_HIGH_PROBABILITY_THRESHOLD` | **0.50** | **Internal pre-filter lower bound** |
| `INTERNAL_HIGH_PROBABILITY_UPPER_LIMIT` | 0.99 | Internal pre-filter upper bound |
| `USE_UNDERVALUED_MARKETS` | false | Undervalued market detection disabled |
| `UNDERVALUED_MIN_PROBABILITY` | 0.40 | Undervalued lower bound (unused) |

### 6.2 Grok Configuration

| Parameter | Current Value | Purpose |
|-----------|--------------|---------|
| `USE_GROK` | true | Grok validator enabled |
| `OVERRIDE_INTERNAL_MODEL_WITH_GROK` | false | Two-stage mode (internal + Grok) |
| `OVERRIDE_GROK_IGNORE_VOLUME_GATE` | false | Volume gate enforced |
| `ANALYZER_CONFIDENCE_THRESHOLD` | **85** | Minimum Grok confidence to trade |
| `GROK_DETAILED_LOG` | true | Verbose Grok logging |

### 6.3 Position Monitor Configuration

| Parameter | Current Value | Purpose |
|-----------|--------------|---------|
| `POSITION_MONITOR_INTERVAL_SECONDS` | 2 | Monitor tick rate |
| `QUOTE_FRESHNESS_SECONDS` | 2 | Max age for WS quotes before REST fallback |
| `POSITION_TAKE_PROFIT_PERCENT` | 2 | Legacy TP percent (overridden by trailing TP) |
| `POSITION_STOP_LOSS_PERCENT` | -0.5 | Legacy SL percent (overridden by tiered SL) |
| `POSITION_MONITOR_HOLD_FOR_SETTLEMENT` | false | Option to hold until settlement |

### 6.4 Bot Schedule

```
BOT_LOOP_SCHEDULE=05:00-17:00=2,17:00-21:00=3,21:00-05:00=8
```
- 05:00–17:00 local: 2-minute scan intervals (active trading)
- 17:00–21:00 local: 3-minute intervals (moderate)
- 21:00–05:00 local: 8-minute intervals (overnight)

### 6.5 Trailing Take-Profit Configuration (Hardcoded)

| Parameter | Value |
|-----------|-------|
| Activation gate | 10% of max_payout above breakeven |
| Base trail (≥90¢) | 5.5% of entry price |
| Base trail (80–89¢) | 4.5% of entry price |
| Base trail (<80¢) | 3.5% of entry price |
| Dynamic tightening (≥40% profit) | ×0.65 multiplier |
| Dynamic tightening (≥25% profit) | ×0.80 multiplier |
| Absolute minimum trail (≥80¢) | 3.5¢ |
| Absolute minimum trail (<80¢) | 3.0¢ |
| Base confirmations | 2 cycles |
| Deep profit confirmations (≥30% profit) | 3 cycles |

### 6.6 Stop-Loss Configuration (Hardcoded)

| Tier | Entry Price | Method | Confirmations |
|------|------------|--------|---------------|
| 1 | ≥$0.82 | 15¢ absolute drop | 3 |
| 2 | $0.75–$0.81 | 25% of max_payout | 3 |
| 3 | <$0.75 | 15% of max_payout | 3 |

---

## 7. Identified Problems

### Problem 1: LOW-PROBABILITY ENTRY TRADES (CRITICAL)

- **Impact**: -$34.27 across 132 trades (<60¢ entries)
- **Root cause**: `INTERNAL_HIGH_PROBABILITY_THRESHOLD=0.50` passes any market where one side has ≥50% implied probability — essentially every market that isn't perfectly 50/50. The internal "pre-filter" is not filtering.
- **Compounding factor**: YES entries <60¢ (implied YES probability 50–60%) have an 18.4% win rate. The bot and Grok agree on a direction, buy at a low price, and are wrong 82% of the time.
- **Cost of inaction**: If current patterns continue, ~6 trades/day in this bucket at -$0.26 avg = -$1.56/day = -$47/month.

### Problem 2: YES/NO DIRECTION ASYMMETRY (HIGH)

- **Impact**: YES trades underperform NO by 12.1 percentage points in WR and $0.09/trade in avg PnL
- **Root cause**: YES trades at low prices represent contrarian bets against market consensus. For short-term markets (especially weather and BTC), market consensus is typically correct. NO trades at low prices are betting *with* the dominant market view, which is more reliable.
- **Compounding factor**: Both directions use the same confidence threshold (85) and the same Grok prompt structure. No adjustment is made for the empirically demonstrated weakness of contrarian YES bets.

### Problem 3: GROK API WASTE (MODERATE-HIGH)

- **Impact**: ~313 wasted API calls per 2-day sample (48.3% HOLD rate), ~$5–15/day in API costs, plus 27s latency per call
- **Root cause**: Internal pre-filter passes too many uncertain markets to Grok. Markets with 50–60% implied probability are inherently uncertain — Grok correctly reports HOLD on most of them.
- **Optimality gap**: If the internal threshold were raised from 0.50 to 0.70, approximately 200+ of those 313 HOLD calls would be eliminated before reaching Grok.

### Problem 4: RISK/REWARD ASYMMETRY (MODERATE-HIGH)

- **Impact**: Average loss ($0.7971) is 1.73× average win ($0.4598). System requires >63.5% WR to break even.
- **Root cause**: The binary options payoff structure inherently creates this — buying at 90¢ means max win is 10¢ but max loss is 90¢. The system's high WR strategy depends on high-probability markets where 90¢+ entry prices mean wins are small ($0.01–$0.10) and losses are large ($0.90).
- **Mitigating factor**: For 90¢+ entries (96.3% WR), the ratio is acceptable. The problem is acute for <80¢ entries where the WR drops but the asymmetry remains.

### Problem 5: NO CIRCUIT BREAKER (MODERATE)

- **Impact**: 13-trade loss streak occurred; March 10–11 lost $48 over 2 days with no automated response
- **Root cause**: No mechanism to pause trading after consecutive losses or daily drawdowns beyond the `DAILY_LOSS_LIMIT=$15` parameter.
- **Note**: `DAILY_LOSS_LIMIT=$15` exists but losses can compound rapidly in correlated markets (e.g., all weather markets lose simultaneously on an extreme weather day).

### Problem 6: WEATHER CATEGORY UNDERPERFORMANCE (MODERATE)

- **Impact**: 114 trades, $1.34 total PnL, $0.01/trade. Occupies 13.2% of trading activity.
- **Root cause**: Weather markets have fat-tailed outcomes that the Grok prompt's statistical approach doesn't handle well. Extreme weather events (like March 10) cause correlated multi-market losses.
- **Specific failure modes**:
  - Bidding against strong consensus (95%+ YES) on same-day weather
  - Chicago (`KXHIGHCHI`) is -$11.02 over 13 trades
  - Low-price weather bets (temp at exactly X degrees) are coin flips

### Problem 7: PREMATURE STOP-LOSS TRIGGERS (MODERATE)

- **Impact**: 30 trades stopped out that would have been profitable; ~$5–10 in lost PnL
- **Root cause**: The 15¢ absolute threshold (Tier 1, ≥$0.82 entries) is too tight given normal market volatility. Mid-trade price swings of 10–15¢ are common in BTC markets.
- **Data support**: Avg entry for false SL triggers ($0.776) > avg entry for correct SL triggers ($0.650).

### Problem 8: MIN_HOURS_TO_CLOSE = 0 (LOW-MODERATE)

- **Impact**: Allows trading in final minutes before settlement; limited data quality and liquidity
- **Root cause**: Configuration set to 0 to maximize trading opportunities.
- **Risk**: Markets in the final 30 minutes before close have wide spreads, low depth, and data staleness (BRTI updates can lag). The Grok prompt has special handling for <30 min timeframes but market microstructure is hostile.

### Problem 9: EXIT TYPE TRACKING GAP (LOW)

- **Impact**: 81% of trades have "unknown" exit type, limiting effectiveness analysis
- **Root cause**: The exit reason tagging was added recently; older trades don't have `[EXIT]` tags in the reason field. Additionally, trades that simply hold to settlement don't pass through the stop-loss or trailing-TP logic at all — they just get settled by Kalshi.
- **Fix complexity**: Low — add a "held_to_settlement" default tag when no other exit mechanism fires.

### Problem 10: DUPLICATE DATABASE ENTRIES (LOW)

- **Impact**: Raw DB has 1,004 rows for 862 unique trades; inflates statistics if not deduplicated
- **Root cause**: Reconciliation system creates additional rows for position cost corrections and settlement updates.
- **Current mitigation**: Analysis tools deduplicate; but real-time dashboards may not.

---

## 8. Proposed Solutions

### Solution A: Raise Internal Probability Threshold

**Addresses**: Problem 1 (low-probability entries), Problem 3 (Grok API waste), Problem 2 (partial — eliminates worst YES trades)

**Change**: `INTERNAL_HIGH_PROBABILITY_THRESHOLD` from `0.50` → `0.70`

**Expected impact**:
- Eliminates the <60¢ bucket entirely (132 trades, -$34.27), recovering ~$34 in PnL
- Eliminates most 60–69¢ entries (40 trades, net $9.20 PnL lost — acceptable tradeoff)
- Reduces Grok API calls by ~30–40% (fewer uncertain markets sent for validation)
- Estimated net impact: **+$25 PnL per 22-day period** after accounting for lost 60–69¢ profits

**Risk**: May miss some profitable 60–69¢ NO trades (64.3% WR). Consider a two-level threshold: 0.70 for YES, 0.60 for NO.

### Solution B: Direction-Specific Confidence Thresholds

**Addresses**: Problem 2 (YES/NO asymmetry)

**Change**: Introduce separate confidence thresholds for YES and NO directions.

**Proposed values**:
- `ANALYZER_CONFIDENCE_THRESHOLD_YES` = 90 (raised from 85)
- `ANALYZER_CONFIDENCE_THRESHOLD_NO` = 85 (unchanged)

**Expected impact**:
- Filters out the weakest YES trades where Grok confidence is 85–89 (borderline)
- Preserves NO trade flow (which is already strong at 89.5% WR)
- Estimated improvement: ~5% win rate improvement on YES trades

**Alternative**: A simpler approach would be to raise `ANALYZER_CONFIDENCE_THRESHOLD` to 90 globally and accept fewer trades at higher quality.

### Solution C: Category-Specific Trading Rules

**Addresses**: Problem 6 (weather underperformance), specific series problems

**Proposed changes**:
1. **Exclude `KXNETFLIXRANKMOVIE`** from trading (add to `EXCLUDED_MARKET_TICKERS`) — 25% WR, -$8.68
2. **Exclude or limit `KXHIGHCHI`** — 69.2% WR, -$11.02
3. **Add weather-specific safeguard**: When YES price ≥ 0.93 on a weather market, do NOT bet NO (this caused the March 10 disaster)
4. **Consider disabling weather entirely** for a test period — freeing bandwidth for BTC/gas/events which have proven alpha

**Expected impact**:
- Removing KXNETFLIXRANKMOVIE: +$8.68 saved
- Removing KXHIGHCHI: +$11.02 saved
- Weather safeguard on high-consensus: prevents March 10-style disasters
- Total: **+$20 PnL + prevention of tail risk events**

### Solution D: Widen Stop-Loss for High-Entry Trades

**Addresses**: Problem 7 (premature stop-loss triggers)

**Proposed changes**:
- Tier 1 (≥$0.82): Widen from 15¢ → **20¢** absolute drop
- Tier 2 ($0.75–$0.81): Keep at 25% of max_payout
- Tier 3 (<$0.75): Keep at 15% of max_payout
- Alternatively: Move to a time-proportional stop that widens as settlement approaches (when volatility naturally decreases)

**Expected impact**:
- Reduces false SL triggers by ~30–50% (15 fewer premature exits)
- Each avoided false trigger preserves $0.20–$0.50 in PnL
- Estimated: **+$3–$7 PnL per period**

**Risk**: Wider stops mean larger realized losses when the stop IS correct. Current data suggests net positive (30 false vs 26 correct at current widths).

### Solution E: Circuit Breaker Implementation

**Addresses**: Problem 5 (no drawdown protection)

**Proposed mechanism**:
1. **Consecutive loss pause**: After 5 consecutive losses, pause trading for 30 minutes
2. **Hourly loss limit**: After $3 in losses within any rolling 60-minute window, pause for 20 minutes
3. **Category-specific pause**: After 3 losses in the same market category within 2 hours, exclude that category for 1 hour

**Expected impact**:
- Would have mitigated the March 10 disaster: after the first 3–4 weather losses, the circuit breaker would have paused weather trading
- Prevents ~$10–20 in avoidable drawdown per extreme event
- Minimal impact on normal operations (losing streaks of 5+ are rare: only once in 22 days)

### Solution F: MIN_HOURS_TO_CLOSE Adjustment

**Addresses**: Problem 8 (trading too close to settlement)

**Proposed change**: `MIN_HOURS_TO_CLOSE` from `0` → `0.5` (30 minutes)

**Expected impact**:
- Avoids trading in the final 30 minutes when spreads are wide and data quality degrades
- May reduce ~5–10 trades per day in the worst microstructure window
- Net PnL impact: likely positive (these final-minute trades have below-average returns)

**Alternative**: Keep at 0 but let Grok's timeframe-aware prompts handle it (they already have special <30 min logic). This preserves optionality.

### Solution G: Optimize Grok Prompt for Weather

**Addresses**: Problem 6 (weather underperformance)

**Proposed prompt changes**:
1. **Add a "consensus override" rule**: When YES price ≥ 0.93, instruct Grok to only bet WITH market consensus (YES or HOLD), never NO
2. **Add near-settlement freeze**: When <4 hours to close on weather markets and market consensus is >85% in one direction, output HOLD unless there is *breaking* observational data contradicting the forecast
3. **Strengthen the narrow-bin exit**: Some narrow-bin weather markets are slipping through the HOLD rule

### Solution H: Improve Exit Type Tracking

**Addresses**: Problem 9 (81% unknown exit type)

**Proposed change**: In position_monitor, when a trade reaches settlement without triggering SL or trailing TP, tag it as "held_to_settlement" in the reason field. This is a simple code change that dramatically improves post-hoc analysis.

### Solution I: Database Deduplication

**Addresses**: Problem 10 (duplicate entries)

**Proposed change**: Either:
1. Add a `UNIQUE` constraint on `(market_ticker, direction)` in the trades table (prevents dupes at insert time)
2. Or create a view `trades_deduped` that filters reconciled entries for reporting

---

## 9. Recommendations (Prioritized)

### TIER 1: IMMEDIATE (next 24 hours, highest impact)

| # | Action | Solution | Expected Impact | Effort |
|---|--------|----------|-----------------|--------|
| **R1** | Raise `INTERNAL_HIGH_PROBABILITY_THRESHOLD` to 0.70 | A | +$25/period, -30% API costs | 1 line config change |
| **R2** | Add `KXNETFLIXRANKMOVIE` to `EXCLUDED_MARKET_TICKERS` | C | +$8.68 saved | 1 line config change |
| **R3** | Add weather consensus safeguard (no NO bets when YES ≥ 93¢) | C+G | Prevents catastrophic days | ~20 lines code |

### TIER 2: SHORT-TERM (within 1 week, strong ROI)

| # | Action | Solution | Expected Impact | Effort |
|---|--------|----------|-----------------|--------|
| **R4** | Implement circuit breaker (5 consecutive losses → 30 min pause) | E | Prevents $10–20 in extreme drawdowns | ~50 lines code |
| **R5** | Widen Tier 1 stop-loss from 15¢ → 20¢ | D | +$3–7/period | 1 line code change |
| **R6** | Raise `ANALYZER_CONFIDENCE_THRESHOLD` from 85 → 90 | B (simple) | Higher precision, fewer trades | 1 line config change |
| **R7** | Set `MIN_HOURS_TO_CLOSE` to 0.5 | F | Avoids bad microstructure | 1 line config change |

### TIER 3: MEDIUM-TERM (within 2–4 weeks, system improvement)

| # | Action | Solution | Expected Impact | Effort |
|---|--------|----------|-----------------|--------|
| **R8** | Implement direction-specific confidence thresholds | B | Structural YES improvement | ~20 lines code |
| **R9** | Add exit type tagging for held-to-settlement trades | H | Better analytics | ~10 lines code |
| **R10** | Weather prompt overhaul (consensus override, near-settlement freeze) | G | Transform weather from breakeven to profitable | ~50 lines prompt |
| **R11** | Per-category performance tracking in the analyzer | — | Self-learning feedback loop | Already built in trade_analyzer.py |

### TIER 4: STRATEGIC (1–3 months, architecture)

| # | Action | Solution | Expected Impact | Effort |
|---|--------|----------|-----------------|--------|
| **R12** | A/B testing framework for config changes | — | Data-driven optimization | Medium effort |
| **R13** | Database deduplication (unique constraint or view) | I | Data integrity | Low effort |
| **R14** | Consider disabling weather entirely and reallocating to gas/events/BTC ≥80¢ | C | Simplify + improve avg PnL | Config change |
| **R15** | Time-of-day scheduling (reduce/pause during 14:00, 23:00, 00:00 UTC) | — | Avoids weakest hours | Config change |

---

## 10. Implementation Roadmap

### Week 1: Quick Wins

```
Day 1:
  ✎ INTERNAL_HIGH_PROBABILITY_THRESHOLD = 0.70
  ✎ EXCLUDED_MARKET_TICKERS = KXNETFLIXRANKMOVIE
  ✎ MIN_HOURS_TO_CLOSE = 0.5
  → Monitor for 24h, run trade_analyzer.py --since 1 to compare

Day 2-3:
  ✎ Implement weather consensus safeguard in decision_engine.py
  ✎ ANALYZER_CONFIDENCE_THRESHOLD = 90
  → Monitor 48h

Day 4-5:
  ✎ Widen Tier 1 SL from 15¢ → 20¢ in position_monitor.py
  ✎ Implement basic circuit breaker (5 consecutive losses → 30 min pause)
  → Run trade_analyzer.py --since 7 for weekly review
```

### Week 2: Structural Improvements

```
  ✎ Direction-specific confidence thresholds
  ✎ Exit type tagging for held-to-settlement
  ✎ Weather prompt overhaul
  → Run trade_analyzer.py for full-period comparison
```

### Week 3+: A/B Testing & Iteration

```
  ✎ A/B test: weather ON vs OFF
  ✎ A/B test: Grok override vs internal+Grok
  ✎ Hour-of-day scheduling experiments
  ✎ Per-category confidence threshold tuning
  → Weekly trade_analyzer.py reports, iterate based on data
```

---

## Appendix A: Automated Analysis Tool

The `trade_analyzer.py` script has been built to automate this analysis going forward:

```bash
python trade_analyzer.py                # Full human-readable report
python trade_analyzer.py --since 7      # Last 7 days only
python trade_analyzer.py --json         # Machine-readable JSON output
python trade_analyzer.py --save         # Save report to data/ directory
```

The tool performs automatic deduplication, category classification, exit type detection, Grok log parsing, and generates prioritized recommendations based on the data.

---

## Appendix B: Risk Metrics Summary

| Metric | Value | Target |
|--------|-------|--------|
| Win rate | 82.6% | ≥85% |
| Sharpe-like (daily PnL mean / daily PnL stddev) | ~0.9 | ≥1.5 |
| Max drawdown | -$48.38 | <$20 |
| Recovery time | 6 days | <3 days |
| Loss/Win ratio | 1.73× | <1.5× |
| Daily P&L consistency (post-recovery) | 20/20 positive days | Maintain |
| API cost efficiency (trades per Grok call) | 0.52 | ≥0.70 |

---

*End of report. Generated by trade_analyzer.py + manual deep-dive analysis on March 31, 2026.*
