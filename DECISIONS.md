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

### D-017 · 2026-10-21 · Worker logs: `dramatiq --skip-logging`
**Decision.** The worker runs with `--skip-logging` so every line comes from our structlog JSON pipeline (with
redaction) instead of Dramatiq's plain-text formatter.

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

### D-023 · 2026-10-21 · Proposed tier and licence prices (§2 rule 7)
**Decision.** Starter $39/mo ($390/yr), Pro $149/mo ($1,490/yr), Desk $599/mo for 5 seats ($5,990/yr), self-hosted
$1,999 once with 12 months of updates and $599/yr renewal. Status `proposed`.
**Basis.** (1) Comparables checked 2026-10-21 (`docs/vendor-notes/pricing-comparables.md`): consumer AI research
$39–$99/mo, professional research software $150–$500 per user per month. (2) Unit-economics pre-estimate, to be
replaced by measurement: at p90 a Pro user spends all 100 credits ≈ 20 standard deep dives ≈ $20–40 of LLM cost plus
an amortised share of house reports; at $149 that is 73–87% gross before merchant fees (≈5% + $0.50). Starter at p90:
4 deep dives ≈ $4–8 → 79–90%. Desk at p90: 80 deep dives ≈ $80–160 → 73–87%.
**Gate.** P11's measured cost model must show ≥70% for every tier at p90; if not, credits or prices change.
**Owner task.** Pricing confirmation (STATUS H6).
