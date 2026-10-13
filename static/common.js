// Shared helpers for the three pages. Session token lives in sessionStorage (cleared when the tab closes).
const TK = 'gut_session';
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = iso => iso ? new Date(iso).toLocaleString() : '-';   // browser local timezone
function token() { try { return sessionStorage.getItem(TK); } catch (e) { return null; } }
function logout() { try { sessionStorage.removeItem(TK); } catch (e) {} location.reload(); }

async function api(path, opts = {}) {
  const h = Object.assign({}, opts.headers || {});
  if (token()) h['Authorization'] = 'Bearer ' + token();
  let res;
  try { res = await fetch(path, Object.assign({}, opts, { headers: h })); }
  catch (e) { throw { status: 0, message: 'Network error. Check your connection.' }; }
  if (res.status === 401) { try { sessionStorage.removeItem(TK); } catch (e) {} showLogin('Session expired. Please log in again.'); throw { status: 401, message: 'Session expired.' }; }
  if (opts.raw) return res;
  let body = null; try { body = await res.json(); } catch (e) {}
  if (!res.ok) {
    const d = body && (body.detail ?? body.error);
    throw { status: res.status, message: (d && d.error) || (typeof d === 'string' ? d : 'Request failed (' + res.status + ')'), data: d };
  }
  return body;
}

function navBar(active) {
  const l = [['/app', 'Analyze'], ['/dashboard/glucose', 'Glucose history'], ['/dashboard/reports', 'Reports']];
  return '<nav>' + l.map(([h, t]) => `<a href="${h}" class="${h === active ? 'on' : ''}">${t}</a>`).join('') +
    '<button class="link" onclick="logout()">Log out</button></nav>';
}

// Email OTP login (same registered email + device link used by the ESP32).
function showLogin(msg) {
  const box = $('#login'); const main = $('#main');
  if (main) main.hidden = true; box.hidden = false;
  box.innerHTML = `<h2>Log in</h2><p class="muted">Use the email registered for your device. We will email a 6-digit code.</p>
   <p class="err" id="lerr">${esc(msg || '')}</p>
   <input id="lemail" type="email" placeholder="you@example.com" autocomplete="email">
   <button id="lsend">Send code</button>
   <div id="lstep2" hidden><input id="lotp" inputmode="numeric" maxlength="6" placeholder="6-digit code"><button id="lgo">Verify</button></div>`;
  $('#lsend').onclick = async () => {
    try { await api('/auth/request-otp', { method: 'POST', body: new URLSearchParams({ email: $('#lemail').value }) });
      $('#lstep2').hidden = false; $('#lerr').textContent = 'If the email is registered, a code was sent.'; }
    catch (e) { $('#lerr').textContent = e.message; } };
  $('#lgo').onclick = async () => {
    try { const r = await api('/auth/verify-otp', { method: 'POST', body: new URLSearchParams({ email: $('#lemail').value, otp: $('#lotp').value }) });
      sessionStorage.setItem(TK, r.token); location.reload(); }
    catch (e) { $('#lerr').textContent = e.message; } };
}
function requireLogin(start) {
  if (!token()) { showLogin(); return; }
  $('#login').hidden = true; $('#main').hidden = false; start();
}
const CSS = `body{font-family:system-ui,sans-serif;background:#f4f9f5;color:#1b2e1d;margin:0}
nav{background:#1b5e20;padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
nav a{color:#dcf0de;text-decoration:none;padding:4px 10px;border-radius:14px}nav a.on{background:#fff;color:#1b5e20}
.link{margin-left:auto;background:none;border:0;color:#dcf0de;cursor:pointer}
main,#login{max-width:880px;margin:18px auto;padding:0 14px}#login{background:#fff;padding:20px;border-radius:12px}
.card{background:#fff;border:1px solid #d0e2d2;border-radius:12px;padding:14px;margin:12px 0;overflow-x:auto}
button{background:#2e7d32;color:#fff;border:0;border-radius:18px;padding:8px 16px;cursor:pointer;margin:2px}
button:disabled{opacity:.5}button.sec{background:#dcf0de;color:#1b5e20}
input,select{padding:8px;border:1px solid #d0e2d2;border-radius:8px;margin:2px;max-width:100%}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e3eee4;padding:6px;text-align:left;font-size:14px}
.muted{color:#607362;font-size:13px}.err{color:#d32f2f}.ok{color:#2e7d32}.hl{background:#dcf0de}
.warn{background:#fdecea;border:1px solid #ef8282;border-radius:8px;padding:8px;font-size:13px}
.pill{border-radius:10px;padding:2px 8px;font-size:12px;background:#dcf0de}.pill.failed{background:#fdecea}
@media(prefers-color-scheme:dark){body{background:#101a12;color:#e4efe5}.card,#login{background:#18261b;border-color:#2c4a33}td,th{border-color:#2c4a33}input,select{background:#101a12;color:inherit;border-color:#2c4a33}}`;
document.head.insertAdjacentHTML('beforeend', '<style>' + CSS + '</style>');
