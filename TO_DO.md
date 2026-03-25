# To Do

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