# DECISIONS.md

Every ambiguous choice, with date, decision, alternatives and reason (PROMPT §1.2). Newest last.
Rule of thumb applied throughout: prefer the option that is **safer, more testable and more reversible**.

---

### D-001 · 2026-10-21 · Working identity for all placeholders
**Decision.** Product name `Research Desk (working name)`; legal entity `Research Desk (Private) Limited — working name, not yet incorporated`, country Pakistan; domain `example.com`; support email `support@example.com`; logo = generated text wordmark (`apps/web/public/brand/wordmark.svg`). All values live only in `config/product.yaml`.
**Alternatives.** Inventing a brandable name/domain.
**Reason.** PROMPT §2 rule 1 forbids inventing real-world identities; `example.com` is IANA-reserved. Owner task in STATUS.md and the Owner Launch Checklist.

### D-002 · 2026-10-21 · Python 3.12, uv workspace, one lockfile
**Decision.** `requires-python = ">=3.12,<3.13"`; a uv workspace where each `packages/*` and `apps/*` directory is a member with its own `pyproject.toml`; one root `uv.lock`. Import names are prefixed `rd_` (`rd_core`, `rd_data`, …) to avoid collisions with generic names like `core`.
**Alternatives.** Single flat package; Poetry; Python 3.13.
**Reason.** Workspace members enforce dependency direction (quant cannot import agents); one lockfile keeps pins exact; 3.12 is the floor named in PROMPT §4.1 and has the widest wheel support for the quant stack.

### D-003 · 2026-10-21 · numpy + pandas (not polars) for quant
**Decision.** Pure functions take/return numpy arrays or small typed dataclasses; pandas is used for calendar alignment and resampling at the edges; `pandas-stubs` for mypy.
**Alternatives.** polars.
**Reason.** statsmodels, exchange_calendars and pandas_market_calendars are pandas-native; one dataframe library only (PROMPT §4.1 "pick one").

### D-004 · 2026-10-21 · Valkey instead of Redis server
**Decision.** Run `valkey/valkey` (BSD-3-Clause) as the Redis-protocol server. Client library remains `redis-py`. Env var keeps the name `REDIS_URL`.
**Alternatives.** Redis 7.4 (RSALv2/SSPL), Redis 8 (RSALv2/SSPLv1/AGPLv3 tri-licence).
**Reason.** The self-hosted edition ships the server in the buyer's compose bundle; a permissive licence removes redistribution doubt. Protocol compatible, so swapping back is a one-line image change.

### D-005 · 2026-10-21 · Job queue: Dramatiq on Redis-protocol broker; Postgres is the source of truth
**Decision.** Dramatiq (Redis broker) for transport, retries with exponential backoff and the built-in dead-letter queue. Every research run's state lives in Postgres and is persisted after each transition; queue messages carry only IDs, so a lost message is recoverable by a reconciler that re-enqueues runs stuck in a non-terminal state.
**Alternatives.** Arq (asyncio-native but in maintenance mode), Celery (heavier, more moving parts), Postgres-only queue.
**Reason.** Mature retries and dead-letter out of the box; async actors supported; DB-as-truth gives resumability and idempotency independent of the broker.

### D-006 · 2026-10-21 · Scheduler leader election via Postgres advisory lock + unique job keys
**Decision.** The scheduler process holds `pg_try_advisory_lock(<constant>)` on a dedicated connection; only the holder fires. Each fire inserts into `scheduled_job_runs` with a unique key `(job_type, date, session)` so even two leaders cannot double-fire.
**Reason.** Two independent guards; both testable without timing tricks.

