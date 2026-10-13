# Firmware changes (ESP32 `.ino`)

The `.ino` was supplied in chat, not inside the ZIP, so these are exact replacement blocks to paste into it.
**Not compiled or flashed** (no toolchain/hardware here). Pins, display, touch mapping, ADS1115 code,
password/OTP wizards and the boot splash are untouched.

## Why the device stays simple
CSV upload, glucose selection and PDF/QR downloads happen on the phone/PC web app (`/app`, `/dashboard/*`).
The device does what only it can do: measure glucose, post it, show the QR to the web app, and show a short result summary.

## 1. DELETE (gut-risk pipeline)
- globals `lastSeenTimestamp`, `dashboardPayload`, `dashboardHttpCode`-based dashboard flow, `qrDeviceCode`
- functions `drawResultScreen`, `pollLatest`, `fetchDashboardData`, `drawDashboard`, `drawMicrobesTab`,
  `drawEnzymesTab`, `drawGutPage`, `drawRecoPage`, `drawDashboardError`, `riskGradient`, `drawArrow`
- the "GET LAST RESULT" block at the end of `setup()` (`/latest/...`)
- screens `SCREEN_DASHBOARD, SCREEN_GUT, SCREEN_PHYTO, SCREEN_FOOD, SCREEN_DASHBOARD_ERROR` and their branches in `loop()`
- `/dashboard-data`, `/latest/<id>` are gone from the backend and will return 404.

## 2. ADD: post glucose + fasting question
```cpp
String lastReadingRef = "";

// Yes/No screen: the models treat glucose as FASTING glucose, so we record the answer.
// returns 1 = fasting, 0 = not fasting
int askFasting() {
  drawHeader("Glucose Test");
  drawCard(20, 60, 440, 130);
  T("Was this measured fasting?", 240, 100, F_TITLE, COL_TEXT, COL_WHITE, AL_C);
  T("(no food for 8+ hours)", 240, 135, F_BODY, COL_TEXT_SOFT, COL_WHITE, AL_C);
  display.fillRoundRect(40, 215, 180, 50, 25, COL_PRIMARY);  T("Yes", 130, 240, F_BODYB, COL_WHITE, COL_PRIMARY, AL_C);
  display.fillRoundRect(260, 215, 180, 50, 25, COL_AMBER);   T("No", 350, 240, F_BODYB, COL_WHITE, COL_AMBER, AL_C);
  waitTouchRelease();
  while (true) {
    int x, y;
    if (readTouch(x, y) && y >= 215 && y <= 265) {
      int r = (x < 240) ? 1 : 0; waitTouchRelease(); return r;
    }
    delay(30);
  }
}

// POST /device/glucose  (form: device_id, value, fasting, client_ref). Retrying with the same
// client_ref returns the same stored reading (no duplicates). Returns HTTP code.
int postGlucoseReading(float value, int fasting) {
  if (lastReadingRef.length() == 0)
    lastReadingRef = String((uint32_t)esp_random(), HEX) + String((uint32_t)esp_random(), HEX);
  String body = "device_id=" + API_KEY + "&value=" + String(value, 1) +
                "&fasting=" + (fasting ? "true" : "false") + "&client_ref=" + lastReadingRef;
  String resp;
  int code = postForm("/device/glucose", body, resp);   // postForm already exists in the sketch
  if (code == 200 || code == 201) lastReadingRef = "";  // done: next test gets a new ref
  return code;
}
```

