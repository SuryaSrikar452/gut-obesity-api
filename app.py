import os, io, json, shap, pandas as pd, xgboost as xgb
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

UPLOAD_PAGE = """
<html><body style="font-family:sans-serif;text-align:center;margin-top:40px">
<h2>Upload your CSV sample</h2>
<form action="/upload/{device_id}" method="post" enctype="multipart/form-data">
  <input type="file" name="file" accept=".csv" required><br><br>
  <button type="submit">Send</button>
</form>
</body></html>
"""

@app.get("/upload/{device_id}", response_class=HTMLResponse)
def upload_form(device_id: str):
    return UPLOAD_PAGE.format(device_id=device_id)

@app.post("/upload/{device_id}")
async def upload_csv(device_id: str, file: UploadFile):
    df = pd.read_csv(io.BytesIO(await file.read()))
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
        "top_microbes": json.dumps(top10),
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    return JSONResponse({"risk_percent": risk, "top_microbes": top10})

@app.get("/latest/{device_id}")
def latest(device_id: str):
    r = (sb.table("predictions").select("*")
         .eq("device_id", device_id)
         .order("created_at", desc=True).limit(1).execute())
    if not r.data:
        return {"status": "empty"}
    return {"status": "ok", **r.data[0]}

@app.get("/health")
def health():
    return {"status": "ok"}