### D-007 · 2026-10-21 · Agent runtime: Anthropic Python SDK + our own orchestrator (deviation from §4.1 default)
**Decision.** Agents run on the Messages API through `rd_agents.llm.LLMClient` (wrapping `anthropic` SDK), with our own typed tool registry, per-agent tool allow-lists, state machine and evidence ledger. The Claude Agent SDK is **not** used in the production worker.
**Alternatives.** `claude-agent-sdk` (PROMPT default).
**Reason (checked 2026-10-21, docs/vendor-notes/anthropic.md).** (1) The Agent SDK spawns the Claude Code CLI as a subprocess and ships built-in Bash/Read/Write/WebFetch tools; PROMPT §6.4 and §14 require that agents reading untrusted text have *no* shell, network or write tools — with our own loop the tool list is exactly what we register, nothing to disable. (2) `allowed_tools` auto-approves rather than restricts, and nested subagents are allowed by default; "only the Director dispatches" is simpler to guarantee when we own dispatch. (3) §6.5 byte-for-byte replay needs every request/response captured at the HTTP-call level, which `LLMClient` does directly. (4) Batch API and prompt caching are first-class in the Messages API. Reversible: `LLMClient` is an interface.

### D-008 · 2026-10-21 · Auth: decided at P7, constraint recorded now
**Decision (provisional).** Must be self-hostable (no SaaS-only auth), support email + OAuth, TOTP MFA for admins, orgs/teams. Candidate: Better Auth (MIT, Next.js) issuing JWTs verified by FastAPI via JWKS. Final choice recorded at P7.

### D-009 · 2026-10-21 · Container images pulled via `mirror.gcr.io`, pinned by digest
**Decision.** Compose and Dockerfiles reference `mirror.gcr.io/<repo>:<tag>@sha256:<digest>`. Digests in docs/vendor-notes/containers.md.
**Reason.** Docker Hub anonymous pulls are rate-limited (hit during the build). The digest pins content regardless of registry; the mirror is a public Google endpoint. Owners may swap the registry prefix with no other change.

### D-010 · 2026-10-21 · FRED adapter off by default
**Decision.** Build and test the FRED adapter, ship with `provider_fred: false`. Source US yields/macro from primary US-government publishers first.
**Reason.** FRED's legal terms (search snippet, 2026-10-21) prohibit using FRED content to develop or train software including LLMs/generative AI; our product feeds values to LLM agents. Legal opinion needed → STATUS.md Blocked on humans.

### D-011 · 2026-10-21 · Live vendor endpoints unreachable from the build sandbox → cassettes recorded in CI
**Decision.** Contract-test cassettes are recorded by a GitHub Actions workflow (`record-cassettes.yml`, manual trigger, open egress, public keyless sources only; keyed sources need repo secrets). Until a cassette has been recorded from the live source, its file carries `"provenance": "schema-derived"` and the contract test reports it as such; CI fails the release gate (P11) if any cassette is still schema-derived.
**Reason.** Honesty rule (§1.4): never pretend a hand-built response was recorded.

### D-012 · 2026-10-21 · Observability defaults
**Decision.** structlog JSON logs with a secret-redaction processor; OpenTelemetry SDK with OTLP/HTTP exporter only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (otherwise a no-op provider); Prometheus `/metrics` on the API; Sentry only when `SENTRY_DSN` is set.
**Reason.** Zero external dependency in dev and self-hosted, full telemetry when configured.

### D-013 · 2026-10-21 · Billing: Paddle sandbox default, Lemon Squeezy fallback
Per PROMPT §2 rule 10. Pakistan seller onboarding/payout not confirmed from official docs → owner task (docs/vendor-notes/billing.md).

### D-014 · 2026-10-21 · Frontend package manager: pnpm (pinned via `packageManager` field)
**Reason.** Strict, content-addressed lockfile; available in the toolchain.

### D-015 · 2026-10-21 · Secret scanning: gitleaks in pre-commit and CI
gitleaks binary pinned by version and SHA-256 (`make tools`) runs in pre-commit (staged changes), in CI over the full
git history, and over every committable file (`scripts/scan_committable.sh`, ignored files such as `.env` excluded).
`tests/test_secret_scanning.py` plants keys of several shapes (including `redis://:password@`) and proves they are
caught. Fixture and eval directories are covered by the same scan, so no separate regex test is needed.

### D-016 · 2026-10-21 · Dramatiq licence noted (LGPL-3.0)
**Decision.** Keep Dramatiq (D-005). It is used unmodified as an imported library, which LGPL-3.0 permits in proprietary
products; the self-hosted bundle must ship its licence text and allow replacement of the library (Python packages are
replaceable by design). Recorded for the P10 licence-notice bundle.

