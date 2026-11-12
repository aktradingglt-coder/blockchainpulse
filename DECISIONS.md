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
  10% of index (otherwise mark, index and the funding derived from the mark are quarantined). A perpetual's 24h
  contract volume must match its 24h quote volume (within 10^-6): × the 24h VWAP for a linear (PF_) contract, equal
  for an inverse (PI_) one, whose contract is worth one quote unit; both hold to float precision on every ticker of
  the recording; else the volume is quarantined (P1 review rounds 24–25). A requested symbol the exchange did not
  return is reported.
- Bars (every source): a high more than 3× the bar's larger of open and close, or a low under a third of the smaller,
  is a bad print even when every bar shares it (a column keyed in the wrong unit, which the field's own series cannot
  show): the recorded Kraken bars' widest wicks are +3.0% and −4.5%; a genuine flash-crash wick beyond 3× would be
  withheld, not served (fails closed) (P1 review round 26). A response whose every bar is quarantined is refused.
- Treasury yield-curve CSV: a body cut off is refused as an outage — a last row with missing fields or a number cut
  short (Treasury prints every yield with two decimals), or a year's file that starts after the 7th of January (the
  file runs newest first, so a cut between rows loses the year's oldest days) (P1 review round 24). A tenor's yield
  more than 1.0 percentage point outside the band its neighbouring tenors draw that day (an end tenor: farther from
  the nearer neighbour than the two nearest differ, by more than 1.0) is quarantined, even when every day's value of
  that tenor shares it (the 10-year divided by 10, the 2-year doubled). Measured only on the recorded 2026 file (201
  days): genuine curves stay within 0.08 of the band; the Treasury host is not reachable from the build environment
  for a longer calibration (STATUS) (P1 review round 26).
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

### D-036 · 2026-10-21 (rev. 2026-11-11, round 25) · SEC company facts: scale errors, not restatements
**Decision.** An XBRL fact entered at the wrong scale (thousands as units and the like) is exactly a power of 1000 off,
and nothing in the filing marks it. `rd_data.adapters.sec_scale` finds such facts from the rest of the same EDGAR
response, and holds them (quarantine) or flags them (kept, with a warning). The evidence, in turn:
1. *Neighbouring periods.* The median of the 4 nearest periods of the same concept, unit and duration, as reported by
   other filings, within 5 years of the fact (a filer's figures from decades ago say little about today's scale).
   - A fact more than 10^3.5 away is held.
   - On a stable concept (typical distance under 10^0.15), a fact more than 10^2.5 away and within 10^0.5 of a power
     of 1000 is held.
   - Anything else more than 10^2.5 away in the 4 newest filings is flagged.
2. *The same period in several filings.* Facts are grouped by scale. The group clearly nearest its neighbours (by
   ≥ 10^1, judged without the disputing filings' own values) is kept; groups a power of 1000 from it are held. When
   the neighbours do not decide, a group two or more filings report beats a single filing's value; otherwise both
   filings' facts of that concept are held. A filing is held whole when most of what it shares with
   other filings is a power of 1000 off.
3. *Sums across periods.* Year to date = earlier year to date + latest quarter, the parts usually from different
   filings. A sum that rescaling one filing's facts by 10, 100, 1/10 or 1/100 makes hold exactly (within 0.1% of its
   smallest term) holds that filing's facts when they sit on the slip's side of their history and the other filing's
   do not sit the step off theirs: a second quarter's statement keyed in the wrong unit breaks its half-year against
   the first quarter's genuine filing. Only figures of four or more significant digits beyond their rounding unit
   count (GE's $5M and $10M NCI shares of OCI add up after such a rescale by chance), and the tolerance is the
   smallest term's (a restated BRK 2022 half-year a tenth of which nearly matched two offsetting quarters, judged
   against the largest term, held two genuine facts) (P1 review round 26).
4. *The same year.* A year-to-date fact against the same year's shorter year-to-date from other filings, judged on the
   ratio the concept shows in other years (at least 3 years, spread under 10^0.3).
5. *The filing's own comparatives.* A newest-period cell about a power of 1000 from the prior years the same filing
   reports beside it (at least two, agreeing within 10^0.5).
