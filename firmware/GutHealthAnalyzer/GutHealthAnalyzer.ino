// ============================================================
// GUT HEALTH ANALYZER  -  UI v3
// ESP32 + ILI9481 TFT + XPT2046 Touch + ADS1115
// Password login, glucose test (+fasting question), QR to web app, result summary
//
// v3 changes vs v2:
//  - REMOVED the gut-risk pipeline (risk %, top microbes, dashboard tiles, /latest, /dashboard-data)
//  - Glucose readings are POSTed to /device/glucose (idempotent via client_ref)
//  - The QR now opens the web app (/app): login, pick glucose, upload CSV, reports, PDF
//  - The device shows a short "Analysis Ready" summary from /device/latest-report
//  - The device Key is no longer printed on the QR screen (it is a credential); see Serial
// Libraries: WiFiManager (tzapu), ArduinoJson v6, LovyanGFX, Adafruit_ADS1X15,
//            Wire, Preferences, HTTPClient, qrcode.h + qrcode.c in the sketch folder
// ============================================================
#define LGFX_USE_V1
#include <WiFiManager.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include "qrcode.h"
#include <LovyanGFX.hpp>

// ============================================================
// DISPLAY + TOUCH PANEL (unchanged)
// ============================================================
class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ILI9481 _panel;
  lgfx::Bus_SPI       _bus;
  lgfx::Touch_XPT2046 _touch;
public:
  LGFX(void) {
    {
      auto cfg = _bus.config();
      cfg.spi_host = SPI2_HOST;
      cfg.spi_mode = 0;
      cfg.freq_write = 20000000;
      cfg.freq_read  = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = 36;
      cfg.pin_mosi = 35;
      cfg.pin_miso = 37;
      cfg.pin_dc = 4;  // GPIO8 is ADS1115 SDA
      _bus.config(cfg);
      _panel.setBus(&_bus);
    }
    {
      auto cfg = _panel.config();
      cfg.pin_cs = 10;
      cfg.pin_rst = 3;
      cfg.pin_busy = -1;
      cfg.panel_width = 320;
      cfg.panel_height = 480;
      cfg.offset_rotation = 0;
      cfg.readable = false;
      cfg.invert = false;
      cfg.rgb_order = false;
      cfg.dlen_16bit = false;
      cfg.bus_shared = true;
      _panel.config(cfg);
    }
    {
      auto cfg = _touch.config();
      cfg.x_min = 300;
      cfg.x_max = 3900;
      cfg.y_min = 300;
      cfg.y_max = 3900;
      cfg.pin_int = -1;
      cfg.bus_shared = true;
      cfg.spi_host = SPI2_HOST;
      cfg.freq = 1000000;
      cfg.pin_sclk = 36;
      cfg.pin_mosi = 35;
      cfg.pin_miso = 37;
      cfg.pin_cs = 7;
      _touch.config(cfg);
      _panel.setTouch(&_touch);
    }
    setPanel(&_panel);
  }
};
static LGFX display;

// ============================================================
// FONTS
// ============================================================
#define F_TITLE  (&lgfx::fonts::FreeSansBold12pt7b)
#define F_BODY   (&lgfx::fonts::FreeSans9pt7b)
#define F_BODYB  (&lgfx::fonts::FreeSansBold9pt7b)
#define F_BIG    (&lgfx::fonts::FreeSansBold18pt7b)
#define F_KEY    (&lgfx::fonts::FreeSansBold18pt7b)
#define F_TINY   (&lgfx::fonts::DejaVu9)
#define TRANSP   0xFFFFFFFFu
#define AL_L 0
#define AL_C 1
#define AL_R 2

// ============================================================
// BRAND COLORS
// ============================================================
uint16_t COL_BG, COL_PRIMARY, COL_PRIMARY_DARK, COL_SECONDARY, COL_ACCENT, COL_ACCENT_SOFT,
         COL_AMBER, COL_WHITE, COL_TEXT, COL_TEXT_SOFT, COL_RED, COL_RED_SOFT, COL_WARN_BG,
         COL_SHADOW, COL_BORDER;

void initPalette() {
  COL_BG           = display.color565(244, 249, 245);
  COL_PRIMARY      = display.color565(46, 125, 50);
  COL_PRIMARY_DARK = display.color565(27, 94, 32);
  COL_SECONDARY    = display.color565(67, 160, 71);
  COL_ACCENT       = display.color565(102, 187, 106);
  COL_ACCENT_SOFT  = display.color565(220, 240, 222);
  COL_AMBER        = display.color565(240, 150, 20);
  COL_WHITE        = display.color565(255, 255, 255);
  COL_TEXT         = display.color565(27, 46, 29);
  COL_TEXT_SOFT    = display.color565(96, 115, 98);
  COL_RED          = display.color565(211, 47, 47);
  COL_RED_SOFT     = display.color565(239, 130, 130);
  COL_WARN_BG      = display.color565(253, 236, 234);
  COL_SHADOW       = display.color565(222, 231, 223);
  COL_BORDER       = display.color565(208, 226, 210);
}

// ============================================================
// GLOBALS
// ============================================================
const char* CLOUD_HOST = "https://gut-obesity-api-ulld.onrender.com";
Preferences prefs;
String API_KEY;
String lastReportId = "";
String lastReadingRef = "";           // idempotency key for the glucose POST (reused on retry)
unsigned long lastPoll = 0;
const unsigned long POLL_INTERVAL_MS = 3000;
bool onQRScreenNow = false;
bool resultShown = false;             // true while the "Analysis Ready" screen is up
float lastGlucoseValue = -1.0f;
bool lastGlucoseValid = false;
int lastSessionHttpCode = 0;

// ============================================================
// ADS1115 / GLUCOSE SENSOR (unchanged)
// ============================================================
Adafruit_ADS1115 ads;
bool adsOk = false;
#define ADS_SDA_PIN 8
#define ADS_SCL_PIN 9
const unsigned long GLUCOSE_BASELINE_MS = 20000;
const unsigned long GLUCOSE_RECORD_MS   = 30000;
const unsigned long GLUCOSE_SAMPLE_MS   = 100;
const float GLUCOSE_SLOPE     = 0.004664;
const float GLUCOSE_INTERCEPT = 54.59;
unsigned long stageTotalMs = 1;
unsigned long stageMsLeft  = 1;
const int GLUCOSE_BTN_X = 296;
const int GLUCOSE_BTN_Y = 7;
const int GLUCOSE_BTN_W = 168;
const int GLUCOSE_BTN_H = 30;

// ============================================================
// DRAW HELPERS
// ============================================================
#define TOUCH_FLIP_X true
bool readTouch(int &x, int &y) {
  uint16_t tx, ty;
  if (!display.getTouch(&tx, &ty)) return false;
  x = TOUCH_FLIP_X ? (display.width() - 1 - (int)tx) : (int)tx;
  y = (int)ty;
  return true;
}

// Text helper: y is the vertical CENTER. al: 0 left, 1 center, 2 right. bg = TRANSP for transparent.
void T(const char* s, int x, int y, const lgfx::IFont* f, uint16_t fg,
       uint32_t bg = TRANSP, uint8_t al = AL_L) {
  display.setFont(f);
  display.setTextSize(1);
  if (bg == TRANSP) display.setTextColor(fg);
  else display.setTextColor(fg, (uint16_t)bg);
  display.setTextDatum(al == AL_L ? lgfx::textdatum::middle_left :
                       (al == AL_C ? lgfx::textdatum::middle_center : lgfx::textdatum::middle_right));
  display.drawString(s, x, y);
}

String fitText(const char* s, int maxW, const lgfx::IFont* f) {
  display.setFont(f);
  display.setTextSize(1);
  String t = s;
  if (display.textWidth(t.c_str()) <= maxW) return t;
  while (t.length() > 1) {
    String u = t + "...";
    if (display.textWidth(u.c_str()) <= maxW) return u;
    t.remove(t.length() - 1);
  }
  return t;
}

