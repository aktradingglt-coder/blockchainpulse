# CLAUDE.md

Guidance for Claude (and any AI assistant) working in this repository.

## How to use this file

This project is being built quickly with AI help ("vibe coding"). That is fine for a prototype, but
**before anything touches a paying customer, it has to meet the production-readiness checklist below.**

When you write or change code here:

1. **Check the change against the checklist.** If a feature touches auth, data, payments, uploads,
   email, APIs, AI calls or infrastructure, find the matching section and build to it — don't
   improvise the shortcut version.
2. **Say what's missing.** If a change can't meet a relevant item yet, say so plainly in your reply
   and in the PR description (e.g. "no rate limit on this login endpoint yet"). Never imply
   something is production-ready when it isn't.
3. **Never take shortcuts on the non-negotiables:** secrets in code, client-only validation, money as
   floats, string-built SQL, "allow everything" CORS, unchecked object IDs in URLs, or destructive
   actions with no confirmation or undo.
4. **Don't claim you tested something you didn't.** Report what was actually run and what still
   needs a human to check.
5. **Keep it readable.** No 2,000-line files, no dead code from earlier attempts, no packages you
   haven't confirmed exist and are the one intended.

---

## Production-readiness checklist

### Data model and database
- [ ] A schema someone designed, instead of one the AI improvised table by table
- [ ] A data model that survives your second customer asking for "one small change"
- [ ] Database migrations that run forward and roll back
- [ ] Migrations that don't lock the main table at 2pm on a Tuesday
- [ ] Indexes, because the query that was instant on 50 test rows times out on 5 million real ones
- [ ] Connection pooling, so a traffic spike doesn't exhaust the database
- [ ] Backups every night
- [ ] A restore from those backups, tested, because a backup you've never restored is a hope
- [ ] Point-in-time recovery for the day someone runs a delete without a where clause
- [ ] Soft deletes, so "I deleted it by accident" is a support ticket and not an incident
- [ ] Pagination that still works on page 400
- [ ] Caching, and clearing that cache when the data changes

### Authentication and accounts
- [ ] Login that still works when someone puts a plus sign in their email
- [ ] Password reset links that expire and work once
- [ ] MFA, plus a recovery path for the user who lost their phone
- [ ] Sessions that time out, and a way to kill every session for one user
- [ ] SSO for the enterprise customer who won't sign without it
- [ ] SAML, because that customer's IT team doesn't do magic links
- [ ] SCIM, so a fired employee loses access the moment IT offboards them
- [ ] Rate limits on login, so nobody can guess passwords at scale
- [ ] Lockouts that stop attackers without locking out your CFO the night before the board meeting

### Authorization and multi-tenancy
- [ ] Roles and permissions deeper than "admin" and "everyone else"
- [ ] Checks that stop user A from reading user B's data by changing one number in the URL
- [ ] Tenant isolation, so one customer's bug never shows another customer's records
- [ ] An admin "log in as this user" button for support, with an audit trail on every use
- [ ] Least-privilege access for everyone, including the AI agent holding production credentials
- [ ] Quarterly access reviews someone signs off on

### Input, data correctness and internationalization
- [ ] Validation on the server, not just in the form
- [ ] Timezones stored right, so the 9am reminder doesn't fire at 4am
- [ ] The customer with an apostrophe in their last name
- [ ] Emoji, accents and right-to-left text in every field
- [ ] Money stored as integers, because 0.1 + 0.2 doesn't equal 0.3 in floating point
- [ ] Currency, rounding and tax rules for every country you sell into
- [ ] Idempotency keys, so a retried payment doesn't charge the card twice
- [ ] Deduplication for the user who clicks submit four times
- [ ] Exports, because sooner or later every customer wants their data in a spreadsheet
- [ ] Imports that survive the CSV someone saved in 2009
- [ ] Translations, with dates and numbers formatted for each locale

