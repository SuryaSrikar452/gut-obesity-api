"""API tests with an in-memory fake Supabase. REQUIRE fastapi + httpx (+ xgboost for the model tests).
They are skipped automatically when those packages are missing.
Run: pytest -q"""
import os, sys, types, unittest, uuid, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
os.environ.update(SUPABASE_URL="http://x", SUPABASE_SERVICE_KEY="k", BREVO_API_KEY="b", TOKEN_SECRET="s" * 40,
                  PUBLIC_BASE_URL="https://example.test")
try:
    import fastapi, httpx  # noqa
    HAVE = True
except ImportError:
    HAVE = False
try:
    import xgboost  # noqa
    HAVE_XGB = True
except ImportError:
    HAVE_XGB = False
from core.microbiome import SPECIES


class _Q:
    def __init__(self, db, name): self.db, self.name, self.f, self.op, self.payload, self._order, self._lim, self.cnt = db, name, [], "select", None, None, None, False
    def select(self, *a, count=None): self.cnt = bool(count); return self
    def insert(self, d): self.op, self.payload = "insert", d; return self
    def update(self, d): self.op, self.payload = "update", d; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, k, v): self.f.append(lambda r: r.get(k) == v); return self
    def gte(self, k, v): self.f.append(lambda r: r.get(k) >= v); return self
    def lte(self, k, v): self.f.append(lambda r: r.get(k) <= v); return self
    def lt(self, k, v): self.f.append(lambda r: r.get(k) < v); return self
    def in_(self, k, vs): self.f.append(lambda r: r.get(k) in vs); return self
    def order(self, k, desc=False): self._order = (k, desc); return self
    def limit(self, n): self._lim = n; return self
    def execute(self):
        rows = self.db.setdefault(self.name, [])
        if self.op == "insert":
            d = dict(self.payload); d.setdefault("id", str(uuid.uuid4()))
            if self.name in ("glucose_readings",): d.setdefault("created_at", "now")
            rows.append(d); return types.SimpleNamespace(data=[d], count=1)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if self.op == "update":
            for r in hit: r.update(self.payload)
        elif self.op == "delete":
            for r in hit: rows.remove(r)
        else:
            if self._order: hit.sort(key=lambda r: r.get(self._order[0]) or "", reverse=self._order[1])
            total = len(hit)
            if self._lim: hit = hit[: self._lim]
            return types.SimpleNamespace(data=[dict(r) for r in hit], count=total)
        return types.SimpleNamespace(data=[dict(r) for r in hit], count=len(hit))


class FakeSB:
    def __init__(self): self.db = {}; self.files = {}
    def table(self, n): return _Q(self.db, n)
    @property
    def storage(self):
        sb = self
        class B:
            def upload(self, path, data, opts=None): sb.files[path] = data
        return types.SimpleNamespace(from_=lambda b: B())


def fake_recommender():
    m = types.ModuleType("recommender"); m.MODEL_VERSION = "test-model"
    m.last_input = {}
    def rec(age, h, w, fbg, ab):
        m.last_input = {"fbg": fbg, "ab": list(ab)}
        item = {"enzyme": "EC 1.1.1.267", "enzyme_name": "DXR", "prob": 50.0, "compound": "Baicalein", "pic50": 5.66, "sources": ["x"]}
        return {"bmi": 22.0, "bmi_category": "Normal", "fbg_category": "Normal", "phyto": [item], "food": [item], "model_version": "test-model"}
    m.recommend_from_microbiome = rec
    return m


def csv_bytes(sid="S1", v=1.0):
    return (",".join(["Sample_ID"] + SPECIES) + "\n" + ",".join([sid] + [str(v)] * 20)).encode()