### D-017 · 2026-10-21 · Worker logs: launcher configures JSON logging before Dramatiq starts
**Decision.** The worker starts via `python -m rd_worker.main`, which configures structlog JSON logging (with
redaction) in the Dramatiq supervisor process and then runs Dramatiq's CLI with `--skip-logging`, so neither the
supervisor nor its children use Dramatiq's plain-text formatter.

### D-018 · 2026-10-21 · Compose stack runs via `make dev` only
**Decision.** `infra/compose/compose.yaml` paths are relative to the repo root and require the generated `.env`;
`make dev` passes `--project-directory .` and `--env-file .env`. A CA bundle for TLS-intercepting proxies is staged
(git-ignored) as a BuildKit secret and is empty by default, so builds elsewhere are unaffected.

### D-019 · 2026-10-21 · Brand theme and fonts (§2 rule 3)
**Decision.** Neutral theme in `config/design_tokens.yaml`: near-white/near-black surfaces, one blue accent
(#1D4ED8 light, #7CB2FB dark), semantic green/red/amber, light and dark variants; every listed text/background pair
≥ 4.5:1 (`packages/core/tests/test_design.py`). Fonts: Inter (sans) and JetBrains Mono (mono), both SIL OFL 1.1.
**Alternatives.** Brand-like saturated palettes; system fonts only.
**Reason.** Finance readers expect restraint; one accent keeps charts legible; OFL fonts are free to embed in PDFs and
self-hosted bundles. Brand review is an owner task (STATUS H1).

### D-020 · 2026-10-21 · Defaults: USD, UTC, 7-day trial, 70% margin, launch exclusions (§2 rules 4–8, 11)
**Decision.** Display currency USD; server time UTC with each reader's IANA timezone in the UI (captured from the
browser at sign-up, editable); trial 7 days; minimum gross margin 0.70; target users = the default list in §2;
excluded coverage at launch = Pakistan-listed securities until a legal opinion is filed in `docs/legal/`.
**Reason.** These are the values PROMPT §2 prescribes; recorded here so every value in `config/product.yaml` has a
decision behind it.

### D-021 · 2026-10-21 · Sales countries and blocked jurisdictions (§2 rule 5, §13.1)
**Decision.** Sales worldwide by default, filtered by `config/jurisdiction_rules.yaml` on the buyer's billing
country. Automatically blocked: CU, IR, KP, SY, RU, BY and the Crimea/Donetsk/Luhansk regions (UA-43, UA-14, UA-09).
Pakistan (`PK`) is `not_yet_cleared` (no sales) until counsel advises on SECP Research Analyst Regulations.
**Alternatives.** OFAC comprehensive list only (CU, IR, KP, SY, regions) — narrower.
**Reason.** Safer superset of comprehensive US/EU/UK programs; easier to unblock a country later than to unwind
sales made into a sanctioned one. Syria's US sanctions were largely lifted in 2025, but it stays blocked until counsel
confirms (EU/UK and residual measures). Counsel review is owner task H7.

### D-022 · 2026-10-21 · Credit costs (§12.1)
**Decision.** Deep dive: quick 3 / standard 5 / extended 10 credits; Ask the Desk 1 credit per answer; screener run 1.
Trial 3 credits (= one quick deep dive), Starter 20, Pro 100, Desk 400 pooled.
**Reason.** PROMPT §12.2 expects a deep dive to cost about $1–2 in LLM spend before caching; pricing 1 credit at
roughly $0.40–0.50 of LLM cost keeps a standard deep dive (5 credits) inside the per-credit value of every paid
tier. To be re-derived from measured token usage at P11.

