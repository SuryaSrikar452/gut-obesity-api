import os, io, time, random, secrets, hashlib, requests, shap, pandas as pd, xgboost as xgb
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client

# ============================================================
# ENV VARS you must set on Render (Dashboard -> your service -> Environment):
#   SUPABASE_URL, SUPABASE_SERVICE_KEY   (already set from before)
#   BREVO_API_KEY                        (already set from before)
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

with open("upload_page.html") as f:
    UPLOAD_PAGE = f.read()

SESSION_EXPIRED_PAGE = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session expired</title></head><body style="font-family:sans-serif;text-align:center;padding:60px 20px;">
<h2>QR expired or invalid</h2><p>Please generate a new QR code on the device and scan again.</p>
</body></html>"""

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

BREVO_API_KEY = os.environ["BREVO_API_KEY"]
SENDER_EMAIL = "guthealdevice@gmail.com"

def send_otp_email(to_email: str, otp: str):
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender": {"email": SENDER_EMAIL, "name": "Gut Health Analyzer"},
            "to": [{"email": to_email}],
            "subject": "Your Gut Health Analyzer OTP",
            "textContent": f"Your Gut Health Analyzer verification code is: {otp}\n\nThis code expires in 15 minutes.",
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        print(f"[Brevo ERROR] status={resp.status_code} body={resp.text}")
    resp.raise_for_status()

# ============================================================
# Registration website -- UNCHANGED
# ============================================================
@app.get("/register", response_class=HTMLResponse)
def register_page():
    return REGISTER_PAGE

@app.post("/register")
async def register(email: str = Form(...)):
    email = email.strip().lower()
    existing = sb.table("users").select("*").eq("email", email).execute()
    if existing.data:
        return JSONResponse(status_code=400, content={"error": "This email is already registered."})
    sb.table("users").insert({"email": email}).execute()
    return {"status": "ok", "message": "Registered. Power on your device and enter this email to activate it."}

# ============================================================
# Device claim / OTP login -- UNCHANGED
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

@app.post("/request-otp")
async def request_otp(device_id: str = Form(...), email: str = Form(...)):
    email = email.strip().lower()
    r = sb.table("users").select("*").eq("email", email).eq("device_id", device_id).eq("claimed", True).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Email/device do not match our records."})

    otp = str(random.randint(100000, 999999))
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
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
    if datetime.fromisoformat(row["otp_expires_at"]) < datetime.now(timezone.utc):
        return JSONResponse(status_code=403, content={"error": "OTP expired. Request a new one."})

    sb.table("users").update({"otp_code": None, "otp_expires_at": None}).eq("device_id", device_id).execute()
    return {"status": "ok"}

# ============================================================
# NEW: secure, single-use, short-lived upload sessions
# ============================================================
SESSION_TTL_SECONDS = 120  # 2 minutes

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

@app.post("/upload-session")
async def create_upload_session(request: Request, device_id: str = Form(...)):
    # device_id must belong to a claimed device -- same trust model already
    # used elsewhere in this app (device_id itself is the device's identity,
    # established once via /claim-device + OTP).
    r = sb.table("users").select("*").eq("device_id", device_id).eq("claimed", True).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    user_row = r.data[0]

    token = secrets.token_urlsafe(32)
    device_code = f"{secrets.randbelow(1000000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)

    sb.table("upload_sessions").insert({
        "user_id": user_row["id"],
        "device_id": device_id,
        "token_hash": _hash(token),
        "device_code_hash": _hash(device_code),
        "expires_at": expires_at.isoformat(),
    }).execute()

    base = str(request.base_url).rstrip("/")
    return {
        "status": "ok",
        "upload_url": f"{base}/upload?t={token}",
        "device_code": device_code,
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": SESSION_TTL_SECONDS,
    }

def _get_valid_session(token: str):
    r = sb.table("upload_sessions").select("*").eq("token_hash", _hash(token)).execute()
    if not r.data:
        return None
    session = r.data[0]
    if session["revoked"] or session["used_at"]:
        return None
    if datetime.fromisoformat(session["expires_at"]) < datetime.now(timezone.utc):
        return None
    return session

@app.get("/upload", response_class=HTMLResponse)
def upload_form(t: str):
    session = _get_valid_session(t)
    if not session:
        return HTMLResponse(SESSION_EXPIRED_PAGE, status_code=403)
    return UPLOAD_PAGE.replace("__TOKEN__", t)

@app.post("/upload")
async def upload_csv(t: str = Form(...), device_code: str = Form(...), file: UploadFile = None):
    session = _get_valid_session(t)
    if not session:
        return JSONResponse(status_code=403, content={"error": "QR expired or invalid. Generate a new QR."})

    if _hash(device_code.strip()) != session.get("device_code_hash"):
        return JSONResponse(status_code=403, content={"error": "Incorrect code shown on device."})

    device_id = session["device_id"]  # derived from session, never trusted from client

    if not check_rate_limit(device_id):
        return JSONResponse(status_code=429, content={"error": "Too many uploads. Please wait a few minutes and try again."})

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
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    # single-use: revoke immediately after a successful upload
    sb.table("upload_sessions").update({
        "used_at": datetime.now(timezone.utc).isoformat(),
        "revoked": True,
    }).eq("id", session["id"]).execute()

    return {"status": "ok", "risk_percent": risk, "top_microbes": top10}

# ============================================================
# UNCHANGED
# ============================================================
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