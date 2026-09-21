# Jet Tesfa V12 — Intelligence + Bilingual Release

V12 keeps the hardened V9 core and the benchmark-gated V10 intelligence layer, then adds a persistent Hebrew/English interface switch with correct RTL/LTR behavior and language-aware browser speech recognition/synthesis.

## Run
`python jet_v12.py`

Production: `gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 30 jet_v12:app`

## Safety / rollback
The V9 `jet_app.py` remains untouched. V10 intelligence upgrades remain feature-flagged and benchmark-gated. Set `JET_UPGRADE=0` to disable the intelligence layer.
