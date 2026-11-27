# TradingView Lightweight Charts

Checked: 2026-10-21, NOTICE fetched from github.com/tradingview/lightweight-charts.

- Licence Apache-2.0 with NOTICE: "TradingView Lightweight Charts™ Copyright (c) 2025 TradingView, Inc. https://www.tradingview.com/".
- README requirement (mirrors, consistent): add the NOTICE attribution and a link to tradingview.com on a user-visible page; the `attributionLogo` chart option satisfies the link requirement.
- Implementation: keep `attributionLogo: true` and include the NOTICE text on `/legal/attributions`.
- Server-side report charts (PDF/email) are rendered by our own Python chart code, not by this library.
