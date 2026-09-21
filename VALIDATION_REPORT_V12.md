# Jet Tesfa V12 — Validation Report

Date: 2026-09-21

## Upgrade policy
- V9 hardened core preserved.
- V10 intelligence features retained only behind their benchmark gate and rollback flags.
- V12 adds the requested Hebrew/English UI layer and V12 production entrypoint.
- Amharic is intentionally not exposed in V12, per the final requirement.

## Automated regression suites
- V6 core: 40/40 PASS
- V8 upgrade: 5/5 PASS
- V9 hardening: 8/8 PASS
- V7 frontend compatibility: 9/9 PASS
- V10 intelligence: 78/78 PASS
- V12 bilingual/deployment: 10/10 PASS
- Total: 150/150 PASS (suites executed in isolated processes to avoid test-state contamination)

## Intelligence benchmark gate
- Intent exact accuracy: 63.4% -> 97.6%, PASS
- Relevance mean nDCG@3: 27.4 -> 83.4, PASS
- Cross-currency budget correctness: 83.3% -> 100.0%, PASS
- 3/3 benchmark-gated upgrades cleared their required margins.

## Additional validation
- Python compile check: PASS
- V12 direct WSGI smoke tests: /, /health, /ready, /api/config, /api/search all PASS
- Hebrew/English switch: PASS
- RTL/LTR switching: PASS
- Language preference persistence: PASS
- Voice recognition/synthesis locale follows selected language: PASS
- Railway/Procfile target jet_v12:app: PASS

This report records the automated checks performed in the supplied environment. It does not claim that no defect can ever exist under every external provider, browser, network, or production configuration.
