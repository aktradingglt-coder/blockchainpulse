# Vendor notes

One file per vendor. Each records **what was checked, when, how, and what is still unverified**.
Rule (PROMPT §1.11): never rely on memory for endpoints, limits, prices or licence terms.

## Build-environment constraint (2026-10-21)

The build sandbox's egress policy blocks direct HTTPS (and WebFetch DNS) to most data hosts:
`publicreporting.cftc.gov`, `data.sec.gov`, `www.sec.gov`, `api.stlouisfed.org`, `fred.stlouisfed.org`,
`api.eia.gov`, `api.binance.com`, `www.okx.com`, `api.bybit.com`, `api.kraken.com`, `api.llama.fi`,
`developer.paddle.com`, `docs.lemonsqueezy.com`, `docs.anthropic.com`.
Reachable: `pypi.org`, `registry.npmjs.org`, `github.com`, `mirror.gcr.io`, `platform.claude.com`, `code.claude.com`.
Web search (indexed snippets) works. Where a fact comes only from a search snippet or a third-party
source, the note says so and the item stays **[unverified]** until a human or a CI job with network
confirms it. GitHub Actions runners do have open egress, so live smoke tests and cassette recording run there.

| Vendor | File | Status |
| --- | --- | --- |
| Anthropic (models, pricing, SDKs) | anthropic.md | verified via bundled current API reference (cached 2026-10-06) + code.claude.com |
| CFTC COT (Socrata) | cftc.md | dataset IDs **[unverified]** |
| SEC EDGAR | sec-edgar.md | rate limit + UA verified via sec.gov search snippet |
| FRED | fred.md | **legal risk flagged** |
| EIA | eia.md | partially verified |
| Crypto exchanges / CCXT | crypto-exchanges.md | terms **[unverified per exchange]** |
| DefiLlama | defillama.md | terms **[unverified]** |
| Twelve Data, Databento, others | commercial-data.md | commercial, feature-flagged |
| Paddle / Lemon Squeezy | billing.md | Pakistan seller onboarding **[unverified — owner task]** |
| TradingView Lightweight Charts | lightweight-charts.md | NOTICE verified from GitHub |
| Container images | containers.md | digests recorded |
