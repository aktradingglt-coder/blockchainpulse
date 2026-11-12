# Pricing comparables (PROMPT §2 rule 7)

Checked: 2026-10-21 via web search (aggregator and vendor-adjacent pages; vendor pricing pages themselves were not
reachable from the build sandbox). Every figure below is a **list price seen in a search result, unverified**;
the owner confirms pricing before launch (STATUS.md H6). Products that market trading signals are deliberately
not named here (PROMPT §2).

| Category | Example product (as reported) | Reported price | Source (search result) |
| --- | --- | --- | --- |
| Consumer AI equity research assistant | Fiscal.ai (formerly FinChat) Pro | $39/month in one listing, $64/month in another (conflicting) | yespress.io product page; nexusfi.com data-provider listing |
| Low-cost multi-asset data terminal | Eulerpool Pro / Max | $49 / $99 per month (self-reported, treat with caution) | eulerpool.com blog, 2026-08-08 |
| Professional charting/research software | Optuma Charts / Research | ~$150 / ~$300 per user per month, billed annually | liberatedstocktrader.com review |
| Professional research data platform | YCharts Standard / Professional | $300 / $500 per user per month | costbench.com |
| Crypto derivatives analytics | Laevitas Premium / Enterprise | $50 / $500 per month (listing alignment garbled) | quicknode.com builders guide |
| Institutional AI research | AlphaSense, Hebbia, others | quote-based; third-party estimates ~$3.3k–$17.5k per seat per year | allmind.ai 2026 pricing guide |
| Institutional terminal | Bloomberg Terminal | ~$2,300–$2,665 per user per month | costbench.com; eulerpool.com |

## Reading

- The proposed **Starter $49/month** sits at the upper end of the consumer AI-research price point.
- **Pro $179/month** sits between consumer tools ($39–$99) and professional research software ($150–$500 per user),
  justified by multi-asset coverage, crypto derivatives, alerts and Ask the Desk.
- **Desk $719/month for 5 seats** (~$144/seat) undercuts per-seat professional platforms.
- **Self-hosted $1,999 once + $599/year updates** has no direct public comparable; it is priced at roughly
  11 months of Pro, with no hosting or LLM cost for us because buyers bring their own keys.

These prices remain `pricing_status: proposed` until `scripts/cost_model.py` (P11) shows every tier at ≥70% gross
margin at p90 usage; if the measurement says otherwise, prices or credit allowances change, not the guardrail.