uint16_t mixRGB(uint8_t r1, uint8_t g1, uint8_t b1, uint8_t r2, uint8_t g2, uint8_t b2, float t) {
  if (t < 0) t = 0;
  if (t > 1) t = 1;
  return display.color565((uint8_t)(r1 + (r2 - r1) * t), (uint8_t)(g1 + (g2 - g1) * t),
                          (uint8_t)(b1 + (b2 - b1) * t));
}

void drawDrop(int cx, int cy, int r, uint16_t col) {
  display.fillCircle(cx, cy + r / 2, r, col);
  display.fillTriangle(cx, cy - (r * 3) / 2, cx - (r * 87) / 100, cy, cx + (r * 87) / 100, cy, col);
}

void waitTouchRelease() {
  // Avoid "press-through": a finger still down would hit the next screen's button.
  unsigned long t = millis();
  int x, y;
  while (readTouch(x, y) && millis() - t < 1500) delay(20);
  delay(40);
}

// ============================================================
// COMMON UI
// ============================================================
void drawHeader(const char* title) {
  display.fillScreen(COL_BG);
  for (int y = 0; y < 44; y++)
    display.drawFastHLine(0, y, 480, mixRGB(27, 94, 32, 56, 142, 60, y / 43.0f));
  display.fillRect(0, 44, 480, 3, COL_ACCENT);
  display.fillCircle(28, 22, 15, COL_WHITE);
  display.fillRoundRect(21, 20, 14, 5, 2, COL_PRIMARY);
  display.fillRoundRect(26, 15, 5, 14, 2, COL_PRIMARY);
  T(title, 54, 22, F_TITLE, COL_WHITE);
}

void drawCard(int x, int y, int w, int h) {
  display.fillRoundRect(x + 2, y + 3, w, h, 12, COL_SHADOW);
  display.fillRoundRect(x, y, w, h, 12, COL_WHITE);
  display.drawRoundRect(x, y, w, h, 12, COL_BORDER);
}

void drawFooter(const char* text) {
  T(text, 240, 304, F_TINY, COL_TEXT_SOFT, COL_BG, AL_C);
}

// ============================================================
// TOUCH KEYPAD (unchanged)
// ============================================================
struct KeyBtn { int x, y, w, h; char label[6]; };
KeyBtn keys[12];

void layoutKeypad() {
  const char* labels[12] = { "1", "2", "3", "4", "5", "6", "7", "8", "9", "<-", "0", "OK" };
  int cols = 3, btnW = 130, btnH = 42, gap = 8, rowGap = 6;
  int startX = (480 - (cols * btnW + (cols - 1) * gap)) / 2;
  int startY = 112;
  for (int i = 0; i < 12; i++) {
    int r = i / cols, c = i % cols;
    keys[i].x = startX + c * (btnW + gap);
    keys[i].y = startY + r * (btnH + rowGap);
    keys[i].w = btnW;
    keys[i].h = btnH;
    strcpy(keys[i].label, labels[i]);
  }
}

void drawBackspaceIcon(int cx, int cy, uint16_t fg, uint16_t bg) {
  display.fillTriangle(cx - 17, cy, cx - 6, cy - 11, cx - 6, cy + 11, fg);
  display.fillRoundRect(cx - 6, cy - 11, 24, 22, 3, fg);
  for (int o = 0; o < 2; o++) {
    display.drawLine(cx + 1 + o, cy - 5, cx + 11 + o, cy + 5, bg);
    display.drawLine(cx + 1 + o, cy + 5, cx + 11 + o, cy - 5, bg);
  }
}

void drawCheckIcon(int cx, int cy, uint16_t fg) {
  for (int o = -1; o <= 1; o++) {
    display.drawLine(cx - 11, cy + o, cx - 3, cy + 8 + o, fg);
    display.drawLine(cx - 3, cy + 8 + o, cx + 12, cy - 8 + o, fg);
  }
}

void drawKey(int i, bool pressed) {
  bool isBack = strcmp(keys[i].label, "<-") == 0;
  bool isOk   = strcmp(keys[i].label, "OK") == 0;
  bool special = isBack || isOk;
  uint16_t fill = pressed ? (special ? COL_PRIMARY_DARK : COL_ACCENT_SOFT)
                          : (special ? COL_SECONDARY : COL_WHITE);
  uint16_t fg = special ? COL_WHITE : COL_TEXT;
  int x = keys[i].x, y = keys[i].y, w = keys[i].w, h = keys[i].h;
  display.fillRect(x - 1, y - 1, w + 3, h + 6, COL_BG);
  int dy = pressed ? 2 : 0;
  if (!pressed) display.fillRoundRect(x, y + 3, w, h, 10, COL_SHADOW);
  display.fillRoundRect(x, y + dy, w, h, 10, fill);
  display.drawRoundRect(x, y + dy, w, h, 10, special ? COL_PRIMARY_DARK : COL_BORDER);
  int cx = x + w / 2, cy = y + dy + h / 2;
  if (isBack) drawBackspaceIcon(cx, cy, fg, fill);
  else if (isOk) drawCheckIcon(cx, cy, fg);
  else T(keys[i].label, cx, cy, F_KEY, fg, fill, AL_C);
}

void drawKeypad() {
  for (int i = 0; i < 12; i++) drawKey(i, false);
}

void drawPasswordDots(const String& entered, bool error) {
  display.fillRect(100, 72, 280, 36, COL_BG);
  display.fillRoundRect(110, 76, 260, 28, 14, COL_WHITE);
  display.drawRoundRect(110, 76, 260, 28, 14, error ? COL_RED : COL_BORDER);
  int dotSize = 14, gap = 12, maxDots = 8;
  int totalW = maxDots * dotSize + (maxDots - 1) * gap;
  int startX = (480 - totalW) / 2;
  for (int i = 0; i < maxDots; i++) {
    int cx = startX + i * (dotSize + gap) + dotSize / 2;
    if (i < (int)entered.length()) display.fillCircle(cx, 90, dotSize / 2, error ? COL_RED : COL_PRIMARY);
    else display.fillCircle(cx, 90, 4, COL_BORDER);
  }
}

int hitKey(int tx, int ty) {
  for (int i = 0; i < 12; i++)
    if (tx >= keys[i].x && tx <= keys[i].x + keys[i].w && ty >= keys[i].y && ty <= keys[i].y + keys[i].h)
      return i;
  return -1;
}

String waitForKeyTap() {
  while (true) {
    int tx, ty;
    if (readTouch(tx, ty)) {
      int idx = hitKey(tx, ty);
      if (idx >= 0) {
        drawKey(idx, true);
        delay(120);
        drawKey(idx, false);
        delay(80);
        return String(keys[idx].label);
      }
    }
    delay(30);
  }
}

// ============================================================
// PASSWORD SCREEN (also used for the 6-digit email code)
// ============================================================
void drawPasswordScreen(const char* title, const char* subtitle, const String& entered, bool error) {
  drawHeader(title);
  T(subtitle, 240, 62, F_BODY, error ? COL_RED : COL_TEXT_SOFT, COL_BG, AL_C);
  drawPasswordDots(entered, error);
  layoutKeypad();
  drawKeypad();
}

String capturePassword(const char* title, const char* subtitle) {
  String entered = "";
  drawPasswordScreen(title, subtitle, entered, false);
  while (true) {
    String key = waitForKeyTap();
    if (key == "<-") {
      if (entered.length() > 0) entered.remove(entered.length() - 1);
    } else if (key == "OK") {
      if (entered.length() > 0) return entered;
    } else {
      if (entered.length() < 8) entered += key;
    }
    drawPasswordDots(entered, false);
  }
}

// ============================================================
// API KEY / SETUP / LOGIN (unchanged)
// ============================================================
String generateApiKey() {
  const char* charset = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  String key = "GUT-";
  for (int i = 0; i < 8; i++) key += charset[esp_random() % strlen(charset)];
  return key;
}

