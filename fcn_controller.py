#!/usr/bin/env python3
"""
FCN Browser Controller v2 — drives FreeChatNow via Browser Use CLI (headed mode).

Run:  python3 fcn_controller.py
Opens at http://localhost:8765
"""
import http.server
import json
import subprocess
import threading
import urllib.parse
import time
import os
import re

PORT = 8765
BROWSER_USE = os.path.expanduser("~/.browser-use-env/bin/browser-use")
STATE = {"status": "disconnected", "url": "", "title": "", "username": "",
         "messages": [], "error": "", "step": ""}
STATE_LOCK = threading.Lock()

def bu(args, timeout=30):
    """Run browser-use CLI command, return stdout."""
    cmd = [BROWSER_USE] + args
    env = {**os.environ, "PATH": f"{os.path.expanduser('~/.browser-use-env/bin')}:{os.environ.get('PATH', '')}"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            return f"ERR:{r.stderr.strip() or r.stdout.strip()[:200]}"
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERR:timeout"
    except FileNotFoundError:
        return "ERR:browser-use not found"

def bu_json(args, timeout=30):
    """Run browser-use with --json."""
    out = bu(args + ["--json"], timeout=timeout)
    if out.startswith("ERR:"):
        return {"error": out[4:]}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": "parse failed", "raw": out[:200]}

def set_step(s):
    with STATE_LOCK:
        STATE["step"] = s

def update_state():
    """Read current browser page state into STATE."""
    s = bu(["state", "--json"])
    if s.startswith("ERR:"):
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["error"] = s[:100]
        return
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return

    with STATE_LOCK:
        STATE["last_state"] = data
        STATE["url"] = data.get("url", "")
        STATE["title"] = data.get("title", "")
        STATE["status"] = "connected"

    # Extract chat messages via JS
    js = """
JSON.stringify({
  messages: (() => {
    // Try various selectors FCN might use for messages
    const selectors = [
      '.chat-message', '.message', '[class*=msg]', '[class*=chatline]',
      '[class*=line]', '[class*=content] p', '.chat-content div',
      '#chat-body div', '[class*=conversation] div'
    ];
    for (const sel of selectors) {
      const els = document.querySelectorAll(sel);
      if (els.length > 3) {
        return Array.from(els).slice(-25).map(el => el.textContent.trim()).filter(t => t);
      }
    }
    // Fallback: grab any visible text blocks
    const body = document.body;
    const allText = body.innerText.split('\\n').filter(t => t.trim()).slice(-30);
    return allText;
  })(),
  title: document.title,
  url: window.location.href,
  hasChatInput: !!document.querySelector('textarea, input[type=text], [contenteditable]'),
  hasLoginForm: !!document.querySelector('input[name="username"]'),
  rawBody: document.body.innerText.substring(0, 500)
});
"""
    extract = bu(["eval", js])
    if extract and not extract.startswith("ERR:"):
        try:
            parsed = json.loads(extract)
            with STATE_LOCK:
                msgs = parsed.get("messages", [])
                if isinstance(msgs, list) and len(msgs) > 0:
                    STATE["messages"] = msgs
        except json.JSONDecodeError:
            pass

# ──────────────────────────────────────────────
# HTTP Server
# ──────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(html.encode())

    def _options(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._options()

    def log_message(self, format, *args):
        """Suppress default HTTP server logs for cleaner output."""
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/state":
            update_state()
            with STATE_LOCK:
                self._json({
                    "status": STATE["status"],
                    "url": STATE["url"],
                    "title": STATE["title"],
                    "username": STATE["username"],
                    "messages": STATE["messages"],
                    "step": STATE["step"],
                    "error": STATE["error"]
                })

        elif path == "/api/suggest":
            context = urllib.parse.parse_qs(parsed.query).get("context", [""])[0]
            persona = urllib.parse.parse_qs(parsed.query).get("persona", [""])[0]
            tone = urllib.parse.parse_qs(parsed.query).get("tone", ["casual"])[0]
            length = urllib.parse.parse_qs(parsed.query).get("length", ["medium"])[0]
            count = int(urllib.parse.parse_qs(parsed.query).get("count", ["5"])[0])
            custom = urllib.parse.parse_qs(parsed.query).get("custom", [""])[0]
            self._json({"suggestions": self._generate(context, persona, tone, length, count, custom)})

        elif path == "/":
            self._html(FRONTEND_HTML)

        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else "{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/login":
            self._handle_login(data)

        elif path == "/api/send":
            self._handle_send(data)

        elif path == "/api/openers":
            count = data.get("count", 5)
            self._json({"suggestions": self._generate(
                "", data.get("persona", ""), data.get("tone", "casual"),
                "short", count, "Generate conversation openers for entering a chat room"
            )})

        elif path == "/api/close":
            bu(["close"])
            with STATE_LOCK:
                STATE["status"] = "disconnected"
                STATE["username"] = ""
                STATE["messages"] = []
            self._json({"status": "closed"})

        else:
            self._json({"error": "not found"}, 404)

    def _handle_login(self, data):
        username = data.get("username", f"Bot_{int(time.time()) % 10000}")
        gender = data.get("gender", "m")
        birthdate = data.get("birthdate", "1990-06-14")
        room = data.get("room", "SextChat")

        def step(msg):
            set_step(msg)
            time.sleep(1.5)

        try:
            step("🟢 Closing previous session...")
            bu(["close"], timeout=10)

            step(f"🌐 Opening FreeChatNow (room: {room})...")
            r = bu(["--headed", "open", f"https://www.freechatnow.com/chat/{room.lower()}"], timeout=20)
            if r.startswith("ERR:"):
                self._json({"error": f"Navigate failed: {r[:100]}"}, 500)
                return
            time.sleep(4)

            step("📝 Filling username...")
            bu(["eval", f"document.querySelector('input[name=\"username\"]').value = '{username}'; "
                       f"document.querySelector('input[name=\"username\"]').dispatchEvent(new Event('input', {{bubbles:true}}));"])
            time.sleep(0.5)

            step("👤 Selecting gender...")
            gender_val = "m"
            bu(["eval", f"document.querySelector('select[name=\"gender\"]').value = '{gender_val}'; "
                       f"document.querySelector('select[name=\"gender\"]').dispatchEvent(new Event('change', {{bubbles:true}}));"])
            time.sleep(0.5)

            step("📅 Setting birthdate...")
            bu(["eval", f"document.querySelector('input[name=\"birthdate\"]').value = '{birthdate}'; "
                       f"document.querySelector('input[name=\"birthdate\"]').dispatchEvent(new Event('input', {{bubbles:true}}));"])
            time.sleep(0.5)

            step("✅ Checking age confirmation...")
            bu(["eval", "document.querySelector('input[type=\"checkbox\"]').checked = true; "
                       "document.querySelector('input[type=\"checkbox\"]').dispatchEvent(new Event('change', {bubbles:true}));"])
            time.sleep(0.5)

            step("🚪 Clicking 'Chat As Guest'...")
            bu(["eval", "document.querySelector('button[type=\"submit\"][value=\"guest\"]').click();"])
            time.sleep(5)

            step("🔄 Checking page state...")
            update_state()
            with STATE_LOCK:
                STATE["username"] = username

            # Check if we got to the chat
            title = STATE.get("title", "")
            url = STATE.get("url", "")
            msgs = STATE.get("messages", [])
            name_match = username in str(STATE.get("messages", []))

            self._json({
                "status": "logged_in" if ("chat" in title.lower() or len(msgs) > 2 or name_match) else "maybe",
                "username": username,
                "url": url,
                "title": title,
                "messages": msgs,
                "step": "✅ Done" if ("chat" in title.lower() or len(msgs) > 2) else "⚠️ Page loaded, check chat"
            })

        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_send(self, data):
        msg = data.get("message", "")
        if not msg:
            self._json({"error": "no message"}, 400)
            return

        # Find and fill the chat input, then send
        escaped = msg.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        js = f"""
(() => {{
    const input = document.querySelector('textarea') || document.querySelector('[contenteditable]') || document.querySelector('input[type=text]');
    if (!input) return 'no input found';
    if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {{
        input.value = '{escaped}';
        input.dispatchEvent(new Event('input', {{bubbles: true}}));
        input.dispatchEvent(new Event('change', {{bubbles: true}}));
    }} else if (input.isContentEditable) {{
        input.textContent = '{escaped}';
        input.dispatchEvent(new Event('input', {{bubbles: true}}));
    }}
    return 'filled';
}})();
"""
        r = bu(["eval", js])
        time.sleep(0.5)
        # Send Enter key
        bu(["eval", """
(() => {
    const input = document.querySelector('textarea') || document.querySelector('[contenteditable]');
    if (!input) return 'no input';
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    // Also try clicking any send button
    const sendBtn = document.querySelector('button[type=submit], [class*=send], [class*=submit]');
    if (sendBtn) sendBtn.click();
    return 'sent: ' + document.title;
})();
"""])

        time.sleep(2)
        update_state()
        self._json({"sent": True, "result": r[:200] if not r.startswith("ERR:") else r})

    def _generate(self, context, persona_desc, tone, length, count, custom):
        """Call OpenRouter to generate suggestions."""
        import urllib.request

        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            try:
                with open(os.path.expanduser("~/.browser-use/config.json")) as f:
                    cfg = json.load(f)
                    api_key = cfg.get("api_key", "")
            except Exception:
                pass

        if not api_key:
            return ["⚠️ API key not configured — set OPENROUTER_API_KEY in environment"]

        system = (
            f"You are a chat assistant for an adult chat room (18+). "
            f"Suggest {count} realistic responses the user could send. "
            f"Tone: {tone}. Message length: {length}."
        )
        if persona_desc:
            system += f"\nPersona: {persona_desc}"
        if custom:
            system += f"\nCustom instruction: {custom}"

        system += (
            "\n\nRules: Natural conversation. Vary the approach for each suggestion. "
            "Number them 1-N with each on a new line. "
            "Never include username prefix. No explanations. Just the suggestions."
        )

        user_msg = f"Chat context:\n\"\"\"\n{context}\n\"\"\"\n\nGenerate {count} suggestions." if context else f"Generate {count} conversation openers."

        payload = json.dumps({
            "model": "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg}
            ],
            "temperature": 0.8,
            "max_tokens": 800
        }).encode()

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "http://localhost:8765",
                "X-Title": "FCN Assistant"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                content = result["choices"][0]["message"]["content"]
                suggestions = []
                for line in content.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r'^\d+[.)]\s*(.*)', line)
                    if m:
                        suggestions.append(m.group(1))
                    elif not suggestions:
                        suggestions.append(line)
                return suggestions[:count] if suggestions else [content.strip()]
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            return [f"⚠️ API {e.code}: {body}"]
        except Exception as e:
            return [f"⚠️ Error: {str(e)[:80]}"]


