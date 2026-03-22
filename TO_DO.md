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