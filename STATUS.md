# STATUS.md

_Last updated: 2026-10-23_

## Current phase

**P0 Foundations — PASSED (review round 4).** **P1 Data layer — review rounds 1–16 returned ONE MORE PASS; all
major findings fixed (ledger rounds 5–20); awaiting review round 17.** P2 does not start until the P1 reviewer returns PASS.

## Done (P0), with evidence

| Item | Evidence |
| --- | --- |
| Planning docs: ARCHITECTURE, BUILD_PLAN, DECISIONS, CRITIC_LEDGER, STATUS | repo root |
| Vendor notes (what was checked, when, what is unverified) | `docs/vendor-notes/` |
| Single config surface (`config/*.yaml`) validated at start-up | `packages/core/tests/test_config.py` |
| Design tokens, every text pair ≥ 4.5:1 in light and dark | `packages/core/tests/test_design.py` |
| uv workspace, exact pins, one lockfile; pnpm lockfile; images by digest; actions by SHA | `tests/test_repo_hygiene.py` |
| Settings from env, secrets as `SecretStr` | `packages/core/tests/test_settings.py` |
| JSON logging with secret redaction + trace IDs; OTel tracing; optional Sentry with scrubbing | `packages/core/tests/test_redaction.py`, `test_logging_tracing.py` |
| FastAPI `/healthz` `/readyz` `/metrics` `/v1/status` `/v1/meta`, security headers, CORS allow-list | `apps/api/tests/test_app_unit.py`, `tests/integration/test_services.py` |
| Postgres 16 + TimescaleDB, Alembic migration, heartbeats | `tests/integration/test_database.py` |
| Dramatiq worker (retries, dead-letter, async actors) with end-to-end probe | `tests/integration/test_services.py`, `scripts/smoke.sh` |
| Scheduler with Postgres advisory-lock leader election and fail-over | `tests/integration/test_leader_lock.py`, `test_processes.py` |
| Next.js app (TS strict, Tailwind, ESLint), tokens generated from YAML | `apps/web` lint/typecheck/test/build |
| `make dev` boots db, valkey, migrate, api, worker, scheduler, web — all healthy — then smoke | `docs/evidence/p0/make-dev.txt` |
| Secret scanning active (pre-commit + CI full history); planted keys detected | `tests/test_secret_scanning.py`, CI `secrets` job |
| CI: python, web, stack (compose + unit/integration with 80% coverage gate), secrets, audit + licence gate | `.github/workflows/ci.yml` |
| Dependency licence gate (allow-list + reviewed LGPL/data exceptions) | `scripts/check_licences.py`, `tests/test_licence_gate.py` |
| Placeholder decisions incl. proposed prices with comparables | `DECISIONS.md` D-019…D-023, `docs/vendor-notes/pricing-comparables.md` |
| Threat model draft (STRIDE) | `docs/security/threat-model.md` |

## Done (P1), with evidence

| Item | Evidence |
| --- | --- |
| Canonical schemas, typed errors, licence tags per deployment mode | `packages/data/tests/test_schemas_licence.py` |
| Adapter pipeline: flag → validate → cache → fetch → parse → QA → freshness → licence | `rd_data/adapters/base.py`, `test_adapters_unit.py` |
| HTTP: host allow-list (SSRF guard), retries with full jitter + Retry-After, typed errors, redacted URLs | `test_http.py` |
| Shared rate limits (atomic Lua token buckets), circuit breaker, response cache — on real Valkey | `test_resilience.py`, `tests/integration/test_data_redis.py` |
| Freshness per §5.4 with exchange calendars; COT measured from release (D-027) | `test_freshness.py` |
| QA checks catch seeded bad data (gaps, spikes, duplicates, non-monotonic, OHLC violations, rejected rows) | `test_quality.py` |
| Seeded bad data end to end through the adapters, mostly by corrupting real cassettes (bad prints, impossible values, NaN, wrong subject/series/year, renamed columns, malformed payloads, stale symbols) | `test_hardening.py` |
| Provider circuit state on the status page | `/v1/status` → `providers`, `tests/integration/test_services.py` |
| Adapters: CFTC (6 datasets), SEC EDGAR, US Treasury, EIA, Kraken Futures (CCXT), FRED (flag off), Twelve Data (flag off) | `test_adapters_unit.py`, `test_kraken_unit.py` |
| Contract tests replay **real** responses recorded by CI (CFTC ×6 = every dataset ID, SEC ×3, Treasury, Kraken ×2) | `test_contracts.py`, `cassettes/`, workflow `record-cassettes.yml` |
| Nightly live drift check against providers | `.github/workflows/live-smoke.yml`, `rd_data.recording --check-drift` |
| Per-package coverage gate: rd_data ≥ 90% lines and branches (measured in `CRITIC_LEDGER.md`) | `make coverage`, `scripts/check_coverage.py` |