void runSetupWizard() {
  String pw1 = capturePassword("Gut Health Analyzer", "Create a device password");
  String pw2 = capturePassword("Gut Health Analyzer", "Re-enter to confirm");
  while (pw1 != pw2) {
    drawPasswordScreen("Gut Health Analyzer", "Passwords didn't match", pw2, true);
    delay(900);
    pw1 = capturePassword("Gut Health Analyzer", "Create a device password");
    pw2 = capturePassword("Gut Health Analyzer", "Re-enter to confirm");
  }
  API_KEY = generateApiKey();
  prefs.putString("pw", pw1);
  prefs.putString("apikey", API_KEY);
  prefs.putBool("setup_done", true);
  Serial.println("Setup complete. API key: " + API_KEY);
}

void runLoginGate() {
  String correctPw = prefs.getString("pw", "");
  if (correctPw.length() == 0) {
    prefs.putBool("setup_done", false);
    runSetupWizard();
    correctPw = prefs.getString("pw", "");
  }
  while (true) {
    String attempt = capturePassword("Gut Health Analyzer", "Enter device password");
    if (attempt.length() == correctPw.length() && attempt.equals(correctPw)) {
      Serial.println("Password accepted.");
      return;
    }
    Serial.println("Password rejected.");
    drawPasswordScreen("Gut Health Analyzer", "Incorrect password", "", true);
    delay(900);
  }
}

// ============================================================
// GLUCOSE BUTTON (unchanged)
// ============================================================
void drawGlucoseButton() {
  display.fillRoundRect(GLUCOSE_BTN_X, GLUCOSE_BTN_Y, GLUCOSE_BTN_W, GLUCOSE_BTN_H, 15, COL_WHITE);
  drawDrop(GLUCOSE_BTN_X + 22, GLUCOSE_BTN_Y + 10, 6, COL_RED);
  T("Glucose Test", GLUCOSE_BTN_X + 38, GLUCOSE_BTN_Y + GLUCOSE_BTN_H / 2, F_BODYB,
    COL_PRIMARY_DARK, COL_WHITE, AL_L);
}

bool hitGlucoseButton(int tx, int ty) {
  const int pad = 6;
  return tx >= GLUCOSE_BTN_X - pad && tx <= GLUCOSE_BTN_X + GLUCOSE_BTN_W + pad &&
         ty >= GLUCOSE_BTN_Y - pad && ty <= GLUCOSE_BTN_Y + GLUCOSE_BTN_H + pad;
}

// ============================================================
// NETWORK HELPERS
// ============================================================
String urlEncode(const String& s) {
  String o;
  const char* hex = "0123456789ABCDEF";
  for (unsigned i = 0; i < s.length(); i++) {
    uint8_t c = (uint8_t)s[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') o += (char)c;
    else { o += '%'; o += hex[c >> 4]; o += hex[c & 15]; }
  }
  return o;
}

int postForm(const String& path, const String& body, String& resp) {
  HTTPClient http;
  http.setTimeout(30000);  // Render cold start
  http.begin(String(CLOUD_HOST) + path);
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  int code = http.POST(body);
  resp = (code > 0) ? http.getString() : String("");
  http.end();
  Serial.printf("%s -> HTTP %d\n", path.c_str(), code);
  return code;
}

String errorFromResponse(const String& resp, int code) {
  if (code <= 0) return "Could not reach the server. Check Wi-Fi.";
  StaticJsonDocument<384> d;
  if (deserializeJson(d, resp) == DeserializationError::Ok && d["error"].is<const char*>())
    return d["error"].as<String>();
  return "Server error " + String(code) + ".";
}

void drawWrapped(const String& s, int x, int y, int maxW, int lineH, int maxLines, uint16_t fg, uint16_t bg) {
  display.setFont(F_BODY);
  display.setTextSize(1);
  String rest = s;
  for (int line = 0; line < maxLines && rest.length(); line++) {
    String cur = rest;
    if (display.textWidth(cur.c_str()) > maxW) {
      int cut = cur.length();
      while (cut > 1 && display.textWidth(cur.substring(0, cut).c_str()) > maxW) cut--;
      int sp = cur.lastIndexOf(' ', cut);
      if (sp > 0) cut = sp;
      rest = cur.substring(cut);
      rest.trim();
      cur = cur.substring(0, cut);
    } else {
      rest = "";
    }
    T(cur.c_str(), x, y + line * lineH, F_BODY, fg, bg);
  }
}

void showMessage(const char* title, const String& msg, const char* hint) {
  drawHeader(title);
  drawCard(20, 70, 440, 150);
  drawWrapped(msg, 40, 110, 400, 28, 4, COL_RED, COL_WHITE);
  T(hint, 240, 262, F_BODYB, COL_PRIMARY, COL_BG, AL_C);
  delay(300);
  int x, y;
  waitTouchRelease();
  while (!readTouch(x, y)) delay(30);
  waitTouchRelease();
}

void showBusy(const char* line) {
  drawHeader("Gut Health Analyzer");
  T(line, 240, 150, F_TITLE, COL_PRIMARY, COL_BG, AL_C);
  T("The server may take up to 30 s to wake up.", 240, 190, F_TINY, COL_TEXT_SOFT, COL_BG, AL_C);
}

// ============================================================
// QR SCREEN  (QR now opens the web app: login -> glucose -> CSV -> reports)
// ============================================================
void drawQRStep(int cy, int n, const char* text) {
  display.fillCircle(260, cy, 13, COL_ACCENT_SOFT);
  display.drawCircle(260, cy, 13, COL_ACCENT);
  char b[4];
  snprintf(b, sizeof(b), "%d", n);
  T(b, 260, cy, F_BODYB, COL_PRIMARY_DARK, COL_ACCENT_SOFT, AL_C);
  T(text, 284, cy, F_BODY, COL_TEXT, COL_WHITE, AL_L);
}

void drawQRFailed(int httpCodeHint) {
  currentScreenReset();
  drawHeader("Gut Health");
  drawCard(20, 70, 440, 150);
  T("Could not open the web app QR", 40, 105, F_BODYB, COL_RED, COL_WHITE);
  T("Check Wi-Fi, and that this device", 40, 140, F_BODY, COL_TEXT, COL_WHITE);
  T("is linked to your account.", 40, 165, F_BODY, COL_TEXT, COL_WHITE);
  T("Tap anywhere to retry", 240, 262, F_BODYB, COL_PRIMARY, COL_BG, AL_C);
}

void drawQRScreen(const String& url) {
  QRCode qr;
  uint8_t buf[qrcode_getBufferSize(6)];  // v6 ECC_LOW holds 134 bytes; the /app URL is ~50
  int8_t qrRc = qrcode_initText(&qr, buf, 6, ECC_LOW, url.c_str());
  if (qrRc < 0) { drawQRFailed(0); return; }
  resultShown = false;
  drawHeader("Gut Health");

  int scale = 4;  // v6 = 41 modules * 4 = 164 px
  int qrPixels = qr.size * scale;
  int cardPad = 12;
  int cardW = qrPixels + cardPad * 2;
  int cardX = 24, cardY = 58;
  drawCard(cardX, cardY, cardW, cardW);
  int qx = cardX + cardPad, qy = cardY + cardPad;
  for (uint8_t y = 0; y < qr.size; y++)
    for (uint8_t x = 0; x < qr.size; x++)
      if (qrcode_getModule(&qr, x, y))
        display.fillRect(qx + x * scale, qy + y * scale, scale, scale, COL_TEXT);

  // (the device Key is intentionally NOT shown: it is a credential)
  T("Scan with your phone", cardX + cardW / 2, cardY + cardW + 22, F_BODYB, COL_PRIMARY, COL_BG, AL_C);

  drawCard(232, 58, 224, 227);
  T("Open the web app", 248, 80, F_TITLE, COL_TEXT, COL_WHITE);
  T("Log in with your email", 248, 106, F_BODY, COL_TEXT_SOFT, COL_WHITE);
  display.drawFastVLine(260, 160, 12, COL_BORDER);
  display.drawFastVLine(260, 198, 12, COL_BORDER);
  drawQRStep(146, 1, "Scan, log in");
  drawQRStep(185, 2, "Pick glucose, upload CSV");
  drawQRStep(224, 3, "Summary shows here");
  display.drawFastHLine(248, 245, 192, COL_BORDER);
  display.fillCircle(254, 265, 4, COL_ACCENT);
  T("Waiting for analysis", 266, 265, F_BODY, COL_TEXT_SOFT, COL_WHITE);
  drawFooter("Powered by AI - Gut Health");
  if (adsOk) drawGlucoseButton();
  onQRScreenNow = true;
}

void currentScreenReset() {
  resultShown = false;
  onQRScreenNow = false;  // loop(): tap anywhere to retry
}

// Ask the backend for the web-app URL (also confirms this device is linked: 403 otherwise).
bool createUploadSession(String &url) {
  HTTPClient http;
  http.setTimeout(30000);
  http.begin(String(CLOUD_HOST) + "/upload-session");
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  int httpCode = http.POST("device_id=" + API_KEY);
  bool ok = false;
  if (httpCode == 200) {
    StaticJsonDocument<512> doc;
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok &&
        doc["upload_url"].is<const char*>()) {
      url = doc["upload_url"].as<String>();
      ok = true;
    }
  }
  lastSessionHttpCode = httpCode;
  Serial.printf("upload-session HTTP %d\n", httpCode);
  http.end();
  return ok;
}

