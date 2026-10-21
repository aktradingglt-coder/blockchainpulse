# Threat model (draft, P0)

Method: STRIDE per component. Target: OWASP ASVS level 2. Status column says whether the control **exists now**
(with the test that proves it), or which phase delivers it. This draft is revisited at every phase exit and in P11.

## Assets

1. Customer accounts, org membership, entitlements, credits, billing state.
2. Secrets: Anthropic key, data-vendor keys, billing webhook secret, licence-signing private key, BYO customer keys.
3. Licensed market data (contractual redistribution limits) and the evidence ledger.
4. Published research and the track record (integrity is the product).
5. Self-hosted licence tokens and signed update manifests.

## Trust boundaries

Browser ↔ web/API (internet) · API ↔ billing provider webhooks (internet) · worker ↔ data vendors and Anthropic
(internet egress) · worker ↔ untrusted text (news/filings) · worker ↔ sandbox container · services ↔ Postgres/Valkey
(private network) · self-hosted install ↔ update server · admin console ↔ everything.

## STRIDE by component

| Component | Threat (STRIDE) | Control | Status |
| --- | --- | --- | --- |
| Web app | **S** session hijack, credential stuffing | Maintained auth library, MFA for admins, rate limits per IP/user, secure cookies | P7 |
| Web app | **T** XSS injecting into reports | React escaping, strict CSP (`script-src 'self'`), report HTML rendered from schema, never raw LLM HTML | CSP header now (next.config.ts); renderer P6 |
| Web app | **I** clickjacking | `frame-ancestors 'none'`, `X-Frame-Options: DENY` | Done (P0) |
| API | **S** forged tokens | JWT via JWKS verification, short expiry | P7 |
| API | **T** CSRF on cookie-auth routes | SameSite cookies + CSRF token for state-changing routes | P7 |
| API | **R** disputed credit spend / admin action | Append-only audit log for admin, billing, entitlement, licence events | P7 |
| API | **I** data leak across tenants | Org-scoped repository layer + Postgres RLS + isolation tests | P7 |
| API | **I** stack traces / secrets in responses or logs | Generic error bodies; structlog redaction processor | Redaction done: `packages/core/tests/test_redaction.py`, `test_logging_tracing.py` |
| API | **D** request floods, credit-burning abuse | Rate limits per user/IP/token, abuse detection, budgets per run | P7/P11 |
| API | **E** privilege escalation via admin routes | Role checks + MFA-required admin scope, authz matrix tests | P7 |
| API | **I** browser cross-origin reads | Strict CORS allow-list from settings | Done: `test_cors_allows_only_configured_origins` |
| Billing webhooks | **S/T** forged or replayed events | HMAC signature verification over raw body, timestamp tolerance, event-ID idempotency store | P7 |
| Worker / agents | **T** prompt injection via news/filings | Untrusted wrapping; tool-less readers; injection evals at 100% | P3–P5 |
| Worker / agents | **I** model writes invented numbers | Numeric binding, number linter, verifier, direction checks | P3–P5 |
| Worker / agents | **D** runaway cost/loops | Hard per-run budgets (tokens, tool calls, wall clock, USD), global kill switch | P4 |
| Worker → vendors | **S** SSRF via user-supplied URLs (webhooks, feeds) | Host allow-list, private ranges blocked, DNS-rebinding-safe resolver | P8 (first user-supplied URL) |
| Sandbox | **E/I** code execution escapes, exfiltration | Separate container, no network, read-only inputs, CPU/mem/time limits; test proves no network | P3 |
| Queue (Valkey) | **T/I** unauthenticated access | `requirepass`, private network, ports bound to 127.0.0.1 in dev | Done (compose) |
| Postgres | **I** credential leakage | Random per-install credentials (`scripts/dev_env.sh`), env only, never committed | Done; gitleaks test `test_committable_tree_is_clean` |
| Postgres | **T** evidence tampering | Insert-only ledger (trigger), SHA-256 hashes | P3 |
| Scheduler | **T** double-fired jobs | Advisory-lock leader + unique job keys | Leader lock done: `tests/integration/test_leader_lock.py`, `test_processes.py`; keys P8 |
| Secrets in repo | **I** committed keys | gitleaks in pre-commit and CI (full history); planted-key test | Done: `tests/test_secret_scanning.py`, CI `secrets` job |
| Supply chain | **T** malicious dependency/image/action | Exact pins + lockfiles, images by digest, actions by commit SHA, pip-audit/pnpm audit, SBOM + image scan (P11) | Pins + audit done (`tests/test_repo_hygiene.py`, CI `audit`); SBOM/scan P11 |
| Self-hosted licence | **S/T** forged or tampered licence | Ed25519 signatures, public key compiled in, fails closed | P10 |
| Self-hosted updates | **T** malicious update | Signed release manifests verified before apply | P10 |
| BYO keys | **I** leakage of customer vendor keys | Envelope encryption at rest, never logged, redaction | P7/P10 |
| Observability | **I** secrets in traces/Sentry | Sentry `before_send` redaction, `send_default_pii=False`; spans carry IDs not payloads | Done (P0 config) |

## Open questions for P11 review

- Hosted cloud provider and its managed-secret service (assumption: AWS).
- Whether customer API tokens need IP allow-lists for Desk tier.
- Retention period for LLM transcripts vs reproducibility requirement (§6.5) and privacy (§13.3).
