"""Browser Use Cloud driver — provisions and controls remote Chromium browsers via REST API."""
import asyncio
import json
import logging
import os
import random
import re
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
        self.cdp_url: str = ""
        self.status: str = "created"
        self._connected: bool = False

    async def connect(self) -> bool:
        """Provision a cloud browser (box) via API. Returns True on success."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        api_key = settings.browser_use_api_key
        if not api_key:
            logger.error("BROWSER_USE_API_KEY not set")
            self.status = "error"
            return False

        headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}

        # Build box config
        box_config = {}
        
        custom_proxy = self.persona.get("proxy_custom", "").strip()
        if custom_proxy:
            box_config["proxy"] = "custom"
            box_config["custom_proxy"] = custom_proxy
        else:
            proxy = self.persona.get("proxy_country", "us")
            if proxy != "us":
                box_config["proxy"] = proxy

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Create a new box (cloud browser)
                resp = await client.post(f"{API_BASE}/boxes", json=box_config, headers=headers)
                
                if resp.status_code == 402:
                    logger.error("Browser Use Cloud: insufficient credits")
                    self.status = "error"
                    return False
                    
                resp.raise_for_status()
                data = resp.json()
                
                self.box_id = data.get("id", "")
                self.live_url = data.get("live_url", "")
                self.cdp_url = data.get("cdp_url", "")
                
                if self.cdp_url:
                    self._connected = True
                    self.status = "connected"
                    logger.info(f"Box provisioned: {self.box_id}")
                    return True
                else:
                    logger.error(f"No CDP URL in response: {data}")
                    self.status = "error"
                    return False

        except httpx.HTTPStatusError as e:
            logger.error(f"API error creating box: {e.response.status_code} {e.response.text[:200]}")
            self.status = "error"
            return False
        except Exception as e:
            logger.error(f"Failed to create box: {e}")
            self.status = "error"
            return False

    async def login(self) -> bool:
        """Navigate to FCN, fill login form, and click Chat As Guest."""
        if not self._connected:
            logger.error("Cannot login: browser not connected")
            return False

        api_key = settings.browser_use_api_key
        headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}

        username = self.persona.get("username", "ChatBot_42")
        gender = self.persona.get("gender", "f")
        # Auto-calculate birthdate for 22-26 year old
        age = random.randint(22, 26)
        year = time.localtime().tm_year - age
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthdate = f"{year}-{month:02d}-{day:02d}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Navigate to FCN
                nav_resp = await client.post(f"{API_BASE}/boxes/{self.box_id}/navigate", json={
                    "url": f"https://www.freechatnow.com/chat/sextchat"
                }, headers=headers)
                nav_resp.raise_for_status()
                await asyncio.sleep(3)

                # Fill username
                await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": f"document.querySelector('input[name=\"username\"]').value = '{username}';"
                            f"document.querySelector('input[name=\"username\"]').dispatchEvent(new Event('input', {{bubbles:true}}));"
                }, headers=headers)
                await asyncio.sleep(0.5)

                # Select gender
                await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": f"document.querySelector('select[name=\"gender\"]').value = '{gender}';"
                            f"document.querySelector('select[name=\"gender\"]').dispatchEvent(new Event('change', {{bubbles:true}}));"
                }, headers=headers)
                await asyncio.sleep(0.5)

                # Set birthdate
                await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": f"document.querySelector('input[name=\"birthdate\"]').value = '{birthdate}';"
                            f"document.querySelector('input[name=\"birthdate\"]').dispatchEvent(new Event('input', {{bubbles:true}}));"
                }, headers=headers)
                await asyncio.sleep(0.5)

                # Check age checkbox
                await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": "document.querySelector('input[type=\"checkbox\"]').checked = true;"
                            "document.querySelector('input[type=\"checkbox\"]').dispatchEvent(new Event('change', {bubbles:true}));"
                }, headers=headers)
                await asyncio.sleep(0.5)

                # Click Chat As Guest
                await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": "document.querySelector('button[type=\"submit\"][value=\"guest\"]').click();"
                }, headers=headers)
                await asyncio.sleep(5)

                self.status = "logged_in"
                logger.info(f"Logged in as {username}")
                return True

        except Exception as e:
            logger.error(f"Login failed: {e}")
            self.status = "error"
            return False

    async def read_chat(self) -> list:
        """Extract visible chat messages from the page."""
        if not self._connected:
            return []

        api_key = settings.browser_use_api_key
        headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": """
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
                }, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result", "[]")
                if isinstance(result, str):
                    return json.loads(result)
                return result or []
        except Exception as e:
            logger.error(f"Read chat failed: {e}")
            return []

    async def send_message(self, message: str) -> bool:
        """Type a message into the chat input and send it."""
        if not self._connected or not message:
            return False

        api_key = settings.browser_use_api_key
        headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}
        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{API_BASE}/boxes/{self.box_id}/eval", json={
                    "code": f"""
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
"""
                }, headers=headers)
                resp.raise_for_status()
                await asyncio.sleep(1)
                return True
        except Exception as e:
            logger.error(f"Send message failed: {e}")
            return False

    async def disconnect(self):
        """Destroy the cloud browser box."""
        if not self.box_id:
            return
        api_key = settings.browser_use_api_key
        headers = {"X-Browser-Use-API-Key": api_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(f"{API_BASE}/boxes/{self.box_id}", headers=headers)
        except Exception:
            pass
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