# ═══════════════════════════════════════════════════════════════
# FRONTEND HTML
# ═══════════════════════════════════════════════════════════════
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>FCN Assistant — Browser Control</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1218;--surface:#1a1f2a;--surface-2:#232a38;--border:#2d3648;--text:#d6dee8;--text-dim:#8892a4;--accent:#6c8cff;--green:#34d399;--orange:#f59e0b;--red:#ef4444;--radius:8px}
html{font-size:15px}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh}
.sidebar{width:220px;background:var(--surface);border-right:1px solid var(--border);padding:1.25rem 0;flex-shrink:0;display:flex;flex-direction:column}
.sidebar h1{font-size:.85rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);padding:0 1.25rem 1rem;border-bottom:1px solid var(--border);margin-bottom:.5rem}
.nav-item{display:flex;align-items:center;gap:.5rem;padding:.6rem 1.25rem;cursor:pointer;color:var(--text-dim);font-size:.85rem;border:none;background:none;width:100%;text-align:left;transition:all .15s}
.nav-item:hover{background:var(--surface-2);color:var(--text)}
.nav-item.active{color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent)}
.main{flex:1;padding:1.5rem;overflow-y:auto;max-width:1100px}
.page{display:none}.page.active{display:block}
h2{font-size:1.3rem;font-weight:600;margin-bottom:.2rem}
.subtitle{color:var(--text-dim);font-size:.85rem;margin-bottom:1.25rem}
/* Status bar */
.status-bar{display:flex;align-items:center;gap:.75rem;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:.6rem 1rem;margin-bottom:1rem;font-size:.82rem;flex-wrap:wrap}
.status-dot{width:10px;height:10px;border-radius:50%;background:var(--text-dim);flex-shrink:0}
.status-dot.connected{background:var(--green)}
.status-dot.error{background:var(--red)}
.status-dot.logged-in{background:var(--accent)}
.step-badge{background:color-mix(in srgb,var(--accent) 15%,transparent);color:var(--accent);padding:.2rem .5rem;border-radius:4px;font-size:.75rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:1.25rem;margin-bottom:1rem}
.card-title{font-size:.8rem;font-weight:600;color:var(--text-dim);margin-bottom:.75rem;text-transform:uppercase;letter-spacing:.05em}
.chat-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.chat-feed{height:450px;overflow-y:auto;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:.75rem;font-size:.85rem;line-height:1.5}
.chat-msg{padding:.3rem 0;border-bottom:1px solid color-mix(in srgb,var(--border) 50%,transparent);word-break:break-word}
.chat-msg:last-child{border-bottom:none}
.suggestion-card{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:.6rem .85rem;margin-bottom:.5rem;display:flex;align-items:center;gap:.75rem;transition:border-color .15s}
.suggestion-card:hover{border-color:var(--accent)}
.suggestion-card .num{font-size:.75rem;font-weight:700;color:var(--accent);background:color-mix(in srgb,var(--accent) 15%,transparent);width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.suggestion-card .text{flex:1;font-size:.9rem;line-height:1.4}
.actions{display:flex;gap:.35rem;flex-shrink:0}
.btn{padding:.4rem .8rem;border-radius:var(--radius);border:none;font-size:.8rem;font-weight:500;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:.35rem}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{filter:brightness(1.1)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}
.btn-secondary{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
.btn-secondary:hover{border-color:var(--accent)}
.btn-green{background:color-mix(in srgb,var(--green) 20%,transparent);color:var(--green)}
.btn-green:hover{background:color-mix(in srgb,var(--green) 30%,transparent)}
.btn-sm{padding:.3rem .6rem;font-size:.75rem}
input,select,textarea{width:100%;padding:.45rem .6rem;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-size:.85rem;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-bottom:.6rem}
.form-row.three{grid-template-columns:1fr 1fr 1fr}
.mb{margin-bottom:.6rem}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top:2px solid var(--accent);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.persona-item{display:flex;align-items:center;justify-content:space-between;padding:.6rem .75rem;border:1px solid var(--border);border-radius:var(--radius);margin-bottom:.4rem}
.persona-item .name{font-weight:500;font-size:.85rem}
.persona-item .meta{font-size:.75rem;color:var(--text-dim)}
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.toast{position:fixed;bottom:1.5rem;right:1.5rem;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:.6rem 1rem;font-size:.82rem;opacity:0;transform:translateY(10px);transition:all .25s;pointer-events:none}
.toast.show{opacity:1;transform:translateY(0)}
.flex{display:flex;align-items:center;gap:.5rem}
@media(max-width:900px){.chat-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<nav class="sidebar">
  <h1>FCN Assistant</h1>
  <button class="nav-item active" data-page="browser"><span>🌐</span> Browser</button>
  <button class="nav-item" data-page="login"><span>🔑</span> Login</button>
  <button class="nav-item" data-page="personas"><span>👤</span> Personas</button>
</nav>

<div class="main">

<!-- ═══ BROWSER CONTROL ═══ -->
<div class="page active" id="page-browser">
  <h2>Browser Control</h2>
  <p class="subtitle">Live chat from FreeChatNow. Suggestions auto-populate from context.</p>

  <div class="status-bar" id="status-bar">
    <span class="status-dot" id="status-dot"></span>
    <span id="status-text">Not connected</span>
    <span id="status-step" class="step-badge" style="display:none"></span>
    <span style="flex:1"></span>
    <span id="status-url" style="color:var(--text-dim);font-size:.78rem;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
  </div>

  <div class="chat-grid">
    <div>
      <div class="card" style="margin-bottom:0">
        <div class="card-title">Live Feed</div>
        <div class="chat-feed" id="chat-feed">
          <p style="color:var(--text-dim);font-style:italic;font-size:.82rem">Login to see the chat room.</p>
        </div>
      </div>
    </div>

    <div>
      <div class="card" style="margin-bottom:0">
        <div class="card-title">Suggestions</div>
        <div class="form-row">
          <div><label>Tone</label>
            <select id="suggest-tone">
              <option value="casual">Casual</option>
              <option value="flirty">Flirty</option>
              <option value="teasing">Teasing</option>
              <option value="deep">Deep</option>
              <option value="funny">Funny</option>
              <option value="direct">Direct</option>
            </select>
          </div>
          <div><label>Length</label>
            <select id="suggest-length">
              <option value="short">Short</option>
              <option value="medium" selected>Medium</option>
              <option value="long">Long</option>
            </select>
          </div>
        </div>
        <div class="mb">
          <label>Extra instruction (optional)</label>
          <input type="text" id="suggest-custom" placeholder="e.g., keep it subtle">
        </div>
        <div class="flex">
          <button class="btn btn-primary" id="btn-suggest" onclick="generateSuggestions()">✨ Generate</button>
          <button class="btn btn-secondary" onclick="generateOpeners()">🚀 Openers</button>
          <button class="btn btn-secondary" onclick="refreshState()">🔄 Refresh</button>
        </div>
        <div id="suggestions-area" style="margin-top:.75rem"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ LOGIN ═══ -->
<div class="page" id="page-login">
  <h2>Login to FreeChatNow</h2>
  <p class="subtitle">Browser Use will open a visible browser window, fill the form, and log you in.</p>
  <div class="card" style="max-width:500px">
    <div class="form-row three">
      <div><label>Username</label><input id="login-user" value="ChatBot_42"></div>
      <div><label>Gender</label><select id="login-gender"><option value="m">Male</option><option value="f">Female</option><option value="o">Other</option></select></div>
      <div><label>Birthdate</label><input id="login-birth" value="1990-06-14" type="date"></div>
    </div>
    <div class="mb">
      <label>Room</label>
      <select id="login-room">
        <option value="SextChat">Sexting Chat</option>
        <option value="Chat">General Chat</option>
        <option value="Flirt">Flirt Chat</option>
        <option value="Roleplay">Roleplay Chat</option>
      </select>
    </div>
    <div class="flex">
      <button class="btn btn-primary" onclick="login()" id="btn-login">🔗 Connect & Login</button>
      <button class="btn btn-secondary" onclick="disconnect()">⏹ Disconnect</button>
    </div>
    <div id="login-status" style="margin-top:.6rem;font-size:.82rem;color:var(--text-dim)"></div>
  </div>
</div>

<!-- ═══ PERSONAS ═══ -->
<div class="page" id="page-personas">
  <h2>Personas</h2>
  <p class="subtitle">Manage your chat personas. These control suggestion style.</p>
  <div class="card">
    <div class="card-title">New / Edit</div>
    <div class="form-row three">
      <div><label>Name</label><input id="p-name" placeholder="Cool Chris"></div>
      <div><label>Username</label><input id="p-username" placeholder="ChrisTX_90"></div>
      <div><label>Gender</label><select id="p-gender"><option value="">—</option><option value="m">Male</option><option value="f">Female</option><option value="o">Other</option></select></div>
    </div>
    <div class="mb"><label>Bio / Personality</label><textarea id="p-bio" rows="2" placeholder="Confident, playful. Loves hiking and music."></textarea></div>
    <button class="btn btn-primary btn-sm" onclick="savePersona()">Save</button>
    <button class="btn btn-secondary btn-sm" onclick="clearPersona()" style="margin-left:.35rem">Clear</button>
  </div>
  <div class="card">
    <div class="card-title">Saved</div>
    <div id="persona-list"><p style="color:var(--text-dim);font-size:.82rem;font-style:italic">No personas yet.</p></div>
  </div>
</div>

</div>

<div class="toast" id="toast"></div>

<script>
// ─── State ───
let personas = JSON.parse(localStorage.getItem('fcn_personas') || '[]');
let activePersonaId = localStorage.getItem('fcn_active_persona') || null;
let lastMessages = [];

function toast(m) { const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show'); clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),2500); }

// Navigation
document.querySelectorAll('.nav-item').forEach(n => n.addEventListener('click', () => {
  document.querySelectorAll('.page,.nav-item').forEach(e => e.classList.remove('active'));
  document.getElementById('page-'+n.dataset.page).classList.add('active');
  n.classList.add('active');
}));

// ─── Login ───
async function login() {
  const btn = document.getElementById('btn-login');
  const status = document.getElementById('login-status');
  btn.disabled = true; btn.textContent = '⏳ Logging in...';
  status.innerHTML = '<span class="spinner"></span> Connecting browser...';

  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('login-user').value,
        gender: document.getElementById('login-gender').value,
        birthdate: document.getElementById('login-birth').value,
        room: document.getElementById('login-room').value
      })
    });
    const data = await r.json();
    if (data.error) {
      status.textContent = '❌ ' + data.error;
      toast('Login failed');
    } else {
      status.innerHTML = '✅ <b>' + (data.username || '') + '</b> — ' + (data.step || 'done');
      if (data.messages && data.messages.length) {
        document.getElementById('chat-feed').innerHTML = data.messages.map(m => '<div class="chat-msg">' + escHtml(m) + '</div>').join('');
      }
      toast('Connected! Watch the browser window.');
      startPolling();
    }
  } catch(e) {
    status.textContent = '❌ Server unreachable. Is fcn_controller.py running?';
  }
  btn.disabled = false; btn.textContent = '🔗 Connect & Login';
}