6. *Exact relations within one filing and period*, solved together where they share facts:
   - US-GAAP identities: income tax = current + deferred = federal + state + foreign (and each part's breakdown),
     taxes paid, pre-tax domestic + foreign, SG&A, liabilities and equity = assets;
   - statutory tax = statutory rate × pre-tax income;
   - statement identities, each term under whichever of its usual tags the filing uses: change in cash (either cash
     tag) = operating + investing + financing (+ FX, under any of three tags, when reported); comprehensive income =
     net income + OCI (parent portion, or total); comprehensive income including NCI = profit + OCI, and = the
     parent's + the NCI's, and (for filers with no profit line) = net income + NCI share + OCI, in total (843 of 855
     clean instances within 0.1%) or as the parent's and the NCI's (384 of 439); profit = net income + NCI share; net income = income from continuing operations + discontinued operations, the parent's
     shares (355 of 437: C 149 of 149, MMM 32 of 32, GOOGL 11 of 11, GE 163 of 245); income from continuing operations
     including NCI = the parent's + the NCI's share, of continuing operations (155 of 201: C 69 of 74, GE 86 of 127) or
     tagged as the NCI's share of net income (288 of 406: C 149 of 149, GE 135 of 233, MMM 4 of 24); income from
     continuing operations (or profit, or net
     income) = pre-tax income − tax, with equity-method income added after tax where the pre-tax line excludes it;
     pre-tax income = revenue − costs and expenses; operating income = gross profit − operating expenses, and =
     revenue − costs and expenses; a bank's revenue = net interest income + noninterest income (588 of 588 clean
     instances within 0.1%), and so is its net revenue where tagged as such (223 of 223, JPM and WFC); a bank's
     pre-tax income = net revenue (either total, or its two parts) − provision for credit losses − noninterest expense
     (223 of 223 with net revenue, 365 of 373 with `Revenues`, 637 of 645 with the parts; every miss is a BAC 2010–11
     filing); a bank's net interest income = interest income − interest expense (892 of 892: JPM 227, WFC 225, BAC
     225, C 215; it applies at no other filer); net interest income after the provision = net interest income −
     provision (with the total provision 370 of 478: 365 of 365 at JPM and WFC, 5 of 113 at C; with the loan-loss
     provision alone 178 of 198: 176 of 176 at C, 2 of 22 at JPM; with the loans' credit-loss expense, as C tags its
     provision since CECL, 42 of 75: 39 of 39 at C, 3 of 36 at JPM; an instance that does not close is left alone); revenue = contract revenue + other revenue (90 of 90, one filer); liabilities and equity
     = liabilities + equity (including NCI, or the parent's and the NCI apart) + temporary equity where reported (in
     total, or the parent's and the redeemable NCI apart; clean instances closing per variant: 1,710 and 1,736 of
     1,762 with total equity, 849 and 889 of 889 with the parent's and the NCI apart); equity including NCI =
     the parent's + NCI (934 of 935); equity = common stock + paid-in capital (or the two as one line, or, for a
     filing with no paid-in-capital line, no-par common stock alone, as XOM tags it) + preferred (at par, or
     including its paid-in capital as JPM and BAC tag it) − treasury stock + AOCI + retained earnings (1,195 of 1,347
     with the lines apart; 305 of 394 with one line; AMZN 138 of 138, JPM 130 of 140, C 132 of 132; WFC (21 of 138)
     and most BAC filings (8 of 96) do not close). With treasury stock tagged as a negative amount (a convention filers do
     use: GE re-tagged it so in 2015) the same relation is kept as a veto only: where it closes exactly as reported,
     its facts are taken as consistent, and neither a rescale nor a flip of them counts as a repair. AOCI has no such
     convention in the calibration set (the opposite sign closes 1 clean instance of 1,741, by coincidence), so AOCI
     flips are repaired. The NCI's share of OCI tagged with the opposite sign (C's 2011–14 filings) is likewise a
     veto only: as an ordinary identity it held 29 genuine Citi facts; cash-flow-hedge and
     AFS OCI
     rollforwards; gross profit; current + noncurrent assets and liabilities. Several of these do not fit every
     filer's statements (a net-income line that includes discontinued operations, costs that include non-operating
     items, total OCI that includes the NCI's share): a relation that no repair below explains is left alone, and one
     that closes only loosely never vetoes a sign repair;
   - changes = closing − opening balance (goodwill, cash);
   - relations whose lines and signs vary by filer (rate reconciliation, OCI and its parts, the unrecognized tax
     benefit rollforward): the signs are learned from the filing's own table, and the relation is used only if they
     close it in all its years but at most one (and in at least two). Among sign assignments closing the same years,
     the one with the smallest residual wins: a line tiny next to the table's balance closes it within the tolerance
     with either sign, but only its true sign leaves no residual. Tables of more than 12 lines are not learned.

   A relation counts as broken evidence only if rescaling a minimal group of its facts by a power of 1000 closes it,
   and only if the rescaled facts and the rest are both needed for it to close. That group is held if its own history
   (other filings, or failing that its own comparatives as evidence against it) blames it. Otherwise the facts of the
   relations it closes are held. A relation that closes exactly as reported (within 0.1%) must stay exact under a
   repair, not merely within 1%. Statement identities and footnote tables are searched for rescales as separate
   systems (joined, the tax table would be too large to search whole). When no rescale explains a broken relation,
   flipping the sign of one fact may, closing it exactly (within 0.1%: reported figures are exact up to rounding, and
   a flip that lands inside the 1% tolerance is a coincidence) without breaking any relation of the joined system
   that closes exactly as reported, so a total another identity confirms is never the one flipped. Relations that
   close only loosely, and relations whose signs were learned from the filing (they adapt to a sign error carried
   through a whole filing, as a flipped tax total in every column of a 10-K), do not veto a flip. The fact is held when its sign opposes
   most of its (at least two) neighbouring periods, or when it is the only fact whose flip closes that relation;
   otherwise nothing is held. Headline totals over trailing periods are also checked: trailing twelve months = the
   last fiscal year + this year to date − last year to date, each part from the same filing where it reports it.
   These relations chain periods together, so each is checked on its own: a flip that closes one exactly, without
   breaking any relation that closes exactly as reported, is held as above. A slipped decimal (one fact typed 10× or
   100× off, or a tenth or a hundredth) is found the same way: when no power-of-1000 rescale explains a broken
   relation, rescaling one fact by 10, 100, 1/10 or 1/100 that closes it exactly (within 0.1%) without breaking any
   relation that closes exactly as reported explains it. The facts whose rescale closes it and that sit about that
   far from their own history are held; with no history to decide, every one of them is. Relations whose signs were
   learned from the filing are not repaired this way (on clean data one JPM 2013 tax-benefit line closed so by
   coincidence). Several facts slipped together (two totals, a revenue and its cost) are found by the same group
   search as power-of-1000 rescales, with steps of 10 and 100 that must close exactly; it runs over statement
   identities and footnote tables joined (a pair such as assets and liabilities-and-equity still equal each other,
   but each breaks its own statement identity), and history blames a side by half the step. Every fact of such a
   group that has history must sit about the step from it (within 10^0.5): without that, groups of small lines whose
   rescale happened to land inside the 0.1% tolerance of a large relation held 31 genuine JPM 2009 and GE facts.
   A whole statement keyed 10× or 100× off, or with every sign reversed, inside a column that is otherwise genuine
   (an income or cash-flow statement is 30–50% of its flow column, so the column's median does not move) is too large
   for that group search. It is grown from a term of a broken relation along the relations that close exactly as
   reported (their terms add up with it, so they share its scale or sign); 60% of the grown facts with history must
   sit the step off it (within 10^0.5) or, for a flip, have the opposite sign to it; the period's other facts that
   moved too, and are tied by no relation that closes as reported to a fact left out, join it. The statement is held
   when rescaling or flipping it closes the broken relation exactly (within 0.1%), needing both sides. Where the
   relation ties a flow statement to balances it does not contain (the change in cash against closing minus opening
   cash, with or without disposal-group cash, as GE and TSLA tag it), that exact closure is the evidence: a rescaled
   statement need only sit on the slip's side of its history at the median (a fast grower's genuine flows sit far
   above theirs: NVDA's cash flows at a tenth of their value sit only 10^-0.2 to 10^-0.6 below their history), and a
   flipped one need not oppose its history at all (financing and exchange flows change sign on their own). Flips are
   tried only there: tables and rollforwards change sign conventions between filings in clean data, and trying flips
   on them held 231 genuine C, GE and TSLA facts. A slipped cash-flow statement also takes its period's cash-flow lines by element
   name (proceeds, payments, repayments, working-capital changes), which swing so far on their own that a slipped one
   can sit on its history (XOM debt repayments genuinely 10^1.0 above theirs). This search runs before the smaller
   groups, which could otherwise split the statement (two balances close a cash change across two years). Facts that
   close a relation exactly with held facts are held with them when both sit off the rest of their column on the same
   side (by 10^0.25 or more at the median): a slipped figure cannot add up exactly with genuine ones of its size
   (GOOGL 2026 pre-tax income and tax, 10^0.3-0.5 below their history beside a held net income). Measured against
   their history instead of their column, AMZN's genuine 2025 growth (every flow 10^0.3-0.5 above its history)
   carried one held tax line into 17 genuine lines. Income to common stock equal to a held net income of the same
   filing and period (within 10^0.05) is held with it, so the EPS ratio weighs EPS and shares against it; on clean
   data this adds the two META 2012 figures beside its held net income (337 clean holds).
   A broken relation that nothing above explains, in a relation the filer's other filings close (at least four other
   instances, 95% of them within 1%), shows a figure entered wrong by some other factor (assets ×1.5 beside
   liabilities and equity), but it cannot be told from the filer's own tagging of that period: holding such breaks
   held 201 more genuine facts on clean data (GOOGL's newest equity components, JPM 2009 totals, BRK comprehensive
   income). Its facts in the
   four most recent filings are flagged instead (kept, and named in the quality report for review); on clean data
   the flag marks 19 recent facts at 2 of the 15 filers (MMM cash-flow totals beside a disposal-group line, GOOGL's
   newest equity components) (P1 review round 26).
