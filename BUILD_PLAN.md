# BUILD_PLAN.md

How the build proceeds, restated from PROMPT §16–17. A phase starts only after the previous phase's independent
reviewer returns **PASS** (logged in `CRITIC_LEDGER.md`). "Done" always means measured.

## Working method

1. Small verified steps: after every meaningful change run `make check` (ruff, ruff format --check, mypy --strict,
   pytest for touched packages, tsc/eslint for web).
2. No stubs, fake data or `TODO`/`FIXME` in shipped code; anything that genuinely needs a human (contract, legal
   opinion, account) is built fully behind a feature flag with fixture-backed tests and listed in
   `STATUS.md → Blocked on humans`.
3. At each phase exit, spawn a fresh reviewer agent that sees only the repo, `PROMPT.md` and the exit criteria.
   Fix the top defect first; re-review until PASS.
4. Commit per logical step on branch `claude/amazing-maxwell-c7t5id`; push; one draft PR tracks the work.

## Phases

| Phase | Build | Exit criteria (measured) | Evidence location |
| --- | --- | --- | --- |
| P0 Foundations | uv workspace, package skeletons, settings/config loader, JSON logging with redaction, OTel tracing, FastAPI `/healthz` `/readyz` `/metrics`, worker + scheduler processes that connect and heartbeat, Next.js skeleton, docker-compose (Timescale + Valkey + api + worker + scheduler + web), Makefile, pre-commit, gitleaks, GitHub Actions CI, threat-model draft, planning docs, vendor notes | CI green on skeleton; `make dev` boots all services healthy; secret scan active and proven to catch a planted key | `docs/evidence/p0/` |
| P1 Data layer | Adapter protocol, canonical schemas, Redis token bucket, cache, retries/backoff/jitter, circuit breaker, QA checks, freshness, licence gate; adapters CFTC, EDGAR, FRED (flag off), EIA, one CCXT exchange, Twelve Data (flagged) | Cassette contract tests pass; QA checks catch seeded bad data; licence-gate test passes | `docs/evidence/p1/` |
| P2 Quant engine | Every metric in PROMPT §3.2 with `docs/metrics.md`, unit/property/golden tests; continuous futures; event study; technical engine incl. price-action module | ≥90% line+branch coverage; golden tests pass; metrics doc complete | coverage report |
| P3 Tools and ledger | Typed tools, immutable ledger, typed errors, binding resolver, number linter, no-network sandbox | Tools callable in isolation; immutability tested; sandbox no-network test | test output |
| P4 Agents and workflow | Prompts (versioned), LLMClient, state machine, budgets, resumability, parallel dispatch | R2 deep dive end-to-end on fixtures with mocked and real LLM; crash-resume test | run transcripts |
| P5 Verification and compliance | Verifier, compliance engine, revision loops, jurisdiction rules, eval suite (≥150 frozen scenarios + traps) | §15.3 thresholds met | eval results |
| P6 Reports and charts | Schemas, R1–R5 templates, all channel renderers, server-side charts, provenance hovers | Golden snapshots approved by reviewer; PDF renders; a11y checks pass | snapshots |
| P7 Web app, auth, billing | Marketing, app, admin, auth (D-008), entitlements, credits, Paddle sandbox, webhooks | E2E purchase→report green; tenant isolation green | Playwright report |
| P8 Scheduling, alerts, delivery | Leader scheduler, house reports, R3/R4 triggers, alert engine, channels | 7-day simulated calendar: every report once, on time, no duplicates | simulation log |
| P9 Scorecard | Prediction tracking, deterministic grading, public page, calibration | Grading tested; full denominator shown | tests |
| P10 Self-hosted | Licence sign/verify, wizard, BYO keys, signed updates, backups | Fresh-VM install <30 min by reviewer; tamper test fails closed | install log |
| P11 Hardening | Load, chaos, security scans, cost model, tuning | All §15 gates; margins ≥70% at p90 | reports |
| P12 Launch readiness | User docs, methodology, legal templates, status page, runbooks, onboarding emails, samples | Final independent PASS against §17 | RELEASE_REPORT.md |

## Known environment constraints for this build

- Vendor data hosts are blocked from the build sandbox (docs/vendor-notes/README.md). Cassettes are recorded in
  CI (D-011); anything not yet recorded from the live source is labelled `schema-derived`.
- Docker Hub pulls are rate-limited here; images come from `mirror.gcr.io` pinned by digest (D-009).
- Real-LLM runs (P4 exit, P5 live evals) require `ANTHROPIC_API_KEY` in the environment; if absent in the build
  sandbox, those specific checks are run in CI with a repository secret or listed as blocked.