async function disconnect() {
  try { await fetch('/api/close', {method: 'POST'}); } catch(e) {}
  document.getElementById('chat-feed').innerHTML = '<p style="color:var(--text-dim);font-style:italic;font-size:.82rem">Disconnected.</p>';
  document.getElementById('status-text').textContent = 'Disconnected';
  document.getElementById('status-dot').className = 'status-dot';
  document.getElementById('status-url').textContent = '';
  document.getElementById('status-step').style.display = 'none';
  polling = false;
}

// ─── Polling ───
let polling = false;
function startPolling() { polling = true; pollState(); }

async function pollState() {
  if (!polling) return;
  try {
    const r = await fetch('/api/state');
    const data = await r.json();
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    const urlEl = document.getElementById('status-url');
    const stepEl = document.getElementById('status-step');

    dot.className = 'status-dot ' + (data.status === 'connected' ? 'logged-in' : data.status === 'error' ? 'error' : '');
    txt.textContent = data.username ? '👤 ' + data.username : (data.status === 'connected' ? 'Connected' : 'Disconnected');
    urlEl.textContent = data.url || '';
    if (data.step) { stepEl.textContent = data.step; stepEl.style.display = 'inline'; } else { stepEl.style.display = 'none'; }

    const feed = document.getElementById('chat-feed');
    if (data.messages && data.messages.length > 0 && JSON.stringify(data.messages) !== JSON.stringify(lastMessages)) {
      lastMessages = data.messages;
      feed.innerHTML = data.messages.map(m => '<div class="chat-msg">' + escHtml(m) + '</div>').join('');
      feed.scrollTop = feed.scrollHeight;
    }
  } catch(e) {
    document.getElementById('status-text').textContent = '⚠️ Server offline';
    document.getElementById('status-dot').className = 'status-dot error';
  }
  setTimeout(pollState, 3000);
}

