# Jet Tesfa v9 — Hardened Validation Report

## Scope
Deep local audit of the supplied v8 package, preserving the legacy search/ranking/DNA flow while hardening deployment behavior.

## Changes
- Version normalized to 9.0.0 across runtime and regression tests.
- Connector searches execute concurrently (bounded thread pool) to reduce aggregate latency.
- eBay OAuth application token is cached until shortly before expiry.
- Production readiness gate rejects weak/default admin token, debug mode, demo mode, or zero live sources.
- Alert-check endpoint now requires admin authentication.
- Public event ingestion now uses an explicit allow-list to reduce analytics/model poisoning by arbitrary event names.
- Runtime SQLite database removed from the distributable; it is created on first run.
- Existing customer-value-first commission guard retained (commission weight <= 3%).

## Executed validation
- Python compile: PASS
- Legacy/core suite: 40/40 PASS
- Frontend suite: 9/9 PASS
- v8 admin/brand suite: 5/5 PASS
- v9 hardening suite: 8/8 PASS
- Total automated tests: 62/62 PASS
- Simulation: 20,000/20,000 scenarios kept customer value ahead of a higher-commission alternative; all scores remained in range.

## Live integration status
- eBay: production-capable adapter exists; requires real EBAY_CLIENT_ID / EBAY_CLIENT_SECRET. Affiliate attribution additionally requires EBAY_CAMPAIGN_ID.
- Awin: feed adapter exists; requires a real AWIN_FEED_URL.
- AliExpress: NOT marked live. No AliExpress production credentials/approved API endpoint were supplied, so no fabricated connector was added. Add only against the approved affiliate API/feed for the account.

## Production gate
Set JET_ENV=production, JET_DEMO=0, JET_DEBUG=0, a random JET_ADMIN_TOKEN of at least 32 characters, and at least one live source credential/feed. `/ready` will fail closed otherwise.

## Remaining external validation before commercial launch
A true end-to-end purchase/commission test cannot be proven offline. It requires production credentials and a real click -> merchant -> conversion/reporting cycle for each affiliate network. This package therefore passes local code/integration simulation, not merchant-side commission settlement certification.
