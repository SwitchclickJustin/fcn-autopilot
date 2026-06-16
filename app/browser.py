"""Browser Use Cloud v3 driver — provisions remote Chromium via REST API + CDP."""
import asyncio
import json
import logging
import random
import time
from typing import Optional

import httpx
from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.browser-use.com/api/v3"

# Decoda residential proxy pool — each entry: host:port:username:password
# Picks a random one each session for IP rotation
DECODA_PROXIES = [
    {"host": "gate.decodo.com", "port": 10001, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10002, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10003, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10004, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10005, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10006, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10007, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10008, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10009, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10010, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
]


class BrowserSession:
    """Represents a single Browser Use Cloud v3 browser session."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.box_id: str = ""
        self.live_url: str = ""
        self.status: str = "created"
        self._connected: bool = False
        self._page = None
        self._cdp = None
        self._playwright = None

    async def _api(self, method: str, path: str, json_data: dict = None) -> Optional[dict]:
        """Make an API call to Browser Use Cloud v3."""
        api_key = settings.browser_use_api_key
        if not api_key:
            logger.error("BROWSER_USE_API_KEY not set")
            return None

        headers = {"X-Browser-Use-API-Key": api_key}
        if json_data:
            headers["Content-Type"] = "application/json"

        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
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

    async def connect(self) -> bool:
        """Provision a cloud browser with Decoda proxy, connect via CDP."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        # Pick a random Decoda proxy from the pool for IP rotation
        decoda = random.choice(DECODA_PROXIES)

        # Create browser via REST API with Decoda custom proxy
        browser_config = {
            "timeout": 60,
            "browserScreenWidth": 1280,
            "browserScreenHeight": 720,
            "enableRecording": False,
            "customProxy": decoda,  # REST API uses camelCase
        }

        result = await self._api("POST", "browsers", browser_config)
        if not result:
            logger.error("Browser API returned None — SDK/proxy setup failed")
            self.status = "error"
            return False
        if not result.get("cdpUrl"):
            logger.error(f"Browser API response missing cdpUrl: {json.dumps(result)[:200]}")
            self.status = "error"
            return False

        self.box_id = result.get("id", "")
        self.live_url = result.get("liveUrl", "")
        cdp_url = result.get("cdpUrl", "")
        logger.info(f"Cloud browser created: {self.box_id}, proxy port {decoda['port']}")

        # Connect via CDP WebSocket using Playwright
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            wss_url = cdp_url.replace("https://", "wss://")
            self._cdp = await self._playwright.chromium.connect_over_cdp(wss_url, timeout=30000)

            # Use the first default context/page or create one
            contexts = self._cdp.contexts
            if contexts:
                pages = contexts[0].pages
                self._page = pages[0] if pages else await contexts[0].new_page()
            else:
                ctx = await self._cdp.new_context()
                self._page = await ctx.new_page()

            # Auto-dismiss dialogs & close popup windows
            self._page.on("dialog", lambda dialog: asyncio.ensure_future(self._handle_dialog(dialog)))
            self._page.on("popup", lambda popup: asyncio.ensure_future(self._close_popup(popup)))

            # Block popups at the JS source — override window.open before any page scripts run
            await self._page.add_init_script("""
                (() => {
                    const origOpen = window.open;
                    window.open = function(url, ...args) {
                        if (url && (url.includes('freechatnow') || url.includes('fcnchat'))) {
                            return origOpen.call(window, url, ...args);
                        }
                        return null;
                    };
                    // Kill any ad layer that appears
                    setInterval(() => {
                        document.querySelectorAll('iframe').forEach(f => {
                            if (f.src && !f.src.includes('freechatnow') && !f.src.includes('fcnchat')) {
                                f.remove();
                            }
                        });
                    }, 2000);
                })();
            """)

            self._connected = True
            self.status = "connected"
            logger.info("CDP connection established")
            return True

        except ImportError:
            logger.error("playwright not installed — run: pip install playwright")
            self.status = "error"
            return False
        except Exception as e:
            logger.error(f"CDP connection failed: {e}")
            self.status = "error"
            return False