async function refreshState() { await pollState(); }

// ─── Suggestions ───
async function generateSuggestions() {
  const area = document.getElementById('suggestions-area');
  area.innerHTML = '<div class="flex" style="color:var(--text-dim);font-size:.82rem"><span class="spinner"></span> Generating...</div>';

  const persona = personas.find(p => p.id === activePersonaId);
  const context = lastMessages.join(' | ');
  const tone = document.getElementById('suggest-tone').value;
  const length = document.getElementById('suggest-length').value;
  const custom = document.getElementById('suggest-custom').value;
  const personaText = persona ? persona.name + ': ' + (persona.bio || '') : '';
  const params = new URLSearchParams({context, persona: personaText, tone, length, count: '5', custom});

  try {
    const r = await fetch('/api/suggest?' + params);
    const data = await r.json();
    area.innerHTML = '';
    if (data.suggestions) {
      data.suggestions.forEach((s, i) => {
        const card = document.createElement('div');
        card.className = 'suggestion-card';
        card.innerHTML = `<div class="num">${i+1}</div>
          <div class="text">${escHtml(s)}</div>
          <div class="actions">
            <button class="btn btn-green btn-sm" onclick="sendMessage('${escAttr(s)}')">Send</button>
            <button class="btn btn-secondary btn-sm" onclick="copyText('${escAttr(s)}')">Copy</button>
          </div>`;
        area.appendChild(card);
      });
    }
  } catch(e) {
    area.innerHTML = '<p style="color:var(--red);font-size:.82rem">Error: ' + e.message + '</p>';
  }
}