### Security
- [ ] Secrets out of the code, out of the repo and out of the AI chat history
- [ ] Rotating every key the AI pasted into a config file last month
- [ ] Confirming every package the AI installed exists and is the one you meant
- [ ] Dependency scanning, because the AI pulled in libraries nobody reviewed
- [ ] Input sanitization everywhere a user can type
- [ ] Protection against SQL injection, XSS, CSRF and the rest of the OWASP list
- [ ] File uploads that get scanned, size-limited and stored where they can't execute
- [ ] CORS rules that aren't "allow everything"
- [ ] Security headers on every response
- [ ] Encryption at rest and in transit
- [ ] Logs that record who did what, without recording passwords, tokens or card numbers
- [ ] A penetration test before your enterprise customer's security team runs their own
- [ ] Answers to the security questionnaire every enterprise buyer sends, with evidence behind each one
- [ ] A disclosure process for the day a stranger emails you a vulnerability
- [ ] A plan for the day a laptop with production access gets stolen

### Compliance and privacy
- [ ] SOC 2, which means policies, evidence and an auditor, not a checkbox
- [ ] HIPAA, the moment a single patient record touches your system
- [ ] A BAA with every vendor that touches that data, including your AI model provider
- [ ] Audit trails that show who viewed a record, not just who changed it
- [ ] PCI scope, and card numbers kept out of your database
- [ ] GDPR deletion requests that reach every table, backup and log
- [ ] Data residency for the customer whose contract says EU only
- [ ] Retention rules, and code that deletes what your contract says you'll delete
- [ ] Terms and a privacy policy that match what the code does

### Infrastructure and deployment
- [ ] Dev, staging and production environments that match each other
- [ ] Infrastructure written as code, so the setup lives somewhere other than one person's memory
- [ ] A deploy pipeline that runs the tests before anything reaches customers
- [ ] One-click rollbacks, because Friday deploys happen
- [ ] Zero-downtime deploys, because your customers don't have maintenance windows
- [ ] Feature flags, so you can ship the code without shipping the feature
- [ ] Autoscaling for the Monday morning spike
- [ ] A CDN, so the app loads fast in Singapore too
- [ ] DNS, SSL certificates, and renewals that don't expire on a Sunday
- [ ] Storage rules, so you stop paying to keep 2021's temp files
- [ ] Cost alerts, so a runaway agent loop shows up the same day and not on the invoice (we learned this one for $1,382.59)
- [ ] A disaster recovery plan, written down and tested once a year
- [ ] A second region, if your contract promises uptime one region can't deliver

### Background work and email
- [ ] Background job queues, so slow work doesn't freeze the page
- [ ] Retries with backoff, so one flaky API call doesn't become a lost order
- [ ] A dead-letter queue for the jobs that fail every retry
- [ ] Cron jobs that don't run twice the day you add a second server
- [ ] SPF, DKIM and DMARC, so your password resets don't land in spam
- [ ] Bounce and complaint handling on every transactional email

### Observability and operations
- [ ] Structured logs you can search at 3am
- [ ] Error tracking that groups the same bug instead of sending you 4,000 emails
- [ ] Uptime checks from outside your own network
- [ ] Alerts that page a human, tuned until they stop paging for nothing
- [ ] Dashboards for latency, error rate and throughput
- [ ] Tracing for one slow request across every service it touches
- [ ] An on-call rotation, and a person who picks up
- [ ] Runbooks, so the engineer on call knows what "queue depth critical" means
- [ ] Postmortems that change something
- [ ] A status page customers check before they email you

### Testing
- [ ] Unit tests written by someone who knows what the logic should do
- [ ] Integration tests for every third-party API you depend on
- [ ] End-to-end tests for signup, checkout and the other flows that make you money
- [ ] Load testing before the launch email goes out
- [ ] Test data that looks like production without being production data
- [ ] Regression tests, so fixing bug 41 doesn't bring back bug 12
- [ ] Testing on the five-year-old laptop your biggest customer's ops team still uses
- [ ] Accessibility compliance, because the enterprise contract requires it

