import os, io, time, base64, secrets, hashlib, requests, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, Request, Header, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from supabase import create_client
from recommender import recommend_from_microbiome, recommend_from_nearest_cohort, MODEL_VERSION, get_food_source_for_enzyme, get_plant_source_for_enzyme
from core.microbiome import parse_microbiome_csv, CsvValidationError, SPECIES
from core.tokens import make_token, read_token, TokenError
from core.pdf_report import report_to_pdf, reports_to_csv

# ============================================================
# ENV VARS (see .env.example): SUPABASE_URL, SUPABASE_SERVICE_KEY, BREVO_API_KEY,
# TOKEN_SECRET (>=32 random chars), PUBLIC_BASE_URL (https public URL, used in QR codes)
# ============================================================

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Gut Health Analyzer API")
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:  # pages are served same-origin; only set this if you host the frontend elsewhere
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Idempotency-Key", "Content-Type"])

# Root endpoint so opening the Render service URL does not show "Not Found".
# This does not change any existing API routes used by the ESP32 or upload page.
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Gut Health Analyzer API",
        "message": "Render service is running."
    }

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

# ---- OTP attempt limiter (6-digit code must not be brute-forceable) ----
OTP_MAX_ATTEMPTS = 5
OTP_ATTEMPT_WINDOW_SEC = 900
_otp_attempts = {}

def otp_attempts_exceeded(device_id: str) -> bool:
    now = time.time()
    log = _otp_attempts.setdefault(device_id, [])
    log[:] = [t for t in log if now - t < OTP_ATTEMPT_WINDOW_SEC]
    return len(log) >= OTP_MAX_ATTEMPTS

# ---- OTP *request* limiter (stops email spam / abuse of /request-otp) ----
OTP_REQ_MAX = 3
OTP_REQ_WINDOW_SEC = 600
_otp_requests = {}

def otp_request_allowed(key: str) -> bool:
    now = time.time()
    log = _otp_requests.setdefault(key, [])
    log[:] = [t for t in log if now - t < OTP_REQ_WINDOW_SEC]
    if len(log) >= OTP_REQ_MAX:
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
# Device claim: a device is linked to an email ONLY after the emailed OTP
# is verified. /request-otp emails a code (and records the device as pending),
# /verify-otp checks the code and sets claimed=True.
# ============================================================
def _start_otp(device_id: str, email: str):
    """Validate the pairing, store an OTP and email it. Returns a JSONResponse on failure, else None."""
    email = email.strip().lower()
    r = sb.table("users").select("*").eq("email", email).execute()
    if not r.data:
        return JSONResponse(status_code=404, content={"error": "Email not registered. Please register on our website first."})
    row = r.data[0]

    if row["claimed"] and row["device_id"] != device_id:
        return JSONResponse(status_code=403, content={"error": "This email is already linked to a different device."})

    other = sb.table("users").select("email").eq("device_id", device_id).eq("claimed", True).execute()
    if other.data and other.data[0]["email"] != email:
        return JSONResponse(status_code=403, content={"error": "This device is already linked to a different email."})

    if not (otp_request_allowed("dev:" + device_id) and otp_request_allowed("mail:" + email)):
        return JSONResponse(status_code=429, content={"error": "Too many code requests. Wait a few minutes and try again."})

    otp = f"{secrets.randbelow(1000000):06d}"  # CSPRNG, zero-padded
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    # Record the device as pending (claimed stays as-is); linking happens in /verify-otp.
    update = {"otp_code": otp, "otp_expires_at": expires}
    if not row["claimed"]:
        update["device_id"] = device_id
    sb.table("users").update(update).eq("email", email).execute()

    try:
        send_otp_email(email, otp)
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Could not send OTP email. Try again."})
    return None

@app.post("/request-otp")
async def request_otp(device_id: str = Form(...), email: str = Form(...)):
    err = _start_otp(device_id, email)
    return err if err is not None else {"status": "ok"}

@app.post("/claim-device")
async def claim_device(device_id: str = Form(...), email: str = Form(...)):
    # Deprecated: no longer links anything by itself (that allowed anyone who knew a
    # registered email to claim it). It now just emails the verification code.
    err = _start_otp(device_id, email)
    return err if err is not None else {"status": "ok", "next": "verify-otp"}

@app.post("/verify-otp")
async def verify_otp(device_id: str = Form(...), otp: str = Form(...)):
    if otp_attempts_exceeded(device_id):
        return JSONResponse(status_code=429, content={"error": "Too many attempts. Request a new OTP later."})

    r = sb.table("users").select("*").eq("device_id", device_id).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Device not recognized. Request a code first."})

    otp = otp.strip()
    row = next((x for x in r.data
                if x.get("otp_code") and secrets.compare_digest(str(x["otp_code"]), otp)), None)
    if row is None:
        _otp_attempts.setdefault(device_id, []).append(time.time())
        return JSONResponse(status_code=403, content={"error": "Incorrect OTP."})

    expires = row.get("otp_expires_at")
    if not expires or datetime.fromisoformat(expires) < datetime.now(timezone.utc):
        return JSONResponse(status_code=403, content={"error": "OTP expired. Request a new one."})

    # Code proven: this device is now linked to the email.
    sb.table("users").update({"claimed": True, "device_id": device_id,
                              "otp_code": None, "otp_expires_at": None}).eq("id", row["id"]).execute()
    _otp_attempts.pop(device_id, None)
    return {"status": "ok"}

