"""Browser Use Cloud driver — provisions remote Chromium via REST API and handles popups."""
import asyncio
import json
import logging
import os
import random
import time
from typing import Optional

import httpx
from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.browser-use.com/api/v3"

class BrowserSession:
    """Represents a single Browser Use Cloud session."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.box_id: str = ""
        self.live_url: str = ""
        self.status: str = "created"
        self._connected: bool = False

    async def _api(self, method: str, path: str, json_data: dict = None) -> Optional[dict]:
        """Make an API call to Browser Use Cloud."""
        api_key = settings.browser_use_api_key
        if not api_key:
            logger.error("BROWSER_USE_API_KEY not set")
            return None

        headers = {"X-Browser-Use-API-Key": api_key}
        if json_data:
            headers["Content-Type"] = "application/json"

        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(method, url, json=json_data, headers=headers)
                
                if resp.status_code == 402:
                    logger.error("Browser Use Cloud: insufficient credits")
                    return None
                    
                resp.raise_for_status()
                return resp.json() if resp.text else {}
        except httpx.HTTPStatusError as e:
            logger.error(f"API error {method} {path}: {e.response.status_code} {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"API call failed {method} {path}: {e}")
            return None

    async def _eval(self, code: str) -> Optional[str]:
        """Run JavaScript in the cloud browser via the API."""
        result = await self._api("POST", f"boxes/{self.box_id}/eval", {"code": code})
        if result:
            return result.get("result", "")
        return None

    async def connect(self) -> bool:
        """Provision a cloud browser (box) with proxy config."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        box_config = {}
        
        custom_proxy = self.persona.get("proxy_custom", "").strip()
        if custom_proxy:
            box_config["proxy"] = "custom"
            box_config["custom_proxy"] = custom_proxy
        else:
            proxy = self.persona.get("proxy_country", "us")
            if proxy != "us":
                box_config["proxy"] = proxy

        result = await self._api("POST", "boxes", box_config)
        if not result or not result.get("cdp_url"):
            self.status = "error"
            return False

        self.box_id = result.get("id", "")
        self.live_url = result.get("live_url", "")
        self._connected = True
        self.status = "connected"
        logger.info(f"Box provisioned: {self.box_id}")
        return True

    async def _close_popups(self):
        """Close any popups, overlays, or ad dialogs on the current page."""
        js = """
(() => {
    let closed = 0;
    
    // 1. Click close buttons
    const closeSelectors = [
        '.close', '.modal-close', '[class*=close]', '[class*=dismiss]',
        'button[class*=close]', 'a[class*=close]', '[aria-label*=close]',
        '.popup-close', '.ad-close', '.overlay-close',
        'button:contains("X")', 'button:contains("Close")',
        'button:contains("No thanks")', 'a:contains("Skip")'
    ];
    for (const sel of closeSelectors) {
        try {
            const el = document.querySelector(sel);
            if (el) { el.click(); closed++; }
        } catch(e) {}
    }
    
    // 2. Remove overlay/modal elements that block the page
    const blockers = document.querySelectorAll(
        '.modal, .overlay, .popup, [class*=modal], [class*=overlay], [class*=popup], ' +
        '[class*=ad-], [id*=ad-], iframe[src*=ad], iframe[src*=12chats]'
    );
    blockers.forEach(el => {
        if (el.style) el.style.display = 'none';
        if (el.tagName === 'IFRAME') el.remove();
        closed++;
    });
    
    // 3. Remove any fixed-position blockers
    document.querySelectorAll('div').forEach(el => {
        const style = window.getComputedStyle(el);
        if (style.position === 'fixed' && style.zIndex > 1000 && el.offsetHeight > 100) {
            el.style.display = 'none';
            closed++;
        }
    });
    
    return closed;
})();
"""
        await self._eval(js)

    async def login(self) -> bool:
        """Navigate to FCN, handle popups, fill login form."""
        if not self._connected:
            logger.error("Cannot login: browser not connected")
            return False

        username = self.persona.get("username", "ChatBot_42")
        gender = self.persona.get("gender", "f")
        age = random.randint(22, 26)
        year = time.localtime().tm_year - age
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthdate = f"{year}-{month:02d}-{day:02d}"

        # Navigate to FCN
        nav = await self._api("POST", f"boxes/{self.box_id}/navigate",
                              {"url": "https://www.freechatnow.com/chat/sext"})
        if not nav:
            self.status = "error"
            return False
        await asyncio.sleep(4)

        # Close any popups that appeared
        await self._close_popups()
        await asyncio.sleep(1)

        # Fill username
        await self._eval(f"""
            document.querySelector('input[name="username"]').value = '{username}';
            document.querySelector('input[name="username"]').dispatchEvent(new Event('input', {{bubbles:true}}));
        """)
        await asyncio.sleep(0.5)

        # Select gender
        await self._eval(f"""
            document.querySelector('select[name="gender"]').value = '{gender}';
            document.querySelector('select[name="gender"]').dispatchEvent(new Event('change', {{bubbles:true}}));
        """)
        await asyncio.sleep(0.5)

        # Set birthdate
        await self._eval(f"""
            document.querySelector('input[name="birthdate"]').value = '{birthdate}';
            document.querySelector('input[name="birthdate"]').dispatchEvent(new Event('input', {{bubbles:true}}));
        """)
        await asyncio.sleep(0.5)

        # Check age checkbox
        await self._eval("""
            document.querySelector('input[type="checkbox"]').checked = true;
            document.querySelector('input[type="checkbox"]').dispatchEvent(new Event('change', {bubbles:true}));
        """)
        await asyncio.sleep(0.5)

        # Close any remaining popups before clicking submit
        await self._close_popups()
        await asyncio.sleep(0.5)

        # Click Chat As Guest
        await self._eval("""
            document.querySelector('button[type="submit"][value="guest"]').click();
        """)
        await asyncio.sleep(5)

        self.status = "logged_in"
        logger.info(f"Logged in as {username}")
        return True

    async def read_chat(self) -> list:
        """Extract visible chat messages from the page."""
        if not self._connected:
            return []

        result = await self._eval("""
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
""")
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        return []

    async def send_message(self, message: str) -> bool:
        """Type a message into the chat input and send it."""
        if not self._connected or not message:
            return False

        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        result = await self._eval(f"""
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
""")
        await asyncio.sleep(1)
        return result is not None

    async def disconnect(self):
        """Destroy the cloud browser box."""
        if self.box_id:
            await self._api("DELETE", f"boxes/{self.box_id}")
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