@unittest.skipUnless(HAVE, "fastapi/httpx not installed")
class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import supabase
        cls.sb = FakeSB(); supabase.create_client = lambda *a, **k: cls.sb
        cls.rec = fake_recommender(); sys.modules["recommender"] = cls.rec
        sys.modules.pop("app", None)
        import app as appmod
        from fastapi.testclient import TestClient
        cls.app = appmod; cls.c = TestClient(appmod.app)

    def setUp(self):
        self.sb.db.clear(); self.sb.files.clear()
        self.sb.db["users"] = [{"id": 1, "email": "a@x.com", "device_id": "GUT-AAAA", "claimed": True},
                               {"id": 2, "email": "b@x.com", "device_id": "GUT-BBBB", "claimed": True}]
        from core.tokens import make_token
        self.ha = {"Authorization": "Bearer " + make_token("session", {"uid": 1, "dev": "GUT-AAAA"}, 3600)}
        self.hb = {"Authorization": "Bearer " + make_token("session", {"uid": 2, "dev": "GUT-BBBB"}, 3600)}

    def glucose(self, dev="GUT-AAAA", v=95, ref=None, fasting=None):
        d = {"device_id": dev, "value": v}
        if ref: d["client_ref"] = ref
        if fasting is not None: d["fasting"] = fasting
        return self.c.post("/device/glucose", data=d)

    def analyze(self, gid, hdr=None, csv=None, src="new_test", sid=None, **hd):
        data = {"glucose_reading_id": gid, "glucose_source": src, "age": 40, "height_cm": 170, "weight_kg": 70}
        if sid: data["sample_id"] = sid
        return self.c.post("/analyses", data=data, files={"file": ("s.csv", csv or csv_bytes(), "text/csv")},
                           headers=dict(hdr or self.ha, **hd))

    # 1. auth / access control
    def test_requires_login(self):
        for p in ("/glucose", "/reports", "/reports/export.csv"):
            self.assertEqual(self.c.get(p).status_code, 401, p)
        self.assertEqual(self.c.get("/glucose", headers={"Authorization": "Bearer junk"}).status_code, 401)

    def test_unknown_device_cannot_post_glucose(self):
        self.assertEqual(self.glucose(dev="GUT-NOPE").status_code, 403)
        self.assertEqual(self.glucose(v=10).status_code, 400)

    # 2/3/10. glucose recording, retries, history ordering, isolation
    def test_glucose_record_idempotent_ordered_and_isolated(self):
        a = self.glucose(v=90, ref="r1"); self.assertEqual(a.status_code, 201)
        again = self.glucose(v=90, ref="r1"); self.assertEqual(again.json()["id"], a.json()["id"])
        self.assertEqual(len(self.sb.db["glucose_readings"]), 1)
        self.glucose(v=110); self.sb.db["glucose_readings"][1]["measured_at"] = "9999-01-01T00:00:00+00:00"
        items = self.c.get("/glucose", headers=self.ha).json()["items"]
        self.assertEqual([i["value"] for i in items], [110.0, 90.0])      # newest first
        self.assertEqual(self.c.get("/glucose", headers=self.hb).json()["items"], [])
        self.assertEqual(self.c.get("/glucose/" + a.json()["id"], headers=self.hb).status_code, 404)

    # 4-8. analysis: user's glucose + user's CSV reach the models, snapshot saved
    def test_analysis_uses_selected_glucose_and_uploaded_csv(self):
        g = self.glucose(v=131, fasting=True).json()
        self.glucose(v=88)  # a different, newer reading must not be substituted
        r = self.analyze(g["id"], src="previous"); self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.rec.last_input["fbg"], 131.0)
        self.assertEqual(self.rec.last_input["ab"], [1.0] * 20)
        rep = self.c.get("/reports/" + r.json()["report_id"], headers=self.ha).json()
        self.assertEqual(rep["status"], "completed"); self.assertEqual(rep["glucose"]["source"], "previous")
        self.assertEqual(rep["glucose"]["fasting"], True); self.assertEqual(rep["sample_id"], "S1")
        self.assertEqual([f["name"] for f in rep["features"]], SPECIES)
        self.assertEqual(len(self.sb.files), 1)
        self.assertNotIn("risk_percent", rep)

    def test_malformed_csv_rejected_without_report(self):
        g = self.glucose().json()
        r = self.analyze(g["id"], csv=b"a,b\n1,2\n"); self.assertEqual(r.status_code, 400)
        self.assertEqual(self.sb.db.get("analysis_reports", []), [])

    def test_multi_sample_requires_choice(self):
        g = self.glucose().json()
        two = csv_bytes("A") + b"\n" + csv_bytes("B").split(b"\n")[1]
        r = self.analyze(g["id"], csv=two); self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["sample_ids"], ["A", "B"])
        self.assertEqual(self.analyze(g["id"], csv=two, sid="B").status_code, 201)

    def test_idempotent_retry(self):
        g = self.glucose().json()
        r1 = self.analyze(g["id"], **{"Idempotency-Key": "k1"}); r2 = self.analyze(g["id"], **{"Idempotency-Key": "k1"})
        self.assertEqual(r1.json()["report_id"], r2.json()["report_id"])
        self.assertEqual(len(self.sb.db["analysis_reports"]), 1)

    def test_model_failure_marks_failed_not_completed(self):
        g = self.glucose().json(); orig = self.app.recommend_from_microbiome
        self.app.recommend_from_microbiome = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        try: r = self.analyze(g["id"])
        finally: self.app.recommend_from_microbiome = orig
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.sb.db["analysis_reports"][0]["status"], "failed")
        self.assertEqual(self.c.get("/reports/export.csv", headers=self.ha).text.count("\n"), 1)  # header only

    def test_cannot_analyze_with_other_users_reading(self):
        g = self.glucose().json()
        self.assertEqual(self.analyze(g["id"], hdr=self.hb).status_code, 404)

    # 11-13. PDF + QR
    def make_report(self):
        g = self.glucose().json(); return self.analyze(g["id"]).json()["report_id"]

    def test_pdf_download_and_isolation(self):
        rid = self.make_report()
        r = self.c.get(f"/reports/{rid}/pdf", headers=self.ha)
        self.assertEqual(r.status_code, 200); self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(self.c.get(f"/reports/{rid}/pdf", headers=self.hb).status_code, 404)
        self.assertEqual(self.c.get(f"/reports/{uuid.uuid4()}/pdf", headers=self.ha).status_code, 404)
        self.assertEqual(self.c.get("/reports/not-a-uuid", headers=self.ha).status_code, 404)

    def test_qr_link_direct_download_no_auth_header(self):
        rid = self.make_report()
        link = self.c.post(f"/reports/{rid}/download-link", headers=self.ha).json()
        self.assertTrue(link["url"].startswith("https://example.test/d/"))
        r = self.c.get("/d/" + link["url"].rsplit("/", 1)[1])
        self.assertEqual(r.status_code, 200); self.assertIn("attachment", r.headers["content-disposition"])
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertEqual(self.c.post(f"/reports/{rid}/download-link", headers=self.hb).status_code, 404)

    def test_qr_token_expired_invalid_and_wrong_purpose(self):
        rid = self.make_report()
        from core.tokens import make_token
        expired = make_token("pdf", {"rid": rid, "uid": 1}, -5)
        self.assertEqual(self.c.get("/d/" + expired).status_code, 410)
        self.assertEqual(self.c.get("/d/garbage").status_code, 403)
        self.assertEqual(self.c.get("/d/" + make_token("session", {"rid": rid, "uid": 1}, 60)).status_code, 403)
        self.assertEqual(self.c.get("/d/" + make_token("pdf", {"rid": rid, "uid": 2}, 60)).status_code, 404)  # uid mismatch

    # 9. removed endpoints
    def test_gut_risk_endpoints_gone(self):
        for m, p in (("post", "/dashboard-data"), ("get", "/latest/GUT-AAAA"), ("get", "/history/GUT-AAAA"), ("post", "/recommend")):
            self.assertIn(getattr(self.c, m)(p).status_code, (404, 405), p)
        self.assertNotIn("xgboost", sys.modules.get("app").__dict__)


