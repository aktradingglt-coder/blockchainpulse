# Research Desk (working name) — developer entry points. Run `make help`.
SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
COMPOSE := docker compose --project-directory . --env-file .env -f infra/compose/compose.yaml
UV := uv
WEB := pnpm --dir apps/web
GITLEAKS_VERSION := 8.30.1
GITLEAKS_SHA256 := 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
# Behind a TLS-intercepting proxy, set EXTRA_CA_BUNDLE to its CA file; it is staged (git-ignored) for image builds.
EXTRA_CA_BUNDLE ?= $(firstword $(wildcard /root/.ccr/ca-bundle.crt))

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

.PHONY: install
install: ## Install Python (uv) and web (pnpm) dependencies from lockfiles
	$(UV) sync --frozen --all-packages
	$(WEB) install --frozen-lockfile

.PHONY: tools
tools: .tools/bin/gitleaks ## Install pinned, checksum-verified CLI tools into .tools/

.tools/bin/gitleaks:
	mkdir -p .tools/bin .tools/tmp
	curl -fsSL -o .tools/tmp/gitleaks.tgz https://github.com/gitleaks/gitleaks/releases/download/v$(GITLEAKS_VERSION)/gitleaks_$(GITLEAKS_VERSION)_linux_x64.tar.gz
	echo "$(GITLEAKS_SHA256)  .tools/tmp/gitleaks.tgz" | sha256sum -c -
	tar -xzf .tools/tmp/gitleaks.tgz -C .tools/bin gitleaks
	rm -rf .tools/tmp

.env:
	./scripts/dev_env.sh

.tools/build_ca.crt: FORCE
	mkdir -p .tools
	if [ -n "$(EXTRA_CA_BUNDLE)" ]; then cp "$(EXTRA_CA_BUNDLE)" $@; else : > $@; fi

.PHONY: FORCE
FORCE:

.PHONY: dev
dev: .env .tools/build_ca.crt ## Build and boot every service; waits until all are healthy, then runs the smoke check
	$(COMPOSE) up --build --detach --wait --wait-timeout 240
	$(MAKE) smoke

.PHONY: smoke
smoke: .env ## End-to-end probe of the running stack (API ready, web up, worker pipeline, scheduler leader)
	./scripts/smoke.sh

.PHONY: down
down: .env ## Stop the stack (keeps volumes)
	$(COMPOSE) down

.PHONY: clean-dev
clean-dev: .env ## Stop the stack and delete its volumes
	$(COMPOSE) down --volumes

.PHONY: ps
ps: .env ## Show service health
	$(COMPOSE) ps

.PHONY: logs
logs: .env ## Tail logs
	$(COMPOSE) logs -f --tail=100

.PHONY: lint
lint: ## ruff (lint + format check), eslint
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run lint-imports
	$(WEB) lint

.PHONY: typecheck
typecheck: ## mypy --strict, tsc --noEmit
	$(UV) run mypy packages apps/api apps/worker apps/scheduler tests scripts
	$(WEB) typecheck

.PHONY: test
test: tools ## Unit tests (no services needed)
	$(UV) run pytest -m "not integration"
	$(WEB) test

.PHONY: test-integration
test-integration: .env tools ## Integration tests against the dev stack's Postgres/Valkey (run `make dev` first)
	set -a; source .env; set +a; $(UV) run pytest -m integration

.PHONY: secrets-scan
secrets-scan: tools ## gitleaks over the full git history and over every committable file (ignored files excluded)
	.tools/bin/gitleaks git --config .gitleaks.toml --redact --no-banner .
	./scripts/scan_committable.sh

.PHONY: audit
audit: ## Dependency vulnerability audit (pip-audit, pnpm audit)
	$(UV) export --frozen --all-packages --no-dev --no-hashes --no-emit-workspace -o .tools/requirements-audit.txt
	$(UV) run pip-audit --strict -r .tools/requirements-audit.txt
	$(WEB) audit --prod --audit-level high

.PHONY: licences
licences: ## Dependency licence gate (allow-list + reviewed exceptions)
	$(UV) run python scripts/check_licences.py

.PHONY: coverage
coverage: .env tools ## Unit + integration tests with the coverage gate (needs the running stack)
	rm -f .coverage .coverage.*
	set -a; source .env; set +a; $(UV) run pytest --cov --cov-report=term-missing --cov-report=xml --cov-report=json
	$(UV) run python scripts/check_coverage.py

.PHONY: check
check: lint typecheck test ## Everything a contributor runs before pushing

.PHONY: migrate
migrate: .env ## Apply migrations to the dev database from the host
	set -a; source .env; set +a; $(UV) run python -m rd_db.migrate
