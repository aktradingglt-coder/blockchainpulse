# SEC EDGAR

Checked: 2026-10-21 (sec.gov "Accessing EDGAR Data" via search snippet; direct fetch blocked).

- Endpoints: `https://data.sec.gov/submissions/CIK##########.json`, `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`, `.../companyconcept/...`, `.../frames/...`; nightly bulk `submissions.zip` / `companyfacts.zip`; full-index files.
- Fair access: **max 10 requests/second** (SEC may lower it); we target ≤ 8 r/s shared across workers.
- **User-Agent must declare company name + admin contact email** → env `SEC_EDGAR_USER_AGENT`.
- Excess traffic yields 429/403 and temporary IP blocks (third-party report).
- Submissions JSON holds recent filings; older ones are in referenced extra files **[verify count]**.
- Licence: public US government data; attribution "Source: SEC EDGAR".
- Point-in-time rule: fundamentals are keyed by `filed` date, not period end (PROMPT §3.2).