### D-023 · 2026-10-21 · Proposed tier and licence prices (§2 rule 7) — revised after P0 review rounds 2 and 3
**Decision.** Starter $49/mo ($539/yr), Pro $179/mo ($2,090/yr), Desk $719/mo for 5 seats ($8,450/yr); self-hosted
$1,999 once with 12 months of updates and $599/yr renewal. Status `proposed`.
**Basis.** (1) Comparables checked 2026-10-21 (`docs/vendor-notes/pricing-comparables.md`). (2) Pre-measurement
unit economics, encoded in `config/product.yaml → commercial.cost_assumptions` and enforced by
`test_every_paid_tier_meets_margin_guardrail_monthly_and_annual`:
- p90 proxy: every credit spent, at the **upper** cost bound of $0.40 per credit (≈$2 per 5-credit deep dive before
  caching, PROMPT §12.2);
- amortised house research (R1/R3/R4/R5 generated once for everyone): $3 per seat per month, i.e. ≈$500/month of
  house reports spread over ≥170 paying seats. Below that scale the house-report cost is a launch investment and the
  per-tier guardrail does not hold — stated here rather than hidden;
- merchant-of-record fee ≈5% + $0.50 per transaction (monthly: 12 fees a year; annual: one).
Margin = (price − LLM − house − fee) / price for the billing period:
| Tier | Monthly price → margin | Annual price → margin | Annual discount |
| --- | --- | --- | --- |
| Starter (20 cr) | $49 → 71.5% | $539 → 70.4% | 8.3% |
| Pro (100 cr) | $179 → 70.7% | $2,090 → 70.3% | 2.7% |
| Desk (400 cr, 5 seats) | $719 → 70.6% | $8,450 → 70.1% | 2.1% |
History: round-1 draft ($39/$149/$599, annual = 10× monthly) ignored fees; round-2 revision ($169/$649) still failed
on annual plans and omitted house reports (P0 review round 3, N3). Prices are deliberately conservative because the
cost bound is pre-caching; P11's measured cost model (`scripts/cost_model.py`) replaces the assumptions and may
allow lower prices or larger annual discounts. **Owner task.** Pricing confirmation (STATUS H6).

### D-024 · 2026-10-21 · Market-data vendors contracted: none (§2 rule 9)
**Decision.** `{{DATA_VENDORS}}` = none. Free/public sources (CFTC, SEC EDGAR, EIA, US Treasury/BLS/BEA, one crypto
exchange via CCXT) are built live in P1; FRED is built but off (D-010); commercial adapters (Twelve Data first,
Databento later) are built behind feature flags with fixture-backed tests. Contracts are owner task H5.

### D-025 · 2026-10-21 · Package dependency direction enforced by import-linter
**Decision.** `[tool.importlinter]` contracts in `pyproject.toml` (run in `make lint`, pre-commit and CI) encode
ARCHITECTURE.md §3.1; each new package adds its contract when it is created.

### D-026 · 2026-10-21 · Web container logs: Next.js defaults until P7
**Decision.** The Next.js standalone server only prints start-up lines in P0 (no request handling beyond the landing
page and health route). Structured JSON request logging for the web app (via instrumentation hook and the same
redaction rules) is delivered with authentication in P7, when the web app starts handling user data.

### D-027 · 2026-10-21 · COT freshness measured from the scheduled release, not the position date
**Decision.** PROMPT §5.4's COT windows (stale after 8 days, blocked after 15) are measured from the scheduled
public release (the Friday after the Tuesday position date, 20:30 UTC), not from the position date.
**Alternatives.** Measure from the Tuesday position date (literal reading).
**Reason.** Measured from Tuesday, the newest available report would be labelled "stale" every Wednesday–Friday
before a newer one exists — telling readers data is out of date when it is the latest there is. From the release
date, "stale" means a weekly release was missed, which is what the label should convey. Holiday-delayed releases
only delay the label. Implemented in `rd_data.adapters.cftc.release_time`, tested in `test_adapters_unit.py`.

### D-028 · 2026-10-21 · Zlib licence added to the dependency allow-list
**Decision.** Add `Zlib` to the licence gate's allow-list (`scripts/check_licences.py`).
**Reason.** NumPy (pulled in by `exchange_calendars`/`pandas` for P1 trading calendars) declares
`BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` for bundled components. Zlib is an OSI-approved permissive licence
(attribution in source distributions, altered versions marked); it imposes nothing on a proprietary hosted or
self-hosted product beyond the notice bundle produced in P10.