### Billing and payments
- [ ] Subscriptions, upgrades, downgrades and proration
- [ ] Failed-payment retries and dunning emails
- [ ] Invoices with the customer's PO number on them, because procurement won't pay without it
- [ ] Sales tax and VAT
- [ ] Refunds, credits and partial refunds
- [ ] Usage metering that matches what you bill, to the cent
- [ ] Annual contracts with custom terms your billing logic never planned for
- [ ] Revenue reports your finance team can reconcile

### APIs and integrations
- [ ] Webhooks that verify signatures
- [ ] Webhooks that retry, with a log of every failed delivery
- [ ] A versioned API, so you can change it without breaking your customers' integrations
- [ ] API keys customers can create and revoke
- [ ] API rate limits that protect you without blocking real integrations
- [ ] A plan for the day a third-party API changes its response format without telling you
- [ ] OAuth token refresh, because tokens expire at the worst possible moment
- [ ] A sandbox where customers can test their integration

### AI features
- [ ] Protection against prompt injection in every field that reaches the model
- [ ] Customer and patient data kept out of prompts that leave your network
- [ ] A model spend cap per customer, so one heavy user can't run up your bill
- [ ] Evals, so you find out when a model update makes your answers worse
- [ ] A fallback for the afternoon your model provider goes down
- [ ] A migration plan for the day your model gets deprecated
- [ ] Prompt and output logs for debugging, minus whatever you're not allowed to keep
- [ ] A human approval step for anything the AI does that can't be undone

### User experience
- [ ] Pages that load on hotel wifi
- [ ] Empty states, loading states and error states on every screen
- [ ] Error messages a customer can act on, instead of "something went wrong"
- [ ] Forms that keep what people typed when one field fails validation
- [ ] Emails that render in every mail client your customers use
- [ ] Notifications people can turn off
- [ ] Undo
- [ ] Keyboard navigation and screen reader support
- [ ] A back button that does what people expect

### Code health and process
- [ ] Code a human can read
- [ ] Code review on every change
- [ ] A branching strategy, so two people can work without overwriting each other
- [ ] Monthly dependency upgrades, so you're never two major versions behind
- [ ] Framework upgrades before the old version stops getting security patches
- [ ] Deleting the dead code the AI left behind from its first three attempts
- [ ] Splitting up the 2,000-line file the AI kept appending to
- [ ] Architecture decisions written down, with the reasons
- [ ] A README that gets a new engineer running locally in under an hour
- [ ] Documentation for whoever inherits this

### Business operations and ownership
- [ ] An admin panel for your ops team
- [ ] Support tooling, so customer success can answer questions without pinging engineering
- [ ] A changelog customers can read
- [ ] Every account, domain and repo in the company's name, not the name of whoever built the prototype
- [ ] A bus factor higher than one

---

## The people this still needs

Besides keeping all of the above working every day, forever, this app still needs people who know
what they're doing. AI output does not replace these roles — flag it when a change needs one of them:

| Responsibility | Who |
| --- | --- |
| Reading every line the AI wrote before it ships | Senior engineer |
| Designing the data model before your second customer breaks it | Backend engineer |
| Explaining why the AI's fix for one bug created two new ones | Senior engineer |
| Deploys, rollbacks, servers and the cloud bill | DevOps engineer |
| Answering the page at 3am when payments stop | Whoever's on call |
| Patching the framework the week a vulnerability goes public | Security |
| Walking an auditor through how patient data moves | Security and compliance |
| Testing what the AI says it tested | QA |
| Running a migration on a live database without losing a row | Backend engineer |
| Saying no to the feature that breaks three others | Tech lead |
| Turning "works on my laptop" into "works for every customer at once" | Architect |
| Debugging the code when the AI that wrote it can't | Senior engineer |
