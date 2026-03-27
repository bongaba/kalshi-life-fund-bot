# To Do

## Position Monitor Phase 1 Handoff

### Completed in this phase

1. Replaced the old quote helper in `position_monitor.py` with a REST-backed `QuoteEngine`.
2. Quote validity is now strict:
	- only executable bids are trusted
	- a quote is valid only when `price > 0` and matching `size_fp > 0`
3. The monitor now uses `position_fp` from Kalshi positions instead of the incorrect `position` field.
4. P&L is now marked from the actual side-specific executable bid only:
	- YES positions use YES bid
	- NO positions use NO bid
5. The monitor now keeps a per-ticker last-known-good quote cache with a 60-second freshness limit.
6. Settlement-aware hold logic exists but is configurable:
	- if `status` is finalized/settled/resolved/closed, the monitor will hold
	- if `settlement_timer_seconds < 3600`, the monitor will hold
	- if `close_time` is within 1 hour, the monitor will hold
 	- this gate is bypassed by default because `POSITION_MONITOR_HOLD_FOR_SETTLEMENT=false`
7. Close orders remain IOC-only and now explicitly send `post_only: false`.
8. Settlement hold is now configurable and defaults to disabled via:
	- `POSITION_MONITOR_HOLD_FOR_SETTLEMENT=false`
	- `POSITION_MONITOR_SETTLEMENT_HOLD_SECONDS=3600`
9. Missing live exchange positions are now reconciled into `trades.db` when no local `OPEN` row exists.
10. The quote engine now infers missing NO/YES bid sizes from complementary opposite-side ask sizes when Kalshi omits one side's size field but the binary prices line up.

### Current behavior after phase 1

1. Every loop still fetches positions via REST.
2. For each non-zero position, the monitor fetches `/markets/{ticker}` via REST.
3. The raw market payload is fed into `QuoteEngine.update()`.
4. If a fresh executable quote exists for the held side, the monitor evaluates P&L.
5. If the DB has no `OPEN` row for a live exchange position, the monitor backfills a reconciled `OPEN` row using the API position cost basis.
6. If no executable quote exists, the monitor skips the position.
7. If `POSITION_MONITOR_HOLD_FOR_SETTLEMENT=false`, the settlement-aware hold gate is bypassed.

### Why this was necessary

1. The old monitor could mark positions using invalid synthetic values, especially `0.50`, when Kalshi returned no usable live quotes.
2. That caused profitable NO positions to appear as heavy losses and triggered false stop-loss exits.
3. We also discovered a separate earlier issue where canceled IOC entries were being stored as `OPEN`; that has already been fixed in `execution_bot.py`.
4. Some live exchange positions were missing entirely from `trades.db`, which meant the monitor had no entry price and could not compute P&L without fallback logic.

### Verified live API behavior that drove this design

1. `/markets/{ticker}` returns fields like:
	- `yes_bid_dollars`
	- `yes_bid_size_fp`
	- `no_bid_dollars`
	- sometimes `no_bid_size_fp` is `null` even when the complementary YES ask side is populated
	- `close_time`
	- `settlement_timer_seconds`
	- `status`
2. For problematic BTC contracts, live responses showed cases where:
	- markets were `finalized` with no executable depth
	- markets were still `active` but `no_bid_size_fp` was `null` while complementary `yes_ask_size_fp` was populated
3. The monitor now treats the complementary ask size as executable depth only when the binary prices line up cleanly.

### Known limitations after phase 1

1. This phase is REST-first only. No WebSocket ingestion is implemented yet.
2. The last-known-good quote cache is in-memory only. A process restart clears it.
3. The complementary-size inference is based on observed live payload behavior and should be revalidated against Kalshi docs or more samples.
4. There is no persistent audit trail yet for quote-cache events, stale-cache use, or consecutive invalid quote cycles.
5. Reconciled DB rows use an API-derived average cost basis and `order_status='reconciled'`, which is safer than skipping but less authoritative than a true execution record.

### Next phases to implement

1. Add WebSocket-first quote ingestion for `ticker` and `orderbook_delta`.
2. Dynamically subscribe/unsubscribe WebSocket tickers based on currently held positions.
3. Persist `last_valid` quote cache across restarts.
4. Add alerting when a position has no valid executable quote for multiple consecutive cycles.
5. Add a configurable freshness window and configurable settlement hold threshold via `.env`.
6. Confirm the exact NO-side size semantics from Kalshi docs or additional payload samples.
7. Add a reconciliation job outside the monitor loop if you want DB backfill to happen proactively rather than only when a missing live position is encountered.
8. Add tests around:
	- valid quote gate
	- complementary-size inference
	- DB reconciliation for missing live positions
	- stale cache behavior
	- settlement-aware hold
	- finalized market handling
	- partial-fill retry behavior

### Where to resume next time

