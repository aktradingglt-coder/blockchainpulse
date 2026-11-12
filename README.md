# Research Desk (working name)

Multi-asset, evidence-backed AI market research (FX, commodities, futures, US stocks, crypto). The product brief is
`PROMPT.md`; the plan is `ARCHITECTURE.md` and `BUILD_PLAN.md`; progress is in `STATUS.md`.

## Quick start

Requirements: Docker with Compose v2, `uv` 0.11, Node 22 with pnpm 10, GNU make.

```bash
make install      # Python + web dependencies from lockfiles
make dev          # build images, boot db/valkey/api/worker/scheduler/web, wait for health, run smoke checks
make check        # lint + types + unit tests
make test-integration   # integration tests against the running stack
make secrets-scan # gitleaks over history and working tree
make help         # everything else
```

Services (local only, bound to 127.0.0.1): web http://localhost:3000, API http://localhost:8000
(`/healthz`, `/readyz`, `/v1/status`, `/metrics`).

Secrets never go in files: `make dev` generates a git-ignored `.env` with random local credentials; see
`.env.example` for every variable name.