Schema-derived (synthetic, labelled) cassettes remain for keyed vendors EIA, FRED and Twelve Data until keys exist
(D-011); the P11 release gate fails while any remain. Cassette size: ~12 MB in total, mostly SEC company facts (7.4 MB).

## Next

1. Independent P1 review; fix findings until PASS.
2. P2 per BUILD_PLAN.md.

## Blocked on humans

| # | Item | Why | Where it is tracked |
| --- | --- | --- | --- |
| H1 | Product name, legal entity, domain, support email, logo | Working values only (D-001) | config/product.yaml |
| H2 | Legal opinion per sales country; Pakistan SECP Research Analyst Regulations question | Regulatory | docs/legal/ (to be filed) |
| H3 | Legal review of FRED terms (generative-AI clause) | FRED adapter off by default (D-010) | docs/vendor-notes/fred.md |
| H4 | Merchant-of-record confirmation that a Pakistan-incorporated company can onboard and receive payouts (Paddle; Lemon Squeezy fallback) | Not confirmed in official docs | docs/vendor-notes/billing.md |
| H5 | Commercial data contracts (Twelve Data, Databento, others) and each crypto exchange's display terms | Licences | docs/vendor-notes/commercial-data.md, crypto-exchanges.md |
| H6 | Pricing confirmation (proposed: Starter $49, Pro $179, Desk $719 per month; licence $1,999; pending P11 cost-model measurement) | Commercial | config/product.yaml `pricing_status: proposed`, DECISIONS D-023 |
| H7 | Sanctions/jurisdiction list review by counsel | Legal | config/jurisdiction_rules.yaml |

| H8 | `EIA_API_KEY` repository secret so CI can record real EIA cassettes (and Twelve Data / FRED keys once contracted/cleared) | Vendor keys | `.github/workflows/record-cassettes.yml` |

| H9 | Confirm the repository name `blockchainpulse` (in local paths, CI links and recorded cassette sources) is not a third-party trading-signals brand; the product's working name is "Research Desk (working name)" | Brand rule (PROMPT §1) | docs/OWNER_LAUNCH_CHECKLIST.md |

Owner-facing list: `docs/OWNER_LAUNCH_CHECKLIST.md`.

## Forward notes (recorded by reviewers, owned by later phases)

- P7: application connects as a dedicated non-owner, non-superuser role so Postgres RLS applies; nonce-based CSP;
  structured web logs (D-026).
- P10: self-hosted LAN installs with `APP_ENV=production` refuse plain-http CORS origins — decide policy.
- P2/P3 (from P1 review round 4, minor): cross-check Kraken ticker snapshots (mark/index/last, OI, volume) against
  recent bars and history so a coherent ×50 across every field is caught; detect a single Treasury tenor frozen while
  the rest of the curve moves; model EIA monthly/quarterly periods and release calendars before enabling them.
- P8 (from P1 review round 6): event-driven R3/R4 recaps must bypass the response cache (COT 6 h, EIA 1 h TTLs) on
  release days; use a U.S. bond-market (SIFMA) calendar for Treasury freshness so Columbus/Veterans Day curves are not
  labelled stale; make `CircuitBreaker.release_probe` atomic on Valkey (compare-and-delete script).
- Before FRED is enabled (from P1 review round 7, minor): per-series bounds for share-type percent series
  (unemployment, participation: 0–100%) — the global percent range has a −100% floor and no ceiling (D-033).
- Before FRED is enabled (from P1 review round 8, minor): cap the freshness origin (`last_updated`) by the newest
  observation plus its cadence, so an annual series refreshed yesterday but a year old is not labelled fresh.
- P11 (from P1 review round 9, minor): when the exchange itself lacks the 11+ bars of context bad-print QA needs (an
  outage or a newly listed contract), hold the latest bar or mark freshness unverified instead of serving it with a
  warning.
