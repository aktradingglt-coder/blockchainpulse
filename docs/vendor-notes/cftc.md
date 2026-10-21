# CFTC Commitments of Traders (public reporting, Socrata)

Checked: 2026-10-21 via web search (direct access blocked from build sandbox).

- Host: `https://publicreporting.cftc.gov` (Socrata Open Data API, keyless; SODA query params `$where`, `$order`, `$limit`, `$offset`).
- Report families (confirmed by search): Legacy futures-only, Legacy F&O combined, Disaggregated futures-only, Disaggregated F&O combined, TFF futures-only, TFF F&O combined, Supplemental.
- TFF categories: Dealer/Intermediary, Asset Manager/Institutional, Leveraged Funds, Other Reportables. TFF is **not** a disaggregation of the legacy report.
- Release schedule: normally Friday 15:30 ET with data as of the preceding Tuesday **[verify; holiday shifts exist]**.

## Dataset IDs — [unverified]

From builder memory, to be confirmed by the live smoke test (`make smoke-live`, runs in CI with network):

| Report | Dataset ID |
| --- | --- |
| Legacy futures-only | `6dca-aqww` |
| Legacy combined | `jun7-fc8e` |
| Disaggregated futures-only | `72hh-3qpy` |
| Disaggregated combined | `kh3c-gbw2` |
| TFF futures-only | `gpe5-46if` |
| TFF combined | `yw9f-hn96` |

IDs live in `config/providers.yaml`, never in code.

Licence: US federal government work; public. Attribution text: "Source: U.S. Commodity Futures Trading Commission".