6a. *Ratios at the filer's own level.* EPS × weighted shares = income to common stock (diluted and basic, and
   diluted EPS × basic shares for filers that tag no diluted share count, as XOM; income available to common stock
   where the filing tags it, else net income), and cash is nearly all of cash including restricted cash (either
   total's tag), hold at a level of the filer's own rather than exactly. EPS: preferred dividends still move the level
   of a bank that tags no income to common stock by up to 10^1.5 (C, 2009), so an instance is a slipped decimal only
   when its level sits 10 or 100 times (within 10^0.1) from the filer's median level; no clean instance of the 15
   filers sits there, EPS of any size included. Against net income, BAC's genuine FY2011 EPS ($0.01 on $1.4B of net
   income, nearly all of it preferred dividends) sat 10^-1.09 from the level its 2012 10-K's history showed and was
   held in the 2012–14 10-Ks; against income to common stock it sits at 10^0.09 (round 26 regression check) (three 2008–10 NVDA filings sit exactly 10^3 off, shares tagged in thousands, which this rule leaves
   to the power-of-1000 checks). Stock splits move EPS and shares together and leave the level alone. Cash: its share
   of the total moves at most 10^0.27 from a filer's level in clean data (META, 2026), so any move of 10^0.5 or more
   is a slip. A term confirmed by an exact relation of its own filing (net income, the cash total) is not blamed; of
   the others, the one clearly farthest from its history is held, else all of them. An EPS instance at its usual
   level (within 10^0.3, EPS × shares of net income's sign) although one of its terms is held moved with that term
   (net income and EPS slipped together): of EPS and shares, the one history clearly blames (by 10^0.25 over the
   other) is held with it. Looser, this held 29 genuine NVDA 2008–10 and META 2012 EPS facts next to held share
   counts and income. When an EPS ratio slips while net income's identities close, and net income's whole flow
   column sits within 10^0.5 of that step off its history, the statement was keyed in the wrong unit: the column is
   held, not EPS, also when a size bound held net income first (a fast grower's genuine column sits up to 10^0.4
   above its neighbours, NVDA 2026, too far for the column rule of rule 11 alone).
   Diluted EPS sits within 10^0.3 of basic EPS in clean data (cents rounding of small EPS), so a move of 10^0.5
   between them is a slip, and basic EPS beside a held diluted EPS it agrees with is held with it.
6b. *Parts over their totals.* A never-negative part more than twice its never-negative total in one filing and
   period (current assets, cash, PP&E, goodwill, intangibles, inventory, receivables, loans and securities of total
   assets or of liabilities and equity; deposits, current liabilities, payables and long-term debt of total
   liabilities; cash, inventory and receivables of current assets; payables of current liabilities; cost of revenue of
   costs and expenses): one of the two slipped. No clean part exceeds its total by more than 6% (MMM's Q3-2022 cost
   of revenue over its costs and expenses; one BAC 2008 total of assets tagged 0) and genuine shares reach 0.84, so a
   decimal slip in either lands far past the bound. History blames one, else both are held; a held total takes the
   twin it equals (assets, liabilities and equity) with it, as no identity sees two totals slipped together where
   neither has parts tagged that close. Whichever check holds one twin, the other, equal to it and more than 10^0.5
   from its own history, is held too.
7. *Impossible size.* A currency fact is held when it exceeds a multiple of the company's total assets. Total assets
   is the median of the 4 nearest period ends, from other filings, held facts left out. The multiples were measured on
   the 15 filers below with the bound itself switched off:

   | Lines | Held above | Largest clean value |
   | --- | --- | --- |
   | Tax amounts (expense and parts, reconciliation, taxes paid, deferred and uncertain tax) | 1× | 0.43× (about 30,000 facts) |
   | Changes in balances (working capital, period increases) and OCI | 1× | 0.34×, 0.61× and 0.14× |
   | Cash-flow lines (gross proceeds, payments, repayments; net cash by activity) | 2× | 0.96× (AAPL purchases of securities) and 0.72× |
   | Revenue, cost and purchase lines | 20× | 2.34× (AMZN 2011 pro-forma revenue; 2.33× reported revenue) |
   | Everything else | 3× | 1.51× (XOM gross PP&E) |

   The only clean values above 3× outside revenue and cost lines are option intrinsic values that were themselves
   entered at the wrong scale (NVDA FY2012–13, up to 14.9×; MMM Q1-2022, 4.1×): they are now held. Revenue and cost lines get a wide bound because asset turnover is higher for low-margin distributors
   and traders than for any calibration filer; a ×1000 error still lands far above it. Exempt:
   - companies under $10M of assets;
   - derivative notionals and client assets under management, custody or fiduciary care;
   - cumulative equity balances (paid-in capital, retained earnings or deficit, total equity, treasury stock), as
     instants only: their flows and AOCI are bounded;
   - dei market values.
8. *Spreading.* A held fact's siblings in the same table, a filing's column once mostly held, and relations that
   close around a held fact whose other facts are off their own history too.
9. *Balance jumps.* A balance in a recent filing that moved more than 10^2 from the previous period end is flagged.
10. *Impossible signs.* A negative value of a balance or total that cannot be negative is held: assets, liabilities,
    their current parts, liabilities and equity, cash, PP&E, goodwill, intangibles, inventory, receivables, payables,
    long-term debt, contract revenue, cost of revenue and of goods sold, a bank's interest income and noninterest
    expense, common stock at par and share counts (27 concepts; of 33,433 clean facts in 10-K and 10-Q filings and their amendments (33,219 and 214),
    counted in every taxonomy before the adapter's own filters, one is negative: a
    Citi Q4-2020 cost of revenue of −$46M, held with its annual figure;
    total `Revenues` is left out, as asset managers can report it negative). For headline totals (net, operating and
    pre-tax income, tax expense, net cash flows, revenue, a bank's net revenue, cost of revenue, operating expenses,
    comprehensive income) and equity balances (equity, equity including NCI, retained earnings, AOCI: where two clean
    filings report one period with opposite signs, they report different amounts, a restatement this rule ignores, as
    AOCI at AAPL 2008 and XOM 2011),
    filings that report the same amount with opposite signs, and half-year or year-to-date sums that one filing's
    flip closes, are held on the side that one filing alone reports or that opposes most of its neighbouring periods'
    signs; undecided cases are left alone. Other lines are not sign-checked across filings: their sign conventions
    flip between filings so often (OCI, hedges, allowances) that checking them held 1,900 clean facts.
11. *Shifted columns.* A filing's column for one date is held whole when at least 10 of its currency facts can be
    compared with the same periods in other filings, their neighbouring periods, or (a line no other filing reports)
    the filing's own other periods within 400 days, and the column sits a power of 1000 from them (median within
    10^0.5, 60% of facts within 10^1), or, more tightly, a power of 10 or 100 (median within 10^0.3, 60% of facts
    within 10^0.5: no clean column comes near; the closest, a 2010 META comparative, has its median at 10^-0.57 with
    52% of its facts within 10^0.5 of 10^-1). Balances and flows are separate columns, as the balance sheet and the
    other statements are keyed apart (and a mostly held column is held whole on the same split). For a young filer
    this is often the only evidence; it adds no clean holds.

