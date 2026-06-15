"""Browser Use Cloud v3 driver — provisions remote Chromium browsers via CDP protocol."""
import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Optional

import httpx
import websockets
from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.browser-use.com/api/v3"

class BrowserSession:
    """Represents a single Browser Use Cloud v3 browser session (raw CDP browser)."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.box_id: str = ""
        self.live_url: str = ""
        self.cdp_url: str = ""
        self.status: str = "created"
        self._connected: bool = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._cmd_id: int = 0

    async def _cdp_send(self, method: str, params: dict = None) -> dict:
        """Send a CDP command via WebSocket and return the result."""
        if not self._ws:
            raise RuntimeError("CDP WebSocket not connected")
        self._cmd_id += 1
        msg = {"id": self._cmd_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))
        # Read responses until we get the matching id
        while True:
            raw = await self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._cmd_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})
            # Otherwise it's an event — ignore for now

    async def connect(self) -> bool:
        """Provision a cloud browser via POST /api/v3/browsers. Returns True on success."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        api_key = settings.browser_use_api_key
        if not api_key:
            logger.error("BROWSER_USE_API_KEY not set — add it to Railway environment variables")
            self.status = "error"
            return False

        headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}

        # Build browser config
        body = {
            "timeout": 60,  # 1 hour inactivity timeout
            "enableRecording": False,
        }

        # Handle proxy — supports socks5:// and http:// formats
        custom_proxy = self.persona.get("proxy_custom", "").strip()
        if custom_proxy:
            # Try socks5://user:pass@host:port format
            proxy_match = re.match(r"(socks5|http|https)://(.+?):(.+?)@(.+?):(\d+)", custom_proxy)
            if proxy_match:
                body["customProxy"] = {
                    "host": proxy_match.group(4),
                    "port": int(proxy_match.group(5)),
                    "username": proxy_match.group(2),
                    "password": proxy_match.group(3),
                }
            else:
                # Try user:pass@host:port format (no scheme)
                proxy_match = re.match(r"(.+?):(.+?)@(.+?):(\d+)", custom_proxy)
                if proxy_match:
                    body["customProxy"] = {
                        "host": proxy_match.group(3),
                        "port": int(proxy_match.group(4)),
                        "username": proxy_match.group(1),
                        "password": proxy_match.group(2),
                    }
                else:
                    logger.warning(f"Could not parse custom proxy: {custom_proxy}, using default")
                    body["proxyCountryCode"] = "us"
        else:
            proxy = self.persona.get("proxy_country", "us")
            if proxy and proxy != "none":
                body["proxyCountryCode"] = proxy.lower()

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{API_BASE}/browsers", json=body, headers=headers)
                logger.info(f"BU Cloud API response: {resp.status_code}")

                if resp.status_code == 402:
                    logger.error("Browser Use Cloud: insufficient credits")
                    self.status = "error"
                    return False
                
                if resp.status_code == 422:
                    logger.error(f"BU Cloud validation error: {resp.text[:500]}")
                    self.status = "error"
                    return False

                resp.raise_for_status()
                data = resp.json()

                self.box_id = data.get("id", "")
                self.live_url = data.get("liveUrl", "") or data.get("live_url", "")
                self.cdp_url = data.get("cdpUrl", "") or data.get("cdp_url", "")

                if not self.cdp_url:
                    logger.error(f"No CDP URL in response: {data}")
                    self.status = "error"
                    return False

                # Connect to CDP WebSocket
                self._ws = await websockets.connect(self.cdp_url, max_size=2**22)
                self._connected = True
                self.status = "connected"
                logger.info(f"Browser provisioned: {self.box_id}, live: {self.live_url}")
                return True

        except httpx.HTTPStatusError as e:
            logger.error(f"API error creating browser: {e.response.status_code} {e.response.text[:300]}")
            self.status = "error"
            return False
        except Exception as e:
            logger.error(f"Failed to create browser: {e}")
            self.status = "error"
            return False

    async def login(self) -> bool:
        """Navigate to FCN, fill login form, and click Chat As Guest using CDP."""
        if not self._connected or not self._ws:
            logger.error("Cannot login: browser not connected")
            return False

        username = self.persona.get("username", "ChatBot_42")
        gender = self.persona.get("gender", "f")
        # Auto-calculate birthdate for 22-26 year old
        age = random.randint(22, 26)
        year = time.localtime().tm_year - age
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthdate = f"{year}-{month:02d}-{day:02d}"

        try:
            # Navigate to FCN SextChat
            logger.info(f"Navigating to FCN as {username}...")
            await self._cdp_send("Page.navigate", {"url": "https://www.freechatnow.com/chat/sextchat"})
            await asyncio.sleep(3)

            # Fill username
            js = f"document.querySelector('input[name=\"username\"]').value = '{username}';" \
                 f"document.querySelector('input[name=\"username\"]').dispatchEvent(new Event('input', {{bubbles:true}}));"
            await self._cdp_send("Runtime.evaluate", {"expression": js})
            await asyncio.sleep(0.5)

            # Select gender
            js = f"document.querySelector('select[name=\"gender\"]').value = '{gender}';" \
                 f"document.querySelector('select[name=\"gender\"]').dispatchEvent(new Event('change', {{bubbles:true}}));"
            await self._cdp_send("Runtime.evaluate", {"expression": js})
            await asyncio.sleep(0.5)

            # Set birthdate
            js = f"document.querySelector('input[name=\"birthdate\"]').value = '{birthdate}';" \
                 f"document.querySelector('input[name=\"birthdate\"]').dispatchEvent(new Event('input', {{bubbles:true}}));"
            await self._cdp_send("Runtime.evaluate", {"expression": js})
            await asyncio.sleep(0.5)

            # Check age checkbox
            js = "document.querySelector('input[type=\"checkbox\"]').checked = true;" \
                 "document.querySelector('input[type=\"checkbox\"]').dispatchEvent(new Event('change', {bubbles:true}));"
            await self._cdp_send("Runtime.evaluate", {"expression": js})
            await asyncio.sleep(0.5)

            # Click Chat As Guest
            js = "document.querySelector('button[type=\"submit\"][value=\"guest\"]').click();"
            await self._cdp_send("Runtime.evaluate", {"expression": js})
            await asyncio.sleep(5)

            self.status = "logged_in"
            logger.info(f"Logged in as {username}")
            return True

        except Exception as e:
            logger.error(f"Login failed: {e}")
            self.status = "error"
            return False

    async def read_chat(self) -> list:
        """Extract visible chat messages from the page via CDP Runtime.evaluate."""
        if not self._connected:
            return []

        try:
            result = await self._cdp_send("Runtime.evaluate", {
                "expression": """
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
""",
                "returnByValue": True
            })
            raw = result.get("result", {}).get("value", "[]")
            if isinstance(raw, str):
                return json.loads(raw)
            return raw if isinstance(raw, list) else []
        except Exception as e:
            logger.error(f"Read chat failed: {e}")
            return []

    async def send_message(self, message: str) -> bool:
        """Type a message into the chat input and send it via CDP."""
        if not self._connected or not message:
            return False

        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

        try:
            result = await self._cdp_send("Runtime.evaluate", {
                "expression": f"""
(() => {{
    const input = document.querySelector('textarea') || document.querySelector('[contenteditable]') || document.querySelector('input[type=text]');
    if (!input) return 'no input';
    input.value = '{escaped}';
    input.dispatchEvent(new Event('input', {{bubbles: true}}));
    input.dispatchEvent(new Event('change', {{bubbles: true}}));
    input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
    const btn = document.querySelector('button[type=submit], [class*=send]');
    if (btn) btn.click();
    return 'sent';
}})();
""",
                "returnByValue": True
            })
            await asyncio.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Send message failed: {e}")
            return False

    async def disconnect(self):
        """Destroy the cloud browser via DELETE /api/v3/browsers/{id}."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if not self.box_id:
            return

        api_key = settings.browser_use_api_key
        headers = {"X-Browser-Use-API-Key": api_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(f"{API_BASE}/browsers/{self.box_id}", headers=headers)
                logger.info(f"Browser {self.box_id} destroyed")
        except Exception as e:
            logger.warning(f"Failed to destroy browser {self.box_id}: {e}")

        self._connected = False
        self.status = "disconnected"


class BrowserManager:
    """Manages the lifecycle of Browser Use Cloud v3 browser sessions."""

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