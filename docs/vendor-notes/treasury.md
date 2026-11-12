# U.S. Treasury — Daily Treasury Par Yield Curve Rates

Checked: 2026-10-21. Format verified from a **real response recorded by CI** (`record-cassettes` workflow,
`packages/data/tests/cassettes/us_treasury/`); terms from web search (the build sandbox cannot reach the host).

- Endpoint: `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all`
  with `type=daily_treasury_yield_curve`, `field_tdr_date_value={year}`, `_format=csv`. Keyless.
- One CSV per calendar year, newest day first. Columns: `Date` (MM/DD/YYYY) and tenors `1 Mo`, `1.5 Month`,
  `2 Mo`, `3 Mo`, `4 Mo`, `6 Mo`, `1 Yr`, `2 Yr`, `3 Yr`, `5 Yr`, `7 Yr`, `10 Yr`, `20 Yr`, `30 Yr`; values in
  percent. Tenors not published on a day are empty (e.g. `4 Mo` before 2022, `1.5 Month` from 2025).
- The adapter requires the long-standing columns (`Date`, `1 Mo`, `3 Mo`, `6 Mo`, `1 Yr`, `2 Yr`, `5 Yr`, `10 Yr`,
  `30 Yr`) and blocks the response if one is missing (a renamed column would otherwise silently drop a tenor).
  Cassettes record the CSV header as their shape for nightly drift checks.
- Published on U.S. government-securities business days, normally after 15:30 ET. Freshness is judged against
  the NYSE calendar; on bond-market-only holidays the curve is labelled stale, never blocked early.
- Rate limit: none published; the adapter stays at 1 request/second, 30/minute.

Licence: U.S. government work, public domain in the United States; no restriction on display, derived use or
redistribution found. Attribution used: "Source: U.S. Department of the Treasury, Daily Treasury Par Yield Curve
Rates". **[verify]** the site's reuse statement once reachable (owner checklist).

Primary source for U.S. yields while FRED stays off (D-010).