All checks run on every concept the company reports; a `concepts` filter narrows only what is returned, and the
returned concepts alone date the response (a concept last reported years ago is as old as its last report).

A filing's current-period column is its us-gaap currency facts for its latest period end reported with at least half as
many facts as its busiest one (a cover public float, a footnote amount dated after the quarter or a cover-only
amendment does not move it; a 10-K's prior-year end with more facts does not either). When that column
is held entirely, whether the whole filing or only the column is wrong, the filing no longer makes older figures
current: the response's as-of date is the newest filing whose own column is shown, the figures are judged stale a week
after the held filing arrived (blocked 120 days after their own filing), and a note names both filings. Its
comparatives may still be served. Facts that are undated, filed in the future or filed before their period ended are
set aside with a warning.

**Alternatives.**
- Quarantine every cross-filing disagreement: rejected, it hides restatements.
- MAD per concept (PROMPT §5.3's robust z-score): rejected for company facts, as genuine one-offs sit up to 10^3
  from their neighbours; outliers are caught instead by the relations of rules 6 and 6a, which a slipped decimal
  breaks (held where a rescale or flip explains the break; flagged for review where nothing does, rule 6). A slip in
  a line that is in no relation (and under the 10^2.5 flag) is not detected (see Limits).
- A size bound on tax lines against the line's own history: measured and rejected (a genuine Meta tax line is 39× that
  yardstick, while Microsoft's GILTI ×1000 lands at 30×). Against total assets the same lines separate cleanly (rule 7).
- One size bound for every line (100×, 10× when flagged): too loose; ×1000 tax and cash-flow lines landed at 1.4–8.2×
  total assets and were served (round 12).
- A step-from-last-year rule for flows: measured and rejected (10 genuine 10^2–10^3 moves in the newest AMZN filings).
- A "part ≤ total" bound for every line: measured and rejected. Genuine parts reach 815× their total when the other
  parts offset (GE, Citi). Rule 6b keeps it only for never-negative parts of never-negative totals, at 2×.
- A fixed-sign rate reconciliation: it closes in 50 of 360 clean instances, and once held genuine Apple facts by
  chance.

**Reason: measured.** Calibration used the recorded AMZN company facts (the contract cassette) and the full company
facts of 14 more large filers: AAPL, MSFT, NVDA, JPM, C, WFC, BAC, BRK, XOM, GE, MMM, TSLA, META and GOOGL. These came
from a public EDGAR cache; they were used for measurement only and are not committed.

On clean data:
- AMZN: 10 facts held, all in its FY2014 10-K, which reports the 2013–14 quarterly and annual income-tax expense with
  the opposite sign to every other filing of those periods (a real error in the recorded data); 4 genuine one-offs
  flagged.
- The 15 filers together: 335 historical facts held, **none in any filer's newest filing**. These are a mix, not yet
  classified, of real errors in old filings (share counts; NVDA's and MMM's option intrinsic values; sign
  inconsistencies such as TSLA's positive 2010–11 domestic pre-tax income beside a pre-tax loss, or goodwill
  "increases" in periods goodwill fell) and genuine values withheld. Before the round-11 changes the same data held
  3,789.
- The 59 AMZN 10-K and 10-Q filing dates since 2012, each as an as-of date: from 2015-01-30 on, every snapshot holds
  the FY2014 10-K's 10 sign-flipped tax facts (at the four 2015 dates also 2–5 of that filing's rate-reconciliation
  lines held with them, and at 04-24 and 07-24 two tax-expense facts of other filings; at the filing's own date its 2014 tax figures are withheld until the next filing restates
  them); besides that filing, 10 snapshots hold 1–4 genuine facts in 3 concepts (purchase obligations, OCI translation,
  noncurrent restricted cash). No wrong value is served at any date. The cost is withholding, which is the side these
  rules err on (P2 backtests must treat held facts as missing).

Each USD concept's current-period facts in one filing were seeded one at a time, in the full company facts as recorded
(later 10-Qs are present for AAPL, NVDA and JPM; the round-12 reviewer found re-running as of each 10-K's own filing
date changes the counts by at most 4). Counts are held / flagged / missed; seeds whose value is zero (unchanged by any
scaling) are counted separately in every row.

| Filing (USD concepts) | ÷1000 | ×1000 | Zero values |
| --- | --- | --- | --- |
| AMZN 10-Q 26-000026 (159) | 142 / 11 / 5 | 150 / 5 / 3 | 1 |
| AMZN 10-K 26-000004 (245) | 213 / 24 / 7 | 233 / 6 / 5 | 1 |
| AAPL 10-K FY2025 (174) | 160 / 13 / 1 | 171 / 2 / 1 | 0 |
| MSFT 10-K FY2026 (232) | 198 / 18 / 11 | 218 / 6 / 3 | 5 |
| NVDA 10-K FY2026 (196) | 156 / 19 / 16 | 186 / 3 / 2 | 5 |
| JPM 10-K FY2025 (387) | 351 / 16 / 10 | 357 / 13 / 7 | 10 |

Every ×1000 miss lands below its tier's size bound (for example MSFT's FDII line at 0.8× total assets, JPM's tax
contingencies at 0.1×), so size alone cannot tell it apart. Mis-scaled current columns are held entirely in
the newest filing of all 15 filers (both factors, rounds 12–13) and, through rule 11, in young filers' early filings
(reviewer round 14 found them served for filers with 1–7 earlier filings before it). A whole filing mis-scaled in a
company's very first filing has nothing in the response to be compared with, beyond the size bounds.