void showQRScreen() {
  String url;
  bool ok = createUploadSession(url);
  if (!ok && lastSessionHttpCode == 403) {
    // Server does not know this device as claimed (e.g. DB reset): link it again.
    prefs.putBool("claimed", false);
    runClaimWizard();
    ok = createUploadSession(url);
  }
  if (ok) drawQRScreen(url);
  else drawQRFailed(0);
}

// ============================================================
// ANALYSIS SUMMARY (from /device/latest-report; no CSV or profile data)
// ============================================================
void drawReportSummary(JsonObject d) {
  onQRScreenNow = false;
  resultShown = true;
  drawHeader("Analysis Ready");
  drawCard(20, 56, 440, 236);
  T("Top predicted targets (research use only)", 36, 74, F_BODYB, COL_TEXT, COL_WHITE);
  int y = 100;
  for (int k = 0; k < 2; k++) {
    JsonArray a = d[k ? "food" : "phyto"].as<JsonArray>();
    T(k ? "Food" : "Phytochemical", 36, y, F_TINY, COL_TEXT_SOFT, COL_WHITE);
    y += 16;
    int n = 0;
    for (JsonObject it : a) {
      if (n++ >= 3) break;
      char line[96];
      snprintf(line, sizeof(line), "%s - %s  (%.0f%%)",
               (const char*)(it["compound"] | "?"), (const char*)(it["enzyme"] | ""),
               (double)(it["prob"] | 0.0f));
      String s = fitText(line, 400, F_BODY);
      T(s.c_str(), 36, y, F_BODY, COL_TEXT, COL_WHITE);
      y += 24;
    }
    y += 6;
  }
  T("Full PDF report: web app > Reports. Tap for QR.", 240, 308, F_TINY, COL_TEXT_SOFT, COL_BG, AL_C);
  if (adsOk) drawGlucoseButton();
}

// silent = true only records the current report id (used at boot so an old report is not re-shown)
void pollLatest(bool silent = false) {
  HTTPClient http;
  http.begin(String(CLOUD_HOST) + "/device/latest-report?device_id=" + API_KEY);
  int code = http.GET();
  if (code == 200) {
    DynamicJsonDocument doc(3072);
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok &&
        doc["status"] == "ok" && doc["report_status"] == "completed") {
      String rid = doc["report_id"].as<String>();
      if (rid != lastReportId) {
        lastReportId = rid;
        if (!silent) drawReportSummary(doc.as<JsonObject>());
      }
    }
  }
  http.end();
}

// ============================================================
// ADS1115
// ============================================================
void readAllChannels(int16_t v[4]) {
  v[0] = ads.readADC_SingleEnded(0);
  v[1] = ads.readADC_SingleEnded(1);
  v[2] = ads.readADC_SingleEnded(2);
  v[3] = ads.readADC_SingleEnded(3);
}

// ============================================================
// GLUCOSE STAGE SCREEN (unchanged)
// ============================================================
void setStageTotal(unsigned long total) {
  stageTotalMs = total > 0 ? total : 1;
  stageMsLeft  = stageTotalMs;
}

void drawGlucoseCountdown(unsigned long msLeft) {
  if (msLeft > stageTotalMs) msLeft = 0;
  stageMsLeft = msLeft;
  display.fillRect(30, 158, 420, 46, COL_WHITE);
  const int bx = 36, bw = 408, by = 166;
  display.fillRoundRect(bx, by, bw, 10, 5, COL_SHADOW);
  float done = 1.0f - (float)msLeft / (float)stageTotalMs;
  int fw = (int)(done * bw);
  if (fw > 0) {
    if (fw < 10) fw = 10;
    display.fillRoundRect(bx, by, fw, 10, 5, COL_SECONDARY);
  }
  char b[24];
  snprintf(b, sizeof(b), "%lus remaining", msLeft / 1000);
  T(b, bx, 192, F_BODY, COL_TEXT_SOFT, COL_WHITE);
}

void drawGlucoseStage(const char* line1, const char* line2) {
  drawHeader("Experimental Glucose Test");
  drawCard(20, 60, 440, 150);
  display.fillCircle(62, 108, 28, COL_ACCENT_SOFT);
  display.drawCircle(62, 108, 28, COL_ACCENT);
  drawDrop(62, 100, 11, COL_RED);
  String l1 = fitText(line1, 340, F_TITLE);
  String l2 = fitText(line2, 340, F_BODY);
  T(l1.c_str(), 108, 96, F_TITLE, COL_TEXT, COL_WHITE);
  T(l2.c_str(), 108, 124, F_BODY, COL_TEXT_SOFT, COL_WHITE);
  drawGlucoseCountdown(stageMsLeft);
}

// ============================================================
// GLUCOSE RESULT (unchanged)
// ============================================================
void drawGlucoseResult(bool responseDetected, float midpointEstimate, float lowerEstimate,
                       float upperEstimate, float baselineA3, int16_t baselineMinA3,
                       int16_t baselineMaxA3, int16_t responseMinA3, int16_t responseMaxA3) {
  drawHeader("Experimental Glucose Test");
  drawCard(20, 56, 440, 112);
  T("EXPERIMENTAL ESTIMATE", 36, 71, F_TINY, COL_TEXT_SOFT, COL_WHITE);
  if (responseDetected) {
    float uncertainty = (upperEstimate >= lowerEstimate) ? (upperEstimate - lowerEstimate) / 2.0f
                                                         : (lowerEstimate - upperEstimate) / 2.0f;
    char nb[16];
    snprintf(nb, sizeof(nb), "%.0f", midpointEstimate);
    display.setFont(F_BIG);
    display.setTextSize(1);
    int nw = display.textWidth(nb);
    T(nb, 36, 104, F_BIG, COL_PRIMARY, COL_WHITE);
    T("mg/dL", 36 + nw + 8, 112, F_BODYB, COL_TEXT_SOFT, COL_WHITE);
    char ub[24];
    snprintf(ub, sizeof(ub), "+/- %.0f", uncertainty);
    T(ub, 444, 104, F_TITLE, COL_TEXT_SOFT, COL_WHITE, AL_R);
    char rb[40];
    snprintf(rb, sizeof(rb), "Range: %.0f to %.0f mg/dL", lowerEstimate, upperEstimate);
    T(rb, 36, 146, F_BODY, COL_TEXT, COL_WHITE);
  } else {
    T("No clear response detected", 36, 102, F_TITLE, COL_RED, COL_WHITE);
    T("Check strip/sample and retry.", 36, 134, F_BODY, COL_TEXT_SOFT, COL_WHITE);
  }
  char l1[56];
  snprintf(l1, sizeof(l1), "A3 baseline: %.1f  (min %d / max %d)", baselineA3, baselineMinA3, baselineMaxA3);
  char l2[56];
  snprintf(l2, sizeof(l2), "A3 response min/max: %d / %d", responseMinA3, responseMaxA3);
  T(l1, 24, 184, F_BODY, COL_TEXT_SOFT, COL_BG);
  T(l2, 24, 204, F_BODY, COL_TEXT_SOFT, COL_BG);
  display.fillRoundRect(20, 220, 440, 38, 8, COL_WARN_BG);
  display.drawRoundRect(20, 220, 440, 38, 8, COL_RED_SOFT);
  T("DIY experimental estimate; not clinically validated.", 32, 232, F_TINY, COL_RED, COL_WARN_BG);
  T("Do not use for medical decisions.", 32, 247, F_TINY, COL_RED, COL_WARN_BG);
  int bx = 140, by = 268, bw = 200, bh = 40;
  display.fillRoundRect(bx, by + 3, bw, bh, 20, COL_SHADOW);
  display.fillRoundRect(bx, by, bw, bh, 20, COL_PRIMARY);
  T("Continue", bx + bw / 2, by + bh / 2, F_BODYB, COL_WHITE, COL_PRIMARY, AL_C);
  while (true) {
    int tx, ty;
    if (readTouch(tx, ty)) {
      if (tx >= bx && tx <= bx + bw && ty >= by && ty <= by + bh) {
        delay(200);
        waitTouchRelease();
        return;
      }
    }
    delay(30);
  }
}

