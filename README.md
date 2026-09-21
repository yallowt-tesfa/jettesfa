# Jet Tesfa v8 — AI Opportunity Engine + Private Control Center

JET is a need-first opportunity engine. It interprets a customer's request, discovers candidate offers through authorized connectors, normalizes and deduplicates product identity, evaluates customer fit, deal/price truth, seller trust, quality, timing and confidence, and returns BUY / WAIT / TRACK / ALTERNATIVE / AVOID guidance.

## Core modules
- Hebrew/English intent parser and budget extraction
- Anonymous Customer DNA personalization
- Product identity / GTIN dedupe
- Landed-cost calculation
- Price Truth + historical price memory
- Seller Trust + data confidence
- JET Score with affiliate-revenue guardrail
- Champion/Challenger Model Arena with minimum real-observation gate
- Outcome events: impression, outbound click, purchase, refund, satisfaction
- Demand discovery insights
- Price alerts and alert checking
- Source health / connector isolation
- eBay Browse API connector
- Awin product-feed connector
- Demo connector for zero-credential testing
- Rate limiting, request caps, session sanitization, security headers
- /health and /ready endpoints for Railway

## Run locally
Python 3.11+:

    python jet_app.py

Open http://127.0.0.1:8000

Production server:

    pip install -r requirements.txt
    gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 45 jet_app:app

## Railway
`railway.toml` and `Procfile` are included. Healthcheck path: `/ready`.
Railway injects `$PORT`; JET listens on it.

## Real connectors
Set credentials as Railway Variables, never in index.html or GitHub:
- EBAY_CLIENT_ID
- EBAY_CLIENT_SECRET
- EBAY_CAMPAIGN_ID
- EBAY_MARKETPLACE
- AWIN_FEED_URL

Keep JET_DEMO=1 until a real connector is confirmed. Then JET_DEMO=0 if desired.

## Persistence
The package defaults to SQLite for a self-contained runnable build. On Railway, attach a persistent Volume if retaining SQLite. For larger multi-instance production, migrate the repository layer to managed PostgreSQL before horizontal scaling.

## Validation
Run:

    PYTHONWARNINGS='error::ResourceWarning' python -m unittest -v test_v6.py
    python simulate_v6.py

See VALIDATION_REPORT.md.

## v7 customer experience
- Internal terms such as Customer DNA, affiliate scoring, model names and revenue logic are not exposed in the main customer journey.
- One need-first input: type or speak naturally.
- Turn-based conversational voice uses browser SpeechRecognition + SpeechSynthesis when supported; typed input remains the universal fallback.
- Voice defaults to Hebrew (`he-IL`).
- Affiliate disclosure remains only as a short footer disclosure for transparency.
- The backend intelligence and v6 validation suite remain unchanged; v7 is a customer-experience layer on top of the validated engine.


## Jet Tesfa v8 additions
- Customer-facing brand corrected to Jet Tesfa.
- Value-first positioning: opportunity fit rather than lowest-price-only messaging.
- Private `/admin` control center for funnel, satisfaction, anonymous DNA count, model arena and source health.
- Admin analytics APIs require `JET_ADMIN_TOKEN` via Bearer authorization.
- Core V6 ranking/search contracts are preserved for rollback safety.

## v9 hardening
See `HARDENING_REPORT_V9.md`. Production readiness now fails closed when demo/debug/default admin configuration is present. Connector search is parallelized and eBay OAuth tokens are cached. Live affiliate settlement still requires each network's production credentials and an external end-to-end conversion test.
