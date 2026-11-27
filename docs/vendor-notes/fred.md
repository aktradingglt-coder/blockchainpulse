# FRED (Federal Reserve Bank of St. Louis)

Checked: 2026-10-21 via search snippets of fred.stlouisfed.org/legal and /docs/api/terms_of_use.html.

- API key required (`FRED_API_KEY`), `https://api.stlouisfed.org/fred/series/observations`.
- Many series are **third-party copyrighted**; notes contain "Copyright". Use beyond personal use requires the owner's permission. The adapter reads series notes and marks any series with a copyright notice `display_allowed=false` until cleared.
- Attribution: cite the source, note "accessed via FRED", keep copyright notices.

## Legal risk — escalated

The FRED legal page (search snippet) **prohibits using FRED services or content to develop or train software, including machine learning, large language models and generative AI**, including non-commercial use. Feeding FRED values into an LLM-written research product may fall inside that clause.

Consequence (D-010): the FRED adapter is built and tested but **off by default** (`feature_flags.provider_fred=false`). US macro/yield series are sourced first from primary publishers (US Treasury, BLS, BEA — US government works) where possible. Listed under STATUS.md → Blocked on humans: legal opinion on FRED terms.