// ============================================================
// GLUCOSE TEST (measurement logic unchanged)
// ============================================================
void runGlucoseTest() {
  lastGlucoseValid = false;  // never reuse a stale reading
  if (!adsOk) return;
  float baseline[4] = {0, 0, 0, 0};
  int16_t baselineMin[4] = {32767, 32767, 32767, 32767};
  int16_t baselineMax[4] = {-32768, -32768, -32768, -32768};
  long sums[4] = {0, 0, 0, 0};
  unsigned long count = 0;
  unsigned long start = millis();
  unsigned long lastDraw = 0;

  setStageTotal(GLUCOSE_BASELINE_MS);
  drawGlucoseStage("Keep the strip dry.", "Collecting baseline...");
  while (millis() - start < GLUCOSE_BASELINE_MS) {
    int16_t v[4];
    readAllChannels(v);
    for (int i = 0; i < 4; i++) {
      sums[i] += v[i];
      if (v[i] < baselineMin[i]) baselineMin[i] = v[i];
      if (v[i] > baselineMax[i]) baselineMax[i] = v[i];
    }
    count++;
    unsigned long elapsed = millis() - start;
    if (elapsed - lastDraw > 500) {
      drawGlucoseCountdown(GLUCOSE_BASELINE_MS - elapsed);
      lastDraw = elapsed;
    }
    delay(GLUCOSE_SAMPLE_MS);
  }
  if (count == 0) {
    drawGlucoseResult(false, 0, 0, 0, 0, 0, 0, 0, 0);
    return;
  }
  for (int i = 0; i < 4; i++) baseline[i] = (float)sums[i] / count;
  const int16_t baselineA3Min = baselineMin[3];
  const int16_t baselineA3Max = baselineMax[3];
  const float baselineA3 = baseline[3];
  const int32_t baselineRange = (int32_t)baselineA3Max - baselineA3Min;
  const float detectionThreshold = max(25.0f, (float)baselineRange * 0.75f);
  Serial.println("========== BASELINE ==========");
  for (int i = 0; i < 4; i++)
    Serial.printf("A%d baseline=%.2f min=%d max=%d\n", i, baseline[i], baselineMin[i], baselineMax[i]);
  Serial.printf("A3 detection threshold=%.1f counts\n", detectionThreshold);
  Serial.println("time_ms,A0,A1,A2,A3,dA0,dA1,dA2,dA3,response_detected");

  setStageTotal(GLUCOSE_RECORD_MS);
  drawGlucoseStage("Apply blood now.", "Watching for a sustained A3 change...");

  const int REQUIRED_CONFIRM_SAMPLES = 5;
  const unsigned long RESPONSE_WINDOW_MS = 1000;
  int confirmCount = 0;
  int16_t confirmMinA3 = 32767;
  int16_t confirmMaxA3 = -32768;
  bool responseDetected = false;
  bool responseWindowStarted = false;
  unsigned long responseStart = 0;
  unsigned long lastSample = 0;
  lastDraw = 0;
  int16_t responseMinA3 = 32767;
  int16_t responseMaxA3 = -32768;
  int16_t observedMinA3 = 32767;
  int16_t observedMaxA3 = -32768;
  unsigned long recordStart = millis();

  while (millis() - recordStart < GLUCOSE_RECORD_MS) {
    if (millis() - lastSample < GLUCOSE_SAMPLE_MS) continue;
    lastSample = millis();
    int16_t v[4];
    readAllChannels(v);
    float d[4];
    for (int i = 0; i < 4; i++) d[i] = (float)v[i] - baseline[i];
    if (v[3] < observedMinA3) observedMinA3 = v[3];
    if (v[3] > observedMaxA3) observedMaxA3 = v[3];
    bool outsideBaseline = (d[3] >= detectionThreshold) || (d[3] <= -detectionThreshold);
    if (!responseWindowStarted) {
      if (outsideBaseline) {
        if (confirmCount == 0) {
          confirmMinA3 = v[3];
          confirmMaxA3 = v[3];
        } else {
          if (v[3] < confirmMinA3) confirmMinA3 = v[3];
          if (v[3] > confirmMaxA3) confirmMaxA3 = v[3];
        }
        confirmCount++;
      } else {
        confirmCount = 0;
        confirmMinA3 = 32767;
        confirmMaxA3 = -32768;
      }
      if (confirmCount >= REQUIRED_CONFIRM_SAMPLES) {
        responseDetected = true;
        responseWindowStarted = true;
        responseStart = millis();
        responseMinA3 = confirmMinA3;
        responseMaxA3 = confirmMaxA3;
        drawGlucoseStage("Response detected.", "Finishing brief capture...");
        Serial.printf("A3_RESPONSE_START_MS=%lu\n", responseStart - recordStart);
      }
    } else {
      if (v[3] < responseMinA3) responseMinA3 = v[3];
      if (v[3] > responseMaxA3) responseMaxA3 = v[3];
    }
    Serial.printf("%lu,%d,%d,%d,%d,%.2f,%.2f,%.2f,%.2f,%d\n", millis() - recordStart, v[0], v[1], v[2],
                  v[3], d[0], d[1], d[2], d[3], responseWindowStarted ? 1 : 0);
    unsigned long elapsed = millis() - recordStart;
    if (elapsed - lastDraw > 500) {
      drawGlucoseCountdown(GLUCOSE_RECORD_MS - elapsed);
      lastDraw = elapsed;
    }
    if (responseWindowStarted && millis() - responseStart >= RESPONSE_WINDOW_MS) break;
  }
  Serial.println("========== RESPONSE WATCH COMPLETE ==========");
  Serial.printf("A3 baseline average=%.2f min=%d max=%d range=%ld\n", baselineA3, baselineA3Min,
                baselineA3Max, (long)baselineRange);
  Serial.printf("A3 observed during watch min=%d max=%d\n", observedMinA3, observedMaxA3);

  if (!responseDetected || responseMinA3 == 32767 || responseMaxA3 == -32768) {
    Serial.println("NO VALID RESPONSE within 30 seconds: no sustained A3 change detected.");
    drawGlucoseResult(false, 0, 0, 0, baselineA3, baselineA3Min, baselineA3Max, observedMinA3, observedMaxA3);
    return;
  }

  // Experimental, not clinically validated: calibration was fitted to raw A3 peaks.
  float a3Mid = ((float)responseMinA3 + (float)responseMaxA3) / 2.0f;
  float lowerEstimate = GLUCOSE_SLOPE * (float)responseMinA3 + GLUCOSE_INTERCEPT;
  float midpointEstimate = GLUCOSE_SLOPE * a3Mid + GLUCOSE_INTERCEPT;
  float upperEstimate = GLUCOSE_SLOPE * (float)responseMaxA3 + GLUCOSE_INTERCEPT;
  Serial.printf("A3_RESPONSE_MIN=%d A3_RESPONSE_MAX=%d A3_RESPONSE_MID=%.1f\n", responseMinA3, responseMaxA3, a3Mid);
  Serial.printf("Experimental midpoint estimate=%.1f mg/dL (range %.1f to %.1f)\n",
                midpointEstimate, lowerEstimate, upperEstimate);
  Serial.println("WARNING: midpoint/min/max features require new calibration; not clinically validated.");

  // Backend accepts 40-600 mg/dL.
  if (midpointEstimate >= 40.0f && midpointEstimate <= 600.0f) {
    lastGlucoseValue = midpointEstimate;
    lastGlucoseValid = true;
  }
  drawGlucoseResult(true, midpointEstimate, lowerEstimate, upperEstimate, baselineA3, baselineA3Min,
                    baselineA3Max, responseMinA3, responseMaxA3);
}

