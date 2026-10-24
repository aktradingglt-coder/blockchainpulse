# EIA Open Data API v2

Checked: 2026-10-21 via search (rOpenSci eia package docs, third-party mirror notes).

- Key: `api_key` **query parameter** (not a header) → env `EIA_API_KEY`. Must be scrubbed from logs/cassettes (query-string redaction is implemented in the HTTP layer).
- Routes: `petroleum/stoc/wstk` (weekly stocks), `natural-gas/stor/wkly` (weekly storage). Legacy v1 series IDs under `/v1/`.
- Max 5,000 rows per JSON response; paginate with `offset`/`length`.
- Throttle not published; third-party guidance ≈ <5 r/s burst. We use 2 r/s.
- v2 dropped the `updated` field → freshness uses the release calendar.
- Licence: US government data, public domain **[verify EIA copyright page]**; attribution "Source: U.S. Energy Information Administration".
