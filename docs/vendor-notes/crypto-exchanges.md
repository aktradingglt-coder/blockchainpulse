# Crypto exchanges via CCXT

Checked: 2026-10-21. Exchange hosts are blocked from the build sandbox.

- CCXT (MIT) is a client library; **each exchange's API terms govern commercial display** of its data.
- P1 builds one exchange adapter via CCXT (public endpoints: tickers, OHLCV, funding rate, open interest). Default exchange is chosen in `config/providers.yaml`.
- Licence status for display in a paid product: **[unverified for every exchange]** → `display_allowed=false` in hosted mode until the owner confirms each exchange's terms; derived analytics (funding annualised, dispersion) remain allowed only if `derived_data_allowed` is confirmed. Listed under Blocked on humans.
- Geo-restrictions: several exchanges block US IPs for derivatives endpoints; the hosted deployment region matters.