### D-029 · 2026-10-21 · CCXT exchange for P1: Kraken Futures
**Decision.** The one CCXT exchange adapter built in P1 is Kraken Futures (public perpetual OHLCV, funding, open
interest, mark/index prices).
**Alternatives.** Binance, Bybit, OKX.
**Reason.** Contract cassettes are recorded from GitHub Actions runners in the US (D-011); Binance and Bybit refuse
US IPs for derivatives endpoints, so their responses could not be recorded or drift-checked. Kraken Futures serves
public endpoints to US IPs and has perpetuals on the majors. Display terms remain unverified (crypto-exchanges.md):
the adapter's licence is `byo_key_only` until the owner confirms them.

### D-030 · 2026-10-21 · Freshness origin: bar close and release time; fixed-width bars
**Decision.** Freshness is always measured from when data *became* true. Bars are judged from their close (a 1h bar
labelled 10:00 is as of 11:00). Macro and inventory series are judged from their release: FRED from the series'
`last_updated`, EIA weekly series from the scheduled release (petroleum Wednesday, natural gas Thursday, 15:30 UTC
after the week-ending Friday), capped at the time we retrieved the data. Bars of a fixed width other than one
session (1h, 4h, 1w) get a rule derived from the daily-bar row of §5.4: stale once the next bar is overdue
(close + width + delay + 5 min), blocked after three missed bars (close + 3 × width + delay + 30 min). Intraday bars
count open-market time of the instrument's calendar (a Friday 15:00 ET equity bar is not overdue over the weekend);
bars a day or wider count wall-clock time. The delay of a delayed feed comes from the provider's recorded licence
terms, never from the caller. Because an estimated release is capped at retrieval time, two guards keep the cap
from hiding bad rows: the newest record may not be dated in the future, and an estimated release more than 12 hours
after retrieval (data held before it was published) refuses the response.
**Alternatives.** Measure from the observation/period date (the literal timestamp on the record); treat intraday bars
as crypto funding/OI.
**Reason.** The independent P1 review showed the literal reading blocks monthly and quarterly releases on release
day, labels EIA data stale three days after its release, and makes 1h bars permanently stale and 4h bars blocked half
the time. §5.4 does not list fixed-width bars; the daily-bar rule (1 missed bar stale, 3 blocked) is its closest row.

### D-031 · 2026-10-21 · Bad prints vs genuine moves: what is quarantined
**Decision.** Price series (bar closes, highs and lows; Treasury par yields, which are daily market prices): an
extreme change (robust z > 10 on log returns, MAD with a mean-absolute-deviation fallback) that comes back within two
points is a bad print and is quarantined. An extreme move in the last two points, or in the first two, cannot be told
apart from a bad print and is quarantined as *unconfirmed* until later bars confirm it; confirmed level shifts are
kept with a warning. A move beyond ×10 in one bar is implausible, so its run and its confirmation extend to five
points (three bad ticks in a row are not a market move). Volume is quarantined only when 100× away from the series'
median. Macro series: values outside the series' physical range and reverting outliers are quarantined. For *flow*
statistics (monthly/quarterly releases such as unemployment or GDP) an extreme *latest* release is kept with a
warning. *Levels* — EIA weekly quantities, COT open interest, and daily market series such as FRED yields, FX and
commodity prices — are judged on relative changes and treated like prices: an extreme latest move is held until
later observations confirm it (P1 review rounds 3 and 5). Open interest is held only beyond ×3/÷3 (expiry rolls of
20–40% are shown and flagged).
Non-finite numbers are refused by every canonical schema.
**Alternatives.** Quarantine only interior two-sided spikes (the original rule); quarantine every extreme latest
value, macro included.
**Reason.** A bad print on the latest bar is the most damaging error a report can repeat; holding it until
confirmation costs at most two bars and the freshness label says so. Official statistics are revised rather than
mis-printed, and hiding a genuine record release (e.g. April 2020 unemployment) would misstate the data.