**Limit (stated, not hidden).** The remaining misses are of four kinds:
- *Rate reconciliations the filing's own years cannot vouch for.* NVDA FY2026 does not close as tagged (15% off with
  the learned signs) and AAPL's does not either (the $10.2B EU state-aid charge uses Apple's own tag). AMZN's and JPM's
  have 13 lines, beyond the 12 whose signs are learned (the round-13 reviewer found one assignment closing 2 of 3
  years within 0.9% for each, probably by chance among 8,192). MSFT FY2026 reports its new table for one year only, so
  there is nothing to learn the signs from. A wrong line in such a table cannot be told apart from the table itself.
- *Lines whose relation is defined only in the filing's XBRL calculation linkbase*: cash-flow details (working-capital
  changes, gross investing and financing flows), footnote rollforwards with filer-specific lines, and new single-year
  tables. The company-facts API P1 uses does not carry which lines make up each section or their weights, and guessing
  them from element names could hold real data.

- *Sign errors in lines outside the identities of rule 6 and the sign-checked totals and balances of rule 10.*
  Filers flip sign conventions between filings for many lines (OCI flows, hedges, allowances, treasury stock) in
  clean data, so a flipped sign there cannot be told from a convention change within this source; and a flip that
  moves an exact identity by less than its 1% tolerance is not distinguishable from rounding.
- *Slipped decimals in lines outside the relations of rules 6 and 6a.* A line 10× or 100× off that no relation of
  its filing contains, under the 10^2.5 flag of rule 3, is served: genuine moves of that size are common in such
  lines (10 genuine 10^2–10^3 moves in the newest AMZN filings alone), so history cannot tell them apart. Measured
  on the newest filing's current cells of 11 headline lines at all 15 filers (×10, ÷10, ×100: 462 seeds), 456 are
  held; the 6 served are GE revenue, GE cash including restricted cash and MMM operating cash flow, lines that no
  relation closes in those filings (round 24). Slips across several facts or a whole column are held through the
  same relations and rule 11: of 124 seeded groups at the 15 filers (assets with liabilities and equity, revenue with
  cost of revenue, net income with one or both EPS, net income with comprehensive income, ×10 or ÷10), 123 are held,
  and 120 of 120 whole current columns (all facts or flows only, ×10, ÷10, ×100, ÷100); the one served is GE's
  comparative revenue, the GE revenue line above (round 25).

Fetching and checking each filing's calculation linkbase is the complete fix. It is recorded for the phase that puts
fundamentals into reports (STATUS). (P1 review rounds 6–12.)