1. Start in `position_monitor.py`.
2. Read the `QuoteEngine` class first.
3. Then inspect `monitor_positions_once()` to see the exact phase-1 decision flow.
4. If adding WebSockets next, do not replace the REST path immediately.
	Keep REST as the fallback path and feed both sources into the same `QuoteEngine`.
5. Preserve the non-negotiable invariant:
	never compute P&L or fire exits from invalid, synthetic, implied, or stale prices.

## Future Support: Multivariate And Combo Markets

- Current behavior intentionally skips multivariate and combo markets because the bot assumes reliable binary YES/NO pricing.
- Future work should add explicit support for Kalshi multivariate and combo market structures instead of treating them like plain binary markets.

## Plan

1. Identify and classify multivariate/combo markets using fields such as `mve_collection_ticker`, ticker prefixes, and market metadata.
2. Capture and log representative raw payloads for skipped multivariate/combo markets so pricing and structure differences are documented.
3. Review Kalshi multivariate market docs and confirm how tradable prices, sides, and order placement differ from standard binary markets.
4. Design a separate pricing/normalization path for combo markets instead of reusing the current binary `yes_price`/`no_price` assumptions.
5. Update decision filtering so combo markets are routed to a dedicated analyzer path rather than skipped or mixed into binary logic.
6. Add execution safeguards to ensure order construction matches the actual schema for multivariate/combo instruments.
7. Add test cases with recorded market payloads covering binary, multivariate, and partially populated orderbook responses.

## Short-Term Debugging Follow-Up

- Monitor the new skip logs to confirm which markets are excluded because they are multivariate/combo versus missing usable price data.
- Revisit support once enough real payload examples have been collected from production logs.

## Process Improvement Roadmap

1. Make requirements less dumb.
2. Delete unnecessary process steps.
3. Optimize.
4. Accelerate.
5. Automate.

## Requirements And Environment Hardening

1. Pin dependency versions for reproducibility.
2. Split dependencies into runtime vs optional/dev files:
	- requirements-runtime.txt
	- requirements-dev.txt
3. Remove duplicate or unnecessary dependency entries (for example duplicate sklearn/scikit-learn style overlaps).
4. Keep only required runtime packages for production trading path.

## Canonical Paths

1. Keep one canonical trading entrypoint.
2. Keep one canonical scanner path.
3. Deprecate/archive duplicate entrypoint scripts to avoid drift and confusion.
4. Ensure README/run instructions point to canonical scripts only.

## Trading Path Optimization

1. Reduce repeated API calls.
2. Centralize balance fetch to one call per cycle in the trading loop.
3. Pass balance snapshot through to downstream logging/notifications instead of refetching.
4. Preserve gate order so cheap filters run before model calls:
	- excluded ticker
	- missing/invalid prices
	- volume gate
	- time-to-close gate
	- cash-ratio gate

## Performance Reporting and Monitoring

1. Add hourly performance report functionality:
	- Query rolling past 24 hours of trades from SQLite database (current timestamp minus 24h)
	- Calculate win/loss ratio and count
	- Calculate total profit and loss (P&L)
	- Format report for Discord webhook delivery
2. Send report via Discord at top of each hour (configurable via cron or scheduled task).
3. Add manual query command/endpoint to check recent performance on-demand.
4. Integrate into current execution_bot.py trade schedule:
	- Use same Discord webhook as trade notifications
	- Coordinate with existing cycle timings
	- Avoid duplicate API/DB calls
5. Metrics to include:
	- Total trades in rolling last 24h
	- Won trades count and percentage
	- Lost trades count and percentage
	- Total P&L (absolute $)
	- Win/loss streak (current consecutive wins/losses)
	- Largest win and largest loss
6. Add configuration options:
	- Report timezone/hour preference (when to send)
	- Enable/disable hourly reports
	- Save report history for trend analysis

## Scanner Cost/Speed Tuning

1. Keep batched Grok analysis in undervalued_market_scan.py.
2. Tune batch size for cost vs reliability.
3. Tune max markets analyzed per cycle.
4. Tune scan interval for freshness vs cost.
5. Track per-cycle metrics for tuning decisions:
	- markets fetched
	- markets analyzed
	- batches sent
	- AI call duration
	- hits found

## Network Reliability Standards

1. Add one shared HTTP helper with consistent timeout/retry behavior.
2. Standardize timeout defaults across fetch/order/scanner requests.
3. Standardize retry policy with backoff for transient errors (429/5xx/connectivity).
4. Standardize error logging format for status code + response body snippets.

## Automation

1. Add pre-commit checks (lint/format/basic checks).
2. Add CI workflow for syntax/lint/smoke checks on push.
3. Add scheduled housekeeping:
	- log rotation policy check
	- DB backup/checkpoint
	- resolved-trade reconciliation verification
-