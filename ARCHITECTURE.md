# ARCHITECTURE.md

Research Desk (working name) — a multi-tenant SaaS and self-hosted product in which a team of AI agents writes
evidence-backed market research on FX, commodities, futures, US stocks and crypto. This document restates
`PROMPT.md` in our own words, fixes the system shape, and lists risks and assumptions. Decisions referenced as
`D-nnn` are in `DECISIONS.md`.

## 1. The product in one paragraph

Data comes in from licensed or public sources through adapters that normalise, quality-check and licence-tag every
value. Deterministic Python code computes every statistic. LLM agents decide *what* to look at and *how to explain
it*, but they never type a number: prose refers to values by binding (`⟦E17.data.spread|fmt=0.00⟧`), and the
renderer fills them in from an immutable evidence ledger with a source, as-of time and method attached. Before
anything is published a verifier checks every claim against that ledger, a compliance engine checks the wording,
and only then is the report rendered (web, email, PDF, Telegram, Discord, Markdown, JSON). Impersonal house
reports are generated once per schedule and fanned out to everyone; on-demand deep dives and Q&A are metered
by credits. Every scenario and level ever published is graded after its horizon on a public track-record page.

## 2. Principles that shape the code

1. **Deterministic core, probabilistic edge.** `rd_quant` is pure functions with property and golden tests.
   LLMs plan, select, interpret and write.
2. **Evidence or silence.** Every user-visible number resolves to an `Evidence` or `QuantResult` record. Missing
   data is reported as missing.
3. **Licence-aware by construction.** Each value carries a `licence_tag`; the renderer refuses raw values whose
   provider forbids display in the current deployment mode (hosted vs self-hosted).
4. **Postgres is the source of truth.** Run state, ledger, entitlements, schedules. Queues carry IDs only (D-005).
5. **Everything replayable.** Inputs, evidence, prompts (by git hash), model IDs and every LLM request/response
   are stored per run; `replay <run_id>` re-renders byte for byte.
6. **One config surface.** Product identity, prices, flags, models, budgets, jurisdiction rules live in
   `config/*.yaml`, validated by Pydantic at startup. Secrets only in env/secret manager.

## 3. Components

```
                    ┌────────────── apps/web (Next.js) ───────────────┐
 browser ─────────► │ marketing · app · admin · report viewer (hovers)│
                    └───────────────┬──────────────────────────────────┘
                                    │ REST + SSE (JWT)
                    ┌───────────────▼──────────────┐   webhooks   ┌──────────────┐
                    │ apps/api (FastAPI)           │◄─────────────┤ Paddle / LS  │
                    │ auth · entitlements · credits│              └──────────────┘
                    │ idempotency · SSE progress   │
                    └──────┬───────────────┬───────┘
                 enqueue IDs│               │ reads
                    ┌──────▼─────┐   ┌─────▼──────────────────────────┐
 apps/scheduler ───►│ Valkey     │   │ Postgres 16 + TimescaleDB       │
 (leader lock,      │ (Dramatiq) │   │ runs · ledger · bars · users ·  │
  unique job keys)  └──────┬─────┘   │ entitlements · predictions      │
                    ┌──────▼─────────┴───────────────────────────────┐
                    │ apps/worker                                     │
                    │  research runs: Orchestrator state machine      │
                    │   ├─ rd_agents (LLMClient → Anthropic API)       │
                    │   ├─ tool registry → rd_data adapters / rd_quant │
                    │   ├─ evidence ledger (immutable, hashed)         │
                    │   ├─ verifier · rd_compliance                    │
                    │   └─ rd_reports renderers (HTML/PDF/email/…)     │
                    │  ingestion · alerts · delivery · grading         │
                    └──────┬──────────────────────────────────────────┘
                           │ no-network container
                    ┌──────▼──────┐
                    │ sandbox     │ ad-hoc quant analysis (CPU/mem/time limits)
                    └─────────────┘
 apps/mcp-server: read-only research tools for paying users' own AI clients (metered, licence-gated).
```

### 3.1 Python packages (dependency direction is enforced: arrows point to allowed imports)

| Package | Responsibility | May import |
| --- | --- | --- |
| `rd_core` | settings, config loading, domain enums, errors, UTC time utils, money/units, logging, tracing | — |
| `rd_data` | `ProviderAdapter` protocol, canonical schemas, rate limiter, cache, circuit breaker, QA checks, freshness, licence gate, adapters | core |
| `rd_quant` | pure analytics functions | core |
| `rd_compliance` | banned-language rules, required disclosures, jurisdiction rules, personal-advice classifier | core |
| `rd_reports` | report schemas (Pydantic), binding resolver, number linter, renderers, charts | core, data (licence tags), compliance |
| `rd_agents` | LLMClient, prompts, tool wrappers, ledger, workflow state machine, verifier | core, data, quant, reports, compliance |
| `rd_billing` | `BillingProvider`, Paddle + Lemon Squeezy, entitlements, credits/metering | core |
| `rd_licensing` | Ed25519 licence tokens, activation, signed update manifests | core |
| `rd_db` | SQLAlchemy models, Alembic migrations, heartbeats, tenant-scoped repository layer | core |

Apps (`rd_api`, `rd_worker`, `rd_scheduler`, `rd_mcp`) compose the packages and own no domain logic.

### 3.2 Data model highlights

- **Time series** (`bars`, `macro_observations`, `positioning_reports`, `crypto_derivative_metrics`) are Timescale
  hypertables keyed by `(instrument_id, ts)`; every row has `provider`, `source_tz`, `ingested_at`,
  `quality_report_id`, `licence_tag`.
