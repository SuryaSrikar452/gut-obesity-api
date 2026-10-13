"""Pure-Python tests (no FastAPI / xgboost / Supabase needed). Run: python -m unittest discover -s tests -v"""
import os, re, sys, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TOKEN_SECRET", "x" * 40)
import numpy as np, pandas as pd
from core.microbiome import SPECIES, clr, parse_microbiome_csv, CsvValidationError, PSEUDOCOUNT
from core.tokens import make_token, read_token, TokenError
from core.pdf_report import report_to_pdf, reports_to_csv


def csv_bytes(rows, cols=None, idcol="Sample_ID"):
    cols = cols or ([idcol] + SPECIES)
    return pd.DataFrame(rows, columns=cols).to_csv(index=False).encode()


def good_row(sid="S1", val=1.0):
    return [sid] + [val] * len(SPECIES)


class FeatureContract(unittest.TestCase):
    def test_species_match_both_cohort_files_in_order(self):
        for f in ("fbg_input_phyto.xlsx", "fbg_input_food.xlsx"):
            df = pd.read_excel(os.path.join(ROOT, f))
            cols = [c for c in df.columns if c not in ["Sample_ID", "Table_S1_Sample_ID", "HbA1c", "Age", "BMI", "FBG_mgdL"]
                    and not c.startswith("EC_")]
            self.assertEqual(cols, SPECIES, f)
            self.assertEqual(len(cols), 20)

    def test_model_input_dimension_is_3_plus_20(self):
        self.assertEqual(3 + len(SPECIES), 23)

    def test_clr_matches_training_formula_and_handles_zeros(self):
        v = np.array([0.0, 1.0, 10.0] + [0.5] * 17)
        lg = np.log(v + 1e-3)
        np.testing.assert_allclose(clr(v), lg - lg.mean())
        self.assertAlmostEqual(float(clr(v).sum()), 0.0, places=9)
        self.assertTrue(np.isfinite(clr(v)).all())  # zero abundance must not produce -inf
        M = np.vstack([v, v * 0.5])
        np.testing.assert_allclose(clr(M)[0], clr(v))
        self.assertEqual(PSEUDOCOUNT, 1e-3)

    def test_cohort_clr_reproduces_recommender_formula(self):
        df = pd.read_excel(os.path.join(ROOT, "fbg_input_phyto.xlsx"))
        M = df[SPECIES].values.astype(float)
        out = clr(M)
        self.assertEqual(out.shape, (102, 20))
        np.testing.assert_allclose(out.mean(axis=1), 0, atol=1e-9)


class CsvValidation(unittest.TestCase):
    def test_valid_single_sample(self):
        p = parse_microbiome_csv(csv_bytes([good_row()]))
        self.assertEqual(len(p["samples"]), 1)
        self.assertEqual(p["samples"][0]["sample_id"], "S1")
        self.assertEqual(len(p["samples"][0]["abundances"]), 20)

    def test_reordered_columns_are_realigned_by_name(self):
        cols = ["Sample_ID"] + SPECIES[::-1]
        row = ["S1"] + [i / 10 for i in range(20, 0, -1)]   # species k has value (k+1)/10 after realignment
        p = parse_microbiome_csv(csv_bytes([row], cols))
        np.testing.assert_allclose(p["samples"][0]["abundances"], [i / 10 for i in range(1, 21)])

    def test_no_id_column_gets_row_labels_and_extra_columns_reported(self):
        cols = SPECIES + ["Notes"]
        p = parse_microbiome_csv(csv_bytes([[1.0] * 20 + ["x"]], cols))
        self.assertEqual(p["samples"][0]["sample_id"], "row_1")
        self.assertEqual(p["extra_columns"], ["Notes"])

    def test_multiple_samples_kept_separate_not_averaged(self):
        p = parse_microbiome_csv(csv_bytes([good_row("A", 1.0), good_row("B", 2.0)]))
        self.assertEqual([s["sample_id"] for s in p["samples"]], ["A", "B"])
        self.assertEqual(p["samples"][1]["abundances"][0], 2.0)

    def test_missing_species_rejected_with_names(self):
        cols = ["Sample_ID"] + SPECIES[:-1]
        with self.assertRaises(CsvValidationError) as cm:
            parse_microbiome_csv(csv_bytes([["S"] + [1.0] * 19], cols))
        self.assertEqual(cm.exception.details, [SPECIES[-1]])

    def test_unsupported_other_species_are_not_mapped(self):
        cols = ["Sample_ID"] + ["Bacteroides_fragilis"] + SPECIES[1:]
        with self.assertRaises(CsvValidationError):
            parse_microbiome_csv(csv_bytes([["S"] + [1.0] * 20], cols))

    def test_bad_values(self):
        for bad in ("abc", "", "-1"):
            row = good_row(); row[1] = bad
            raw = ",".join(["Sample_ID"] + SPECIES) + "\n" + ",".join(str(x) for x in row)
            with self.assertRaises(CsvValidationError, msg=bad):
                parse_microbiome_csv(raw.encode())

    def test_counts_or_over_100_rejected(self):
        with self.assertRaises(CsvValidationError):
            parse_microbiome_csv(csv_bytes([good_row("S", 50.0)]))        # sums to 1000
        with self.assertRaises(CsvValidationError):
            parse_microbiome_csv(csv_bytes([good_row("S", 12345.0)]))

    def test_zero_abundance_values_allowed_but_all_zero_rejected(self):
        row = good_row(); row[1] = 0.0
        parse_microbiome_csv(csv_bytes([row]))
        with self.assertRaises(CsvValidationError):
            parse_microbiome_csv(csv_bytes([good_row("Z", 0.0)]))

    def test_proportion_scale_warning(self):
        p = parse_microbiome_csv(csv_bytes([good_row("S", 0.01)]))
        self.assertTrue(p["warnings"])

    def test_empty_binary_oversize_duplicates(self):
        for raw in (b"", b"\x00\x01\x02" * 100, b"a" * (2 * 1024 * 1024 + 1)):
            with self.assertRaises(CsvValidationError):
                parse_microbiome_csv(raw)
        with self.assertRaises(CsvValidationError):
            parse_microbiome_csv(csv_bytes([good_row("A"), good_row("A")]))
        with self.assertRaises(CsvValidationError):
            parse_microbiome_csv((",".join(["Sample_ID"] + SPECIES)).encode() + b"\n")  # header only


