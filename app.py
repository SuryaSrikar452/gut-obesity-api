import os, io, json, time, random, requests, smtplib, shap, pandas as pd, xgboost as xgb
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client

# ============================================================
# ENV VARS you must set on Render (Dashboard -> your service -> Environment):
#   SUPABASE_URL, SUPABASE_SERVICE_KEY   (already set from before)
#   SMTP_USER, SMTP_PASS                 (Gmail address + App Password)
# ============================================================

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

model = xgb.XGBClassifier()
model.load_model("model.json")
FEATURES = model.get_booster().feature_names
explainer = shap.TreeExplainer(model)

app = FastAPI()

with open("register_page.html") as f:
    REGISTER_PAGE = f.read()

# ---- rate limiter (unchanged from before) ----
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_SEC = 300
_upload_log = {}

def check_rate_limit(device_id: str):
    now = time.time()
    log = _upload_log.setdefault(device_id, [])
    log[:] = [t for t in log if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(log) >= RATE_LIMIT_MAX:
        return False
    log.append(now)
    return True

SMTP_HOST = "smtp.gmail.com"
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]

def send_otp_email(to_email: str, otp: str):
    msg = MIMEText(f"Your Gut Health Analyzer verification code is: {otp}\n\nThis code expires in 5 minutes.")
    msg["Subject"] = "Your Gut Health Analyzer OTP"
    msg["From"] = SMTP_USER
    msg["To"] = to_email

    # Try port 465 (SSL) first
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=8) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"[SMTP] Sent via port 465 to {to_email}")
        return
    except Exception as e:
        print(f"[SMTP 465 ERROR] {type(e).__name__}: {e}")

    # Fall back to port 587 (STARTTLS)
    try:
        with smtplib.SMTP(SMTP_HOST, 587, timeout=8) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"[SMTP] Sent via port 587 to {to_email}")
        return
    except Exception as e:
        print(f"[SMTP 587 ERROR] {type(e).__name__}: {e}")
        raise

# ============================================================
# Registration website (customer only enters EMAIL, nothing else)
# ============================================================
@app.get("/register", response_class=HTMLResponse)
def register_page():
    return REGISTER_PAGE

@app.post("/register")
async def register(email: str = Form(...)):
    email = email.strip().lower()
    existing = sb.table("users").select("*").eq("email", email).execute()
    if existing.data:
        return JSONResponse(status_code=400, content={
            "error": "This email is already registered."
        })
    sb.table("users").insert({"email": email}).execute()
    return {"status": "ok", "message": "Registered. Power on your device and enter this email to activate it."}

# ============================================================
# Device: one-time claim (binds this specific device_id to the email
# the customer typed, only works if that email has no device bound yet)
# ============================================================
@app.post("/claim-device")
async def claim_device(device_id: str = Form(...), email: str = Form(...)):
    email = email.strip().lower()
    r = sb.table("users").select("*").eq("email", email).execute()
    if not r.data:
        return JSONResponse(status_code=404, content={"error": "Email not registered. Please register on our website first."})

    row = r.data[0]
    if row["claimed"] and row["device_id"] != device_id:
        return JSONResponse(status_code=403, content={"error": "This email is already linked to a different device."})

    already_claimed_elsewhere = sb.table("users").select("*").eq("device_id", device_id).eq("claimed", True).execute()
    if already_claimed_elsewhere.data and already_claimed_elsewhere.data[0]["email"] != email:
        return JSONResponse(status_code=403, content={"error": "This device is already linked to a different email."})

    sb.table("users").update({"device_id": device_id, "claimed": True}).eq("email", email).execute()
    return {"status": "ok"}

# ============================================================
# Device: every-boot login (OTP)
# ============================================================
@app.post("/request-otp")
async def request_otp(device_id: str = Form(...), email: str = Form(...)):
    email = email.strip().lower()
    r = sb.table("users").select("*").eq("email", email).eq("device_id", device_id).eq("claimed", True).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Email/device do not match our records."})

    otp = str(random.randint(100000, 999999))
    expires = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    sb.table("users").update({"otp_code": otp, "otp_expires_at": expires}).eq("email", email).execute()

    try:
        send_otp_email(email, otp)
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Could not send OTP email. Try again."})

    return {"status": "ok"}

@app.post("/verify-otp")
async def verify_otp(device_id: str = Form(...), otp: str = Form(...)):
    r = sb.table("users").select("*").eq("device_id", device_id).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})

    row = r.data[0]
    if not row["otp_code"] or row["otp_code"] != otp:
        return JSONResponse(status_code=403, content={"error": "Incorrect OTP."})
    if datetime.fromisoformat(row["otp_expires_at"]) < datetime.utcnow():
        return JSONResponse(status_code=403, content={"error": "OTP expired. Request a new one."})

    sb.table("users").update({"otp_code": None, "otp_expires_at": None}).eq("device_id", device_id).execute()
    return {"status": "ok"}

# ============================================================
# Prediction pipeline -- triggered by the DEVICE after it reads a
# CSV over USB (device POSTs the file bytes here).
# ============================================================
@app.post("/predict/{device_id}")
async def predict(device_id: str, file: UploadFile):
    if not check_rate_limit(device_id):
        return JSONResponse(status_code=429, content={
            "error": "Too many uploads. Please wait a few minutes and try again."
        })

    try:
        df = pd.read_csv(io.BytesIO(await file.read()))
    except Exception:
        return JSONResponse(status_code=400, content={"error": "That file couldn't be read. Please check it's a valid CSV."})

    if df.empty:
        return JSONResponse(status_code=400, content={"error": "The CSV file is empty."})

    missing = [c for c in FEATURES if c not in df.columns]
    if missing:
        return JSONResponse(status_code=400, content={
            "error": f"CSV is missing {len(missing)} required column(s). Please use the correct sample template."
        })

    df = df[FEATURES]
    X = df.values

    risk = float(model.predict_proba(X)[0][1]) * 100
    shap_vals = explainer.shap_values(X)[0]
    top10 = sorted(zip(df.columns, shap_vals), key=lambda x: abs(x[1]), reverse=True)[:10]
    top10 = [{"microbe": m, "impact": float(v)} for m, v in top10]

    sb.table("predictions").insert({
        "device_id": device_id,
        "risk_percent": risk,
        "top_microbes": top10,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    return {"status": "ok", "risk_percent": risk, "top_microbes": top10}

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