@unittest.skipUnless(HAVE_XGB, "xgboost not installed")
class RealModels(unittest.TestCase):
    """Fits the real models from the cohort xlsx files (a few seconds)."""
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("recommender", None)
        import recommender; cls.r = recommender

    def test_dimensions_and_both_models_predict(self):
        import numpy as np
        for k in (self.r._PHYTO, self.r._FOOD):
            self.assertEqual(k.model.n_features_in_, 23)
        ab = [1.0] * 20
        out = self.r.recommend_from_microbiome(55, 165, 72, 150, ab)
        self.assertEqual(len(out["phyto"]), 3); self.assertEqual(len(out["food"]), 3)
        self.assertTrue(all(0 <= i["prob"] <= 100 for i in out["phyto"] + out["food"]))

    def test_microbiome_actually_changes_prediction_and_glucose_is_passed_through(self):
        import numpy as np
        a = np.array([5.0] * 20); b = np.array(list(reversed(range(1, 21))), float) / 4
        pa = self.r._PHYTO.model.predict_proba(np.concatenate([[55, 24, 150], self.r._clr(a)]).reshape(1, -1))
        pb = self.r._PHYTO.model.predict_proba(np.concatenate([[55, 24, 150], self.r._clr(b)]).reshape(1, -1))
        self.assertFalse(np.allclose(pa, pb))
        self.assertEqual(self.r.recommend_from_microbiome(55, 165, 72, 150, [1.0] * 20)["fbg_category"], "Diabetes range")

    def test_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            self.r._PHYTO.predict(50, 24, 100, [1.0] * 19)


if __name__ == "__main__":
    unittest.main()