### D-032 · 2026-10-21 · Licence tags: display and derived rights are independent
**Decision.** Add `display_only` to the licence tags: raw values may be shown, analytics derived from them may not.
`ensure_displayable` passes `display_ok`/`display_only`; `ensure_derivable` passes `display_ok`/`derived_only`.
**Reason.** PROMPT §5.1 makes derived analytics conditional on `derived_data_allowed` alone; some exchange and vendor
terms allow display but forbid derived data, which the three-tag model could not represent (P1 review round 1).

### D-033 · 2026-10-21 · Physical bounds and consistency checks (P1 review round 2)
**Decision.** Values that cannot be true are quarantined, never shown:
- EIA weekly stocks: ≥ 0 and below round figures above total U.S. working storage for the product (crude excl. SPR
  800,000 thousand barrels; gasoline 400,000; distillate 300,000; Lower-48 working gas 6,000 Bcf). These are
  engineering ceilings, not market data shown to users; refine against EIA's *Working and Shell Storage Capacity*
  report when the host is reachable. A series mixing units is refused.
- Kraken perpetuals: prices > 0, open interest and volume ≥ 0, funding within ±1% per hourly interval, and mark within
  10% of index (otherwise mark, index and the funding derived from the mark are quarantined). A requested symbol the
  exchange did not return is reported.
- COT: both the long and the short side (with spreading) must sum to open interest; rows for another market are
  rejected.
- Corporate actions: an unexplained split-ratio jump blocks the series only when split dates come from a
  corporate-actions source; without one (Twelve Data today) it is a warning, because a genuine −33% or ×2 day cannot
  be told from an unadjusted split.
- EIA catalogue series also carry a maximum weekly change (crude 40,000 kbbl, gasoline and distillate 25,000 kbbl,
  working gas 500 Bcf — above any recorded weekly build or draw); a latest week beyond it is held.
- Percent series (FRED) have a floor of −100% and no ceiling: hyperinflation and some lending rates exceed 100%.
**Reason.** P1 review round 2 seeded each of these through the adapters and they passed QA; the bounds are wide
enough that no genuine value approaches them.

### D-034 · 2026-10-21 · Circuit-breaker probes cover a whole fetch; bounded responses
**Decision.** In half-open state the single admitted probe belongs to one *fetch* (a context token), so providers
that need several HTTP calls per fetch (FRED: series + observations; Kraken: instruments + data) can complete it and
close the circuit. Success is recorded only after a body parses and clears the last error shown on the status page. A probe that ends
without a verdict (rate-limited, bad request) is given back at once. HTTP 401/403 count as failures, so a revoked
key shows the source as down.
Responses above 64 MB are refused without buffering, and one call waits at most 60 s in total between retries
(a longer `Retry-After` is reported as `RateLimited` with the hint, for the caller or scheduler to honour).
**Reason.** P1 review round 3 showed a multi-call provider could never recover once its circuit opened (the second
call met the taken probe), and that `Retry-After` × retries could stall a fetch for minutes.

