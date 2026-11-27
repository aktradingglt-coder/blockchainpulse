# Billing — Paddle (default) and Lemon Squeezy (fallback)

Checked: 2026-10-21 via search; vendor sites blocked from the sandbox.

## Paddle Billing
- Webhooks: header `Paddle-Signature: ts=<unix>;h1=<hex>`; signed payload is `ts + ":" + raw_body`, HMAC-SHA256 with the per-destination secret; reject old timestamps (SDK default tolerance 5 s; we allow configurable tolerance, default 300 s to tolerate retries, plus event-ID idempotency).
- Sandbox API base `https://sandbox-api.paddle.com` **[verify]**; production `https://api.paddle.com`.
- Merchant of record: calculates, collects and remits tax.
- Sanctioned/restricted countries blocked at signup and checkout (Paddle partner docs).
- `/countries` style endpoint exposes `allow_sales` per country **[verify]**.
- Paddle has a Pakistan-targeted marketing page; **official confirmation that a Pakistan-incorporated company can onboard and receive payouts to a Pakistani bank was NOT found**. Third-party sources claim bank/PayPal/Payoneer payouts. → Owner task: confirm in writing.

## Lemon Squeezy
- Sellers must be payable to a bank or PayPal account in a supported country; full list not readable from sandbox; Pakistan **[unverified]** → owner task.

Both sit behind `packages/billing` `BillingProvider`.