async function generateOpeners() {
  document.getElementById('suggest-custom').value = 'Generate conversation openers';
  await generateSuggestions();
  document.getElementById('suggest-custom').value = '';
}

async function sendMessage(msg) {
  try {
    const r = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    const data = await r.json();
    if (data.sent) { toast('✅ Sent!'); setTimeout(pollState, 1000); }
  } catch(e) { toast('Send failed: ' + e.message); }
}

function copyText(msg) { navigator.clipboard.writeText(msg).then(() => toast('Copied!')).catch(() => {}); }

// ─── Personas ───
function renderPersonas() {
  const list = document.getElementById('persona-list');
  if (!personas.length) { list.innerHTML = '<p style="color:var(--text-dim);font-size:.82rem;font-style:italic">No personas yet.</p>'; return; }
  list.innerHTML = personas.map(p => `<div class="persona-item" style="border-color:${p.id === activePersonaId ? 'var(--accent)' : 'var(--border)'}">
    <div><div class="name">${escHtml(p.name)}</div><div class="meta">${escHtml(p.username)} · ${p.gender||'?'} · ${escHtml((p.bio||'').substring(0,40))}</div></div>
    <div class="flex" style="gap:.35rem">
      <button class="btn btn-secondary btn-sm" onclick="selectPersona('${p.id}')">Use</button>
      <button class="btn btn-secondary btn-sm" onclick="editPersona('${p.id}')">Edit</button>
      <button class="btn btn-secondary btn-sm" onclick="deletePersona('${p.id}')">Del</button>
    </div></div>`).join('');
}