### D-035 · 2026-10-21 · QA always sees at least 30 points of history
**Decision.** Every series adapter fetches at least `QA_CONTEXT` = 30 points (bars, weeks, days) whatever window was
asked for, runs every quality check on that history, and trims the response to the request afterwards. For a
`since` window the extra history is sized by how many bars the window can hold by now (an incremental poll "since
the last stored bar" may hold only a few). Gap checks block only on gaps inside the returned window. Early in a
year the Treasury adapter also fetches the previous year's file as context (never returned). A series still too
short to check reports "bad prints could not be checked" as a warning rather than passing silently. Week-on-week
open-interest moves are held only beyond ×3/÷3 (expiry rolls of 20–40% are shown, flagged). EIA supports weekly
series only until monthly/quarterly periods and release calendars are modelled; unlisted weekly series assume the
earliest (Wednesday) release so a genuine release is never refused as "not yet published".
**Reason.** P1 review round 4: with a short window (`limit=2`, `weeks=4`, a six-day January file) the bad-print and
stock-move checks had fewer than the 11 points they need, so a ×50 latest close passed as fresh.

### D-036 · 2026-10-21 · SEC company facts: scale errors, not restatements
**Decision.** XBRL scale errors (thousands entered as units and the like) are found from EDGAR's own data in four
ways, each judged per concept, unit and duration (`sec_edgar._scale_errors`):
1. *Neighbouring periods.* Each fact is compared with the median of its nearest 4 periods, as reported by other
   filings. A fact more than 10^3.5 away is held. On a stable concept (typical distance under 10^0.15), a fact more
   than 10^2.5 away and within 10^0.5 of a power of 1000 is held. Anything else more than 10^2.5 away in the 4 newest
   filings is flagged and kept.
2. *The same period in several filings.* Facts are grouped by scale. The group clearly nearest its neighbours
   (by ≥ 10^1) is kept, and every group a power of 1000 from it is held. When the neighbours do not decide, both
   filings' facts of that concept are held: a guess could serve the wrong scale. A filing whose comparatives are
   mostly a power of 1000 off (at least 3) is held whole.
3. *Sums.* Currency flows add up across periods: year to date = earlier year to date + latest quarter. The parts
   usually come from different filings. If a sum fails, and scaling one filing's facts in it by a power of 1000 makes
   it add up (within 1%), those facts are held. The scaled sum must need all three terms, because shrinking a term
   into insignificance proves nothing. The side far from its neighbours is the wrong one; if that is unclear, every
   candidate is held.
4. *Same table.* A held fact's siblings (same filing, concept and period end, such as the trailing-twelve-month
   figure beside a mis-scaled quarter) are held with it.

Whenever two filings are weighed against each other, neighbours reported by either of them are left out, so a
mis-scaled filing never vouches for itself. Facts that are undated, filed in the future or filed before their period
ended are set aside with a warning, without blocking the company. A provider whose responses fail QA shows its last
failing check on the status page and counts as degraded, without opening the circuit.

**Alternatives.** Quarantine any cross-filing disagreement; MAD per concept; compare only within one period (the
round-6 rule).

**Reason.** Measured on the recorded AMZN company facts (28,891 facts; 9,052 periods filed more than once):
- Legitimate restatements reach a cross-filing ratio of 41 (a 20:1 split), so quarantining every disagreement would
  hide real data.
- Genuine one-offs sit up to about 10^3 from their neighbours (a $62.8B equity revaluation).
- Sums fail on real data only through rounding (2 ≠ 1 + 2 at $1M precision) or reclassification (2020 debt proceeds),
  and the every-term rule rejects both.

Results on the clean facts: **0 held**, and 4 recent genuine one-offs flagged.

The newest 10-Q (accession 0001018724-26-000026) has 159 USD concepts with current-period facts. Seeding each
concept's current-period facts:

| Seed | Held | Flagged (kept, warned) | Missed |
| --- | --- | --- | --- |
| ÷1000 | 141 | 7 | 11 |
| ×1000 | 145 | 7 | 7 |
| ×1e6 | 152 | 0 | 7 |

Every fact of the newest 10-Q at ×1000 is held, and the response is refused as stale rather than served at the
wrong scale. The round-6 rule left 158 of 158 newest-quarter seeds undetected, and the round-7 version (neighbours
only) left 36 of 159 ÷1000 seeds undetected.

**Limit (stated, not hidden).** What the misses are:
- Under ×1000 and ×1e6, all 7 are of two kinds:
  - Six concepts that first appear in the newest filing (derivative assets and liabilities, AFS gains and losses,
    derivative OCI). There is no earlier period to judge them against, and nothing else in one EDGAR response can show
    their scale is wrong.
  - `PreferredStockValue` = 0, which scaling leaves unchanged (not an error).
- Under ÷1000 there are 4 more:
  - Two balance-sheet instants landing 10^2.1–10^2.4 from their neighbours (accumulated OCI; equity securities without
    a readily determinable fair value). Instants form no sums.
  - Two volatile equity-revaluation gains (one is flagged for its half-year fact).

These are not caught from this one source. (P1 review rounds 6–7.)