- **Evidence** rows are insert-only (DB trigger blocks UPDATE/DELETE) with a SHA-256 over the canonical JSON.
- **Tenancy.** Every customer-owned row has `org_id`; the repository layer requires an `OrgContext` and adds the
  filter; Postgres row-level security is enabled as a second guard.
- **Entitlements** are derived only from verified billing events and audited admin overrides.

### 3.3 Research run lifecycle

`QUEUED → PLANNING → DATA_GATHERING → ANALYSIS → CHALLENGE → SYNTHESIS → VERIFICATION ⇄ REVISION(≤2) →
COMPLIANCE ⇄ REVISION(≤2) → RENDERING → PUBLISHED`, any state → `FAILED_SAFE` (refund credits, notify, alert).
Each transition is a DB write with the run's budget counters (tokens, tool calls, wall clock, USD). A worker
resuming a run reloads state and continues from the last persisted transition; LLM calls already recorded are
replayed from the transcript rather than re-billed.

### 3.4 Agents

Director (Opus), Planner, Data Steward, five asset-class analysts, Macro, Technical, Quant, News (Haiku triage +
Sonnet), Red Team (Opus), Verifier, Compliance, Editor. Tool allow-lists are explicit per agent; agents that read
untrusted text (News) get no write/network/code tools; only the orchestrator dispatches (D-007).

### 3.5 Delivery and schedules

The scheduler fires house jobs with unique keys (`date + session + report_type`), the worker runs the pipeline
once, publishes once, then fans out per-user deliveries (email, Telegram, Discord, webhook) as separate idempotent
jobs keyed `(report_id, user_id, channel)`.

### 3.6 Deployment modes

- **Hosted:** IaC (Terraform) → managed Postgres/Timescale, Valkey, containers for api/worker/scheduler/web,
  object storage for PDFs, secret manager.
- **Self-hosted:** `docker compose up` with the same images, a first-run setup wizard, BYO LLM/data keys, licence
  token verified with the public key compiled into the app.

## 4. Cross-cutting concerns

- **Security:** OWASP ASVS L2 target; STRIDE threat model in `docs/security/threat-model.md`; SSRF guard on all
  outbound fetches (host allow-list, private ranges blocked); envelope encryption for BYO keys; audit log.
- **Observability:** one OTel trace per research run, one span per agent/tool call; JSON logs with redaction;
  Prometheus metrics; status page fed by provider health and pipeline SLOs.
- **Cost control:** hard per-run budgets; prompt caching for static system prompts; Batch API for non-urgent house
  reports; `scripts/cost_model.py` feeds the pricing guardrail (≥70% gross margin at p90 usage).

## 5. Risks

| # | Risk | Impact | Mitigation |
| --- | --- | --- | --- |
| R1 | Data licences forbid display/redistribution of vendor values in a paid product | Legal, product | Licence gate in renderer; derived-only mode; public-domain sources first; vendor contracts owner task |
| R2 | FRED terms prohibit use with generative AI | Legal | FRED off by default (D-010); primary-source macro |
| R3 | LLM states a number or direction not in the data | Trust | Bindings only; number linter; verifier; direction checks; evals at 100% thresholds |
| R4 | Prompt injection via news/filings | Integrity | Untrusted wrapping; tool-less readers; injection evals |
| R5 | Regulatory: personalised advice, Pakistan SECP RA regulations (2026 amendment) | Legal | Impersonal house research; personal-advice router; Pakistan-listed coverage excluded; legal opinions owner task |
| R6 | Cost per deep dive exceeds price headroom | Margin | Measured cost model, budgets, caching, batch, model tiering |
| R7 | Free/public APIs change schema or rate limits | Reliability | Cassette contract tests, schema-drift detection, nightly live smoke in CI, circuit breakers |
| R8 | Build sandbox cannot reach vendor hosts | Test fidelity | Record cassettes in CI (D-011); label schema-derived cassettes honestly |
| R9 | Merchant of record may not onboard a Pakistan-incorporated seller | Revenue | Two providers behind one interface; owner confirmation in writing |
| R10 | Stale data presented as current | Trust | Freshness policy enforced in code; stale label / block |
| R11 | Double-fired schedules or double charges | Money, trust | Advisory lock + unique keys; idempotency keys; webhook event-ID store |
| R12 | Tenant data leak | Security | Org-scoped repository + RLS + isolation tests |
| R13 | Scope: the full build is very large | Delivery | Phase gates with independent reviewers; nothing marked done without measured evidence |

## 6. Assumptions

1. Owner accepts working names and `example.com` until brand/legal are settled (D-001).
2. Hosted cloud provider not specified → Terraform targets a generic container platform; AWS chosen as the
   reference implementation at P11 unless a reason emerges otherwise.
3. US equities coverage uses SEC EDGAR for fundamentals; licensed real-time prices come only with a vendor contract.
   Until then, equity price display is limited to what a licensed adapter can provide in the deployment mode.
4. Economic consensus estimates are not licensed → surprise metrics use actual-vs-prior, labelled.
5. Anthropic API is the only LLM provider at launch; model IDs in `config/models.yaml`.
6. Email provider: Postmark/Resend/SES behind one interface; default chosen at P8.
7. Times are stored in UTC; display conversion happens only at the UI/render edge using the reader's timezone.
8. The build environment's network restrictions (docs/vendor-notes/README.md) do not apply in production or CI.