# ============================================================
# Web session login (reuses the registered email + emailed OTP; no passwords)
# ============================================================
SESSION_TTL = 8 * 3600
DOWNLOAD_LINK_TTL = 300  # QR PDF links live 5 minutes


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@app.post("/auth/request-otp")
async def auth_request_otp(email: str = Form(...)):
    email = email.strip().lower()
    generic = {"status": "ok", "message": "If this email is registered and linked to a device, a code was sent."}
    r = sb.table("users").select("*").eq("email", email).eq("claimed", True).execute()
    if not r.data:
        return generic  # do not reveal which emails exist
    if not otp_request_allowed("web:" + email):
        return JSONResponse(status_code=429, content={"error": "Too many code requests. Wait a few minutes."})
    otp = f"{secrets.randbelow(1000000):06d}"
    sb.table("users").update({
        "otp_code": "w:" + _h(email + otp),  # stored hashed; never matches the device OTP check
        "otp_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }).eq("id", r.data[0]["id"]).execute()
    try:
        send_otp_email(email, otp)
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Could not send the code. Try again."})
    return generic


@app.post("/auth/verify-otp")
async def auth_verify_otp(email: str = Form(...), otp: str = Form(...)):
    email = email.strip().lower()
    if otp_attempts_exceeded("web:" + email):
        return JSONResponse(status_code=429, content={"error": "Too many attempts. Request a new code later."})
    r = sb.table("users").select("*").eq("email", email).eq("claimed", True).execute()
    row = r.data[0] if r.data else None
    stored = (row or {}).get("otp_code") or ""
    ok = bool(row) and stored.startswith("w:") and secrets.compare_digest(stored, "w:" + _h(email + otp.strip()))
    if not ok:
        _otp_attempts.setdefault("web:" + email, []).append(time.time())
        return JSONResponse(status_code=403, content={"error": "Incorrect or expired code."})
    exp = row.get("otp_expires_at")
    if not exp or datetime.fromisoformat(exp) < datetime.now(timezone.utc):
        return JSONResponse(status_code=403, content={"error": "Incorrect or expired code."})
    sb.table("users").update({"otp_code": None, "otp_expires_at": None}).eq("id", row["id"]).execute()
    _otp_attempts.pop("web:" + email, None)
    token = make_token("session", {"uid": row["id"], "dev": row["device_id"]}, SESSION_TTL)
    return {"status": "ok", "token": token, "expires_in": SESSION_TTL}


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Login required.")
    try:
        c = read_token(authorization[7:].strip(), "session")
    except TokenError as e:
        raise HTTPException(401, "Session expired. Log in again." if str(e) == "expired" else "Invalid session.")
    r = sb.table("users").select("id,email,device_id,claimed").eq("id", c["uid"]).execute()
    if not r.data or not r.data[0]["claimed"] or r.data[0]["device_id"] != c["dev"]:
        raise HTTPException(401, "Invalid session.")
    return r.data[0]


# ============================================================
# Glucose readings
# ============================================================
def _glucose_ok(v: float) -> bool:
    return 40 <= v <= 600


@app.post("/device/glucose", status_code=201)
async def device_post_glucose(device_id: str = Form(...), value: float = Form(...),
                              fasting: Optional[bool] = Form(None), client_ref: Optional[str] = Form(None)):
    """ESP32 posts a finished measurement. Retries with the same client_ref return the same row."""
    r = sb.table("users").select("id").eq("device_id", device_id).eq("claimed", True).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    if not _glucose_ok(value):
        return JSONResponse(status_code=400, content={"error": "Glucose must be between 40 and 600 mg/dL."})
    uid = r.data[0]["id"]
    if client_ref:
        ex = sb.table("glucose_readings").select("*").eq("user_id", uid).eq("client_ref", client_ref).execute()
        if ex.data:
            return JSONResponse(status_code=200, content=_glucose_out(ex.data[0]))
    row = sb.table("glucose_readings").insert({
        "user_id": uid, "device_id": device_id, "value": round(float(value), 1), "unit": "mg/dL",
        "fasting": fasting, "client_ref": client_ref,
        "measured_at": datetime.now(timezone.utc).isoformat()}).execute().data[0]
    return _glucose_out(row)


def _glucose_out(r):
    return {"id": r["id"], "value": r["value"], "unit": r["unit"], "fasting": r.get("fasting"),
            "measured_at": r["measured_at"]}


@app.get("/glucose")
def list_glucose(user=Depends(current_user), date_from: Optional[str] = None, date_to: Optional[str] = None,
                 limit: int = Query(200, ge=1, le=1000)):
    q = sb.table("glucose_readings").select("*", count="exact").eq("user_id", user["id"])
    if date_from: q = q.gte("measured_at", date_from)
    if date_to: q = q.lte("measured_at", date_to)
    res = q.order("measured_at", desc=True).limit(limit).execute()
    items = [_glucose_out(r) for r in res.data]
    # link each reading to the reports that used it
    ids = [i["id"] for i in items]
    links = {}
    if ids:
        for rep in (sb.table("analysis_reports").select("id,glucose_reading_id,status")
                    .eq("user_id", user["id"]).in_("glucose_reading_id", ids).execute().data):
            links.setdefault(rep["glucose_reading_id"], []).append({"id": rep["id"], "status": rep["status"]})
    for i in items:
        i["reports"] = links.get(i["id"], [])
    return {"total": res.count if res.count is not None else len(items), "items": items}


@app.get("/glucose/{reading_id}")
def get_glucose(reading_id: str, user=Depends(current_user)):
    r = _own_reading(reading_id, user)
    return _glucose_out(r)


@app.patch("/glucose/{reading_id}/fasting")
async def set_fasting(reading_id: str, fasting: bool = Form(...), user=Depends(current_user)):
    _own_reading(reading_id, user)
    sb.table("glucose_readings").update({"fasting": fasting}).eq("id", reading_id).eq("user_id", user["id"]).execute()
    return {"status": "ok"}


def _valid_uuid(v: str) -> bool:
    try:
        uuid.UUID(v); return True
    except ValueError:
        return False


def _own_reading(reading_id: str, user) -> dict:
    if not _valid_uuid(reading_id):
        raise HTTPException(404, "Reading not found.")
    r = sb.table("glucose_readings").select("*").eq("id", reading_id).eq("user_id", user["id"]).execute()
    if not r.data:  # same answer for "missing" and "someone else's"
        raise HTTPException(404, "Reading not found.")
    return r.data[0]


# ============================================================
# Analysis: user's own microbiome CSV -> both recommendation models
# ============================================================
def _run_analysis(user, reading, source, age, height_cm, weight_kg, raw_csv, sample_id, idem_key):
    parsed = parse_microbiome_csv(raw_csv)
    samples = parsed["samples"]
    if len(samples) > 1 and not sample_id:
        raise HTTPException(422, detail={"error": "The CSV has several samples. Choose one.",
                                         "sample_ids": [x["sample_id"] for x in samples]})
    chosen = next((x for x in samples if sample_id in (None, "", x["sample_id"])), None)
    if chosen is None:
        raise HTTPException(422, detail={"error": "Sample not found in CSV.",
                                         "sample_ids": [x["sample_id"] for x in samples]})
    features = [{"name": n, "value": v} for n, v in zip(SPECIES, chosen["abundances"])]
    now = datetime.now(timezone.utc).isoformat()
    rid = str(uuid.uuid4())
    sb.table("analysis_reports").insert({
        "id": rid, "user_id": user["id"], "device_id": user["device_id"], "status": "processing",
        "glucose_reading_id": reading["id"], "glucose_value": reading["value"], "glucose_unit": reading["unit"],
        "glucose_source": source, "glucose_measured_at": reading["measured_at"], "fasting": reading.get("fasting"),
        "age": age, "height_cm": height_cm, "weight_kg": weight_kg,
        "sample_id": chosen["sample_id"], "csv_sha256": hashlib.sha256(raw_csv).hexdigest(),
        "features": features, "preprocessing": "CLR, pseudocount=1e-3, percent relative abundance",
        "model_version": MODEL_VERSION, "idempotency_key": idem_key, "created_at": now}).execute()
    try:
        path = f"{user['id']}/{rid}.csv"
        sb.storage.from_("microbiome-csv").upload(path, raw_csv, {"content-type": "text/csv"})
        sb.table("analysis_reports").update({"csv_path": path}).eq("id", rid).execute()
        results = recommend_from_microbiome(age, height_cm, weight_kg, reading["value"], chosen["abundances"])
    except Exception as e:  # never leave a half-finished report looking complete
        print(f"[analysis {rid}] failed: {type(e).__name__}")
        sb.table("analysis_reports").update({"status": "failed", "error": "Processing failed."}).eq("id", rid).execute()
        raise HTTPException(500, "Analysis failed. No report was created.")
    sb.table("analysis_reports").update({
        "status": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
        "results": {"phyto": results["phyto"], "food": results["food"],
                    "bmi": results["bmi"], "bmi_category": results["bmi_category"],
                    "fbg_category": results["fbg_category"]}}).eq("id", rid).execute()
    return rid, parsed["warnings"]


def _run_cohort_analysis(user, reading, source, age, height_cm, weight_kg, idem_key=None):
    results = recommend_from_nearest_cohort(age, height_cm, weight_kg, reading["value"])
    matched_sample = results.get("matched_cohort_sample", "Cohort match")
    matched_abundances = results.get("abundances", [])
    features = [{"name": n, "value": v} for n, v in zip(SPECIES, matched_abundances)]
    now = datetime.now(timezone.utc).isoformat()
    rid = str(uuid.uuid4())
    dev_id = user.get("device_id")
    if not dev_id:
        ur = sb.table("users").select("device_id").eq("id", user["id"]).execute()
        if ur.data and ur.data[0].get("device_id"):
            dev_id = ur.data[0]["device_id"]
    if not dev_id:
        dev_id = "device"
    sb.table("analysis_reports").insert({
        "id": rid, "user_id": user["id"], "device_id": dev_id, "status": "completed",
        "glucose_reading_id": reading["id"], "glucose_value": reading["value"], "glucose_unit": reading.get("unit", "mg/dL"),
        "glucose_source": source, "glucose_measured_at": reading.get("measured_at"), "fasting": reading.get("fasting"),
        "age": age, "height_cm": height_cm, "weight_kg": weight_kg,
        "sample_id": matched_sample, "csv_path": f"cohort:{matched_sample}",
        "features": features, "preprocessing": f"Nearest cohort patient ({matched_sample}), CLR pseudocount=1e-3",
        "model_version": MODEL_VERSION, "idempotency_key": idem_key,
        "created_at": now, "completed_at": now,
        "results": {
            "phyto": results["phyto"], "food": results["food"],
            "bmi": results["bmi"], "bmi_category": results["bmi_category"],
            "fbg_category": results["fbg_category"]
        }
    }).execute()
    return rid, matched_sample



@app.post("/analyses", status_code=201)
async def create_analysis(
    glucose_reading_id: str = Form(...), glucose_source: str = Form(...),
    age: int = Form(..., ge=1, le=120), height_cm: int = Form(..., ge=100, le=250),
    weight_kg: int = Form(..., ge=20, le=300), sample_id: Optional[str] = Form(None),
    file: UploadFile = File(...), idempotency_key: Optional[str] = Header(None),
    user=Depends(current_user),
):
    if glucose_source not in ("previous", "new_test"):
        raise HTTPException(400, "glucose_source must be 'previous' or 'new_test'.")
    reading = _own_reading(glucose_reading_id, user)
    if idempotency_key:
        ex = (sb.table("analysis_reports").select("id").eq("user_id", user["id"])
              .eq("idempotency_key", idempotency_key).execute())
        if ex.data:
            return {"status": "ok", "report_id": ex.data[0]["id"], "duplicate": True}
    if not check_rate_limit("an:" + str(user["id"])):
        raise HTTPException(429, "Too many analyses. Wait a few minutes.")
    raw = await file.read(2 * 1024 * 1024 + 1)
    try:
        rid, warnings = await run_in_threadpool(
            _run_analysis, user, reading, glucose_source, age, height_cm, weight_kg, raw, sample_id, idempotency_key)
    except CsvValidationError as e:
        raise HTTPException(400, detail={"error": str(e), "details": e.details})
    return {"status": "ok", "report_id": rid, "warnings": warnings}


@app.post("/csv/inspect")
async def inspect_csv(file: UploadFile = File(...), user=Depends(current_user)):
    """Validate without storing; lets the UI list samples and show errors before running."""
    raw = await file.read(2 * 1024 * 1024 + 1)
    try:
        p = parse_microbiome_csv(raw)
    except CsvValidationError as e:
        raise HTTPException(400, detail={"error": str(e), "details": e.details})
    return {"sample_ids": [s["sample_id"] for s in p["samples"]], "warnings": p["warnings"],
            "ignored_columns": p["extra_columns"]}


# ============================================================
# Reports
# ============================================================
def _report_out(r, full=True):
    out = {"id": r["id"], "created_at": r["created_at"], "status": r["status"],
           "glucose": {"value": r.get("glucose_value"), "unit": r.get("glucose_unit"),
                       "source": r.get("glucose_source"), "measured_at": r.get("glucose_measured_at"),
                       "fasting": r.get("fasting"), "reading_id": r.get("glucose_reading_id")},
           "sample_id": r.get("sample_id"), "model_version": r.get("model_version")}
    if full:
        out.update({"features": r.get("features") or [], "results": r.get("results"),
                    "preprocessing": r.get("preprocessing"), "error": r.get("error"),
                    "completed_at": r.get("completed_at")})
    return out


def _own_report(report_id: str, user) -> dict:
    if not _valid_uuid(report_id):
        raise HTTPException(404, "Report not found.")
    r = sb.table("analysis_reports").select("*").eq("id", report_id).eq("user_id", user["id"]).execute()
    if not r.data:
        raise HTTPException(404, "Report not found.")
    return r.data[0]


@app.get("/reports")
def list_reports(user=Depends(current_user), limit: int = Query(100, ge=1, le=500)):
    rows = (sb.table("analysis_reports").select("*").eq("user_id", user["id"])
            .order("created_at", desc=True).limit(limit).execute().data)
    return {"items": [_report_out(r, full=False) for r in rows]}


# NOTE: declared before /reports/{report_id} so "export.csv" is not treated as an id
@app.get("/reports/export.csv")
def export_all(user=Depends(current_user)):
    rows = (sb.table("analysis_reports").select("*").eq("user_id", user["id"])
            .eq("status", "completed").order("created_at", desc=True).execute().data)
    return Response(reports_to_csv([_report_out(r) for r in rows]), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="gut-reports.csv"'})


@app.get("/reports/{report_id}")
def get_report(report_id: str, user=Depends(current_user)):
    return _report_out(_own_report(report_id, user))


def _pdf_response(row) -> Response:
    if row["status"] != "completed":
        raise HTTPException(409, "Report is not completed.")
    pdf = report_to_pdf(_report_out(row))
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="gut-report-{row["id"][:8]}.pdf"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/reports/{report_id}/pdf")
def report_pdf(report_id: str, user=Depends(current_user)):
    return _pdf_response(_own_report(report_id, user))


@app.get("/reports/{report_id}/csv")
def report_csv(report_id: str, user=Depends(current_user)):
    row = _own_report(report_id, user)
    return Response(reports_to_csv([_report_out(row)]), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="gut-report-{row["id"][:8]}.csv"'})


def _public_base(request: Request) -> str:
    base = (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    if base.startswith("http://") and not any(h in base for h in ("localhost", "127.0.0.1")):
        base = "https://" + base[len("http://"):]
    private = ("localhost", "127.0.0.1", "192.168.", "10.", "172.16.", "0.0.0.0")
    host = base.split("://", 1)[-1]
    if os.environ.get("APP_ENV") == "production" and host.startswith(private):
        raise HTTPException(500, "PUBLIC_BASE_URL must be a public https URL in production.")
    return base


@app.post("/reports/{report_id}/download-link")
def create_download_link(report_id: str, request: Request, user=Depends(current_user)):
    row = _own_report(report_id, user)
    if row["status"] != "completed":
        raise HTTPException(409, "Report is not completed.")
    exp = datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_LINK_TTL)
    token = make_token("pdf", {"rid": row["id"], "uid": user["id"]}, DOWNLOAD_LINK_TTL)
    return {"url": f"{_public_base(request)}/d/{token}", "expires_at": exp.isoformat(),
            "ttl_seconds": DOWNLOAD_LINK_TTL}


@app.get("/d/{token}")
def download_by_token(token: str):
    """Opened by a phone scanning the QR: returns the PDF as an attachment, no page in between."""
    try:
        c = read_token(token, "pdf")
    except TokenError as e:
        msg = "This download link has expired. Generate a new QR code." if str(e) == "expired" \
            else "Invalid download link."
        return JSONResponse(status_code=410 if str(e) == "expired" else 403, content={"error": msg})
    r = sb.table("analysis_reports").select("*").eq("id", c["rid"]).eq("user_id", c["uid"]).execute()
    if not r.data:
        return JSONResponse(status_code=404, content={"error": "Report not found."})
    return _pdf_response(r.data[0])


# ============================================================
# Device-facing summary + web pages
# ============================================================
T2D_SPECIES_PROFILE = {
    "Clostridium_bolteae": {
        "role": "Risk Driver", "tag": "Risk Factor", "sub": "T2D Marker: Elevated FBG",
        "is_risk": True, "weight": 1.35
    },
    "Bifidobacterium_longum": {
        "role": "Protective Probiotic", "tag": "Protective", "sub": "Protective: Insulin Sensitivity",
        "is_risk": False, "weight": 1.30
    },
    "Prevotella_copri": {
        "role": "Insulin Resistance", "tag": "BCAA Risk", "sub": "T2D Driver: High BCAA & Ins. Res.",
        "is_risk": True, "weight": 1.25
    },
    "Roseburia_intestinalis": {
        "role": "Protective SCFA", "tag": "Protective", "sub": "Protective: GLP-1 & Gut Barrier",
        "is_risk": False, "weight": 1.20
    },
    "Eggerthella_lenta": {
        "role": "Inflammatory Biomarker", "tag": "Inflam. Risk", "sub": "T2D Marker: Systemic Inflam.",
        "is_risk": True, "weight": 1.18
    },
    "Dorea_formicigenerans": {
        "role": "Glycemic Dysbiosis", "tag": "Risk Factor", "sub": "T2D Marker: High Fasting Glucose",
        "is_risk": True, "weight": 1.15
    },
    "Bacteroides_vulgatus": {
        "role": "Metabolic Dysbiosis", "tag": "Risk Factor", "sub": "T2D Marker: High HbA1c Dysbiosis",
        "is_risk": True, "weight": 1.12
    },
    "Eubacterium_rectale": {
        "role": "Beneficial SCFA", "tag": "Protective", "sub": "Protective: Butyrate Producer",
        "is_risk": False, "weight": 1.10
    },
    "Streptococcus_salivarius": {
        "role": "Oral/Gut Dysbiosis", "tag": "Marker", "sub": "T2D Marker: Mucosal Dysbiosis",
        "is_risk": True, "weight": 1.08
    },
    "Haemophilus_parainfluenzae": {
        "role": "Opportunistic", "tag": "Risk Factor", "sub": "T2D Marker: Glucose Dysregulation",
        "is_risk": True, "weight": 1.06
    },
    "Bacteroides_thetaiotaomicron": {
        "role": "Protective Commensal", "tag": "Protective", "sub": "Protective: Barrier Integrity",
        "is_risk": False, "weight": 1.05
    },
    "Bacteroides_ovatus": {
        "role": "Metabolic Commensal", "tag": "Commensal", "sub": "Metabolic: Polysaccharide Digestion",
        "is_risk": False, "weight": 1.03
    },
    "Clostridium_hathewayi": {
        "role": "Opportunistic Pathogen", "tag": "Risk Factor", "sub": "T2D Marker: Endotoxin Burden",
        "is_risk": True, "weight": 1.05
    },
}

@app.get("/device/latest-report")
def device_latest_report(device_id: str):
    """Small summary for the ESP32 screen. Contains no CSV data and no clinical profile."""
    r = (sb.table("analysis_reports").select("id,status,created_at,glucose_value,results,features,sample_id")
         .eq("device_id", device_id).order("created_at", desc=True).limit(1).execute())
    if not r.data:
        return {"status": "ok", "report_status": "none"}
    row = r.data[0]
    res = row.get("results") or {}

    raw_feats = row.get("features") or []
    microbes_list = []
    for f in raw_feats:
        if isinstance(f, dict) and f.get("name"):
            try:
                val = float(f.get("value") or 0.0)
                sp_name = str(f["name"])
                prof = T2D_SPECIES_PROFILE.get(sp_name, {
                    "role": "Gut Commensal", "tag": "Commensal", "sub": "Metabolic Commensal",
                    "is_risk": False, "weight": 1.0
                })
                impact_score = val * prof["weight"]
                microbes_list.append({
                    "name": sp_name,
                    "abundance": round(val, 2),
                    "role": prof["role"],
                    "tag": prof["tag"],
                    "subtitle": prof["sub"],
                    "is_risk": prof["is_risk"],
                    "score": impact_score
                })
            except (ValueError, TypeError):
                pass
    microbes_list.sort(key=lambda x: x["score"], reverse=True)
    top_microbes = [
        {
            "name": m["name"],
            "abundance": m["abundance"],
            "role": m["role"],
            "tag": m["tag"],
            "subtitle": m["subtitle"],
            "is_risk": m["is_risk"],
        }
        for m in microbes_list[:3]
    ]

    def _food_label(x):
        sources = x.get("sources") or []
        if isinstance(sources, list) and sources:
            return ", ".join(sources)
        if x.get("food_name") and x.get("food_name") != x.get("compound"):
            return x.get("food_name")
        # Dynamic fallback from model database by enzyme
        src = get_food_source_for_enzyme(x.get("enzyme"))
        if src:
            return src
        return x.get("compound") or ""

    def _phyto_source(x):
        sources = x.get("sources") or []
        if isinstance(sources, list) and sources:
            return ", ".join(sources)
        if x.get("plant"):
            return x.get("plant")
        src = get_plant_source_for_enzyme(x.get("enzyme"))
        if src:
            return src
        return ""


    return {
        "status": "ok",
        "report_status": row["status"],
        "report_id": row["id"],
        "created_at": row["created_at"],
        "glucose": row["glucose_value"],
        "sample_id": row.get("sample_id") or "Cohort Match",
        "microbes": top_microbes,
        "phyto": [
            {
                "compound": x.get("compound"),
                "enzyme": x.get("enzyme"),
                "prob": x.get("prob"),
                "plant": _phyto_source(x),
            }
            for x in (res.get("phyto") or [])[:5]
        ],
        "food": [
            {
                "food_name": _food_label(x),
                "compound": x.get("compound"),
                "enzyme": x.get("enzyme"),
                "prob": x.get("prob"),
            }
            for x in (res.get("food") or [])[:5]
        ],
    }



# ============================================================
# Device profile (age / height / weight entered on the device)
# ============================================================
def _claimed_user_by_device(device_id: str, cols: str = "id"):
    r = sb.table("users").select(cols).eq("device_id", device_id).eq("claimed", True).execute()
    return r.data[0] if r.data else None


@app.post("/device/profile")
async def device_set_profile(device_id: str = Form(...), age: int = Form(..., ge=1, le=120),
                             height_cm: int = Form(..., ge=100, le=250),
                             weight_kg: int = Form(..., ge=20, le=300)):
    u = _claimed_user_by_device(device_id)
    if not u:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    sb.table("users").update({"age": age, "height_cm": height_cm, "weight_kg": weight_kg}).eq("id", u["id"]).execute()
    return {"status": "ok"}


@app.get("/device/profile")
def device_get_profile(device_id: str):
    u = _claimed_user_by_device(device_id, "id,age,height_cm,weight_kg")
    if not u:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    if not (u.get("age") and u.get("height_cm") and u.get("weight_kg")):
        return {"status": "empty"}
    return {"status": "ok", "age": u["age"], "height_cm": u["height_cm"], "weight_kg": u["weight_kg"]}


@app.get("/device/latest-glucose")
def device_latest_glucose(device_id: str):
    """Newest saved reading, for the 'Use Latest' button on the device."""
    u = _claimed_user_by_device(device_id)
    if not u:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    r = (sb.table("glucose_readings").select("*").eq("user_id", u["id"])
         .order("measured_at", desc=True).limit(1).execute())
    if not r.data:
        return {"status": "empty"}
    return dict(_glucose_out(r.data[0]), status="ok")


@app.post("/device/logout")
@app.post("/device/unclaim")
async def device_logout(device_id: str = Form(...)):
    """Unlinks this device from its user account so a new user can link it."""
    sb.table("users").update({
        "claimed": False,
        "device_id": None,
        "upload_code_hash": None,
        "upload_code_expires_at": None,
        "upload_reading_id": None,
        "upload_source": None
    }).eq("device_id", device_id).execute()
    return {"status": "ok", "message": "Device unlinked successfully."}



# ============================================================
# QR upload: the device asks for a short-lived link bound to ONE glucose reading.
# The phone opens /u/{code} (no login), uploads the CSV, and the server runs both models with
# the stored profile + that reading. The code is stored hashed and is cleared after a success.
# ============================================================
UPLOAD_CODE_TTL = 900  # 15 minutes


@app.post("/upload-session")
async def create_upload_session(request: Request, device_id: str = Form(...),
                                glucose_reading_id: Optional[str] = Form(None),
                                glucose_source: str = Form("new_test")):
    u = _claimed_user_by_device(device_id, "id,age,height_cm,weight_kg")
    if not u:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    if not glucose_reading_id:  # old firmware: fall back to the web app login
        return {"status": "ok", "upload_url": f"{_public_base(request)}/app", "device_code": "", "ttl_seconds": 0}
    if glucose_source not in ("previous", "new_test"):
        return JSONResponse(status_code=400, content={"error": "Invalid glucose source."})
    if not (u.get("age") and u.get("height_cm") and u.get("weight_kg")):
        return JSONResponse(status_code=409, content={"error": "Profile missing. Open Edit Profile on the device."})
    if not _valid_uuid(glucose_reading_id) or not sb.table("glucose_readings").select("id") \
            .eq("id", glucose_reading_id).eq("user_id", u["id"]).execute().data:
        return JSONResponse(status_code=404, content={"error": "Glucose reading not found."})
    code = secrets.token_urlsafe(12)  # 16 chars: keeps the QR small
    sb.table("users").update({
        "upload_code_hash": _h(code),
        "upload_code_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=UPLOAD_CODE_TTL)).isoformat(),
        "upload_reading_id": glucose_reading_id, "upload_source": glucose_source}).eq("id", u["id"]).execute()
    return {"status": "ok", "upload_url": f"{_public_base(request)}/u/{code}", "ttl_seconds": UPLOAD_CODE_TTL}


def _upload_user(code: str) -> dict:
    r = (sb.table("users").select("id,email,device_id,claimed,age,height_cm,weight_kg,"
                                  "upload_reading_id,upload_source,upload_code_expires_at")
         .eq("upload_code_hash", _h(code)).eq("claimed", True).execute())
    row = r.data[0] if r.data else None
    exp = (row or {}).get("upload_code_expires_at")
    if not row or not exp or datetime.fromisoformat(exp) < datetime.now(timezone.utc):
        raise HTTPException(410, "This upload link has expired. Open a new QR code on the device.")
    return row


UPLOAD_CSV_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Gut Health - Microbiome Analysis</title>
<style>:root{--p:#2E7D32;--p-dark:#1B5E20;--bg:#F7FBF8;--t:#1B2E1D;--s:#5B6E5D;--b:#cfe3d1}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background:var(--bg);color:var(--t);font-family:system-ui,-apple-system,sans-serif}
.c{background:#fff;border:1px solid rgba(46,125,50,.18);border-radius:18px;padding:26px;max-width:440px;width:100%;box-shadow:0 4px 16px rgba(0,0,0,.04)}
h1{font-size:22px;margin:0 0 6px;color:var(--p-dark)}p{color:var(--s);margin:0 0 16px;font-size:14px;line-height:1.4}
.box{background:#f9fcf9;border:1px solid var(--b);border-radius:12px;padding:14px;margin-bottom:16px}
h3{margin:0 0 4px;font-size:15px;color:var(--p-dark)}
.muted{color:var(--s);font-size:13px;margin:0 0 10px}
input,select,button{width:100%;font-size:15px;padding:12px;margin-top:6px;border-radius:10px;border:1px solid var(--b);background:#fff;font-family:inherit}
button{background:var(--p);color:#fff;border:0;font-weight:600;cursor:pointer;transition:background .2s}
button:hover{background:var(--p-dark)}
button:disabled{opacity:.5;cursor:not-allowed}
.sec-btn{background:#e8f5e9;color:var(--p-dark);border:1px solid var(--p)}
.sec-btn:hover{background:#d0ebd2}
.sep{display:flex;align-items:center;text-align:center;margin:16px 0;color:#8ba08d;font-size:12px;font-weight:700}
.sep::before,.sep::after{content:'';flex:1;border-bottom:1px solid var(--b)}
.sep span{padding:0 10px}
#m{margin-top:14px;font-size:14px;padding:10px;border-radius:8px;display:none}
#m.e{display:block;background:#feeceb;color:#c62828}
#m.o{display:block;background:#e8f5e9;color:var(--p-dark)}
#m.w{display:block;background:#fff8e1;color:#b26a00}</style></head><body><div class="c">
<h1>Microbiome Analysis</h1>
<p>Your glucose measurement and physical profile are recorded on the device. Choose how you want to proceed:</p>

<div class="box">
  <h3>Option 1: Upload Your Own CSV</h3>
  <p class="muted">Upload your sequenced gut microbiome relative abundance CSV file.</p>
  <input type="file" id="f" accept=".csv,text/csv"><select id="s" hidden></select>
  <button id="b" disabled>Upload & Run Analysis</button>
</div>

<div class="sep"><span>OR</span></div>

<div class="box">
  <h3>Option 2: Don't Have a CSV File?</h3>
  <p class="muted">The AI model will match the closest reference profile from our clinical cohort based on your glucose value and calculate recommendations.</p>
  <button id="bc" class="sec-btn">Auto-Predict with Cohort Data</button>
</div>

<div id="m"></div></div>
<script>
const f=document.getElementById('f'),s=document.getElementById('s'),b=document.getElementById('b'),bc=document.getElementById('bc'),m=document.getElementById('m');
f.onchange=()=>{b.disabled=!f.files[0];s.hidden=true;m.style.display='none';};
b.onclick=async()=>{
  b.disabled=bc.disabled=true;m.className='w';m.textContent='Analysing CSV... this can take a few seconds.';
  const fd=new FormData();fd.append('file',f.files[0]);if(!s.hidden&&s.value)fd.append('sample_id',s.value);
  try{
    const r=await fetch(location.pathname.replace(/\\/$/,'')+'/analyze',{method:'POST',body:fd});
    const d=await r.json().catch(()=>({}));
    if(r.ok){m.className='o';m.textContent='Done! Your results are now showing on the device screen.';return;}
    const det=d.detail;
    if(det&&det.sample_ids){s.innerHTML='';det.sample_ids.forEach(i=>{const o=document.createElement('option');o.value=o.textContent=i;s.appendChild(o);});
      s.hidden=false;m.textContent=det.error+' Pick one and press Run again.';b.disabled=bc.disabled=false;return;}
    m.className='e';m.textContent=(det&&det.error)||(typeof det==='string'?det:'Upload failed.');
    if(det&&det.details)m.textContent+=' '+det.details.slice(0,5).join('; ');
    b.disabled=bc.disabled=false;
  }catch(e){m.className='e';m.textContent='Network error. Try again.';b.disabled=bc.disabled=false;}
};
bc.onclick=async()=>{
  b.disabled=bc.disabled=true;m.className='w';m.textContent='Matching nearest reference cohort patient by glucose...';
  try{
    const r=await fetch(location.pathname.replace(/\\/$/,'')+'/analyze-cohort',{method:'POST'});
    const d=await r.json().catch(()=>({}));
    if(r.ok){
      m.className='o';
      m.textContent='Done! Matched reference sample ('+(d.cohort_matched||'Cohort')+'). Results are on your device screen.';
      return;
    }
    m.className='e';m.textContent=(d&&d.error)||(d&&d.detail)||'Prediction failed.';
    b.disabled=bc.disabled=false;
  }catch(e){m.className='e';m.textContent='Network error. Try again.';b.disabled=bc.disabled=false;}
};
</script></body></html>"""


@app.get("/u/{code}", response_class=HTMLResponse)
def upload_csv_page(code: str):
    try:
        _upload_user(code)
    except HTTPException as e:
        return HTMLResponse(f"<p style='font-family:sans-serif;padding:24px'>{e.detail}</p>", status_code=410)
    return UPLOAD_CSV_PAGE


@app.post("/u/{code}/analyze", status_code=201)
async def upload_csv_analyze(code: str, file: UploadFile = File(...), sample_id: Optional[str] = Form(None)):
    user = _upload_user(code)
    if not check_rate_limit("up:" + str(user["id"])):
        raise HTTPException(429, "Too many analyses. Wait a few minutes.")
    rd = (sb.table("glucose_readings").select("*").eq("id", user["upload_reading_id"])
          .eq("user_id", user["id"]).execute())
    if not rd.data:
        raise HTTPException(404, "Glucose reading not found. Open a new QR code on the device.")
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "CSV is larger than 2 MB.")
    try:
        rid, warnings = await run_in_threadpool(
            _run_analysis, user, rd.data[0], user.get("upload_source") or "new_test",
            user["age"], user["height_cm"], user["weight_kg"], raw, sample_id, None)
    except CsvValidationError as e:
        raise HTTPException(400, detail={"error": str(e), "details": e.details})
    sb.table("users").update({"upload_code_hash": None, "upload_code_expires_at": None,
                              "upload_reading_id": None}).eq("id", user["id"]).execute()  # one successful use
    return {"status": "ok", "report_id": rid, "warnings": warnings}


@app.post("/u/{code}/analyze-cohort", status_code=201)
async def upload_csv_analyze_cohort(code: str):
    user = _upload_user(code)
    if not check_rate_limit("up:" + str(user["id"])):
        raise HTTPException(429, "Too many analyses. Wait a few minutes.")
    rd = (sb.table("glucose_readings").select("*").eq("id", user["upload_reading_id"])
          .eq("user_id", user["id"]).execute())
    if not rd.data:
        raise HTTPException(404, "Glucose reading not found. Open a new QR code on the device.")
    reading = rd.data[0]
    rid, matched_sample = await run_in_threadpool(
        _run_cohort_analysis, user, reading, user.get("upload_source") or "new_test",
        user["age"], user["height_cm"], user["weight_kg"])
    sb.table("users").update({"upload_code_hash": None, "upload_code_expires_at": None,
                              "upload_reading_id": None}).eq("id", user["id"]).execute()
    return {"status": "ok", "report_id": rid, "cohort_matched": matched_sample}


@app.post("/device/analyze-cohort", status_code=201)
async def device_analyze_cohort(device_id: str = Form(...),
                                glucose_reading_id: Optional[str] = Form(None),
                                glucose_source: str = Form("new_test")):
    u = _claimed_user_by_device(device_id, "id,device_id,age,height_cm,weight_kg")
    if not u:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    u["device_id"] = device_id
    if not (u.get("age") and u.get("height_cm") and u.get("weight_kg")):
        return JSONResponse(status_code=409, content={"error": "Profile missing. Open Edit Profile on the device."})
    if glucose_reading_id and _valid_uuid(glucose_reading_id):
        rd = sb.table("glucose_readings").select("*").eq("id", glucose_reading_id).eq("user_id", u["id"]).execute()
    else:
        rd = sb.table("glucose_readings").select("*").eq("user_id", u["id"]).order("measured_at", desc=True).limit(1).execute()
    if not rd.data:
        return JSONResponse(status_code=404, content={"error": "Glucose reading not found."})
    reading = rd.data[0]
    if not check_rate_limit("up:" + str(u["id"])):
        return JSONResponse(status_code=429, content={"error": "Too many analyses. Wait a few minutes."})
    rid, matched_sample = await run_in_threadpool(
        _run_cohort_analysis, u, reading, glucose_source,
        u["age"], u["height_cm"], u["weight_kg"])
    sb.table("users").update({"upload_code_hash": None, "upload_code_expires_at": None,
                              "upload_reading_id": None}).eq("id", u["id"]).execute()
    return {"status": "ok", "report_id": rid, "cohort_matched": matched_sample}



def send_report_email(to_email: str, pdf: bytes, filename: str):
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender": {"email": SENDER_EMAIL, "name": "Gut Health Analyzer"},
            "to": [{"email": to_email}],
            "subject": "Your Gut Health Analyzer report (PDF)",
            "textContent": "Your Gut Health Analyzer report is attached as a PDF.\n\n"
                           "Research use only. Not a medical diagnosis.",
            "attachment": [{"name": filename, "content": base64.b64encode(pdf).decode()}],
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"[Brevo ERROR] status={resp.status_code} body={resp.text}")
    resp.raise_for_status()


def _mask_email(e: str) -> str:
    name, _, dom = e.partition("@")
    return (name[:1] + "***@" + dom) if dom else "your registered email"


@app.post("/device/email-report")
async def device_email_report(device_id: str = Form(...), report_id: str = Form(...)):
    """Device button: e-mail the PDF of one of THIS user's completed reports to their registered address."""
    u = _claimed_user_by_device(device_id, "id,email")
    if not u:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    if not _valid_uuid(report_id):
        return JSONResponse(status_code=404, content={"error": "Report not found."})
    r = sb.table("analysis_reports").select("*").eq("id", report_id).eq("user_id", u["id"]).execute()
    if not r.data:
        return JSONResponse(status_code=404, content={"error": "Report not found."})
    row = r.data[0]
    if row["status"] != "completed":
        return JSONResponse(status_code=409, content={"error": "Report is not completed yet."})
    if not check_rate_limit("em:" + str(u["id"])):
        return JSONResponse(status_code=429, content={"error": "Too many emails. Wait a few minutes."})
    try:
        pdf = await run_in_threadpool(lambda: report_to_pdf(_report_out(row)))
        await run_in_threadpool(send_report_email, u["email"], pdf, f"gut-report-{row['id'][:8]}.pdf")
    except Exception as e:
        print(f"[email-report {report_id}] failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "Could not send the report email. Try again."})
    return {"status": "ok", "sent_to": _mask_email(u["email"])}


def _page(name):
    with open(os.path.join("static", name), encoding="utf-8") as f:
        return f.read()


@app.get("/app", response_class=HTMLResponse)
def page_app(): return _page("app.html")


@app.get("/dashboard/glucose", response_class=HTMLResponse)
def page_glucose(): return _page("glucose.html")


@app.get("/dashboard/reports", response_class=HTMLResponse)
def page_reports(): return _page("reports.html")


@app.get("/static/common.js")
def static_js(): return Response(_page("common.js"), media_type="application/javascript")


@app.get("/health")
def health():
    return {"status": "ok"}
