"""
Phytochemical + food recommender (from hba1c_input_phyto_output_model).
Models are re-fitted at startup from the xlsx cohort files with the same
parameters/seed as the original scripts, so no .joblib / pickle version
problems on Render. Input: age, height_cm, weight_kg, fbg.
"""
import os
import numpy as np
import pandas as pd
from xgboost import XGBClassifier

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recommender_data")
CLIN = ["Age", "BMI", "FBG_mgdL"]
IDS = ["Sample_ID", "Table_S1_Sample_ID"]
DROP = ["HbA1c"]
TOP_K = 3
INVALID_IC50 = 2147483647

XGB_PARAMS = dict(
    n_estimators=150, max_depth=1, learning_rate=0.03, subsample=0.7,
    colsample_bytree=0.5, min_child_weight=8, reg_lambda=10.0,
    tree_method="hist", objective="multi:softprob", random_state=42,
)


def _clr(M):
    M = np.asarray(M, float)
    lg = np.log(M + 1e-3)
    return lg - lg.mean() if M.ndim == 1 else lg - lg.mean(axis=1, keepdims=True)


def bmi_category(b):
    return "Underweight" if b < 18.5 else "Normal" if b < 25 else "Overweight" if b < 30 else "Obese"


def fbg_category(f):
    return "Normal" if f < 100 else "Prediabetes range" if f < 126 else "Diabetes range"


def _short(text, n=40):
    t = str(text).split(" / ")[0].strip()
    return t if len(t) <= n else t[: n - 3] + "..."


def _parse_parts(text):
    out = {}
    for chunk in str(text).split(";"):
        if ":" in chunk:
            p, parts = chunk.split(":", 1)
            out[p.strip()] = parts.strip()
    return out


class _Kind:
    def __init__(self, data_file, db_file, kind):
        self.kind = kind
        df = pd.read_excel(os.path.join(DATA, data_file))
        self.targets = [c for c in df.columns if c.startswith("EC_")]
        self.microbes = [c for c in df.columns if c not in IDS + DROP + CLIN + self.targets]
        self.clin = df[CLIN].values.astype(float)
        self.mic = df[self.microbes].values.astype(float)
        self.c_mu = self.clin.mean(axis=0)
        self.c_sd = self.clin.std(axis=0, ddof=1)

        X = np.hstack([self.clin, _clr(self.mic)])
        mu, sd = df[self.targets].mean(), df[self.targets].std()
        Y = ((df[self.targets] - mu) / sd).values
        self.model = XGBClassifier(**XGB_PARAMS).fit(X, Y.argmax(axis=1))

        db = pd.read_excel(os.path.join(DATA, db_file), sheet_name=0)
        if kind == "phyto":
            db["EC No"] = db["EC No"].astype(str).str.strip()
            self.ec_col, self.pic_col = "EC No", "pIC50"
        else:
            db["EC_Number"] = db["EC_Number"].astype(str).str.strip()
            db = db[db["IC50_nM"] != INVALID_IC50]
            self.ec_col, self.pic_col = "EC_Number", "pic50"
        self.db = db.reset_index(drop=True)

    def _best(self, ec_label):
        ec = ec_label.replace("EC ", "").strip()
        d = self.db[self.db[self.ec_col] == ec].sort_values(self.pic_col, ascending=False)
        return None if d.empty else d.iloc[0]

    def predict(self, age, bmi, fbg):
        q = np.array([age, bmi, fbg], float)
        zq = (q - self.c_mu) / self.c_sd
        zr = (self.clin - self.c_mu) / self.c_sd
        j = int(np.argmin(np.sqrt(((zr - zq) ** 2).sum(axis=1))))
        x = np.concatenate([q, _clr(self.mic[j])]).reshape(1, -1)
        probs = np.zeros(len(self.targets))
        probs[self.model.classes_] = self.model.predict_proba(x)[0]
        order = np.argsort(-probs)[:TOP_K]

        items = []
        for k in order:
            ec = self.targets[k].replace("_Abundance", "").replace("EC_", "EC ")
            row = self._best(ec)
            item = {"enzyme": ec, "prob": round(float(probs[k]) * 100, 1)}
            if row is not None:
                if self.kind == "phyto":
                    plants = [p.strip() for p in str(row["Plant"]).split(",") if p.strip()]
                    item.update(enzyme_name=_short(row["Enzyme Name"]), compound=str(row["Phytochemical"]),
                                pic50=round(float(row["pIC50"]), 2), sources=plants[:3])
                else:
                    src = [s.strip() for s in str(row["food source"]).split(";") if s.strip()]
                    item.update(enzyme_name=_short(row["Enzyme_Definition"]), compound=str(row["Food_Compound"]),
                                pic50=round(float(row["pic50"]), 2), sources=src[:4])
            items.append(item)
        return items


_PHYTO = _Kind("fbg_input_phyto.xlsx", "EC_Phytochemical_pIC50_12_rows_t2d.xlsx", "phyto")
_FOOD = _Kind("fbg_input_food.xlsx", "IC50_Matches_food.xlsx", "food")


def recommend_all(age, height_cm, weight_kg, fbg):
    bmi = weight_kg / ((height_cm / 100.0) ** 2)
    return {
        "bmi": round(bmi, 1),
        "bmi_category": bmi_category(bmi),
        "fbg_category": fbg_category(fbg),
        "phyto": _PHYTO.predict(age, bmi, fbg),
        "food": _FOOD.predict(age, bmi, fbg),
    }
