# STATUS.md

_Last updated: 2026-10-21_

## Current phase

**P0 Foundations — PASSED (review round 4).** **P1 Data layer — review rounds 1–5 returned ONE MORE PASS; all
major findings fixed (ledger rounds 5–9); awaiting review round 6.** P2 does not start until the P1 reviewer returns PASS.

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

## Environment notes for reviewers

- The build sandbox cannot reach most data-vendor hosts; cassettes will be recorded in CI (D-011).
- Docker Hub is rate-limited from the sandbox; images come from `mirror.gcr.io` pinned by digest (D-009).