class Tokens(unittest.TestCase):
    def test_roundtrip_expiry_tamper_purpose(self):
        t = make_token("pdf", {"rid": "r1", "uid": 5}, 300)
        self.assertEqual(read_token(t, "pdf")["rid"], "r1")
        with self.assertRaises(TokenError) as cm:
            read_token(t, "pdf", now=time.time() + 301)
        self.assertEqual(str(cm.exception), "expired")
        with self.assertRaises(TokenError):
            read_token(t, "session")                       # a PDF token is not a login session
        payload, sig = t.split(".")
        with self.assertRaises(TokenError):
            read_token(payload + "." + sig[:-2] + "AA", "pdf")
        with self.assertRaises(TokenError):
            read_token("garbage", "pdf")

    def test_token_contains_no_report_data(self):
        t = make_token("pdf", {"rid": "r1", "uid": 5}, 300)
        import base64
        body = base64.urlsafe_b64decode(t.split(".")[0] + "==").decode()
        self.assertNotIn("glucose", body.lower())
        self.assertNotIn("sample", body.lower())


REPORT = {"id": "11111111-aaaa", "created_at": "2026-10-13T00:00:00Z", "status": "completed",
          "glucose": {"value": 95.0, "unit": "mg/dL", "source": "new_test", "measured_at": "2026-10-13T00:00:00Z", "fasting": None},
          "sample_id": "S1", "model_version": "v1", "features": [{"name": s, "value": 1.0} for s in SPECIES],
          "results": {"phyto": [{"enzyme": "EC 1.1.1.267", "enzyme_name": "DXR", "prob": 40.0, "compound": "=cmd()", "pic50": 5.7, "sources": ["x"]}],
                      "food": [{"enzyme": "EC 2.7.13.3", "enzyme_name": "HK", "prob": 20.0, "compound": "Luteolin", "pic50": 5.4, "sources": ["Egg"]}]}}


class Reports(unittest.TestCase):
    def test_pdf_generated(self):
        pdf = report_to_pdf(REPORT)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 2000)

    def test_csv_has_both_kinds_and_neutralises_formulas(self):
        out = reports_to_csv([REPORT])
        self.assertIn("phyto", out); self.assertIn("food", out)
        self.assertIn("'=cmd()", out)
        self.assertNotIn("risk", out.lower())

    def test_snapshot_is_self_contained(self):
        import copy
        r = copy.deepcopy(REPORT); a = reports_to_csv([r]); r["model_version"] = "v2"
        self.assertEqual(a, reports_to_csv([REPORT]))


class GutRiskRemoved(unittest.TestCase):
    def test_no_gut_risk_pipeline_left_in_code(self):
        banned = re.compile(r"\bshap\b|TreeExplainer|risk_percent|top_microbes|model\.json|predictions\"|/latest/|dashboard-data|nearest", re.I)
        for fn in ("app.py", "recommender.py", "requirements.txt", "core/microbiome.py", "core/pdf_report.py"):
            txt = open(os.path.join(ROOT, fn), encoding="utf-8").read()
            hits = [m.group(0) for m in banned.finditer(txt) if fn != "recommender.py" or m.group(0) != "nearest"]
            self.assertEqual(hits, [], fn)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "model.json")))
        for fn in os.listdir(os.path.join(ROOT, "static")):
            txt = open(os.path.join(ROOT, "static", fn), encoding="utf-8").read().lower()
            self.assertNotIn("risk", txt.replace("risk-free", ""), fn)

    def test_recommendation_models_and_lookups_still_present(self):
        for f in ("fbg_input_phyto.xlsx", "fbg_input_food.xlsx", "EC_Phytochemical_pIC50_12_rows_t2d.xlsx", "IC50_Matches_food.xlsx"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, f)), f)
        self.assertIn("xgboost", open(os.path.join(ROOT, "requirements.txt")).read())

    def test_recommender_no_longer_selects_cohort_patient(self):
        src = open(os.path.join(ROOT, "recommender.py"), encoding="utf-8").read()
        self.assertNotIn("argmin", src)
        self.assertNotIn("self.mic[j]", src)


if __name__ == "__main__":
    unittest.main()