// ============================================================
// FASTING QUESTION + POST GLUCOSE
// The models treat glucose as FASTING blood glucose, so the user is asked.
// Returns 1 = fasting, 0 = not fasting.
// ============================================================
int askFasting() {
  drawHeader("Glucose Test");
  drawCard(20, 60, 440, 130);
  T("Was this measured fasting?", 240, 100, F_TITLE, COL_TEXT, COL_WHITE, AL_C);
  T("(no food or drink except water for 8+ hours)", 240, 135, F_BODY, COL_TEXT_SOFT, COL_WHITE, AL_C);
  display.fillRoundRect(40, 215, 180, 50, 25, COL_PRIMARY);
  T("Yes", 130, 240, F_BODYB, COL_WHITE, COL_PRIMARY, AL_C);
  display.fillRoundRect(260, 215, 180, 50, 25, COL_AMBER);
  T("No", 350, 240, F_BODYB, COL_WHITE, COL_AMBER, AL_C);
  waitTouchRelease();
  while (true) {
    int x, y;
    if (readTouch(x, y) && y >= 215 && y <= 265) {
      int r = (x < 240) ? 1 : 0;
      waitTouchRelease();
      return r;
    }
    delay(30);
  }
}

// POST /device/glucose. Retrying with the same client_ref returns the same stored reading.
int postGlucoseReading(float value, int fasting) {
  if (lastReadingRef.length() == 0)
    lastReadingRef = String((uint32_t)esp_random(), HEX) + String((uint32_t)esp_random(), HEX);
  String body = "device_id=" + API_KEY + "&value=" + String(value, 1) +
                "&fasting=" + (fasting ? "true" : "false") + "&client_ref=" + lastReadingRef;
  String resp;
  int code = postForm("/device/glucose", body, resp);
  if (code == 200 || code == 201) lastReadingRef = "";  // done: the next test gets a new ref
  return code;
}

void runGlucoseFlow() {
  runGlucoseTest();
  if (!lastGlucoseValid) {  // the result screen already explained "no clear response"
    showQRScreen();
    return;
  }
  int fasting = askFasting();
  showBusy("Saving reading...");
  int code = postGlucoseReading(lastGlucoseValue, fasting);
  while (code != 200 && code != 201) {  // retry keeps the same client_ref: no duplicates
    String msg = errorFromResponse("", code);
    showMessage("Could not save reading", msg, "Tap to retry");
    showBusy("Saving reading...");
    code = postGlucoseReading(lastGlucoseValue, fasting);
    if (code != 200 && code != 201 && (code == 400 || code == 403)) break;  // not retryable
  }
  if (code == 200 || code == 201) {
    drawHeader("Reading saved");
    drawCard(20, 70, 440, 140);
    T("Glucose saved to your account.", 240, 110, F_BODYB, COL_TEXT, COL_WHITE, AL_C);
    T("Scan the QR code to upload your", 240, 150, F_BODY, COL_TEXT_SOFT, COL_WHITE, AL_C);
    T("microbiome CSV and run the analysis.", 240, 175, F_BODY, COL_TEXT_SOFT, COL_WHITE, AL_C);
    T("Tap to continue", 240, 262, F_BODYB, COL_PRIMARY, COL_BG, AL_C);
    delay(300);
    int x, y;
    waitTouchRelease();
    while (!readTouch(x, y)) delay(30);
    waitTouchRelease();
  } else {
    lastReadingRef = "";
    showMessage("Reading not saved", errorFromResponse("", code), "Tap to continue");
  }
  showQRScreen();
}

// ============================================================
// BOOT SPLASH - leaves fly in and form the Nutra Drug logo (unchanged)
// 8-bit sprite 240x224 = 52 KB; falls back to a plain screen if RAM is short.
// ============================================================
#define F_SERIF  (&lgfx::fonts::FreeSansBold18pt7b)
#define SPL_W 240
#define SPL_H 224
#define SPL_X 120
#define SPL_Y 6
static const float SPL_K   = 0.58f;
static const float SPL_OUT = 4.0f;
static uint16_t SP_DARK, SP_LIME, SP_WHITE;

