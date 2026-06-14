"""Browser Use Cloud driver — provisions and controls remote Chromium browsers."""
import asyncio
import json
import logging
import subprocess
import time
import os
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)

BROWSER_USE_BIN = os.path.expanduser("~/.browser-use-env/bin/browser-use")

def _bu_path():
    """Return path to browser-use CLI."""
    # Check multiple possible install locations
    candidates = [
        BROWSER_USE_BIN,
        os.path.expanduser("~/.browser-use/bin/browser-use"),
        "/root/.browser-use-env/bin/browser-use",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # Try PATH
    import shutil
    return shutil.which("browser-use") or "browser-use"

async def _bu(args: list, timeout: int = 30) -> str:
    """Run a browser-use CLI command asynchronously."""
    cmd = [_bu_path()] + args
    env = {**os.environ, "BROWSER_USE_API_KEY": settings.browser_use_api_key}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = (stderr or stdout).decode().strip()[:200]
            return f"ERR:{err}"
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        return "ERR:timeout"
    except FileNotFoundError:
        return "ERR:browser-use not found"


class BrowserSession:
    """Represents a single Browser Use Cloud session."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.session_id: str = ""
        self.live_url: str = ""
        self.cdp_url: str = ""
        self.status: str = "created"
        self._connected: bool = False

    async def connect(self) -> bool:
        """Provision a cloud browser and connect. Returns True on success."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        proxy = self.persona.get("proxy_country", "us")
        custom_proxy = self.persona.get("proxy_custom", "")
        
        if custom_proxy:
            # Custom proxy (e.g., Decodo socks5://user:pass@host:port)
            await _bu(["config", "set", "cloud_connect_proxy", "custom"], timeout=5)
            await _bu(["config", "set", "cloud_connect_custom_proxy", custom_proxy], timeout=5)
        else:
            # Built-in Browser Use proxy locations
            await _bu(["config", "set", "cloud_connect_proxy", proxy], timeout=5)

        await _bu(["config", "set", "cloud_connect_recording", "false"], timeout=5)

        # Close any existing session first
        await _bu(["close"])

        # Connect to Browser Use Cloud
        result = await _bu(["cloud", "connect"], timeout=45)
        if result.startswith("ERR:"):
            logger.error(f"Cloud connect failed: {result}")
            self.status = "error"
            return False

        # Parse live URL and CDP URL from output
        for line in result.split("\n"):
            if "live_url" in line:
                self.live_url = line.split("live_url:")[-1].strip()
            elif "cdp_url" in line:
                self.cdp_url = line.split("cdp_url:")[-1].strip()

        # Set user-agent from persona config
        ua = self.persona.get("user_agent", "random")
        if ua and ua != "random" and self.cdp_url:
            # Map friendly names to real UA strings
            ua_map = {
                "chrome_win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
                "chrome_mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
                "safari_mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/604.1",
                "safari_ios": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                "firefox_linux": "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
            }
            real_ua = ua_map.get(ua, "")
            if real_ua:
                await _bu(["eval", f"navigator.__defineGetter__('userAgent', () => '{real_ua}');"], timeout=5)

        # Navigate to FCN
        room = self.persona.get("selected_rooms", ["SextChat"])[0]
        nav_result = await _bu(["open", f"https://www.freechatnow.com/chat/{room.lower()}"], timeout=20)
        if nav_result.startswith("ERR:"):
            logger.error(f"FCN navigate failed: {nav_result}")
            self.status = "error"
            return False

        await asyncio.sleep(3)
        self._connected = True
        self.status = "connected"
        logger.info(f"Browser session active. Live URL: {self.live_url}")
        return True

    async def login(self) -> bool:
        """Fill the FCN login form and click Chat As Guest."""
        if not self._connected:
            logger.error("Cannot login: browser not connected")
            return False

        username = self.persona.get("username", "ChatBot_42")
        gender = self.persona.get("gender", "f")
        # Auto-calculate birthdate for 22-26 year old
        import random, datetime
        age = random.randint(22, 26)
        year = datetime.date.today().year - age
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthdate = f"{year}-{month:02d}-{day:02d}"

        # Fill form via JS eval
        js = f"""
(() => {{
    const fill = (sel, val) => {{
        const el = document.querySelector(sel);
        if (!el) return false;
        el.value = val;
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }};
    fill('input[name="username"]', '{username}');
    fill('select[name="gender"]', '{gender}');
    fill('input[name="birthdate"]', '{birthdate}');
    const cb = document.querySelector('input[type="checkbox"]');
    if (cb) {{ cb.checked = true; cb.dispatchEvent(new Event('change', {{bubbles: true}})); }}
    return 'form filled';
}})();
"""
        result = await _bu(["eval", js])
        await asyncio.sleep(1)

        # Click Chat As Guest
        click_result = await _bu(["eval", """
document.querySelector('button[type="submit"][value="guest"]').click();
'submitted';
"""])
        await asyncio.sleep(5)

        self.status = "logged_in"
        logger.info(f"Logged in as {username}")
        return True

    async def read_chat(self) -> list:
        """Extract visible chat messages from the page."""
        if not self._connected:
            return []

        js = """
JSON.stringify((() => {
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
    const allText = document.body.innerText.split('\\n').filter(t => t.trim()).slice(-30);
    return allText;
})());
"""
        result = await _bu(["eval", js])
        if result.startswith("ERR:") or not result:
            return []
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return []

    async def send_message(self, message: str) -> bool:
        """Type a message into the chat input and send it."""
        if not self._connected or not message:
            return False

        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        js = f"""
(() => {{
    const input = document.querySelector('textarea') || document.querySelector('[contenteditable]');
    if (!input) return 'no input found';
    input.value = '{escaped}';
    input.dispatchEvent(new Event('input', {{bubbles: true}}));
    input.dispatchEvent(new Event('change', {{bubbles: true}}));
    input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
    // Also click any send button
    const btn = document.querySelector('button[type=submit], [class*=send]');
    if (btn) btn.click();
    return 'sent';
}})();
"""
        result = await _bu(["eval", js])
        await asyncio.sleep(1)
        return not result.startswith("ERR:")

    async def take_screenshot(self) -> Optional[str]:
        """Take a screenshot, return path."""
        path = f"/tmp/fcn_screenshot_{int(time.time())}.png"
        result = await _bu(["screenshot", path])
        if result.startswith("ERR:"):
            return None
        return path

    async def disconnect(self):
        """Close the browser session."""
        await _bu(["close"])
        self._connected = False
        self.status = "disconnected"


class BrowserManager:
    """Manages the lifecycle of Browser Use Cloud sessions."""

    def __init__(self):
        self.current_session: Optional[BrowserSession] = None

    async def start_session(self, persona: dict) -> Optional[BrowserSession]:
        """Provision + login in one call."""
        if self.current_session:
            await self.current_session.disconnect()

        session = BrowserSession(persona)
        if not await session.connect():
            return None
        if not await session.login():
            await session.disconnect()
            return None

        self.current_session = session
        return session

    async def stop_session(self):
        if self.current_session:
            await self.current_session.disconnect()
            self.current_session = None

browser_manager = BrowserManager()