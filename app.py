import os, io, time, secrets, hashlib, requests, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, Request, Header, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from supabase import create_client
from recommender import recommend_from_microbiome, MODEL_VERSION
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
@app.get("/device/latest-report")
def device_latest_report(device_id: str):
    """Small summary for the ESP32 screen. Contains no CSV data and no clinical profile."""
    r = (sb.table("analysis_reports").select("id,status,created_at,glucose_value,results")
         .eq("device_id", device_id).order("created_at", desc=True).limit(1).execute())
    if not r.data:
        return {"status": "empty"}
    row = r.data[0]
    res = row.get("results") or {}
    return {"status": "ok", "report_status": row["status"], "report_id": row["id"],
            "created_at": row["created_at"], "glucose": row["glucose_value"],
            "phyto": [{"compound": x.get("compound"), "enzyme": x.get("enzyme"), "prob": x.get("prob")}
                      for x in (res.get("phyto") or [])[:3]],
            "food": [{"compound": x.get("compound"), "enzyme": x.get("enzyme"), "prob": x.get("prob")}
                     for x in (res.get("food") or [])[:3]]}


@app.post("/upload-session")
async def create_upload_session(request: Request, device_id: str = Form(...)):
    """Kept for firmware compatibility: the QR now simply opens the web app login."""
    r = sb.table("users").select("id").eq("device_id", device_id).eq("claimed", True).execute()
    if not r.data:
        return JSONResponse(status_code=403, content={"error": "Device not recognized."})
    return {"status": "ok", "upload_url": f"{_public_base(request)}/app", "device_code": "", "ttl_seconds": 0}


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