static float rndf(float a, float b) { return a + (b - a) * ((float)esp_random() / 4294967295.0f); }
static float clamp01(float v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
static float easeOutCubic(float p) { float q = 1.0f - p; return 1.0f - q * q * q; }
static float easeOutBack(float p) {
  const float c1 = 1.4f;
  const float c3 = c1 + 1.0f;
  float q = p - 1.0f;
  return 1.0f + c3 * q * q * q + c1 * q * q;
}

static void splEdge(float &x, float &y) {
  if (esp_random() & 1) {
    x = rndf(-50, SPL_W + 50);
    y = (esp_random() & 1) ? -50 : SPL_H + 50;
  } else {
    y = rndf(-50, SPL_H + 50);
    x = (esp_random() & 1) ? -50 : SPL_W + 50;
  }
}

static void splFan(LGFX_Sprite& s, const float* px, const float* py, int n, float fx, float fy, uint16_t col) {
  for (int i = 0; i < n; i++) {
    int j = (i + 1) % n;
    s.fillTriangle((int)lroundf(fx), (int)lroundf(fy), (int)lroundf(px[i]), (int)lroundf(py[i]),
                   (int)lroundf(px[j]), (int)lroundf(py[j]), col);
  }
}

static void splLeaf(LGFX_Sprite& s, float cx, float cy, float len, float wid, float ang, uint16_t col) {
  const int N = 7;
  float px[2 * N + 2];
  float py[2 * N + 2];
  int n = 0;
  float ca = cosf(ang), sa = sinf(ang);
  for (int i = 0; i <= N; i++) {
    float t = i / (float)N;
    float w = wid * 0.5f * fabsf(sinf(PI * t));
    float al = (t - 0.5f) * len;
    px[n] = cx + ca * al - sa * w;
    py[n] = cy + sa * al + ca * w;
    n++;
  }
  for (int i = N - 1; i >= 1; i--) {
    float t = i / (float)N;
    float w = wid * 0.5f * fabsf(sinf(PI * t));
    float al = (t - 0.5f) * len;
    px[n] = cx + ca * al + sa * w;
    py[n] = cy + sa * al - ca * w;
    n++;
  }
  splFan(s, px, py, n, cx, cy, col);
}

static void splLeafFull(LGFX_Sprite& s, float cx, float cy, float len, float wid, float ang, float sc, bool rib) {
  float L = len * sc;
  float W = wid * sc;
  if (L < 3 || W < 2) return;
  float b = SPL_OUT * sc;
  splLeaf(s, cx, cy, L, W, ang, SP_DARK);
  if (W - 2 * b >= 2 && L - 2.4f * b >= 2) splLeaf(s, cx, cy, L - 2.4f * b, W - 2 * b, ang, SP_LIME);
  if (rib && sc > 0.5f) {
    float ca = cosf(ang), sa = sinf(ang);
    for (int o = 0; o < 2; o++) {
      s.drawLine((int)(cx - ca * L * 0.30f + o * sa), (int)(cy - sa * L * 0.30f - o * ca),
                 (int)(cx + ca * L * 0.28f + o * sa), (int)(cy + sa * L * 0.28f - o * ca), SP_DARK);
    }
  }
}

static void splCapsule(LGFX_Sprite& s, float cx, float cy, float L, float R, float a, float sc) {
  L *= sc;
  R *= sc;
  if (R < 3) return;
  float ca = cosf(a), sa = sinf(a);
  float h = L * 0.5f - R;
  if (h < 0) h = 0;
  float tx = cx + ca * h, ty = cy + sa * h;
  float bx = cx - ca * h, by = cy - sa * h;
  float px[20], py[20];
  int n = 0;
  const int M = 6;
  for (int k = 0; k <= M; k++) {
    float th = a - PI / 2 + PI * k / M;
    px[n] = tx + R * cosf(th);
    py[n] = ty + R * sinf(th);
    n++;
  }
  for (int k = 0; k <= M; k++) {
    float th = a + PI / 2 + PI * k / M;
    px[n] = bx + R * cosf(th);
    py[n] = by + R * sinf(th);
    n++;
  }
  splFan(s, px, py, n, cx, cy, SP_DARK);
  float b = SPL_OUT * sc;
  float r2 = R - b;
  if (r2 < 2) return;
  float g = 5.0f * sc;
  float vx = cosf(a + PI / 2), vy = sinf(a + PI / 2);
  float mx = cx + ca * g, my = cy + sa * g;
  float qx[4] = { cx + vx * r2, cx - vx * r2, mx - vx * r2, mx + vx * r2 };
  float qy[4] = { cy + vy * r2, cy - vy * r2, my - vy * r2, my + vy * r2 };
  splFan(s, qx, qy, 4, (cx + mx) / 2, (cy + my) / 2, SP_WHITE);
  n = 0;
  for (int k = 0; k <= M; k++) {
    float th = a - PI / 2 + PI * k / M;
    px[n] = tx + r2 * cosf(th);
    py[n] = ty + r2 * sinf(th);
    n++;
  }
  px[n] = mx + vx * r2; py[n] = my + vy * r2; n++;
  px[n] = mx - vx * r2; py[n] = my - vy * r2; n++;
  splFan(s, px, py, n, (mx + tx) / 2, (my + ty) / 2, SP_LIME);
}

struct SplPart {
  int kind;            // 0 = leaf, 1 = capsule
  float fx, fy;        // final position
  float len, wid;      // leaf: len/wid   capsule: len/radius
  float ang;           // final angle
  float sx, sy, rot;   // random start
  float delay;
  bool rib;
};

void playBootSplash() {
  SP_WHITE = display.color565(255, 255, 255);
  SP_DARK  = display.color565(14, 92, 50);
  SP_LIME  = display.color565(190, 214, 52);
  display.fillScreen(SP_WHITE);
  LGFX_Sprite spr(&display);
  spr.setColorDepth(8);
  if (spr.createSprite(SPL_W, SPL_H)) {
    auto mx = [](float rx) { return 120.0f + (rx - 352.0f) * SPL_K; };
    auto my = [](float ry) { return   6.0f + (ry -  95.0f) * SPL_K; };
    SplPart parts[5] = {
      { 0, mx(262), my(310), 290 * SPL_K, 108 * SPL_K, -2.02f, 0, 0, 0,   0, true  },
      { 0, mx(368), my(372), 140 * SPL_K,  62 * SPL_K, -0.99f, 0, 0, 0, 150, true  },
      { 0, mx(266), my(150),  92 * SPL_K,  52 * SPL_K,  0.80f, 0, 0, 0, 300, false },
      { 0, mx(470), my(247), 125 * SPL_K,  62 * SPL_K, -0.77f, 0, 0, 0, 450, true  },
      { 1, mx(385), my(190), 205 * SPL_K,  52 * SPL_K, -1.26f, 0, 0, 0, 750, false }
    };
    for (int i = 0; i < 5; i++) {
      splEdge(parts[i].sx, parts[i].sy);
      parts[i].rot = rndf(-4.0f, 4.0f);
    }
    const int NP = 8;
    float psx[NP], psy[NP], ptx[NP], pty[NP], pd[NP], pdur[NP], pa[NP];
    uint16_t pc[NP];
    for (int i = 0; i < NP; i++) {
      splEdge(psx[i], psy[i]);
      ptx[i] = rndf(70, 170);
      pty[i] = rndf(50, 150);
      pd[i]  = rndf(0, 1000);
      pdur[i] = rndf(800, 1200);
      pa[i]  = rndf(0, 6.28f);
      pc[i]  = (i & 1) ? SP_LIME : SP_DARK;
    }
    const float PART_DUR = 1250.0f;
    const float TOTAL    = 2300.0f;
    unsigned long t0 = millis();
    while (true) {
      float t = (float)(millis() - t0);
      spr.fillScreen(SP_WHITE);
      for (int i = 0; i < NP; i++) {
        float p = clamp01((t - pd[i]) / pdur[i]);
        if (p <= 0 || p >= 1) continue;
        float e = easeOutCubic(p);
        float rem = 1.0f - e;
        float rx = (psx[i] - ptx[i]) * rem;
        float ry = (psy[i] - pty[i]) * rem;
        float phi = rem * 2.6f;
        float x = ptx[i] + rx * cosf(phi) - ry * sinf(phi);
        float y = pty[i] + rx * sinf(phi) + ry * cosf(phi);
        splLeaf(spr, x, y, 26 * sinf(PI * p) + 2, 12 * sinf(PI * p) + 1, pa[i] + p * 6.0f, pc[i]);
      }
      for (int i = 0; i < 5; i++) {
        SplPart& q = parts[i];
        float p = clamp01((t - q.delay) / PART_DUR);
        if (p <= 0) continue;
        float e = easeOutBack(p);
        float rem = 1.0f - e;
        float rx = (q.sx - q.fx) * rem;
        float ry = (q.sy - q.fy) * rem;
        float phi = rem * 1.3f;
        float x = q.fx + rx * cosf(phi) - ry * sinf(phi);
        float y = q.fy + rx * sinf(phi) + ry * cosf(phi);
        float ang = q.ang + rem * q.rot;
        float sc  = 0.2f + 0.8f * e;
        if (q.kind == 0) splLeafFull(spr, x, y, q.len, q.wid, ang, sc, q.rib);
        else             splCapsule(spr, x, y, q.len, q.wid, ang, sc);
      }
      spr.pushSprite(SPL_X, SPL_Y);
      if (t >= TOTAL) break;
    }
    spr.deleteSprite();
  }
  for (int k = 1; k <= 6; k++) {
    uint16_t c = mixRGB(255, 255, 255, 14, 92, 50, k / 6.0f);
    T("Nutra Drug", 240, 268, F_SERIF, c, TRANSP, AL_C);
    delay(55);
  }
  for (int k = 1; k <= 5; k++) {
    uint16_t c = mixRGB(255, 255, 255, 96, 115, 98, k / 5.0f);
    T("Gut Health Analyzer", 240, 298, F_BODY, c, TRANSP, AL_C);
    delay(45);
  }
  delay(700);
}

// ============================================================
// EMAIL KEYBOARD + DEVICE CLAIM (/request-otp -> /verify-otp; unchanged)
// ============================================================
struct KbKey { int x, y, w, h; char c; };
KbKey kbKeys[40];
int kbCount = 0;
bool kbSym = false;
const char* KB_LET[3] = {"qwertyuiop", "asdfghjkl", "zxcvbnm"};
const char* KB_SYM[3] = {"1234567890", "@.-_+&%=#", "!?/:;,'*$"};

void kbAdd(int x, int y, int w, int h, char c) {
  if (kbCount < 40) {
    kbKeys[kbCount].x = x; kbKeys[kbCount].y = y; kbKeys[kbCount].w = w; kbKeys[kbCount].h = h;
    kbKeys[kbCount].c = c; kbCount++;
  }
}

void kbBuild() {
  kbCount = 0;
  const int kh = 40, gap = 4, y0 = 88, kw = 44;
  for (int r = 0; r < 3; r++) {
    const char* row = kbSym ? KB_SYM[r] : KB_LET[r];
    int n = strlen(row);
    int extra = (r == 2) ? (62 + gap) : 0;
    int total = n * kw + (n - 1) * gap + extra;
    int x = (480 - total) / 2;
    int y = y0 + r * (kh + gap);
    for (int i = 0; i < n; i++) { kbAdd(x, y, kw, kh, row[i]); x += kw + gap; }
    if (r == 2) kbAdd(x, y, 62, kh, '\b');
  }
  int widths[6] = {72, 56, 56, 56, 56, 120};
  char cs[6] = {'\t', '@', '.', '_', '-', '\r'};  // \t = 123/abc toggle, \r = OK
  int total = 5 * gap;
  for (int i = 0; i < 6; i++) total += widths[i];
  int x = (480 - total) / 2;
  int y = y0 + 3 * (kh + gap);
  for (int i = 0; i < 6; i++) { kbAdd(x, y, widths[i], kh, cs[i]); x += widths[i] + gap; }
}

void kbDrawKey(int i, bool pressed) {
  KbKey &k = kbKeys[i];
  bool special = (k.c == '\b' || k.c == '\t' || k.c == '\r');
  uint16_t fill = pressed ? (special ? COL_PRIMARY_DARK : COL_ACCENT_SOFT)
                          : (special ? COL_SECONDARY : COL_WHITE);
  uint16_t fg = special ? COL_WHITE : COL_TEXT;
  int dy = pressed ? 1 : 0;
  display.fillRect(k.x - 1, k.y - 1, k.w + 3, k.h + 4, COL_BG);
  display.fillRoundRect(k.x, k.y + dy, k.w, k.h, 8, fill);
  display.drawRoundRect(k.x, k.y + dy, k.w, k.h, 8, special ? COL_PRIMARY_DARK : COL_BORDER);
  int cx = k.x + k.w / 2, cy = k.y + dy + k.h / 2;
  if (k.c == '\b') drawBackspaceIcon(cx, cy, fg, fill);
  else if (k.c == '\r') drawCheckIcon(cx, cy, fg);
  else if (k.c == '\t') T(kbSym ? "abc" : "123", cx, cy, F_BODYB, fg, fill, AL_C);
  else { char sb[2] = {k.c, 0}; T(sb, cx, cy, F_BODYB, fg, fill, AL_C); }
}

void kbDrawAll() {
  display.fillRect(0, 86, 480, 184, COL_BG);
  for (int i = 0; i < kbCount; i++) kbDrawKey(i, false);
}

void kbDrawField(const String& text, bool error) {
  display.fillRoundRect(10, 52, 460, 30, 10, COL_WHITE);
  display.drawRoundRect(10, 52, 460, 30, 10, error ? COL_RED : COL_BORDER);
  if (text.length() == 0) {
    T("name@example.com", 22, 67, F_BODY, COL_TEXT_SOFT, COL_WHITE);
    return;
  }
  String shown = text;
  display.setFont(F_BODY);
  display.setTextSize(1);
  while (shown.length() > 1 && display.textWidth(shown.c_str()) > 420) shown.remove(0, 1);
  shown += "_";
  T(shown.c_str(), 22, 67, F_BODY, COL_TEXT, COL_WHITE);
}

bool looksLikeEmail(const String& e) {
  int at = e.indexOf('@');
  int dot = e.lastIndexOf('.');
  return e.length() >= 6 && at > 0 && dot > at + 1 && !e.endsWith(".");
}

String captureEmail() {
  String text = "";
  kbSym = false;
  drawHeader("Enter registered email");
  kbBuild();
  kbDrawAll();
  kbDrawField(text, false);
  while (true) {
    int tx, ty;
    if (!readTouch(tx, ty)) { delay(20); continue; }
    int hit = -1;
    for (int i = 0; i < kbCount; i++) {
      if (tx >= kbKeys[i].x && tx <= kbKeys[i].x + kbKeys[i].w &&
          ty >= kbKeys[i].y && ty <= kbKeys[i].y + kbKeys[i].h) { hit = i; break; }
    }
    if (hit < 0) continue;
    char c = kbKeys[hit].c;
    kbDrawKey(hit, true);
    delay(70);
    waitTouchRelease();
    if (c == '\t') {
      kbSym = !kbSym;
      kbBuild();
      kbDrawAll();
      continue;
    }
    kbDrawKey(hit, false);
    if (c == '\b') {
      if (text.length()) text.remove(text.length() - 1);
    } else if (c == '\r') {
      if (looksLikeEmail(text)) return text;
      kbDrawField(text, true);
      delay(600);
    } else if (text.length() < 60) {
      text += c;
    }
    kbDrawField(text, false);
  }
}

void runClaimWizard() {
  while (true) {
    String email = captureEmail();
    String body = "device_id=" + API_KEY + "&email=" + urlEncode(email);
    String resp;
    int code;
    // The backend links the device only after the emailed code is verified.
    while (true) {
      showBusy("Sending code to your email...");
      code = postForm("/request-otp", body, resp);
      if (code != 200) {
        showMessage("Could not send code", errorFromResponse(resp, code), "Tap to change email");
        break;  // back to email entry
      }
      String otp = capturePassword("Check your email", "Enter the 6-digit code");
      if (otp.length() != 6) {
        showMessage("Invalid code", "The code has 6 digits.", "Tap to get a new code");
        continue;
      }
      showBusy("Verifying...");
      code = postForm("/verify-otp", "device_id=" + API_KEY + "&otp=" + otp, resp);
      if (code == 200) {
        prefs.putBool("claimed", true);
        Serial.println("Device claimed and verified.");
        return;
      }
      showMessage("Verification failed", errorFromResponse(resp, code), "Tap to get a new code");
    }
  }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  display.init();
  display.setRotation(1);
  initPalette();
  playBootSplash();

  Wire.begin(ADS_SDA_PIN, ADS_SCL_PIN);
  if (ads.begin(0x48, &Wire)) {
    ads.setGain(GAIN_ONE);
    adsOk = true;
    Serial.println("ADS1115: OK (SDA=8, SCL=9, gain=GAIN_ONE)");
  } else {
    adsOk = false;
    Serial.println("ADS1115: NOT FOUND - glucose test button disabled");
  }

  prefs.begin("gutdevice", false);

  // BOOT button held at power-on = factory reset (clears password + API key)
  pinMode(0, INPUT_PULLUP);
  bool setupDone = prefs.getBool("setup_done", false);
  if (setupDone && digitalRead(0) == LOW) {
    Serial.println("BOOT held at boot - factory reset (clearing password + API key)");
    prefs.clear();
    API_KEY = "";
    setupDone = false;
  }

  if (!setupDone) runSetupWizard();
  else API_KEY = prefs.getString("apikey", "");

  runLoginGate();

  WiFiManager wm;
  String apName = "GutHealth-Setup-" + API_KEY;
  wm.autoConnect(apName.c_str());

  // Link device to account (first boot only)
  if (!prefs.getBool("claimed", false)) runClaimWizard();

  showQRScreen();

  // Remember the newest existing report so only NEW analyses pop up on the screen.
  pollLatest(true);
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  int tx, ty;
  if (readTouch(tx, ty)) {
    if (onQRScreenNow) {
      // QR screen: only the glucose button is active
      if (adsOk && hitGlucoseButton(tx, ty)) {
        delay(200);
        waitTouchRelease();
        runGlucoseFlow();
      }
    } else {
      // "Analysis Ready" summary or "QR failed" screen
      delay(150);
      if (resultShown && adsOk && hitGlucoseButton(tx, ty)) {
        waitTouchRelease();
        runGlucoseFlow();
      } else {
        waitTouchRelease();
        showQRScreen();  // tap anywhere: back to the QR / retry
      }
    }
  }
  if (onQRScreenNow && millis() - lastPoll > POLL_INTERVAL_MS) {
    pollLatest();
    lastPoll = millis();
  }
}
