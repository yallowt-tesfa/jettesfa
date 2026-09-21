"""
jet_v12 — production entrypoint for Jet Tesfa v12.

Run this instead of jet_app to get the upgraded engine:

    python jet_v12.py
    gunicorn --bind 0.0.0.0:$PORT jet_v12:app

It imports the untouched v9 module, installs the benchmark-approved upgrades,
and adds a small number of read-only endpoints. `jet_app.py` is byte-identical
to the file you supplied; deleting the jet_intel package and pointing the
Procfile back at `jet_app:app` returns the service to v9 exactly.

Added endpoints (all admin-authenticated except /api/config):

    GET /api/config        adds the upgrade status to the existing payload
    GET /api/upgrade       which upgrades are installed, and the gate results
    GET /api/benchmark     re-runs the gate live and returns the numbers
    GET /api/intelligence  LLM, cache, breaker and FX diagnostics
"""
from __future__ import annotations

import json
import os

import jet_app
from jet_intel import bench, install as installer

VERSION = "12.0.0"

# Install at import time so gunicorn workers pick the upgrades up.
installer.install(jet_app)

_legacy_app = jet_app.app


def _json(start, status: int, obj) -> list[bytes]:
    body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    start(
        f"{status} " + ("OK" if status < 400 else "Error"),
        [("Content-Type", "application/json; charset=utf-8"),
         ("Cache-Control", "no-store"),
         *jet_app.security_headers()],
    )
    return [body]


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")

    if path == "/api/upgrade":
        if not jet_app.admin_authorized(environ):
            return _json(start_response, 401, {"error": "admin_auth_required"})
        return _json(start_response, 200, installer.status())

    if path == "/api/benchmark":
        if not jet_app.admin_authorized(environ):
            return _json(start_response, 401, {"error": "admin_auth_required"})
        return _json(start_response, 200, bench.gate(jet_app))

    if path == "/api/intelligence":
        if not jet_app.admin_authorized(environ):
            return _json(start_response, 401, {"error": "admin_auth_required"})
        from jet_intel import fx, llm
        from jet_intel.resilience import BREAKERS, CONNECTOR_CACHE, SEARCH_CACHE
        return _json(start_response, 200, {
            "version": VERSION,
            "llm": llm.stats(),
            "fx": {"base": fx.BASE, "source": fx.rate_source(), "rates": fx.rates()},
            "caches": {"search": SEARCH_CACHE.stats(),
                       "connector": CONNECTOR_CACHE.stats()},
            "breakers": BREAKERS.snapshot(),
        })

    if path == "/api/config":
        st = installer.status()
        return _json(start_response, 200, {
            "version": VERSION,
            "engine": jet_app.ENGINE_NAME,
            "sources": [{"name": c.name, "enabled": c.enabled()}
                        for c in jet_app.CONNECTORS],
            "upgrades": {"applied": st["applied"], "skipped": st["skipped"]},
            "intent_engine": "rules+llm" if st["llm"]["enabled"] else "rules",
        })

    return _legacy_app(environ, start_response)


def serve() -> None:
    from wsgiref.simple_server import make_server
    jet_app.init_db()
    port = int(os.getenv("PORT", "8000"))
    print(installer.report())
    print(f"\nJet Tesfa v{VERSION} -> http://127.0.0.1:{port}\n")
    make_server("0.0.0.0", port, app).serve_forever()


if __name__ == "__main__":
    serve()
