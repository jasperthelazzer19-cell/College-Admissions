"""Candor CSS + static HTML/SVG template strings. Extracted verbatim from app.py.
Imported via `from candor_styles import *`. No logic here.
"""

BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600;6..72,700&display=swap');
:root{
  --bg:#070d14;
  --bg-2:#0a131c;
  --surface:#101a25;
  --surface-2:#16222f;
  --surface-hover:#1a2735;
  --border:rgba(255,255,255,.06);
  --border-strong:rgba(255,255,255,.10);
  --text:#e6edf3;
  --text-2:#9aa6b6;
  --text-3:#5e6b7c;
  --teal:#5fc9b6;
  --teal-2:#36b8a8;
  --teal-dim:#0f3a37;
  --blue:#38bdf8;
  --accent-grad:linear-gradient(135deg,#38bdf8 0%,#5fc9b6 100%);
  --glow:0 0 24px rgba(95,201,182,.18);
  --shadow-card:0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
html,body{background:var(--bg);font-family:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif}
body{
  margin:0;
  font-family:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;
  background:radial-gradient(ellipse at 12% -10%, rgba(56,189,248,.07), transparent 50%),
             radial-gradient(ellipse at 88% 110%, rgba(95,201,182,.06), transparent 50%),
             var(--bg);
  background-attachment:fixed;
  color:var(--text);
  line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--teal);text-decoration:none}
a:hover{color:#79d8c8}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px 80px}
.nav{
  display:flex;align-items:center;gap:22px;padding:16px 28px;
  background:rgba(10,19,28,.85);
  backdrop-filter:saturate(140%) blur(12px);
  -webkit-backdrop-filter:saturate(140%) blur(12px);
  border-bottom:1px solid var(--border);
  margin-bottom:32px;
  position:sticky;top:0;z-index:10;
}
.nav .brand{font-weight:700;font-size:1.05em;color:var(--text);letter-spacing:-.2px}
.nav a{color:var(--text-2);font-size:.91em;font-weight:500;transition:color .15s}
.nav a:hover{color:var(--text)}
.nav .sp{flex:1}
h1,h2,h3{font-family:'Newsreader',Georgia,'Times New Roman',serif;font-feature-settings:"ss01","ss02"}
h1{font-size:2.2em;font-weight:600;letter-spacing:-.5px;margin:8px 0 8px;color:var(--text);line-height:1.15}
h2{font-size:1.45em;font-weight:600;margin:28px 0 12px;color:var(--text);letter-spacing:-.3px;line-height:1.25}
h3{font-size:1.12em;font-weight:600;margin:14px 0 6px;color:var(--text);letter-spacing:-.15px}
p{color:var(--text)}
.muted{color:var(--text-2);font-size:.92em}
.btn{
  display:inline-block;
  background:var(--surface-2);color:var(--text);
  font-weight:600;padding:11px 22px;border-radius:4px;border:1px solid var(--border-strong);
  cursor:pointer;font-size:.93em;text-decoration:none;font-family:inherit;
  transition:all .15s ease;
}
.btn:hover{background:var(--surface-hover);border-color:rgba(95,201,182,.3);color:var(--text);text-decoration:none}
.btn-primary{
  background:var(--accent-grad);color:#031715;border:0;
  box-shadow:0 8px 20px rgba(56,189,248,.18);
  font-weight:700;
}
.btn-primary:hover{filter:brightness(1.05);box-shadow:0 10px 26px rgba(56,189,248,.24);color:#031715}
.btn-light{background:transparent;color:var(--text);border:1px solid var(--border-strong)}
.btn-light:hover{background:var(--surface-2);color:var(--text)}
.btn-sm{font-size:.82em;padding:7px 14px;border-radius:4px}
.card{
  background:var(--surface)!important;
  border:1px solid var(--border)!important;
  border-radius:6px;padding:22px;margin-bottom:14px;
  box-shadow:var(--shadow-card);
  color:var(--text)!important;
}
.card h3, .card h2, .card h1, .card p, .card div, .card span, .card li{color:var(--text)}
.card .muted, .card .muted *{color:var(--text-2)!important}
label{display:block;font-weight:500;font-size:.85em;margin:14px 0 6px;color:var(--text-2);letter-spacing:.1px}
input,select,textarea{
  width:100%;padding:11px 13px;
  border:1px solid var(--border-strong);border-radius:4px;
  font-size:.93em;font-family:inherit;
  background:var(--bg-2);color:var(--text);
  transition:border-color .15s, box-shadow .15s;
}
textarea{min-height:80px;resize:vertical}
input:focus,select:focus,textarea:focus{
  outline:0;border-color:var(--teal);
  box-shadow:0 0 0 3px rgba(95,201,182,.12);
}
input[type="checkbox"]{accent-color:var(--teal);width:auto}
input::placeholder,textarea::placeholder{color:var(--text-3)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:560px){.row{grid-template-columns:1fr}}
.checks label{display:flex;align-items:center;gap:8px;font-weight:500;margin:8px 0;color:var(--text)}
.checks input{width:auto}
.pill{display:inline-block;padding:4px 11px;border-radius:999px;font-size:.7em;font-weight:600;letter-spacing:.5px;text-transform:uppercase;border:1px solid var(--border-strong)}
.pill-dream{background:rgba(244,114,182,.12);color:#f9a8d4;border-color:rgba(244,114,182,.22)}
.pill-reach{background:rgba(251,191,36,.12);color:#fcd34d;border-color:rgba(251,191,36,.22)}
.pill-target{background:rgba(56,189,248,.12);color:#7dd3fc;border-color:rgba(56,189,248,.22)}
.pill-safety{background:rgba(95,201,182,.12);color:var(--teal);border-color:rgba(95,201,182,.22)}
.pill-tier-1{background:var(--accent-grad);color:#031715;border:0;font-weight:700}
.pill-tier-2{background:rgba(95,201,182,.14);color:var(--teal);border-color:rgba(95,201,182,.22)}
.pill-tier-3{background:rgba(56,189,248,.12);color:#7dd3fc;border-color:rgba(56,189,248,.22)}
.pill-tier-4{background:var(--surface-2);color:var(--text-2)}
.pill-tier-5{background:var(--surface-2);color:var(--text-3)}
.pill-public{background:rgba(95,201,182,.12);color:var(--teal);border-color:rgba(95,201,182,.22)}
.pill-private{background:rgba(167,139,250,.12);color:#c4b5fd;border-color:rgba(167,139,250,.22)}
.pill-conf-low{background:var(--surface-2);color:var(--text-3)}
.pill-conf-medium{background:rgba(56,189,248,.10);color:#7dd3fc;border-color:rgba(56,189,248,.18)}
.pill-conf-high{background:rgba(95,201,182,.12);color:var(--teal);border-color:rgba(95,201,182,.22)}
.odds{
  font-size:2.4em;font-weight:800;letter-spacing:-1px;margin:10px 0 4px;
  background:var(--accent-grad);
  -webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;color:transparent;
  filter:drop-shadow(0 0 12px rgba(95,201,182,.25));
}
.flash{
  padding:12px 16px;background:rgba(56,189,248,.06);border:1px solid rgba(56,189,248,.2);
  border-radius:4px;margin-bottom:16px;font-size:.9em;color:var(--text);
}
.flash.error{background:rgba(244,114,182,.08);border-color:rgba(244,114,182,.25);color:#f9a8d4}
.flash.success{background:rgba(95,201,182,.08);border-color:rgba(95,201,182,.25);color:var(--teal)}
.search{display:flex;gap:10px;margin:8px 0 22px}
.search input{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.school-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:5px;padding:16px;transition:all .2s ease;
}
.school-card:hover{
  border-color:rgba(95,201,182,.3);
  background:var(--surface-hover);
  transform:translateY(-2px);
  box-shadow:0 12px 28px rgba(0,0,0,.4),0 0 0 1px rgba(95,201,182,.08);
  text-decoration:none;
}
.school-card a, .school-card a *{color:var(--text)}
.school-card a:hover{text-decoration:none}
.stat-row{display:flex;justify-content:space-between;font-size:.8em;color:var(--text-2);margin-top:9px}
.stat-row span:last-child{color:var(--text);font-weight:500}
.rank-row{
  display:flex;align-items:center;gap:14px;padding:14px 16px;
  background:var(--surface);border:1px solid var(--border);border-radius:5px;margin-bottom:10px;
  transition:all .15s ease;
}
.rank-row:hover{border-color:rgba(95,201,182,.2);background:var(--surface-hover)}
.rank-row .num{font-size:1.3em;font-weight:700;color:var(--text-3);min-width:30px;text-align:center;font-variant-numeric:tabular-nums}
.rank-row .body{flex:1}
.rank-row .body .nm{font-weight:600;color:var(--text)}
.rank-row .body .meta{font-size:.78em;color:var(--text-2)}
.rank-row a, .rank-row a *{color:inherit}
.rank-row a:hover{text-decoration:none}
.bar{display:flex;justify-content:space-between;margin:10px 0 22px;align-items:center;flex-wrap:wrap;gap:10px}
.bar a{font-size:.92em}
.tag-list{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.tag{display:inline-block;padding:4px 10px;border-radius:6px;background:var(--surface-2);color:var(--text-2);font-size:.78em;border:1px solid var(--border)}
table{color:var(--text)}
table th{color:var(--text-2);font-weight:500;text-align:left;font-size:.82em;letter-spacing:.3px;text-transform:uppercase}
table tbody tr{border-top:1px solid var(--border)}
table td{padding:10px 6px;color:var(--text)}
hr{border:0;border-top:1px solid var(--border);margin:24px 0}
::selection{background:rgba(95,201,182,.25);color:#fff}
/* Stat card emphasis */
.stat-card{
  background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:22px;
  position:relative;overflow:hidden;
}
.stat-card::before{
  content:"";position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(95,201,182,.4),transparent);
  opacity:.6;
}
.stat-card .label{font-size:.78em;color:var(--text-2);text-transform:uppercase;letter-spacing:.6px;font-weight:500}
.stat-card .value{font-size:2em;font-weight:700;letter-spacing:-.6px;margin-top:6px;color:var(--text)}
.stat-card .value.accent{
  background:var(--accent-grad);-webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;color:transparent;
}
.stat-card .delta{font-size:.78em;color:var(--text-2);margin-top:4px}
.pick-pill:hover{background:var(--surface-hover)!important;border-color:rgba(95,201,182,.3)!important}
.pick-pill:has(input:checked){background:rgba(95,201,182,.12)!important;border-color:rgba(95,201,182,.45)!important;color:var(--teal)!important}
/* ───────── MOBILE ───────── */
@media (max-width: 720px){
  html,body{overflow-x:hidden}
  .wrap{padding:0 16px 60px}
  /* Nav becomes a horizontal-scroll bar on mobile so all 7 tabs are
     reachable without awkward wrapping. Brand stays fixed at left. */
  .nav{
    padding:10px 14px;gap:0;flex-wrap:nowrap;overflow-x:auto;
    scrollbar-width:none;-webkit-overflow-scrolling:touch;
  }
  .nav::-webkit-scrollbar{display:none}
  .nav .brand{font-size:1em;flex-shrink:0;margin-right:14px}
  .nav .sp{display:none}
  .nav a{font-size:.85em;white-space:nowrap;flex-shrink:0;margin-right:14px}
  .nav a:last-child{margin-right:0}
  .nav .muted{flex-shrink:0;white-space:nowrap;margin-left:8px}
  h1{font-size:1.55em}
  h2{font-size:1.15em}
  h3{font-size:1.02em}
  p{font-size:.95em}
  .card{padding:16px;border-radius:5px;margin-bottom:12px}
  .grid{grid-template-columns:1fr;gap:12px}
  .school-card{padding:14px}
  .stat-card{padding:18px}
  .stat-card .value{font-size:1.6em}
  .odds{font-size:1.85em}
  .rank-table{font-size:.78em}
  .rank-table th,.rank-table td{padding:8px 6px}
  .rank-table th:nth-child(5),.rank-table td:nth-child(5){display:none}  /* hide ACT col */
  .rank-table th:nth-child(7),.rank-table td:nth-child(7){display:none}  /* hide class size */
  .pick-pill{font-size:.78em!important;padding:5px 9px!important}
  .pill{font-size:.65em;padding:3px 8px}
  /* iOS auto-zooms inputs with font-size <16px. Keep at 16 to prevent that. */
  input,select,textarea{font-size:16px}
  .btn{padding:10px 18px;font-size:.92em;min-height:42px}
  .btn-sm{padding:8px 14px;min-height:36px}
  /* Forms: full-width primary buttons on mobile so they're easy to thumb */
  form .btn-primary{display:inline-block}
  /* Tap targets */
  .checks label{padding:8px 0;min-height:44px}
  .pick-pill{min-height:36px}
  /* Tables that overflow get a scroll container */
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
  /* Action plan items stack tighter */
  .action-item{flex-direction:column;gap:8px;padding:14px 0}
  .action-num{margin-bottom:4px}
  /* Bar (back-link + breadcrumbs) tighter */
  .bar{margin:6px 0 16px}
  /* Search rows: input + button stack-friendly */
  .search{flex-direction:column;gap:8px}
  .search input{flex:none;width:100%}
  /* Landing hero tighter on mobile */
  .hero{padding:40px 0 50px!important}
  /* Stat-card grid in simulator goes 1-up on narrow */
  .grid[style*="minmax(220px"]{grid-template-columns:1fr!important}
}
@media (max-width: 480px){
  .wrap{padding:0 12px 50px}
  .nav{padding:10px 12px}
  .nav .brand{font-size:.95em;margin-right:10px}
  .nav a{font-size:.82em;margin-right:12px}
  h1{font-size:1.35em;letter-spacing:-.4px}
  h2{font-size:1.05em;margin:20px 0 10px}
  .card{padding:14px;margin-bottom:10px}
  .stat-card{padding:16px}
  .stat-card .value{font-size:1.4em}
  .odds{font-size:1.6em}
  /* Real Profiles header on mobile */
  .rank-row{flex-wrap:wrap;padding:12px}
  .rank-row .num{min-width:24px;font-size:1.1em}
  /* CTA row — stack secondary below primary */
  .cta-row{flex-direction:column;align-items:stretch!important}
  .cta-row a{text-align:center}
  /* Stats row on landing — 2-up instead of 3-up at narrow */
  .hero .stats{gap:14px!important}
  .hero .stats .stat .num{font-size:1.5em!important}
}
/* Action plan items */
.action-item{display:flex;gap:14px;padding:14px 0;border-top:1px solid var(--border)}
.action-item:first-child{border-top:0;padding-top:6px}
.action-num{
  flex-shrink:0;width:32px;height:32px;border-radius:50%;
  background:rgba(95,201,182,.12);color:var(--teal);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:.95em;border:1px solid rgba(95,201,182,.3);
}
.action-body{flex:1}
.action-title{font-weight:600;color:var(--text);font-size:1.02em;margin-bottom:6px}
.action-meta{font-size:.88em;color:var(--text-2);margin:3px 0;line-height:1.5}
.meta-label{color:var(--text-3);font-weight:500;margin-right:4px;text-transform:uppercase;font-size:.78em;letter-spacing:.4px}
.action-impact{
  display:inline-block;margin-top:8px;padding:4px 10px;border-radius:6px;
  background:rgba(95,201,182,.08);color:var(--teal);font-size:.82em;font-weight:500;
  border:1px solid rgba(95,201,182,.2);
}
/* Competition / program rows */
.comp-row{padding:14px 0;border-top:1px solid var(--border)}
.comp-row:first-child{border-top:0;padding-top:4px}
.comp-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.comp-name{color:var(--text);font-weight:600;font-size:1em}
.comp-name:hover{color:var(--teal)}
.comp-meta{font-size:.86em;color:var(--text-2);margin-top:4px}
.comp-note{font-size:.92em;color:var(--text);margin-top:6px;line-height:1.5}
"""

CANDOR_LOGO_SVG = """<svg viewBox="0 0 64 64" width="22" height="22" xmlns="http://www.w3.org/2000/svg" style="vertical-align:-4px;margin-right:8px">
  <defs>
    <linearGradient id="cdr-g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="100%" stop-color="#5fc9b6"/>
    </linearGradient>
  </defs>
  <path d="M 52 16 A 22 22 0 1 0 52 48" stroke="url(#cdr-g)" stroke-width="6" fill="none" stroke-linecap="round"/>
  <rect x="22" y="36" width="5.5" height="10" fill="url(#cdr-g)" rx="1.2"/>
  <rect x="31" y="28" width="5.5" height="18" fill="url(#cdr-g)" rx="1.2"/>
  <rect x="40" y="20" width="5.5" height="26" fill="url(#cdr-g)" rx="1.2"/>
</svg>"""

RANKING_TABLE_CSS = """
<style>
.rank-table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;font-size:.92em;box-shadow:var(--shadow-card)}
.rank-table th{background:rgba(255,255,255,.02);text-align:left;padding:13px 16px;color:var(--text-2);font-weight:500;font-size:.74em;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.rank-table td{padding:13px 16px;border-bottom:1px solid var(--border);vertical-align:middle;color:var(--text)}
.rank-table tr:hover td{background:rgba(95,201,182,.04)}
.rank-table tr:last-child td{border-bottom:0}
.rank-table .rank-num{font-weight:600;color:var(--text-3);font-variant-numeric:tabular-nums}
.rank-table .name a{color:var(--text);font-weight:600}
.rank-table .name a:hover{color:var(--teal)}
.rank-table .num-col{font-variant-numeric:tabular-nums;color:var(--text-2)}
.rank-table .stars{color:var(--teal);letter-spacing:1px;filter:drop-shadow(0 0 6px rgba(95,201,182,.25))}
.rank-table .stars-empty{color:rgba(255,255,255,.12)}
@media(max-width:720px){.rank-table th.hide-sm,.rank-table td.hide-sm{display:none}}
</style>
"""

CHAT_PAGE_HTML = """
<style>
.chat-msgs{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:18px;min-height:340px;max-height:560px;overflow-y:auto;margin-bottom:14px;box-shadow:var(--shadow-card)}
.msg{margin:12px 0;display:flex;gap:8px}
.msg-user{justify-content:flex-end}
.msg-bubble{max-width:78%;padding:11px 15px;border-radius:6px;font-size:.95em;line-height:1.55}
.msg-user .msg-bubble{background:var(--accent-grad);color:#031715;font-weight:500;box-shadow:0 6px 18px rgba(56,189,248,.22)}
.msg-assistant .msg-bubble{background:var(--surface-2);color:var(--text);border:1px solid var(--border-strong)}
.msg-bubble ul{margin:6px 0;padding-left:18px}
.msg-bubble li{margin:2px 0}
.chat-input{display:flex;gap:10px;align-items:flex-end}
.chat-input textarea{flex:1;min-height:50px;max-height:160px;padding:12px 14px;border:1px solid var(--border-strong);border-radius:4px;font-family:inherit;resize:vertical;font-size:.95em;background:var(--bg-2);color:var(--text)}
.chat-input textarea:focus{outline:0;border-color:var(--teal);box-shadow:0 0 0 3px rgba(95,201,182,.12)}
.chat-input button{padding:12px 24px;border-radius:4px;border:0;background:var(--accent-grad);color:#031715;font-weight:700;cursor:pointer;font-family:inherit;box-shadow:0 8px 20px rgba(56,189,248,.18);transition:filter .15s}
.chat-input button:hover{filter:brightness(1.05)}
.chat-input button:disabled{opacity:.6;cursor:wait;filter:grayscale(.4)}
.suggestions{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 18px}
.suggestion{background:var(--surface-2);border:1px solid var(--border-strong);border-radius:999px;padding:7px 14px;font-size:.83em;cursor:pointer;color:var(--text-2);transition:all .15s;font-family:inherit}
.suggestion:hover{border-color:rgba(95,201,182,.4);color:var(--teal);background:var(--surface-hover)}
.typing{display:flex;gap:4px;padding:11px 15px;background:var(--surface-2);border:1px solid var(--border-strong);border-radius:6px;width:fit-content}
.typing span{width:7px;height:7px;background:var(--teal);border-radius:50%;animation:typing 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes typing{0%,60%,100%{opacity:.25}30%{opacity:1}}
</style>
__HEADER__
<div id="msgs" class="chat-msgs">__MESSAGES__</div>
<div class="suggestions">__SUGGESTIONS__</div>
<div class="chat-input">
  <textarea id="chat-input" placeholder="Ask anything about __PLACEHOLDER__..."></textarea>
  <button id="chat-send" onclick="sendMsg()">Send</button>
</div>
<script>
var SEND_URL = "__SEND_URL__";
var msgs = document.getElementById("msgs");
var input = document.getElementById("chat-input");
var btn = document.getElementById("chat-send");
function escapeHTML(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function renderUserMsg(text){
  var d = document.createElement('div'); d.className='msg msg-user';
  d.innerHTML = '<div class="msg-bubble">'+escapeHTML(text).replace(/\\n/g,'<br>')+'</div>';
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function renderTyping(){
  var d = document.createElement('div'); d.id='typing-row'; d.className='msg msg-assistant';
  d.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function clearTyping(){var t=document.getElementById('typing-row');if(t)t.remove();}
function renderAssistantMsg(html){
  var d = document.createElement('div'); d.className='msg msg-assistant';
  d.innerHTML = '<div class="msg-bubble">'+html+'</div>';
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function sendMsg(text){
  var msg = text || input.value.trim();
  if(!msg) return;
  input.value=''; btn.disabled=true;
  renderUserMsg(msg);
  renderTyping();
  var headers = {'Content-Type':'application/json'};
  var tokenEl = document.querySelector('meta[name="csrf-token"]');
  if(tokenEl) headers['X-CSRFToken'] = tokenEl.content;
  fetch(SEND_URL, {method:'POST', headers: headers, body: JSON.stringify({message: msg})})
    .then(function(r){return r.json().then(function(d){return {status:r.status, body:d};});})
    .then(function(o){
      var d = o.body;
      clearTyping(); btn.disabled=false; input.focus();
      // Paywall / cap responses arrive as 402 / 429 with HTML body
      if(o.status === 402 || o.status === 429){
        renderAssistantMsg(d.html || ('<i>'+escapeHTML(d.error||'Limit reached')+'</i>'));
        return;
      }
      if(d.error){renderAssistantMsg('<i>'+escapeHTML(d.error)+'</i>');return;}
      renderAssistantMsg(d.html || escapeHTML(d.reply || ''));
      if(d.usage){
        var pill = document.getElementById('usage-pill');
        if(pill){
          if(d.usage.is_paid){
            pill.innerHTML = 'PREMIUM · ' + d.usage.month_used + '/' + d.usage.month_limit;
          } else {
            pill.innerHTML = 'FREE · ' + d.usage.free_remaining + ' of ' + d.usage.free_limit + ' left · <a href="/upgrade" style="color:var(--teal)">Upgrade</a>';
          }
        }
      }
    })
    .catch(function(e){clearTyping(); btn.disabled=false; renderAssistantMsg('<i>Network error — try again.</i>');});
}
function suggestionClick(s){sendMsg(s);}
input.addEventListener('keydown', function(e){
  if(e.key==='Enter' && !e.shiftKey){e.preventDefault(); sendMsg();}
});
msgs.scrollTop = msgs.scrollHeight;
input.focus();
</script>
"""


__all__ = ['BASE_CSS', 'CANDOR_LOGO_SVG', 'RANKING_TABLE_CSS', 'CHAT_PAGE_HTML']
