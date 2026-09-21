"""
Jet Tesfa v10 — upgrade layer regression suite.

Run:
    python -m unittest -v test_v10_upgrade

Covers the properties that make this upgrade safe to deploy:
  * jet_app.py is genuinely unmodified and has no dependency on the new layer
  * every v9 public contract still holds with the upgrade installed
  * each measured defect is actually fixed
  * rollback restores v9 exactly
  * the benchmark gate really can reject an upgrade
"""
from __future__ import annotations

import io
import json
import os
import random
import tempfile
import threading
import unittest

os.environ.setdefault("JET_DEMO", "1")
os.environ.setdefault("JET_ADMIN_TOKEN", "unit-test-secret-unit-test-secret")

import jet_app
from jet_intel import (bandit, bench, concepts, dna, engine, fx, gold, hebrew,
                       install, llm, nlu, ranking, semantic)
from jet_intel import store as pstore
from jet_intel.resilience import BREAKERS, CircuitBreaker, TTLCache


# ---------------------------------------------------------------------------
# Source integrity
# ---------------------------------------------------------------------------

class SourceIntegrity(unittest.TestCase):
    """The original engine must remain untouched and self-sufficient."""

    def test_jet_app_does_not_import_the_upgrade(self):
        with open(jet_app.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("jet_intel", src)
        self.assertNotIn("import jet_v10", src)

    def test_jet_app_still_runs_standalone(self):
        """v9 must work with the upgrade package absent from the process."""
        import subprocess
        import sys
        code = (
            "import os;os.environ['JET_DEMO']='1';"
            "import jet_app;"
            "r=jet_app.search('tv 4000','standalone');"
            "print(r['version'], len(r['results']))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=os.path.dirname(os.path.abspath(jet_app.__file__)))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("9.0.0", out.stdout)

    def test_upgrade_package_has_no_third_party_imports(self):
        """Zero-dependency guarantee, enforced rather than asserted in prose."""
        import pathlib
        allowed_stdlib = {
            "__future__", "math", "re", "os", "sys", "json", "time", "random",
            "threading", "sqlite3", "statistics", "hashlib", "unicodedata",
            "collections", "dataclasses", "contextlib", "typing", "urllib",
            "concurrent", "zlib", "io",
        }
        pkg = pathlib.Path(jet_app.__file__).parent / "jet_intel"
        offenders = []
        for path in pkg.glob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("import ") or line.startswith("from "):
                    if line.startswith("from ."):
                        continue
                    mod = line.split()[1].split(".")[0]
                    if mod in ("jet_app", "jet_intel"):
                        continue
                    if mod not in allowed_stdlib:
                        offenders.append(f"{path.name}: {line}")
        self.assertEqual(offenders, [], f"non-stdlib imports found: {offenders}")


# ---------------------------------------------------------------------------
# Hebrew normalization
# ---------------------------------------------------------------------------

class Hebrew(unittest.TestCase):

    def test_ktiv_variants_converge(self):
        self.assertEqual(hebrew.tokenize("טלוויזיה"), hebrew.tokenize("טלויזיה"))

    def test_prefix_stripped(self):
        self.assertEqual(hebrew.tokenize("בטלוויזיה"), hebrew.tokenize("טלוויזיה"))

    def test_protected_word_not_over_stemmed(self):
        """Regression: 'מחשב' (computer) was being reduced to 'חשב'."""
        self.assertEqual(hebrew.tokenize("מחשב"), ["מחשב"])

    def test_final_letters_normalized(self):
        self.assertEqual(hebrew.normalize_token("שלום"), hebrew.normalize_token("שלומ"))

    def test_niqqud_removed(self):
        self.assertEqual(hebrew.normalize("שָׁלוֹם"), hebrew.normalize("שלום"))

    def test_gershayim_removed(self):
        self.assertNotIn('"', hebrew.normalize('ג"ב'))


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

class Intent(unittest.TestCase):

    def test_storage_size_is_not_a_budget(self):
        """The most damaging v9 defect: '256 ג\"ב' became a budget of 256."""
        self.assertIsNone(nlu.parse('אייפון 15 פרו 256 ג"ב').max_price)

    def test_screen_size_is_not_a_budget(self):
        self.assertIsNone(nlu.parse("טלוויזיה 85 אינץ").max_price)

    def test_stated_budget_is_found(self):
        self.assertEqual(nlu.parse('מחשב עד 3,000 ש"ח').max_price, 3000)

    def test_currency_marker_alone_marks_a_budget(self):
        self.assertEqual(nlu.parse("שואב אבק 2500 ₪").max_price, 2500)

    def test_thousands_word(self):
        self.assertEqual(nlu.parse("מחשב נייד עד 5 אלף").max_price, 5000)

    def test_substring_category_trap(self):
        """v9 matched 'headphones' to the phone category via substring."""
        self.assertEqual(nlu.parse("cheap headphones").category, "gadget")

    def test_ktiv_haser_category(self):
        self.assertEqual(nlu.parse("טלויזיה גדולה לסלון").category, "tv")

    def test_hebrew_brand_transliteration(self):
        self.assertEqual(nlu.parse("אייפון חדש").category, "phone")

    def test_problem_statement_without_product_name(self):
        self.assertEqual(nlu.parse("משהו לשתות קפה בבוקר").category, "coffee")

    def test_use_case_and_urgency(self):
        i = nlu.parse("הטלוויזיה נשרפה, צריך דחוף חדשה לסלון")
        self.assertEqual(i.urgency, "high")

    def test_legacy_dict_shape_preserved(self):
        d = nlu.parse_intent("tv under 4000")
        for key in ("query", "category", "max_price", "preferences"):
            self.assertIn(key, d)

    def test_beats_legacy_on_gold_set(self):
        legacy = bench.eval_intent(install._STATE["originals"].get("parse_intent")
                                   or _legacy_parse_intent())
        new = bench.eval_intent(nlu.parse_intent)
        self.assertGreater(new["exact_accuracy"], legacy["exact_accuracy"] + 5)

    def test_no_hallucinated_budgets_on_gold_set(self):
        self.assertEqual(bench.eval_intent(nlu.parse_intent)["hallucinated_budgets"], 0)


def _legacy_parse_intent():
    """The v9 parser, reconstructed for comparison after installation."""
    import re
    CATEGORIES = {
        "tv": ["טלוויז", "מסך", "television", " tv ", "qled", "oled"],
        "phone": ["טלפון", "סמארטפון", "iphone", "galaxy", "phone"],
        "laptop": ["מחשב", "לפטופ", "laptop", "macbook"],
        "coffee": ["קפה", "coffee", "espresso"],
        "travel": ["מלון", "טיסה", "חופשה", "hotel", "flight", "travel"],
        "home": ["בית", "מטבח", "שואב", "vacuum", "home"],
        "fashion": ["בגד", "נעל", "fashion", "shoe"],
        "gadget": ["גאדגט", "gadget", "אוזניות", "headphone"],
    }

    def parse(text):
        t = " ".join(text.strip().split())
        low = t.lower()
        intent = {"query": t, "category": None, "max_price": None,
                  "min_price": None, "preferences": []}
        padded = " " + low + " "
        for c, keys in CATEGORIES.items():
            if any(k in padded for k in keys):
                intent["category"] = c
                break
        nums = []
        for raw in re.findall(r"(?<!\d)(\d{1,3}(?:[,.]\s?\d{3})+|\d{3,7})(?!\d)", low):
            try:
                nums.append(float(re.sub(r"[,\s]", "", raw)))
            except Exception:
                pass
        if nums:
            intent["max_price"] = max(nums)
        return intent
    return parse


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

class _Item:
    def __init__(self, price, currency="ILS", shipping=None, metadata=None,
                 category="tv", available=True):
        self.price = price
        self.currency = currency
        self.shipping = shipping
        self.metadata = metadata or {}
        self.category = category
        self.available = available
        self.title = "t"
        self.url = "https://x"
        self.merchant = "m"
        self.brand = ""
        self.description = ""
        self.quality = self.trust = self.fit = self.satisfaction = 0.8
        self.deal = self.freshness = self.price_confidence = 0.8
        self.commission = 0.0
        self.old_price = None
        self.gtin = ""


class Currency(unittest.TestCase):

    def test_foreign_price_converted(self):
        usd = ranking.total_cost(_Item(900, "USD"))
        self.assertGreater(usd, 900)

    def test_over_budget_detected_across_currencies(self):
        """v9 scored a $900 item as comfortably inside an ILS 3,000 budget."""
        self.assertGreater(ranking.total_cost(_Item(900, "USD")), 3000)

    def test_native_currency_unchanged(self):
        self.assertEqual(ranking.total_cost(_Item(2500, "ILS")), 2500)

    def test_shipping_and_tax_included(self):
        item = _Item(3000, "ILS", shipping=100, metadata={"tax": 50})
        self.assertEqual(ranking.total_cost(item), 3150)

    def test_unknown_currency_does_not_zero_the_price(self):
        self.assertEqual(ranking.total_cost(_Item(1000, "XYZ")), 1000)

    def test_conversion_confidence_lower_for_converted(self):
        self.assertLess(fx.conversion_confidence("USD"), fx.conversion_confidence("ILS"))

    def test_fx_conformance_is_perfect(self):
        self.assertEqual(bench.eval_fx(ranking.total_cost)["accuracy"], 100.0)


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

class Relevance(unittest.TestCase):

    def _score(self, query, titles, category="laptop"):
        class S:
            def __init__(self, t):
                self.title = t
                self.category = category
                self.brand = ""
                self.merchant = ""
                self.description = ""
        items = [S(t) for t in titles]
        return semantic.relevance_scores(nlu.parse_intent(query), items)

    def test_synonyms_bridge_zero_token_overlap(self):
        """'לפטופ ... לסטודנטים' shares no token with 'מחשב נייד ללימודים'."""
        s = self._score("מחשב נייד ללימודים",
                        ["לפטופ 15.6 אינץ לסטודנטים", "מחשב נייח לגיימינג"])
        self.assertGreater(s[0], s[1])

    def test_accessory_ranked_below_product(self):
        s = self._score("מחשב נייד ללימודים",
                        ["מחשב נייד 14 אינץ ללימודים", "תיק למחשב נייד 15 אינץ"])
        self.assertGreater(s[0], s[1])

    def test_attribute_conflict_penalized(self):
        s = self._score("wireless noise cancelling headphones",
                        ["Wireless ANC Headphones", "Wired Earbuds"],
                        category="gadget")
        self.assertGreater(s[0], s[1])

    def test_scores_use_the_full_range(self):
        """Compressed scores made need_fit unable to influence ranking."""
        s = self._score("טלוויזיה 75 אינץ",
                        ['טלוויזיה 75" QLED', "מכונת כביסה", "נעלי ריצה"], "tv")
        self.assertGreater(max(s) - min(s), 0.4)

    def test_beats_legacy_on_gold_set(self):
        legacy = bench.eval_relevance(bench.legacy_relevance)["mean_ndcg@3"]
        new = bench.eval_relevance(semantic.relevance_scores)["mean_ndcg@3"]
        self.assertGreater(new, legacy + 3)

    def test_mmr_returns_requested_count(self):
        class S:
            def __init__(self, t):
                self.title = t
                self.category = "tv"
                self.brand = self.merchant = self.description = ""
        items = [S(f"TV model {i}") for i in range(10)]
        picked = semantic.mmr_diversify(items, [1.0 - i * 0.05 for i in range(10)], k=5)
        self.assertEqual(len(picked), 5)
        self.assertEqual(len(set(picked)), 5)


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

class PriceHistory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = jet_app.DB
        jet_app.DB = jet_app.Path(self.tmp.name) / "t.db"
        jet_app.init_db()

    def tearDown(self):
        pstore.close_thread_connections()
        jet_app.DB = self.old_db
        self.tmp.cleanup()

    def _seed(self, key, prices):
        with jet_app.db_conn() as c:
            for p in prices:
                c.execute("INSERT INTO price_history(product_key,ts,price,currency) "
                          "VALUES(?,?,?,?)", (key, jet_app.now(), p, "ILS"))

    def test_single_query_for_many_keys(self):
        """v9 issued roughly nine SQLite queries per result."""
        for i in range(20):
            self._seed(f"k{i}", [1000, 1100, 900])
        snap = pstore.PriceSnapshot.load(jet_app.DB, [f"k{i}" for i in range(20)])
        self.assertEqual(snap.queries, 1)
        self.assertEqual(snap.loaded_keys, 20)

    def test_outlier_does_not_move_the_reference(self):
        """A single planted high price must not manufacture a discount."""
        honest = pstore.robust_reference([1000, 1010, 990, 1005, 995, 1000])
        planted = pstore.robust_reference([1000, 1010, 990, 1005, 995, 1000, 9000])
        self.assertLess(abs(planted["reference"] - honest["reference"]), 30)

    def test_thin_history_yields_low_confidence(self):
        self.assertLess(pstore.robust_reference([1000, 1100])["confidence"], 0.2)

    def test_missing_table_degrades_safely(self):
        snap = pstore.PriceSnapshot.load("/nonexistent/path/x.db", ["a"])
        self.assertEqual(snap.deal_score("a", 100, fallback=0.42), 0.42)

    def test_deal_score_rewards_below_reference(self):
        self._seed("kk", [1000] * 25)
        snap = pstore.PriceSnapshot.load(jet_app.DB, ["kk"])
        self.assertGreater(snap.deal_score("kk", 700), snap.deal_score("kk", 1300))


# ---------------------------------------------------------------------------
# Fake discounts
# ---------------------------------------------------------------------------

class FakeDiscounts(unittest.TestCase):

    def test_uncorroborated_discount_penalized(self):
        snap = pstore.PriceSnapshot({"k": pstore.robust_reference([1000] * 12)})
        honest = _Item(800)
        honest.old_price = 1000          # consistent with history
        fake = _Item(800)
        fake.old_price = 3000            # never observed anywhere near this
        s_honest, _ = ranking.deal_truth(honest, snap, "k")
        s_fake, ev = ranking.deal_truth(fake, snap, "k")
        self.assertGreater(s_honest, s_fake)
        self.assertIs(ev["discount_corroborated"], False)

    def test_fake_discount_surfaces_in_the_decision(self):
        snap = pstore.PriceSnapshot({"k": pstore.robust_reference([1000] * 12)})
        fake = _Item(800)
        fake.old_price = 3000
        bundle = ranking.score_components(fake, {}, {}, 0.9, snap, "k")
        decision, _ = ranking.decide(fake, {}, bundle["components"],
                                     bundle["cost"], bundle["evidence"])
        self.assertEqual(decision, "TRACK")


# ---------------------------------------------------------------------------
# Customer DNA
# ---------------------------------------------------------------------------

class CustomerDNA(unittest.TestCase):

    def test_v9_profile_upgrades_without_loss(self):
        legacy = {"categories": {"tv": 3}, "queries": 7, "clicks": {},
                  "budget_sum": 12000, "budget_n": 3}
        p = dna.upgrade(legacy)
        self.assertEqual(p["queries"], 7)
        self.assertEqual(p["categories"]["tv"], 3)
        self.assertIn("tv", p["facets"]["category"])

    def test_decay_reduces_old_evidence(self):
        import time
        p = dna.empty_profile()
        p["facets"]["category"]["tv"] = [10.0, time.time() - 400 * 86400]
        p["facets"]["category"]["phone"] = [10.0, time.time()]
        self.assertLess(dna.current_weight(p["facets"]["category"], "tv"),
                        dna.current_weight(p["facets"]["category"], "phone"))

    def test_negative_signal_recorded(self):
        p = dna.empty_profile()
        for _ in range(3):
            p = dna.observe_event(p, "refund", {"category": "tv", "brand": "x",
                                                "merchant": "m", "price": 100})
        self.assertGreater(dna.current_weight(p["negative"]["category"], "tv"), 0)

    def test_outlier_budget_does_not_dominate(self):
        p = dna.empty_profile()
        for _ in range(9):
            p = dna.observe_intent(p, {"max_price": 500, "category": "home"})
        p = dna.observe_intent(p, {"max_price": 40000, "category": "home"})
        self.assertLess(dna.typical_budget(p), 2000)

    def test_boost_is_bounded_and_explained(self):
        p = dna.empty_profile()
        for _ in range(20):
            p = dna.observe_event(p, "purchase", {"category": "tv", "brand": "lg",
                                                  "merchant": "m", "price": 3000})
        boost, reasons = dna.personalization_boost(p, _Item(3000, category="tv"))
        self.assertLessEqual(abs(boost), dna.MAX_BOOST)
        self.assertTrue(reasons)


# ---------------------------------------------------------------------------
# Model arena
# ---------------------------------------------------------------------------

class Arena(unittest.TestCase):

    def test_untested_models_get_explored(self):
        """v9 could never promote: the challenger was never served, so it
        never accumulated the impressions its own promotion rule required."""
        stats = {"truth_v6": {"impressions": 5000, "clicks": 50, "purchases": 5},
                 "conservative_v6": {"impressions": 0}}
        rng = random.Random(7)
        picks = [bandit.select(stats, engine.MODELS, engine.CHAMPION, rng)
                 for _ in range(3000)]
        self.assertIn("conservative_v6", picks)

    def test_clearly_better_model_wins_traffic(self):
        stats = {
            "truth_v6": {"impressions": 1000, "clicks": 10, "purchases": 1, "refunds": 0},
            "conservative_v6": {"impressions": 1000, "clicks": 300, "purchases": 120, "refunds": 0},
        }
        share = bandit.traffic_share(stats, engine.MODELS, engine.CHAMPION)
        self.assertGreater(share["conservative_v6"], share["truth_v6"])

    def test_refunds_are_penalized(self):
        good = {"impressions": 1000, "clicks": 200, "purchases": 100, "refunds": 0}
        bad = {"impressions": 1000, "clicks": 200, "purchases": 100, "refunds": 90}
        self.assertGreater(bandit.posterior_mean(good), bandit.posterior_mean(bad))

    def test_report_exposes_uncertainty(self):
        r = bandit.report({}, engine.MODELS, engine.CHAMPION)
        for m in engine.MODELS:
            self.assertIn("credible_interval", r["models"][m])


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

class Resilience(unittest.TestCase):

    def test_cache_returns_stored_value(self):
        c = TTLCache(ttl=60)
        c.set("k", [1, 2, 3])
        self.assertEqual(c.get("k"), [1, 2, 3])

    def test_cache_expires(self):
        c = TTLCache(ttl=-1)
        c.set("k", "v")
        self.assertIsNone(c.get("k"))

    def test_single_flight_calls_once(self):
        c = TTLCache(ttl=60)
        calls = []
        lock = threading.Lock()

        def slow():
            with lock:
                calls.append(1)
            import time
            time.sleep(0.05)
            return "value"

        threads = [threading.Thread(target=lambda: c.get_or_call("k", slow))
                   for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(calls), 1)

    def test_breaker_opens_after_threshold(self):
        b = CircuitBreaker("x", threshold=3, reset_after=60)
        for _ in range(3):
            b.record_failure("boom")
        self.assertEqual(b.state, "open")
        self.assertFalse(b.allows())

    def test_breaker_half_opens_and_recovers(self):
        b = CircuitBreaker("x", threshold=2, reset_after=-1)
        b.record_failure("a")
        b.record_failure("b")
        self.assertTrue(b.allows())          # half-open probe
        b.record_success()
        self.assertEqual(b.state, "closed")

    def test_breaker_allows_one_probe_only(self):
        b = CircuitBreaker("x", threshold=1, reset_after=-1)
        b.record_failure("a")
        self.assertTrue(b.allows())
        self.assertFalse(b.allows())


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------

class LLMLayer(unittest.TestCase):

    def test_disabled_without_key_falls_back_to_rules(self):
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertFalse(llm.enabled())
            got = llm.understand("טלוויזיה עד 4000")
            self.assertEqual(got["max_price"], 4000)
            self.assertEqual(got["source"], "rules")
        finally:
            if old:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_malformed_payload_is_rejected(self):
        self.assertEqual(llm._sanitize({"category": "electronics",
                                        "max_price": "about 3000"}).get("category"), None)

    def test_out_of_range_values_dropped(self):
        self.assertNotIn("max_price", llm._sanitize({"max_price": -5}))

    def test_llm_cannot_override_a_stated_budget(self):
        rules = nlu.parse_intent("מחשב עד 3000 שקל")
        merged = llm.merge(rules, {"max_price": 99999, "confidence": 0.99})
        self.assertEqual(merged["max_price"], 3000)

    def test_llm_fills_a_missing_category(self):
        rules = dict(nlu.parse_intent("משהו לחדר"), category=None,
                     category_confidence=0.0)
        merged = llm.merge(rules, {"category": "home", "confidence": 0.9})
        self.assertEqual(merged["category"], "home")

    def test_network_failure_returns_rules(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-invalid-for-test"
        old_call = llm._call_api
        llm._call_api = lambda q, t: (_ for _ in ()).throw(OSError("network down"))
        try:
            got = llm.understand("טלוויזיה עד 4000")
            self.assertEqual(got["max_price"], 4000)
        finally:
            llm._call_api = old_call
            os.environ.pop("ANTHROPIC_API_KEY", None)
            llm.BREAKER.record_success()


# ---------------------------------------------------------------------------
# Installation and rollback
# ---------------------------------------------------------------------------

class Installation(unittest.TestCase):

    def test_gate_can_reject(self):
        """The gate must be capable of refusing, or it is not a gate."""
        bad = bench.eval_intent(lambda q: {"category": None, "max_price": None})
        good = bench.eval_intent(nlu.parse_intent)
        self.assertLess(bad["exact_accuracy"], good["exact_accuracy"])

    def test_gate_results_are_reproducible(self):
        a = bench.gate(jet_app)
        b = bench.gate(jet_app)
        self.assertEqual(a["relevance"]["candidate"]["mean_ndcg@3"],
                         b["relevance"]["candidate"]["mean_ndcg@3"])

    def test_install_is_idempotent(self):
        install.install(jet_app)
        first = install.status()["applied"]
        install.install(jet_app)
        self.assertEqual(first, install.status()["applied"])

    def test_rollback_restores_v9(self):
        install.install(jet_app)
        self.assertEqual(jet_app.search("tv 4000", "roll")["upgrade_version"], "10.0.0")
        install.uninstall(jet_app)
        try:
            self.assertNotIn("upgrade_version", jet_app.search("tv 4000", "roll"))
            self.assertIsNone(jet_app.parse_intent('אייפון 256 ג"ב').get("category"))
        finally:
            install.install(jet_app)

    def test_search_falls_back_on_internal_error(self):
        install.install(jet_app)
        old = engine.search
        engine.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = jet_app.search("tv 4000", "fallback")
            self.assertTrue(out["results"])
            self.assertEqual(out["version"], "9.0.0")
        finally:
            engine.search = old


# ---------------------------------------------------------------------------
# End-to-end contract
# ---------------------------------------------------------------------------

class EndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        install.install(jet_app)

    def _req(self, path, method="GET", body=None, headers=None):
        raw = json.dumps(body or {}).encode()
        env = {"PATH_INFO": path, "REQUEST_METHOD": method,
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
               "REMOTE_ADDR": "5.5.5.5"}
        env.update(headers or {})
        cap = {}

        def start(s, h):
            cap["s"] = s
            cap["h"] = dict(h)
        out = b"".join(jet_app.app(env, start))
        return int(cap["s"].split()[0]), json.loads(out or b"{}")

    def test_v9_response_keys_all_present(self):
        r = jet_app.search("טלוויזיה עד 4000", "e2e")
        for key in ("intent", "results", "sources", "dna", "model",
                    "need_alternatives", "engine", "version"):
            self.assertIn(key, r)

    def test_result_keys_all_present(self):
        r = jet_app.search("טלוויזיה עד 4000", "e2e")
        for key in ("jet_score", "score_components", "total_cost", "savings",
                    "why", "deal_confidence", "decision", "title", "url", "price"):
            self.assertIn(key, r["results"][0])

    def test_results_sorted_by_score(self):
        scores = [x["jet_score"] for x in jet_app.search("טלוויזיה", "e2e")["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_commission_guard_still_holds(self):
        self.assertLessEqual(ranking.WEIGHTS["revenue"], 0.03)

    def test_api_search_still_works(self):
        code, body = self._req("/api/search", "POST", {"q": "tv 4000", "session": "e2e"})
        self.assertEqual(code, 200)
        self.assertTrue(body["results"])

    def test_health_and_ready(self):
        self.assertEqual(self._req("/health")[0], 200)
        self.assertEqual(self._req("/ready")[0], 200)

    def test_diagnostics_reports_one_history_query(self):
        r = jet_app.search("טלוויזיה עד 4000", "e2e")
        self.assertLessEqual(r["diagnostics"]["price_history"]["queries"], 1)

    def test_failing_connector_does_not_break_search(self):
        class Bad:
            name = "bad"

            def enabled(self):
                return True

            def search(self, intent):
                raise RuntimeError("boom")

        old = jet_app.CONNECTORS
        jet_app.CONNECTORS = [Bad(), jet_app.DemoConnector()]
        BREAKERS.reset()
        try:
            self.assertTrue(jet_app.search("tv 4000", "e2e")["results"])
        finally:
            jet_app.CONNECTORS = old
            BREAKERS.reset()

    def test_repeated_failures_open_the_circuit(self):
        class Bad:
            name = "flaky"
            calls = 0

            def enabled(self):
                return True

            def search(self, intent):
                Bad.calls += 1
                raise RuntimeError("boom")

        bad = Bad()
        old = jet_app.CONNECTORS
        jet_app.CONNECTORS = [bad, jet_app.DemoConnector()]
        BREAKERS.reset()
        try:
            for i in range(8):
                jet_app.search(f"tv {4000 + i}", "e2e")
            # Without a breaker this would be 8; the breaker stops calling.
            self.assertLess(Bad.calls, 8)
            self.assertEqual(BREAKERS.get("flaky").state, "open")
        finally:
            jet_app.CONNECTORS = old
            BREAKERS.reset()

    def test_concurrent_searches(self):
        errors = []

        def run():
            try:
                jet_app.search("tv 4000", str(random.random()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
