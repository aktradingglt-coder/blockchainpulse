# Owner Launch Checklist (living document; final version is a P12 deliverable)

Everything here needs a human: an account, a contract, a signature, a legal opinion or a brand decision. The build
cannot complete these and never pretends to. Each line links to where the product reads or records the result.

## Identity and brand
- [ ] Choose the product name, legal entity, domain, support email and logo; replace the working values in
      `config/product.yaml → identity` (D-001). Confirm none belongs to an existing company or trading-signals brand.
- [ ] Brand review of the design tokens and fonts (`config/design_tokens.yaml`, D-019).

## Legal
- [ ] Written legal opinion per sales country, filed in `docs/legal/` (PROMPT §13).
- [ ] Pakistan: SECP Research Analyst Regulations (amended Jan 2026) — registration question; until cleared, `PK`
      sales stay `not_yet_cleared` and Pakistan-listed securities stay excluded (D-020, D-021).
- [ ] Sanctions / jurisdiction list review (`config/jurisdiction_rules.yaml`, D-021).
- [ ] FRED terms (generative-AI clause) — decide whether the FRED adapter may ever be enabled (D-010).
- [ ] Terms, privacy policy, disclaimer, refund policy, data attributions (templates arrive in P12).
- [ ] Insurance (professional liability / E&O) as advised by counsel.

## Billing
- [ ] Confirm in writing that Paddle onboards a Pakistan-incorporated company and pays out to its bank; otherwise
      Lemon Squeezy (`docs/vendor-notes/billing.md`, D-013).
- [ ] Confirm prices (`config/product.yaml → commercial`, D-023) after the P11 measured cost model.

## Brand
- [ ] Confirm the repository name `blockchainpulse` is not a third-party trading-signals brand (it appears in local
      paths, CI links and the `source` field of cassettes recorded before 2026-10-21 run 2; new recordings cite only
      the CI run ID). Rename the repository if it is.

## Data vendors
- [ ] Contracts for any commercial data vendor and display/redistribution rights per crypto exchange
      (`docs/vendor-notes/commercial-data.md`, `crypto-exchanges.md`, D-024).
- [ ] Add an `EIA_API_KEY` GitHub Actions secret (free key from eia.gov) and run the `record-cassettes` workflow so
      EIA contract tests replay real responses (D-011); same for Twelve Data / FRED keys once cleared.

## Infrastructure and operations
- [ ] Domains and DNS; email authentication (SPF, DKIM, DMARC); transactional email provider account.
- [ ] Hosted cloud account, secret manager, licence-signing private key generated inside it (never exported).
- [ ] Status page, support inbox, on-call rota for the first week.
- [ ] Self-hosted edition: decide whether `APP_ENV=production` installs on a LAN may serve plain-http origins
      (currently refused; revisit in P10).

## First week after launch
- [ ] Manual spot checks of published reports each day; review the track record and cost dashboards.