- P2, before fundamentals reach reports (from P1 review round 11; D-036 limit): fetch each filing's XBRL calculation
  linkbase and check every calculation relationship with its declared weights. That covers the scale-error misses the
  company-facts API cannot show: cash-flow detail lines, footnote rollforwards and single-year tables.
- P2 (from P1 review round 9, minor): SEC facts held as likely scale errors must be treated as missing in backtests and
  point-in-time panels (10 of 59 historical AMZN snapshots hold 1–4 genuine facts; D-036).
- P11 (from P1 review round 11, minor): a single malformed Kraken candle blocks the whole fetch (schema check);
  drop and report such rows when they are few, as the SEC ticker map does. Clean SEC responses carry a failing
  non-blocking `scale_outliers` check (genuine one-offs flagged for review); the status page should not count
  non-blocking flags as quality failures.
- Before micro-cap coverage (from P1 review round 11, minor): the total-assets bound is skipped below $10M of assets,
  so x1e6 errors in a micro-cap's newly reported lines are not bounded; a market-value or revenue yardstick would.
- P2 (from P1 review round 12, minor): rate-reconciliation lines in tables that do not close as tagged (AMZN's
  cross-border line among them) are missed both ways; a "no line exceeds the rest of the table" check fails on 30 of
  405 clean tables today, so it needs its own calibration. The poison case (previous 10-Q column ÷1000) still
  withholds some genuine newest facts. SEC parsing took up to 7.6 s of CPU per company in review (Citi), and an
  exception inside the scale checks would count as a provider failure; P2 ingestion should run it off the request
  path. A ticker map with over 1% malformed rows fails QA (blocked) rather than counting as an outage.
- P2 (from P1 review round 13, minor): the 1x tax and 3x general size bounds are measured on 15 large filers; for
  loss-makers (gross NOLs, valuation allowances) and companies whose assets jump several-fold in a quarter they could
  withhold genuine data. Rate reconciliations of more than 12 lines (AMZN, JPM) are not sign-learned. The poison case
  (previous 10-Q column /1000) still withholds genuine newest facts; the 5-year neighbour window leaves some newest
  facts without neighbours (no seed regression measured).
- P2 (from P1 review round 14, minor): withholding in historical snapshots (138 of 984 hold 1–10 facts of the then
  newest filing, mostly the filers' own errors); sign checks across filings cover headline totals only; a ticker map
  of 8,000–10,442 rows is served as complete; a company's very first filing mis-scaled whole has no in-source evidence
  beyond the size bounds.
- P2 (from P1 review round 15, minor): non-headline sign flips (cost of goods, S&M, G&A, taxes paid) are served where
  the filing has no line they must add up with; `Revenues` sign flips have no closing relation (left off the
  never-negative list: asset managers can report it negative); MMM's cash-flow statement does not close as tagged, so
  its flips are served; a small OCI flip can sit within the 1% tolerance. Measure cost lines for the never-negative
  list before widening coverage.
- P2 (from P1 review round 16, minor): a `concepts` request for an annual-only line is blocked 120 days after the
  10-K (the same facts are served in an unfiltered response); decide per-concept freshness for annual lines with the
  report layer. EPS sign flips are served (an EPS-vs-net-income sign check would hold genuine data in 28 of 3,600
  clean pairs; use net income to common shareholders). Flips with no exact relation in the response (CostOfRevenue
  without a gross-profit line; GE revenue) remain.
- Before wider SEC coverage (from P1 review round 12): the size tiers of D-036 were measured on 15 large filers; the
  20× bound on revenue and cost lines is set for low-margin distributors and traders that were not measured.
- P3 (from P1 review round 8, minor): report `indices` refer to the records QA saw, not to the records returned after
  holds; the ledger must store record identities (concept/period/accession, bar time), not positions.
- P11 (from P1 review round 7, minor): keep status-page quality notes per request subject (one passing fetch of another
  subject clears the provider-level note today) and record failures raised inside `_parse` and after QA too.

## Environment notes for reviewers

- The build sandbox cannot reach most data-vendor hosts; cassettes will be recorded in CI (D-011).
- Docker Hub is rate-limited from the sandbox; images come from `mirror.gcr.io` pinned by digest (D-009).