## 3. REPLACE `runGlucoseFlow()`
```cpp
void runGlucoseFlow() {
  runGlucoseTest();                       // unchanged: baseline, response detection, estimate
  if (!lastGlucoseValid) { showQRScreen(); return; }   // result screen already explained "no response"
  int fasting = askFasting();
  showBusy("Saving reading...");
  int code = postGlucoseReading(lastGlucoseValue, fasting);
  if (code == 200 || code == 201) {
    drawHeader("Reading saved");
    drawCard(20, 70, 440, 140);
    T("Glucose saved to your account.", 240, 110, F_BODYB, COL_TEXT, COL_WHITE, AL_C);
    T("Scan the QR code to upload your", 240, 150, F_BODY, COL_TEXT_SOFT, COL_WHITE, AL_C);
    T("microbiome CSV and run the analysis.", 240, 175, F_BODY, COL_TEXT_SOFT, COL_WHITE, AL_C);
    T("Tap to continue", 240, 262, F_BODYB, COL_PRIMARY, COL_BG, AL_C);
    delay(300); int x, y; while (!readTouch(x, y)) delay(30); waitTouchRelease();
  } else {
    showMessage("Could not save reading", errorFromResponse("", code), "Tap to continue");
  }
  showQRScreen();
}
```
`lastGlucoseValue` is the *midpoint estimate*; the existing sensor calibration is unchanged and remains
experimental (the disclaimer on the result screen stays).

## 4. QR screen
`/upload-session` still exists but now returns `{upload_url: "<PUBLIC_BASE_URL>/app", device_code: ""}`.
In `drawQRScreen`: change `"Scan to upload"` text steps to
1 "Scan, log in with email" / 2 "Pick glucose, upload CSV" / 3 "Results show here",
and **delete** the `Code:` line (`codeLine`). The 2-minute refresh (`QR_REFRESH_MS`) can be removed:
the URL is static now (set `QR_REFRESH_MS` very large or delete the check in `loop()`).

## 5. REPLACE `pollLatest()` with a report summary poll
```cpp
String lastReportId = "";
void pollLatest() {
  HTTPClient http;
  http.begin(String(CLOUD_HOST) + "/device/latest-report?device_id=" + API_KEY);
  int code = http.GET();
  if (code == 200) {
    StaticJsonDocument<1536> doc;
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok &&
        doc["status"] == "ok" && doc["report_status"] == "completed") {
      String rid = doc["report_id"].as<String>();
      if (rid != lastReportId) { lastReportId = rid; drawReportSummary(doc); }
    }
  }
  http.end();
}

void drawReportSummary(JsonDocument& d) {
  onQRScreenNow = false; resultShown = true;
  drawHeader("Analysis Ready");
  drawCard(20, 56, 440, 236);
  T("Top predicted targets (research use)", 36, 74, F_BODYB, COL_TEXT, COL_WHITE);
  int y = 100;
  for (int k = 0; k < 2; k++) {
    JsonArray a = d[k ? "food" : "phyto"].as<JsonArray>();
    T(k ? "Food" : "Phytochemical", 36, y, F_TINY, COL_TEXT_SOFT, COL_WHITE); y += 16;
    int n = 0;
    for (JsonObject it : a) {
      if (n++ >= 3) break;
      char line[80]; snprintf(line, sizeof(line), "%s - %s  (%.0f%%)",
        (const char*)(it["compound"] | "?"), (const char*)(it["enzyme"] | ""), (double)(it["prob"] | 0.0f));
      String s = fitText(line, 400, F_BODY); T(s.c_str(), 36, y, F_BODY, COL_TEXT, COL_WHITE); y += 24;
    }
    y += 6;
  }
  T("Full PDF report: web app > Reports", 240, 308, F_TINY, COL_TEXT_SOFT, COL_BG, AL_C);
  if (adsOk) drawGlucoseButton();
}
```
Declare `void drawReportSummary(JsonDocument&);` above `pollLatest` if the compiler complains.
`loop()` already calls `pollLatest()` every 3 s while on the QR screen and keeps the
"tap anywhere -> new QR" behaviour for the result screen; no change needed there.

## 6. `loop()` cleanup
Remove the `SCREEN_DASHBOARD / SCREEN_GUT / SCREEN_PHYTO / SCREEN_FOOD / SCREEN_PROFILE / SCREEN_DASHBOARD_ERROR`
branches. Keep the profile wizard if you want the device to remember age/height/weight, but the
web app asks for them per analysis (the server no longer receives them from the device).

## Security note
The device still authenticates with its `GUT-XXXXXXXX` id (as before). That id is a bearer secret:
it is shown on the QR screen as "Key:", so anyone who photographs the screen could post readings or
read the short `/device/latest-report` summary. Consider hiding the key line on the QR screen.