function savePersona() {
  const id = document.getElementById('p-name').dataset.editId || 'p_' + Date.now();
  const d = {id, name: document.getElementById('p-name').value.trim(), username: document.getElementById('p-username').value.trim(), gender: document.getElementById('p-gender').value, bio: document.getElementById('p-bio').value.trim()};
  if (!d.name || !d.username) { toast('Name and username required'); return; }
  const idx = personas.findIndex(p => p.id === id);
  idx >= 0 ? personas[idx] = d : personas.push(d);
  activePersonaId = id;
  localStorage.setItem('fcn_personas', JSON.stringify(personas));
  localStorage.setItem('fcn_active_persona', id);
  clearPersona(); renderPersonas(); toast('Saved');
}

function selectPersona(id) { activePersonaId = id; localStorage.setItem('fcn_active_persona', id); renderPersonas(); toast('Selected'); }
function editPersona(id) { const p = personas.find(x => x.id === id); if(!p) return;
  document.getElementById('p-name').value = p.name; document.getElementById('p-name').dataset.editId = p.id;
  document.getElementById('p-username').value = p.username; document.getElementById('p-gender').value = p.gender||'';
  document.getElementById('p-bio').value = p.bio||''; }
function deletePersona(id) { personas = personas.filter(p => p.id !== id); if(activePersonaId === id) activePersonaId = null;
  localStorage.setItem('fcn_personas', JSON.stringify(personas)); renderPersonas(); toast('Deleted'); }
function clearPersona() { ['p-name','p-username','p-gender','p-bio'].forEach(id => { const el=document.getElementById(id); el.tagName==='SELECT'?el.selectedIndex=0:el.value=''; });
  document.getElementById('p-name').dataset.editId = ''; }

function escHtml(s) { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function escAttr(s) { return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }

renderPersonas();
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
if __name__ == "__main__":
    # Check browser-use
    env = {**os.environ, "PATH": f"{os.path.expanduser('~/.browser-use-env/bin')}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["which", "browser-use"], capture_output=True, text=True, env=env)
    if not r.stdout.strip():
        print("⚠️  browser-use not found in PATH")
        exit(1)

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"🚀 FCN Assistant running at http://localhost:{PORT}")
    print(f"   Open in your browser -> Login -> watch the browser window appear")
    print(f"   Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        bu(["close"], timeout=10)
        server.server_close()