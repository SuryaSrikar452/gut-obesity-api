import os, io, json, time, shap, pandas as pd, xgboost as xgb
from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client
from datetime import datetime

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

model = xgb.XGBClassifier()
model.load_model("model.json")                # native XGBoost dump
FEATURES = model.get_booster().feature_names   # exact column order the model expects
explainer = shap.TreeExplainer(model)

app = FastAPI()

with open("upload_page.html") as f:
    UPLOAD_PAGE = f.read()

# ---- simple in-memory rate limiter: max 5 uploads per 5 min per device ----
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_SEC = 300
_upload_log = {}  # device_id -> list of timestamps

def check_rate_limit(device_id: str):
    now = time.time()
    log = _upload_log.setdefault(device_id, [])
    log[:] = [t for t in log if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(log) >= RATE_LIMIT_MAX:
        return False
    log.append(now)
    return True

@app.get("/upload/{device_id}", response_class=HTMLResponse)
def upload_form(device_id: str):
    return UPLOAD_PAGE.replace("__DEVICE_ID__", device_id)

@app.post("/upload/{device_id}")
async def upload_csv(device_id: str, file: UploadFile):
    if not check_rate_limit(device_id):
        return JSONResponse(status_code=429, content={
            "error": "Too many uploads. Please wait a few minutes and try again."
        })

    if not file.filename.lower().endswith(".csv"):
        return JSONResponse(status_code=400, content={
            "error": "Please upload a .csv file."
        })

    try:
        df = pd.read_csv(io.BytesIO(await file.read()))
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": "That file couldn't be read. Please check it's a valid CSV."
        })

    if df.empty:
        return JSONResponse(status_code=400, content={
            "error": "The CSV file is empty."
        })

    missing = [c for c in FEATURES if c not in df.columns]
    if missing:
        return JSONResponse(status_code=400, content={
            "error": f"CSV is missing {len(missing)} required column(s). "
                     f"Please use the correct sample template."
        })

    df = df[FEATURES]          # reorder/select columns to match model exactly
    X = df.values

    risk = float(model.predict_proba(X)[0][1]) * 100
    shap_vals = explainer.shap_values(X)[0]
    top10 = sorted(
        zip(df.columns, shap_vals), key=lambda x: abs(x[1]), reverse=True
    )[:10]
    top10 = [{"microbe": m, "impact": float(v)} for m, v in top10]

    sb.table("predictions").insert({
        "device_id": device_id,
        "risk_percent": risk,
        "top_microbes": top10,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    return JSONResponse({"status": "ok"})

@app.get("/latest/{device_id}")
def latest(device_id: str):
    r = (sb.table("predictions").select("*")
         .eq("device_id", device_id)
         .order("created_at", desc=True).limit(1).execute())
    if not r.data:
        return {"status": "empty"}
    return {"status": "ok", **r.data[0]}

@app.get("/history/{device_id}")
def history(device_id: str, limit: int = 3):
    r = (sb.table("predictions").select("risk_percent,created_at")
         .eq("device_id", device_id)
         .order("created_at", desc=True).limit(limit).execute())
    return {"status": "ok", "history": r.data}

@app.get("/health")
def health():
    return {"status": "ok"}