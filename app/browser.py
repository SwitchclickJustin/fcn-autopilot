"""Playwright-based browser driver — runs Chromium locally on Railway to control FCN chat."""
import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class BrowserSession:
    """Manages a local Playwright browser session for FCN chat."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.status: str = "created"
        self.live_url: str = ""
        self._browser = None
        self._context = None
        self._page = None

    async def connect(self) -> bool:
        """Launch Playwright Chromium with stealth config. Returns True on success."""
        logger.info(f"Launching local browser for {self.persona.get('username')}")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed — run: pip install playwright && playwright install chromium")
            self.status = "error"
            return False

        try:
            p = await async_playwright().start()

            # Build proxy config from persona
            proxy_settings = None
            custom_proxy = self.persona.get("proxy_custom", "").strip()
            if custom_proxy:
                # Parse socks5://user:pass@host:port or http://user:pass@host:port
                proxy_match = re.match(r"(socks5|http|https)://(.+?):(.+?)@(.+?):(\d+)", custom_proxy)
                if not proxy_match:
                    proxy_match = re.match(r"(.+?):(.+?)@(.+?):(\d+)", custom_proxy)
                if proxy_match:
                    scheme = proxy_match.group(1) if proxy_match.lastindex >= 1 and proxy_match.group(1) in ('socks5', 'http', 'https') else 'http'
                    proxy_settings = {
                        "server": f"{scheme}://{proxy_match.group(4)}:{proxy_match.group(5)}",
                        "username": proxy_match.group(2) if scheme != 'socks5' else proxy_match.group(2),
                        "password": proxy_match.group(3) if scheme != 'socks5' else proxy_match.group(3),
                    }

            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--lang=en-US",
            ]

            self._browser = await p.chromium.launch(
                headless=True,
                args=browser_args,
                proxy=proxy_settings,
            )

            # Create context with stealth user agent
            user_agent_map = {
                "random": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Chrome/Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Chrome/macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Safari/macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
                "Safari/iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
                "Firefox/Linux": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
            }
            ua_key = self.persona.get("user_agent", "random")
            ua = user_agent_map.get(ua_key, user_agent_map["random"])

            self._context = await self._browser.new_context(
                user_agent=ua,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )

            # Stealth: override navigator.webdriver
            await self._context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            """)

            self._page = await self._context.new_page()

            # Auto-dismiss dialogs (alerts, confirms, prompts)
            self._page.on("dialog", lambda dialog: asyncio.ensure_future(self._handle_dialog(dialog)))

            # Close popup windows immediately
            self._page.on("popup", lambda popup: asyncio.ensure_future(self._close_popup(popup)))

            self.status = "connected"
            self.live_url = "local"  # No live URL for local Playwright
            logger.info("Local browser launched successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to launch browser: {e}")
            self.status = "error"
            return False

    async def _handle_dialog(self, dialog):
        """Auto-dismiss any dialog (alert/confirm/prompt)."""
        try:
            await dialog.dismiss()
        except Exception:
            pass

    async def _close_popup(self, popup):
        """Close any popup window immediately."""
        try:
            await popup.close()
        except Exception:
            pass

    async def _close_overlays(self):
        """Close any modal overlays or popup ads on the page."""
        if not self._page:
            return
        try:
            await self._page.evaluate("""
                (() => {
                    // Close any modal overlay by clicking X buttons or close links
                    const closeSelectors = [
                        '.close', '.modal-close', '[class*=close]', '[class*=dismiss]',
                        'button[class*=close]', 'a[class*=close]', '[aria-label*=close]',
                        '[aria-label*=Close]', '.popup-close', '.ad-close',
                        '.overlay-close', '.modal .close', 'button:has-text("X")',
                        'button:has-text("Close")', 'button:has-text("No thanks")',
                        'button:has-text("Continue")', 'a:has-text("Skip")'
                    ];
                    for (const sel of closeSelectors) {
                        const el = document.querySelector(sel);
                        if (el) el.click();
                    }
                    // Remove any overlay divs that block the page
                    const overlays = document.querySelectorAll('.modal, .overlay, .popup, [class*=modal], [class*=overlay], [class*=popup]');
                    overlays.forEach(el => {
                        if (el.style && el.style.display !== 'none') {
                            el.style.display = 'none';
                        }
                    });
                    return overlays.length;
                })();
            """)
        except Exception:
            pass

    async def login(self) -> bool:
        """Navigate to FCN, fill login form, and click Chat As Guest."""
        if not self._page:
            logger.error("Cannot login: browser not launched")
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
            await self._page.goto("https://www.freechatnow.com/chat/sextchat", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await self._close_overlays()
            await asyncio.sleep(1)

            # Fill username
            await self._page.evaluate(f"""
                document.querySelector('input[name="username"]').value = '{username}';
                document.querySelector('input[name="username"]').dispatchEvent(new Event('input', {{bubbles:true}}));
            """)
            await asyncio.sleep(0.5)

            # Select gender
            await self._page.evaluate(f"""
                document.querySelector('select[name="gender"]').value = '{gender}';
                document.querySelector('select[name="gender"]').dispatchEvent(new Event('change', {{bubbles:true}}));
            """)
            await asyncio.sleep(0.5)

            # Set birthdate
            await self._page.evaluate(f"""
                document.querySelector('input[name="birthdate"]').value = '{birthdate}';
                document.querySelector('input[name="birthdate"]').dispatchEvent(new Event('input', {{bubbles:true}}));
            """)
            await asyncio.sleep(0.5)

            # Check age checkbox
            await self._page.evaluate("""
                document.querySelector('input[type="checkbox"]').checked = true;
                document.querySelector('input[type="checkbox"]').dispatchEvent(new Event('change', {bubbles:true}));
            """)
            await asyncio.sleep(0.5)

            # Click Chat As Guest
            await self._page.evaluate("""
                document.querySelector('button[type="submit"][value="guest"]').click();
            """)
            await asyncio.sleep(3)
            await self._close_overlays()
            await asyncio.sleep(2)

            self.status = "logged_in"
            logger.info(f"Logged in as {username}")
            return True

        except Exception as e:
            logger.error(f"Login failed: {e}")
            self.status = "error"
            return False

    async def read_chat(self) -> list:
        """Extract visible chat messages from the page via JavaScript."""
        if not self._page:
            return []

        try:
            result = await self._page.evaluate("""
                (() => {
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
                })();
            """)
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            logger.error(f"Read chat failed: {e}")
            return []

    async def send_message(self, message: str) -> bool:
        """Type a message into the chat input and send it."""
        if not self._page or not message:
            return False

        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

        try:
            result = await self._page.evaluate(f"""
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
            return True
        except Exception as e:
            logger.error(f"Send message failed: {e}")
            return False

    async def disconnect(self):
        """Close the browser and release resources."""
        if self._browser:
            try:
                await self._browser.close()
                logger.info("Browser closed")
            except Exception as e:
                logger.warning(f"Failed to close browser: {e}")
            self._browser = None
            self._context = None
            self._page = None
            self.status = "disconnected"


class BrowserManager:
    """Manages the lifecycle of local Playwright browser sessions."""

    def __init__(self):
        self.current_session: Optional[BrowserSession] = None

    async def start_session(self, persona: dict) -> Optional[BrowserSession]:
        """Launch browser + login in one call."""
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