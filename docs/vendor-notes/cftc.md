# CFTC Commitments of Traders (public reporting, Socrata)

Checked: 2026-10-21. Web search for the report families and schedule (the build sandbox cannot reach the host);
dataset IDs and field names verified from **real responses recorded by CI** (`record-cassettes` workflow, D-011).

- Host: `https://publicreporting.cftc.gov` (Socrata Open Data API, keyless; SODA query params `$where`, `$order`,
  `$limit`, `$offset`). Unauthenticated requests are throttled; the adapter stays at 2 requests/second.
- Report families: Legacy, Disaggregated and Traders in Financial Futures (TFF), each futures-only or futures-and-
  options combined; plus Supplemental (not used).
- TFF categories: Dealer/Intermediary, Asset Manager/Institutional, Leveraged Funds, Other Reportables. TFF is
  **not** a disaggregation of the legacy report.
- Release schedule: normally Friday 15:30 ET, positions as of the preceding Tuesday; holiday weeks release later.
  Freshness is measured from the scheduled release (D-027).

## Dataset IDs

The IDs are constants in `packages/data/src/rd_data/adapters/cftc.py` (`DATASETS`).

| Report | Dataset ID | Verified |
| --- | --- | --- |
| Legacy futures-only | `6dca-aqww` | recorded by CI runs 39568643206 and 39574074933 (S&P 500, 60 weeks, long side = open interest) |
| Disaggregated futures-only | `72hh-3qpy` | recorded by CI runs 39568643206 and 39574074933 (WTI crude) |
| TFF futures-only | `gpe5-46if` | recorded by CI runs 39568643206 and 39574074933 (EUR FX) |
| Legacy combined | `jun7-fc8e` | recorded by CI run 39574074933 (S&P 500, 60 weeks, QA passed) |
| Disaggregated combined | `kh3c-gbw2` | recorded by CI run 39574074933 (WTI crude, 60 weeks, QA passed) |
| TFF combined | `yw9f-hn96` | recorded by CI run 39574074933 (EUR FX, 60 weeks, QA passed) |

`test_every_cftc_dataset_id_is_verified_by_a_real_recording` fails if an ID in `DATASETS` has no recorded 200
response.

Field-name quirks seen in the recorded responses (handled in `FIELDS`): `noncomm_postions_spread_all` (sic),
`swap__positions_short_all` (double underscore), and category fields without the `_all` suffix in the
disaggregated and TFF sets.

Drift: the nightly `live-smoke` workflow re-fetches the recording plan and compares response shapes with the
stored cassettes (`python -m rd_data.recording --check-drift`).

Licence: US federal government work; public domain. Attribution: "Source: U.S. Commodity Futures Trading
Commission, Commitments of Traders".
