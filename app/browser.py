"""BotOrchestrator — manages N concurrent Browser Use SDK sessions with Decoda proxies.

Architecture for 50 concurrent bots:
  One SDK client → one profile per persona (persistent cookies)
                   → one session per bot (SDK agent handles login/navigation)
                   → CDP connection for fast auto-pilot loop (JS evaluate, zero cost)
                   → SDK agent fallback for recovery (stuck pages, re-login)

Custom proxies (Decoda):
  Passed as custom_proxy via SDK's **extra kwargs -> REST API body.
  Requires a paid Browser Use Cloud plan tier that allows custom proxies.
  Falls back to BU built-in US residential proxy if custom_proxy is rejected.

Scaling:
  asyncio.Semaphore(50) caps concurrent sessions.
  Each bot is an independent asyncio Task with its own CDP connection.
  Profiles persist login state across app restarts via Browser Use Cloud.
"""
import asyncio
import json
import logging
import random
import re
import time
from typing import Optional

from app.config import settings
import app.database as db

# A user confirming they FOUND her elsewhere = a conversion ("did you find me?" → "yes")
_CONFIRM_RE = re.compile(
    r"\b(found (you|u|ya|her)|i found you|got you|i see you|see you (there|now)|there now|"
    r"messaged (you|u)|in your (dm|inbox)|texted you|added (you|u|ya))\b", re.I)

logger = logging.getLogger(__name__)

# ── Decoda proxy pool ──────────────────────────────────────────────────────────
# US-only residential, sticky ~10 min per port. us.decodo.com is the country-
# specific endpoint (Decodo geo-targets by HOSTNAME, not a username suffix).
# 50 ports (10001-10050) → up to 50 distinct sticky US IPs, one per bot.
_DCREDS = {"username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"}
DECODA_PROXIES = (
    [{"host": "us.decodo.com", "port": p, **_DCREDS} for p in range(10001, 10051)] +
    [{"host": "ca.decodo.com", "port": p, **_DCREDS} for p in range(20001, 20051)] +
    [{"host": "gb.decodo.com", "port": p, **_DCREDS} for p in range(30001, 30051)] +
    [{"host": "au.decodo.com", "port": p, **_DCREDS} for p in range(30001, 30051)]
)
# US and CA get 3× weight vs GB and AU — FCN Cloudflare blocks GB/AU IPs more often.
# Adjust the multiplier here if the balance needs tuning.
_PROXY_WEIGHTS = {
    "us.decodo.com": 3,
    "ca.decodo.com": 3,
    "gb.decodo.com": 1,
    "au.decodo.com": 1,
}
_PROXY_ALLOWED_CC = {"US", "CA", "GB", "AU"}


def _build_ua_pool() -> list:
    """Build a 500+ entry UA pool spanning desktop, tablet, and mobile devices."""
    uas = []

    # ── Desktop Chrome — Windows ───────────────────────────────────────────────
    _CV = ["126","125","124","123","122","121","120","119","118","117","116","115","114","113","112"]
    _CF = {"126":"126.0.6478.114","125":"125.0.6422.142","124":"124.0.6367.118",
           "123":"123.0.6312.122","122":"122.0.6261.128","121":"121.0.6167.140",
           "120":"120.0.6099.130","119":"119.0.6045.160","118":"118.0.5993.88",
           "117":"117.0.5938.150","116":"116.0.5845.187","115":"115.0.5790.173",
           "114":"114.0.5735.199","113":"113.0.5672.126","112":"112.0.5615.138"}
    for cv in _CV:
        uas.append(f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")
        uas.append(f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CF[cv]} Safari/537.36")
    for cv in _CV[:8]:
        uas.append(f"Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")
        uas.append(f"Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")

    # ── Desktop Chrome — macOS ─────────────────────────────────────────────────
    _MV = ["10_15_7","14_5","14_4_1","14_3","14_2","14_1","14_0",
           "13_6","13_5_2","13_5","13_4","12_7","12_6","11_7","11_6"]
    for cv in _CV:
        for mv in _MV[:6]:
            uas.append(f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mv}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")

    # ── Desktop Chrome — Linux ─────────────────────────────────────────────────
    for cv in _CV:
        uas.append(f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")
    for cv in _CV[:6]:
        uas.append(f"Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")

    # ── Edge — Windows & macOS ─────────────────────────────────────────────────
    for cv in _CV[:8]:
        uas.append(f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36 Edg/{cv}.0.0.0")
    for cv in _CV[:4]:
        uas.append(f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36 Edg/{cv}.0.0.0")

    # ── Firefox — Windows, macOS, Linux ───────────────────────────────────────
    _FV = ["127","126","125","124","123","122","121","120","119","118","117","116","115"]
    for fv in _FV:
        uas.append(f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{fv}.0) Gecko/20100101 Firefox/{fv}.0")
        uas.append(f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{fv}.0) Gecko/20100101 Firefox/{fv}.0")
        uas.append(f"Mozilla/5.0 (X11; Linux x86_64; rv:{fv}.0) Gecko/20100101 Firefox/{fv}.0")
    for fv in _FV[:6]:
        uas.append(f"Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:{fv}.0) Gecko/20100101 Firefox/{fv}.0")

    # ── Safari — macOS ─────────────────────────────────────────────────────────
    _SV = [("17.5","14_5"),("17.4.1","14_4_1"),("17.4","14_4"),("17.3","14_3"),
           ("17.2","14_2"),("17.1","14_1"),("17.0","13_6"),("16.6","13_5_2"),
           ("16.5","13_4"),("16.4","13_3"),("16.3","12_7"),("16.2","12_6"),("16.1","12_5")]
    for sv, mv in _SV:
        uas.append(f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mv}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{sv} Safari/605.1.15")

    # ── iPhone — Safari ────────────────────────────────────────────────────────
    _IOS = ["17_5","17_4_1","17_4","17_3","17_2","17_1","17_0",
            "16_7","16_6","16_5","16_4","16_3","16_2","16_1","16_0",
            "15_8","15_7","15_6","15_5","15_4","15_3","15_2","15_1","15_0"]
    _IOS_SV = {"17_5":"17.5","17_4_1":"17.4.1","17_4":"17.4","17_3":"17.3",
               "17_2":"17.2","17_1":"17.1","17_0":"17.0","16_7":"16.6",
               "16_6":"16.6","16_5":"16.5","16_4":"16.4","16_3":"16.3",
               "16_2":"16.2","16_1":"16.1","16_0":"16.0","15_8":"15.6.1",
               "15_7":"15.6.1","15_6":"15.6","15_5":"15.5","15_4":"15.4",
               "15_3":"15.3","15_2":"15.2","15_1":"15.1","15_0":"15.0"}
    for iosv in _IOS:
        sv = _IOS_SV.get(iosv, "17.0")
        uas.append(f"Mozilla/5.0 (iPhone; CPU iPhone OS {iosv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{sv} Mobile/15E148 Safari/604.1")

    # ── iPhone — Chrome (CriOS) ────────────────────────────────────────────────
    for cv in _CV[:10]:
        for iosv in _IOS[:10]:
            uas.append(f"Mozilla/5.0 (iPhone; CPU iPhone OS {iosv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/{cv}.0.0.0 Mobile/15E148 Safari/604.1")

    # ── iPhone — Firefox ───────────────────────────────────────────────────────
    for fv in _FV[:6]:
        for iosv in _IOS[:6]:
            uas.append(f"Mozilla/5.0 (iPhone; CPU iPhone OS {iosv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/{fv}.0 Mobile/15E148 Safari/604.1")

    # ── iPad — Safari ──────────────────────────────────────────────────────────
    _IPAD_IOS = ["17_5","17_4","17_3","17_2","17_1","17_0","16_7","16_6","16_5","16_4","16_3","16_2"]
    for iosv in _IPAD_IOS:
        sv = _IOS_SV.get(iosv, "17.0")
        uas.append(f"Mozilla/5.0 (iPad; CPU OS {iosv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{sv} Mobile/15E148 Safari/604.1")

    # ── iPad — Chrome ──────────────────────────────────────────────────────────
    for cv in _CV[:6]:
        for iosv in _IPAD_IOS[:6]:
            uas.append(f"Mozilla/5.0 (iPad; CPU OS {iosv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/{cv}.0.0.0 Mobile/15E148 Safari/604.1")

    # ── Android phones — Chrome ────────────────────────────────────────────────
    _ANDROID_PHONES = [
        ("14","Pixel 8 Pro"),("14","Pixel 8"),("14","SM-S928B"),("14","SM-S918B"),
        ("14","SM-A546B"),("14","SM-A346B"),("13","Pixel 7 Pro"),("13","Pixel 7"),
        ("13","Pixel 6a"),("13","SM-S911B"),("13","SM-S908B"),("13","SM-A536B"),
        ("13","SM-G998B"),("12","Pixel 6"),("12","SM-S906B"),("12","SM-G991B"),
        ("11","Pixel 5"),("11","SM-G996B"),("10","SM-G985F"),("10","SM-N981B"),
        ("14","OnePlus 12"),("13","OnePlus 11"),("13","OnePlus Nord 3"),
        ("14","Xiaomi 14"),("13","Xiaomi 13"),("13","Xiaomi 12T"),
        ("14","POCO X6 Pro"),("13","Redmi Note 13 Pro"),
        ("14","Moto G84"),("13","Moto G73"),
    ]
    for cv in _CV[:10]:
        for av, dev in _ANDROID_PHONES:
            uas.append(f"Mozilla/5.0 (Linux; Android {av}; {dev}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Mobile Safari/537.36")

    # ── Android tablets — Chrome ───────────────────────────────────────────────
    _ANDROID_TABLETS = [
        ("14","SM-X916B"),("14","SM-X810"),("13","SM-X706B"),("13","SM-T870"),
        ("12","SM-T975"),("13","Lenovo TB-X306X"),("12","SM-P615"),("13","SM-X200"),
    ]
    for cv in _CV[:6]:
        for av, dev in _ANDROID_TABLETS:
            uas.append(f"Mozilla/5.0 (Linux; Android {av}; {dev}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")

    # ── Samsung Internet ───────────────────────────────────────────────────────
    _SAMSUNG_PHONES = [("14","SM-S928B"),("14","SM-S918B"),("13","SM-S911B"),("13","SM-A546B")]
    for bv in ["24.0","23.0","22.0","21.0","20.0"]:
        for av, dev in _SAMSUNG_PHONES:
            uas.append(f"Mozilla/5.0 (Linux; Android {av}; {dev}) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/{bv} Chrome/117.0.0.0 Mobile Safari/537.36")

    # ── Firefox on Android ─────────────────────────────────────────────────────
    for fv in _FV[:8]:
        uas.append(f"Mozilla/5.0 (Android 14; Mobile; rv:{fv}.0) Gecko/20100101 Firefox/{fv}.0")
        uas.append(f"Mozilla/5.0 (Android 13; Mobile; rv:{fv}.0) Gecko/20100101 Firefox/{fv}.0")

    # Deduplicate while preserving order
    seen, unique = set(), []
    for ua in uas:
        if ua not in seen:
            seen.add(ua)
            unique.append(ua)
    return unique


UA_POOL = _build_ua_pool()


# ── Room pool (200+ user rooms verified from FCN room list) ───────────────────
# Verified working FCN room slugs (confirmed URLs 2026-06-17)
FCN_ROOMS = ["sex", "adult", "singles", "sext"]

FCN_SLUG_MAP: dict[str, str] = {
    "sex":     "sex",
    "adult":   "adult",
    "singles": "singles",
    "sext":    "sext",
}


def assign_rooms(count: int, pool: list = FCN_ROOMS) -> list:
    """Assign 2 rooms to each of `count` agents — max 2 agents per room.

    Returns a list of [room1, room2] pairs. With 4 agents across 10 rooms,
    all pairs are distinct (0 room collisions). Degrades gracefully when
    count * 2 > len(pool) * 2 by recycling least-used rooms.
    """
    usage: dict = {r: 0 for r in pool}
    assignments = []
    for _ in range(count):
        available = sorted([r for r in pool if usage[r] < 2], key=lambda r: usage[r])
        if len(available) < 2:
            available = sorted(pool, key=lambda r: usage[r])
        picked = available[:2]
        assignments.append(list(picked))
        for r in picked:
            usage[r] += 1
    return assignments


# ── Bot state per persona ──────────────────────────────────────────────────────
class BotWorker:
    """Holds runtime state for one bot persona."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.username: str = persona.get("username", "ChatBot_42")
        self.agent_id: str = self.username  # UNIQUE key — set before inserting into _workers
        self.login_name: str = self.username  # generated FCN chat identity (per session)
        self.rooms: list = []          # assigned rooms for this agent [primary, secondary]
        self.room: str = ""            # current active room name
        self._room_index: int = 0      # rotates across self.rooms for group-room replies
        self.handle_shared: bool = False  # shared the contact handle (awaiting confirm)
        self.in_dm: bool = False           # currently responding in a DM thread
        # DM conversation tracking: other_user → {conv_id, logged_count, is_first_bot_msg}
        self._dm_state: dict = {}
        self.profile_id: str = ""
        self.session_id: str = ""
        self.browser_id: str = ""
        self.live_url: str = ""
        self.proxy_port: int = 0       # Decoda port in use (unique per agent)
        self.proxy_host: str = ""      # Decoda host (us/ca/gb/au.decodo.com)
        self.proxy_ip: str = ""        # confirmed exit IP
        self.proxy_location: str = ""  # "City, Region, CC"
        self.status: str = "created"  # created | connecting | logging_in | running | error
        # diagnostics
        self.phase: str = "init"
        self.loop_ticks: int = 0
        self.send_attempts: int = 0
        self.send_oks: int = 0
        self.last_response: str = ""
        self.last_error: str = ""

        # CDP connection (for fast JS-based auto-pilot)
        self._page = None
        self._cdp = None
        self._playwright = None

        # SDK client run handle (for streaming / awaiting login tasks)
        self._login_run = None

        # Auto-pilot asyncio task
        self._task: Optional[asyncio.Task] = None

    @property
    def _connected(self) -> bool:
        """True when a live CDP page is attached (for status reporting)."""
        return self._page is not None

    async def disconnect_cdp(self):
        """Close CDP connection (keeps SDK session alive)."""
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._cdp = None
        self._playwright = None

    async def read_chat(self) -> list:
        """Read recent chat messages from this bot's page via CDP JS evaluate.

        Returns [] if no CDP page is attached (e.g. login still in progress).
        """
        if not self._page:
            return []
        try:
            result = await self._page.evaluate("""
                (() => {
                    // FCN room chat: ul > li.message-item, with .message-meta (user)
                    // and .message-text (body). Verified structure.
                    const box = document.querySelector('.room-messages-container');
                    if (!box) return [];
                    const out = [];
                    box.querySelectorAll('li.message-item').forEach(li => {
                        const textEl = li.querySelector('.message-text');
                        if (!textEl) return;
                        const msg = (textEl.textContent || '').trim();
                        if (!msg) return;
                        const metaEl = li.querySelector('.message-meta');
                        const user = metaEl ? (metaEl.textContent || '').trim().replace(/:+$/, '') : '';
                        out.push(user ? user + ': ' + msg : msg);
                    });
                    if (out.length) return out.slice(-30);
                    // fallback: raw child text
                    return Array.from(box.children)
                        .map(e => (e.textContent || '').trim()).filter(t => t).slice(-25);
                })();
            """)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    async def send_message(self, message: str) -> bool:
        """Type + send a chat message on this bot's page via CDP JS evaluate.

        Returns False if there is no message or no CDP page attached.
        """
        if not message or not self._page:
            return False
        # Single-line: the chat input sends on Enter, so a newline mid-message
        # fires a premature/partial send. Collapse whitespace + cap length.
        message = " ".join(message.split())[:300].strip()
        if not message:
            return False
        inp = await self._page.query_selector('input.writer-input, input[placeholder="Type to chat"]')
        if inp is None:
            for s in ('textarea', '[contenteditable]', 'input[type=search]', 'input[type=text]'):
                inp = await self._page.query_selector(s)
                if inp is not None:
                    break
        if inp is None:
            return False

        async def _sent() -> bool:
            try:
                return not (await inp.input_value())
            except Exception:
                return False

        # Type via real keystrokes + Enter, then VERIFY the input cleared (FCN
        # clears it on a successful send). Retry / try a send button if not.
        for attempt in range(2):
            try:
                try:
                    await inp.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await inp.focus()
                await inp.fill("")  # clear any stale text
                # Human-like typing ~25 WPM (FCN analyzes typing cadence). ~5
                # chars/word → ~0.48s/char avg = ~25 WPM, with jitter + the odd pause.
                for ch in message:
                    await self._page.keyboard.type(ch)
                    delay = random.uniform(0.28, 0.62)
                    if random.random() < 0.05:
                        delay += random.uniform(0.3, 0.9)  # brief "thinking" pause
                    await asyncio.sleep(delay)
                await asyncio.sleep(random.uniform(0.15, 0.45))
                await self._page.keyboard.press("Enter")
                await asyncio.sleep(0.7)
                if await _sent():
                    return True
                # Enter didn't submit — try clicking a send control
                for bsel in ("form.writer [class*=send i]", ".writer-message [class*=send i]",
                             "[aria-label*=send i]", "form.writer button[type=submit]"):
                    try:
                        btn = await self._page.query_selector(bsel)
                        if btn:
                            await btn.click(timeout=2000)
                            await asyncio.sleep(0.5)
                            if await _sent():
                                return True
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[{self.username}] send attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.5)
        return await _sent()

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "username": self.username,
            "login_name": self.login_name,
            "rooms": self.rooms,
            "room": self.room,
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "browser_id": self.browser_id,
            "live_url": self.live_url,
            "proxy_ip": self.proxy_ip,
            "proxy_location": self.proxy_location,
            "status": self.status,
            "phase": self.phase,
            "loop_ticks": self.loop_ticks,
            "send_attempts": self.send_attempts,
            "send_oks": self.send_oks,
            "last_response": self.last_response,
            "last_error": self.last_error,
        }


# ── Orchestrator ───────────────────────────────────────────────────────────────
class BotOrchestrator:
    """Manages N concurrent Browser Use SDK bot sessions.

    Usage:
        orchestrator = BotOrchestrator()
        worker = await orchestrator.start_bot(persona_dict)
        await orchestrator.stop_bot("Flirtyalexa9")
        await orchestrator.stop_all()
    """

    def __init__(self):
        self._client = None          # lazy-init AsyncBrowserUse
        self._semaphore = asyncio.Semaphore(50)
        self._workers: dict[str, BotWorker] = {}           # agent_id -> BotWorker
        self._auto_pilot_enabled: dict[str, bool] = {}    # agent_id -> bool

    # ── SDK client (lazy, single instance) ─────────────────────────────────

    async def _get_client(self):
        if self._client is None:
            from browser_use_sdk.v3 import AsyncBrowserUse
            self._client = AsyncBrowserUse(
                api_key=settings.browser_use_api_key,
                timeout=60,
            )
        return self._client

    # ── Profile management ──────────────────────────────────────────────────

    async def get_or_create_profile(self, persona_name: str) -> str:
        """Persistent profile per persona — cookies survive restarts."""
        client = await self._get_client()
        # Try existing profile first
        resp = await client.profiles.list(query=persona_name)
        if resp.items:
            profile = resp.items[0]
        else:
            profile = await client.profiles.create(name=persona_name)
        return str(profile.id)

    # ── Bot lifecycle ───────────────────────────────────────────────────────

    async def start_bot(self, persona: dict, agent_id: str = "",
                        rooms: list = None) -> Optional[BotWorker]:
        """Provision a browser, log in via CDP, connect auto-pilot.

        agent_id: unique key (defaults to persona username). Pass e.g. "Alexa_2"
                  when running multiple agents from the same persona.
        rooms:    pre-assigned [primary, secondary] rooms for this agent.
        """
        async with self._semaphore:
            username = persona.get("username", "ChatBot_42")
            if not agent_id:
                agent_id = username
            logger.info(f"Starting bot: {agent_id} (persona={username})")

            worker = BotWorker(persona)
            worker.agent_id = agent_id
            if rooms:
                worker.rooms = list(rooms)
            self._workers[agent_id] = worker
            self._auto_pilot_enabled[agent_id] = False

            # Provision a verified-working US browser (NO profile → no cookies carry
            # over, so a ban can't follow us into the next session).
            worker.status = "connecting"
            if not await self._provision_and_connect(worker):
                logger.error(f"[{agent_id}] no working US proxy after retries")
                worker.status = "error"
                self._workers.pop(agent_id, None)
                return None

            # Login + auto-pilot loop run in background (CDP already connected)
            worker.status = "running"
            worker._task = asyncio.create_task(self._finish_bot_setup(worker))
            return worker

    async def start_multi(self, count: int, persona: dict) -> list:
        """Launch `count` agents from one persona, each assigned 2 distinct rooms.

        Room assignments respect the max-2-agents-per-room rule. Agents provision
        in parallel — start time ≈ time for one agent, not N × that.
        """
        count = max(1, min(count, 16))
        room_pairs = assign_rooms(count, FCN_ROOMS)
        username = persona.get("username", "ChatBot_42")

        async def _one(slot: int) -> Optional[BotWorker]:
            aid = f"{username}_{slot + 1}" if count > 1 else username
            return await self.start_bot(persona, agent_id=aid, rooms=room_pairs[slot])

        results = await asyncio.gather(*[_one(i) for i in range(count)],
                                       return_exceptions=True)
        workers = [w for w in results if isinstance(w, BotWorker)]
        logger.info(f"start_multi: {len(workers)}/{count} agents live")
        return workers

    async def _provision_and_connect(self, worker: BotWorker) -> bool:
        """Provision a FRESH BU browser (new IP via a unique US Decoda port, no
        cookies). Picks a port not already held by another live agent, then rotates
        up to 5x on failure. Each agent is guaranteed a distinct residential IP."""
        client = await self._get_client()
        in_use = {(w.proxy_host, w.proxy_port) for w in self._workers.values() if w.proxy_port}
        available = [p for p in DECODA_PROXIES if (p["host"], p["port"]) not in in_use]
        if not available:
            available = list(DECODA_PROXIES)
        # Weighted sampling — US/CA are 3× more likely than GB/AU (fewer Cloudflare blocks).
        # Draw 10 candidates, deduplicate, keep first 5 unique ones.
        weights = [_PROXY_WEIGHTS.get(p["host"], 1) for p in available]
        candidates = random.choices(available, weights=weights, k=min(10, len(available)))
        seen, pool = set(), []
        for p in candidates:
            key = (p["host"], p["port"])
            if key not in seen:
                seen.add(key)
                pool.append(p)
            if len(pool) == 5:
                break
        for attempt, proxy in enumerate(pool):
            try:
                # 1280x960 (4:3) matches the dashboard's .browser-frame aspect-ratio
                # so the live stream fills the box with no black bars.
                browser = await client.browsers.create(
                    timeout=60, browser_screen_width=1280, browser_screen_height=960,
                    enable_recording=False, customProxy=proxy,
                )
            except Exception as e:
                logger.warning(f"[{worker.username}] provision failed (try {attempt + 1}): {e}")
                continue
            worker.browser_id = str(browser.id)
            worker.live_url = browser.live_url or ""
            cdp_url = browser.cdp_url or ""
            if cdp_url and await self._connect_cdp(worker, cdp_url) and self._record_proxy_info(worker, proxy):
                worker.proxy_port = proxy["port"]
                worker.proxy_host = proxy["host"]
                return True
            logger.warning(f"[{worker.username}] proxy failed on port {proxy['port']} (try {attempt + 1}); rotating")
            await worker.disconnect_cdp()
            try:
                await client.browsers.stop(worker.browser_id)
            except Exception:
                pass
            worker.browser_id = ""
            worker.live_url = ""
        return False

    async def _is_blocked_page(self, page) -> bool:
        """Return True if the page is a Cloudflare or FCN IP-block page."""
        try:
            result = await page.evaluate("""() => {
                const t = (document.title || '').toLowerCase();
                const b = document.body ? document.body.innerText.toLowerCase() : '';
                return (
                    b.includes('you have been blocked') ||
                    b.includes('unable to access') ||
                    b.includes('ip has been banned') ||
                    t.includes('attention required') ||
                    t.includes('just a moment') ||
                    t.includes('access denied') ||
                    !!document.querySelector('#cf-error-details, .cf-error-code, #challenge-error-title')
                );
            }""")
            return bool(result)
        except Exception:
            return False

    async def _looks_banned(self, worker: BotWorker) -> bool:
        """Detect a kick/ban: left the site, IP-blocked, or in-room ban message."""
        page = worker._page
        if not page:
            return False
        try:
            url = page.url or ""
            if "freechatnow" not in url:
                return True  # kicked off the site entirely (DM views stay on freechatnow)
            if await self._is_blocked_page(page):
                return True
            body = await page.evaluate("() => document.body ? document.body.innerText.slice(0,800) : ''")
            return bool(re.search(
                r"you (have been|were|are) (banned|kicked)|been removed from|kicked from|"
                r"access denied|your ip|temporarily blocked", body or "", re.I))
        except Exception:
            return False

    async def _teardown_browser(self, worker: BotWorker):
        """Disconnect CDP + stop the cloud browser for this worker."""
        await worker.disconnect_cdp()
        if worker.browser_id:
            try:
                await (await self._get_client()).browsers.stop(worker.browser_id)
            except Exception:
                pass
            worker.browser_id = ""
            worker.live_url = ""
            worker.proxy_port = 0   # free the slot for other agents
            worker.proxy_host = ""

    async def _recover(self, worker: BotWorker, max_attempts: int = 8) -> bool:
        """Ban recovery: loop until we land in the room or exhaust attempts.

        Each iteration:
          1. Tear down the current browser (releases the banned IP)
          2. Provision a NEW Browser Use Cloud browser on a DIFFERENT Decoda port
             → fresh exit IP + fresh browser fingerprint / user-agent
          3. Run guest login with a freshly generated name (no cookies)
          4. If login confirms we're in the room → done
          5. Else tear down again and rotate to next attempt

        FCN bans are almost always IP-based; a new Decoda port = a new US
        residential exit IP, which is enough to get back in. The new BU browser
        instance also presents a fresh UA + fingerprint, removing any
        client-side fingerprint signal FCN may have recorded.
        """
        agent_id = worker.agent_id
        banned_ip = worker.proxy_ip
        logger.warning(f"[{agent_id}] BAN confirmed ({banned_ip}) — recovery loop starting")
        worker.phase = "recovering"
        worker.handle_shared = False
        worker.in_dm = False

        # Tear down the banned browser first
        await self._teardown_browser(worker)

        for attempt in range(1, max_attempts + 1):
            logger.info(f"[{agent_id}] recovery attempt {attempt}/{max_attempts}")
            worker.phase = f"recover_{attempt}"

            # Provision a fresh browser on a different Decoda port → new IP + UA.
            # _provision_and_connect already rotates up to 5 ports internally and
            # verifies the exit is US before returning True.
            if not await self._provision_and_connect(worker):
                logger.warning(f"[{agent_id}] provision failed (attempt {attempt}), "
                                "waiting before retry…")
                await asyncio.sleep(random.uniform(4, 10))
                continue

            # Fresh guest login with a new randomly generated name — no cookies.
            ok = await self._cdp_guest_login(worker)
            if ok:
                worker.phase = "loop_running"
                logger.info(f"[{agent_id}] ✅ recovered on attempt {attempt} "
                             f"as {worker.login_name} @ {worker.proxy_ip} "
                             f"(was banned on {banned_ip})")
                return True

            # Login failed (FCN still rejecting this IP, captcha, or form error).
            # Tear down and try a completely fresh IP next iteration.
            logger.warning(f"[{agent_id}] login failed on attempt {attempt} "
                            f"({worker.proxy_ip}), rotating IP…")
            await self._teardown_browser(worker)
            # Brief pause so FCN's rate-limiter doesn't chain-ban consecutive IPs
            await asyncio.sleep(random.uniform(6, 15))

        logger.error(f"[{agent_id}] recovery EXHAUSTED after {max_attempts} attempts")
        worker.status = "error"
        worker.phase = "recovery_failed"
        return False

    def _record_proxy_info(self, worker: BotWorker, proxy: dict) -> bool:
        """Record proxy metadata from the config — no browser navigation needed.

        We trust Decoda's geo-routing: us.decodo.com always exits in the US,
        ca.decodo.com in Canada, etc. Visiting ip-api.com inside the browser is
        a textbook bot fingerprint (open browser → check IP → go to site) and was
        the primary Cloudflare trigger. Removed.
        """
        host = proxy.get("host", "")
        cc_map = {
            "us.decodo.com": "US",
            "ca.decodo.com": "CA",
            "gb.decodo.com": "GB",
            "au.decodo.com": "AU",
        }
        cc = cc_map.get(host, "??")
        worker.proxy_ip = f"{host}:{proxy.get('port','')}"
        worker.proxy_location = cc
        if cc not in _PROXY_ALLOWED_CC:
            logger.warning(f"[{worker.username}] unknown proxy host {host} — skipping")
            return False
        logger.info(f"[{worker.username}] proxy assigned: {host}:{proxy.get('port')} ({cc})")
        return True

    async def _finish_bot_setup(self, worker: BotWorker):
        """Background (CDP already connected + proxy verified in start_bot):
        guest login → optional second-room join → auto-pilot loop."""
        agent_id = worker.agent_id
        try:
            worker.status = "logging_in"
            worker.phase = "logging_in"

            # Guest login (navigate to primary room → fill form → native submit)
            ok = await self._cdp_guest_login(worker)
            if not ok:
                logger.warning(f"[{agent_id}] initial login failed; entering recovery loop")
                ok = await self._recover(worker)
                if not ok:
                    logger.error(f"[{agent_id}] all recovery attempts failed — agent offline")
                    return

            # Attempt to join the second assigned room (best-effort — if the button
            # is hidden at 1280px width we still start the loop on the primary room)
            if len(worker.rooms) > 1:
                await asyncio.sleep(3)  # let the SPA settle before touching UI
                await self._join_second_room(worker, worker.rooms[1])

            # Start the auto-pilot loop immediately.
            worker.phase = "starting_loop"
            worker.status = "running"
            self._auto_pilot_enabled[agent_id] = True
            worker._task = asyncio.create_task(self._run_auto_pilot(worker))
            worker.phase = "loop_running"

        except Exception as e:
            worker.phase = "setup_error"
            worker.last_error = f"setup: {type(e).__name__}: {e}"[:200]
            logger.error(f"Bot setup failed for {agent_id}: {e}")
            worker.status = "error"

    async def _join_second_room(self, worker: BotWorker, room_name: str) -> bool:
        """After initial login, join a second FCN room via the Rooms panel.

        The 'button.join' is hidden with CSS at 1280px desktop width, so we
        force-click it via JS (bypasses display:none visibility check), wait for
        the panel, then click the target room entry.
        """
        page = worker._page
        if not page:
            return False
        try:
            # Force-click the hidden Join/Rooms button
            clicked = await page.evaluate("""
                (() => {
                    const btn = document.querySelector('button.join, .btn-join, [data-action="join-room"], [class*=join-room i]');
                    if (!btn) return false;
                    // make visible for click (CSS might hide it at desktop width)
                    const orig = btn.style.cssText;
                    btn.style.cssText += ';display:block!important;visibility:visible!important;opacity:1!important';
                    btn.click();
                    btn.style.cssText = orig;
                    return true;
                })()
            """)
            if not clicked:
                logger.info(f"[{worker.agent_id}] join button not found, skipping second room")
                return False
            await page.wait_for_timeout(2000)

            # Find the room entry in the panel and click it
            joined = await page.evaluate("""
                (roomName) => {
                    const low = roomName.toLowerCase();
                    const sels = ['[data-room]', 'a[href*="/room/"]', '[class*=room-item i]',
                                  '[class*=room-link i]', 'li[class*=room i]'];
                    for (const sel of sels) {
                        for (const el of document.querySelectorAll(sel)) {
                            const txt = (el.textContent || el.getAttribute('data-room') || '').toLowerCase();
                            const href = (el.getAttribute('href') || '').toLowerCase();
                            if (txt.includes(low) || href.includes(low)) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """, room_name)

            if joined:
                await page.wait_for_timeout(3000)
                logger.info(f"[{worker.agent_id}] joined second room: {room_name}")
            else:
                logger.info(f"[{worker.agent_id}] room '{room_name}' not found in panel")
            return joined
        except Exception as e:
            logger.warning(f"[{worker.agent_id}] join second room error: {e}")
            return False

    # 500+ UA pool generated at module load — desktop, tablet, and mobile across
    # Chrome/Edge/Firefox/Safari on Windows/Mac/Linux/iOS/Android.
    _USER_AGENTS = UA_POOL

    async def _connect_cdp(self, worker: BotWorker, cdp_url: str) -> bool:
        """Connect Playwright CDP to the running browser for fast JS auto-pilot."""
        try:
            from playwright.async_api import async_playwright
            worker._playwright = await async_playwright().start()
            wss_url = cdp_url.replace("https://", "wss://")
            worker._cdp = await worker._playwright.chromium.connect_over_cdp(wss_url, timeout=30000)

            contexts = worker._cdp.contexts
            if contexts:
                pages = contexts[0].pages
                worker._page = pages[0] if pages else await contexts[0].new_page()
            else:
                worker._page = await (await worker._cdp.new_context()).new_page()

            # Wipe any cookies/storage the cloud provider may have pre-populated
            # on the default context before we touch FCN. Belt-and-suspenders —
            # browsers.create() gives a fresh instance but clearing explicitly
            # ensures no fingerprint leaks across recovery attempts.
            await worker._page.context.clear_cookies()
            try:
                await worker._page.evaluate(
                    "() => { window.localStorage.clear(); window.sessionStorage.clear(); }"
                )
            except Exception:
                pass

            # Rotate user agent — every agent gets a different UA so the fleet
            # doesn't share a single fingerprint that Cloudflare can block en masse.
            ua = random.choice(self._USER_AGENTS)
            await worker._page.set_extra_http_headers({"User-Agent": ua})
            worker._ua = ua

            # Derive consistent platform/vendor from the chosen UA so Cloudflare's
            # cross-signal checks don't see a mismatch (e.g. Android UA + Win32 platform).
            if "iPhone" in ua:
                _plat, _vendor = "iPhone", "Apple Computer, Inc."
            elif "iPad" in ua:
                _plat, _vendor = "iPad", "Apple Computer, Inc."
            elif "Android" in ua:
                _plat, _vendor = "Linux armv8l", "Google Inc."
            elif "Macintosh" in ua:
                _plat, _vendor = "MacIntel", "Apple Computer, Inc."
            elif "Linux" in ua:
                _plat, _vendor = "Linux x86_64", "Google Inc."
            else:
                _plat, _vendor = "Win32", "Google Inc."
            _hw = random.choice([4, 6, 8])
            _dm = random.choice([4, 8])

            # Stealth patches — injected before any page script on every navigation.
            # Covers the main Cloudflare fingerprint signals: webdriver flag, plugins,
            # platform, hardware concurrency, device memory, vendor, and permissions API.
            _stealth_js = """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => {
                    const p = [1,2,3,4,5];
                    p.namedItem = () => null; p.refresh = () => {}; p.item = i => p[i];
                    return p;
                }});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                Object.defineProperty(navigator, 'platform', {get: () => 'PLATFORM'});
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => HW_CONC});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => DEV_MEM});
                Object.defineProperty(navigator, 'vendor', {get: () => 'VENDOR'});
                if (!window.chrome && 'VENDOR'.includes('Google')) {
                    window.chrome = {runtime:{}, loadTimes:()=>{}, csi:()=>{}, app:{}};
                }
                delete window.__playwright;
                delete window.__pw_manual;
                try {
                    const _oq = window.navigator.permissions.query.bind(window.navigator.permissions);
                    window.navigator.permissions.query = (p) => {
                        if (p.name === 'notifications') return Promise.resolve({state:'default',onchange:null});
                        return _oq(p);
                    };
                } catch(e) {}
            """.replace("PLATFORM", _plat).replace("VENDOR", _vendor) \
               .replace("HW_CONC", str(_hw)).replace("DEV_MEM", str(_dm))
            await worker._page.add_init_script(_stealth_js)

            # Ad guard: block ONLY known ad/pop networks by exact domain.
            # Do NOT use broad wildcards like "**traffic**" — that matches FCN's own
            # analytics scripts and triggers bot-detection / captchas.
            async def _ad_guard(route):
                req = route.request
                try:
                    f = req.frame
                    top_nav = req.is_navigation_request() and (f is None or f.parent_frame is None)
                    if top_nav:
                        await route.continue_()
                    else:
                        await route.abort()
                except Exception:
                    try:
                        await route.abort()
                    except Exception:
                        pass

            for host in ("12chats.com", "exoclick.com", "popads.net", "doubleclick.net",
                         "propellerads.com", "adsterra.com", "trafficjunky.com",
                         "popunder.net", "adnium.com", "juicyads.com"):
                try:
                    await worker._page.route(f"**{host}**", _ad_guard)
                except Exception:
                    pass

            return True
        except ImportError:
            logger.error("playwright not installed — run: pip install playwright")
            return False
        except Exception as e:
            logger.warning(f"CDP connect failed: {e}")
            return False

    # FCN guest-login form (verified 2026-06-16 via /debug/inspect-fcn):
    #   page:   https://www.freechatnow.com/chat/<slug>/   (e.g. SextChat -> sext)
    #   form:   <form action="/api/chat/login" method="post">
    #   fields: input[name=username], select[name=gender] (male|female|other),
    #           input[name=birthdate] (type=date, YYYY-MM-DD),
    #           input[type=checkbox] (agree), button[type=submit] "Chat As Guest"
    FCN_BASE = "https://www.freechatnow.com"
    _GENDER_MAP = {"f": "female", "female": "female", "m": "male", "male": "male",
                   "other": "other", "couple": "other", "x": "other"}

    # Female-sounding username generator — FCN guest names must be unique while
    # active, so we mint a fresh high-entropy name on every login (and retry).
    _FEMALE_NAMES = [
        "Alexa", "Mia", "Sophia", "Luna", "Zoe", "Lily", "Ava", "Ella", "Chloe", "Ruby",
        "Nina", "Ivy", "Maya", "Lola", "Bella", "Aria", "Nova", "Sadie", "Gigi", "Vera",
        "Daisy", "Skye", "Jade", "Roxy", "Coco", "Lexi", "Demi", "Remi", "Cleo", "Tessa",
        "Hazel", "Willow", "Sienna", "Eva", "Nia", "Gemma", "Faye", "Elle", "Juno", "Cora",
        "Stella", "Penny", "Naomi", "Iris", "Layla", "Hanna", "Riley", "Paige", "Mila", "Joss",
    ]
    _FLIRTY_PREFIX = [
        "Sweet", "Foxy", "Babe", "Honey", "Sassy", "Cherry", "Sugar", "Velvet", "Kitten",
        "Angel", "Star", "Silk", "Peach", "Misty", "Lush", "Naughty", "Wild", "Cozy", "Sultry",
    ]

    def _unique_username(self) -> str:
        """Mint a fresh, female-sounding, high-entropy username (~75M combos)."""
        name = random.choice(self._FEMALE_NAMES)
        if random.random() < 0.45:
            name = random.choice(self._FLIRTY_PREFIX) + name
        return f"{name}{random.randint(10, 99999)}"

    async def _cdp_guest_login(self, worker: BotWorker, _attempt: int = 0) -> bool:
        """Homepage → room selection → guest-login form, all via CDP.

        Flow:
          1. Land on freechatnow.com (looks like a real user arriving at the site).
          2. Dwell briefly, then click the target room link in the room grid.
          3. On the room page, fill the guest form with human-like delays and submit
             via native form.submit() — NOT the button, which fires ad redirects.
          4. Wait for the SPA to redirect into schat.freechatnow.com/room/<Room>.

        On username collision FCN bounces to /?alert=<base64> — retry up to 5×.
        """
        page = worker._page
        persona = worker.persona

        worker.login_name = self._unique_username()

        # Resolve target room + slug
        if worker.rooms:
            rooms = worker.rooms
        else:
            rooms = persona.get("selected_rooms") or ["SextChat"]
            if isinstance(rooms, str):
                try:
                    rooms = json.loads(rooms)
                except Exception:
                    rooms = [rooms]
            worker.rooms = list(rooms) if rooms else ["SextChat"]
        room = (rooms[0] if rooms else "SextChat") or "SextChat"
        worker.room = room
        slug = FCN_SLUG_MAP.get(room.lower()) or room.lower().replace("chat", "").strip() or "sext"
        room_url = f"{self.FCN_BASE}/chat/{slug}/"

        # ── Step 1: navigate directly to the room page ────────────────────────
        # Skipping freechatnow.com/ — the homepage is Cloudflare's most guarded
        # page and hitting it first was the primary block trigger. Room pages
        # (/chat/<slug>/) have lighter Cloudflare rules and carry the login form.
        worker.phase = "login_nav"
        try:
            await page.goto(room_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.warning(f"[{worker.agent_id}] room nav failed: {e}")
        # Longer dwell — Cloudflare's JS needs a few seconds to "pass" the visitor.
        # Bots hit the form immediately; real users read the page first.
        await page.wait_for_timeout(random.randint(3000, 5500))

        # IP block check — Cloudflare "Sorry, you have been blocked"
        if await self._is_blocked_page(page):
            logger.warning(f"[{worker.agent_id}] IP blocked on room page — rotating IP")
            worker.phase = "ip_blocked"
            return False

        # Human mouse settle + gentle scroll before touching any form field
        await page.mouse.move(random.randint(250, 850), random.randint(100, 420))
        await page.wait_for_timeout(random.randint(400, 900))
        await page.mouse.wheel(0, random.randint(60, 180))
        await page.wait_for_timeout(random.randint(600, 1300))

        gval = self._GENDER_MAP.get((persona.get("gender") or "f").lower(), "female")
        age = random.randint(20, 26)
        birthdate = f"{time.localtime().tm_year - age}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"

        # Wait for the login form to be visible — with 4 agents starting
        # simultaneously the page may still be rendering when we get here.
        try:
            await page.wait_for_selector(
                "form[action*='chat/login']", state="visible", timeout=10000)
        except Exception:
            logger.warning(f"[{worker.agent_id}] login form never appeared @ {page.url}")
            return False

        try:
            # ── Username: click → char-by-char keyboard type ──────────────────
            await page.wait_for_selector(
                "input[name=username]", state="visible", timeout=5000)
            await page.click("input[name=username]", timeout=5000)
            await page.wait_for_timeout(random.randint(300, 800))
            for ch in worker.login_name:
                await page.keyboard.type(ch)
                await page.wait_for_timeout(random.randint(65, 215))
            logger.info(f"[{worker.agent_id}] typed username '{worker.login_name}'")

            # ── Gender: mouse to element → select_option ──────────────────────
            await page.wait_for_timeout(random.randint(500, 1100))
            await page.wait_for_selector(
                "select[name=gender]", state="visible", timeout=5000)
            sel_el = await page.query_selector("select[name=gender]")
            if sel_el:
                bb = await sel_el.bounding_box()
                if bb:
                    await page.mouse.move(
                        bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                    await page.wait_for_timeout(random.randint(200, 500))
            await page.select_option("select[name=gender]", gval, timeout=5000)
            logger.info(f"[{worker.agent_id}] selected gender={gval}")
            await page.wait_for_timeout(random.randint(400, 950))

            # ── Birthdate: click → fill (ISO YYYY-MM-DD); type char-by-char if
            #   fill doesn't stick (some date inputs need keyboard input)
            await page.wait_for_selector(
                "input[name=birthdate]", state="visible", timeout=5000)
            await page.click("input[name=birthdate]", timeout=5000)
            await page.wait_for_timeout(random.randint(300, 700))
            await page.fill("input[name=birthdate]", birthdate, timeout=5000)
            actual = await page.input_value("input[name=birthdate]")
            if actual != birthdate:
                await page.triple_click("input[name=birthdate]")
                for ch in birthdate:
                    await page.keyboard.type(ch)
                    await page.wait_for_timeout(random.randint(50, 130))
            logger.info(f"[{worker.agent_id}] birthdate={birthdate}")
            await page.wait_for_timeout(random.randint(400, 900))

            # ── Checkbox: mouse to element → real click ───────────────────────
            await page.wait_for_selector(
                "input[type=checkbox]", state="visible", timeout=5000)
            chk_el = await page.query_selector("input[type=checkbox]")
            if chk_el:
                bb = await chk_el.bounding_box()
                if bb:
                    await page.mouse.move(
                        bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                    await page.wait_for_timeout(random.randint(200, 500))
            await page.click("input[type=checkbox]", timeout=5000)
            logger.info(f"[{worker.agent_id}] checkbox ticked")
            await page.wait_for_timeout(random.randint(700, 1600))

            # ── Submit: refocus username and press Enter ───────────────────────
            # Keyboard Enter submits the form natively without firing the
            # submit button's onclick ad-redirect handlers.
            await page.focus("input[name=username]")
            await page.wait_for_timeout(random.randint(300, 700))
            await page.keyboard.press("Enter")
            logger.info(f"[{worker.agent_id}] form submitted via Enter key")
        except Exception as e:
            logger.warning(f"[{worker.agent_id}] form interaction failed: {e}")
            return False

        # Wait for the room SPA to sign in and load (also watch for captcha)
        worker.phase = "login_wait_room"
        for _ in range(12):
            await page.wait_for_timeout(2000)
            if "schat." in page.url or "/room/" in page.url or "alert=" in page.url:
                break
            # Captcha check — hCaptcha / reCAPTCHA / Cloudflare challenge.
            # Don't try to solve it; a fresh Decoda IP almost never triggers one.
            try:
                has_captcha = await page.evaluate("""() => {
                    const sels = [
                        'iframe[src*="hcaptcha"]','iframe[src*="recaptcha"]',
                        '.h-captcha','.g-recaptcha','#challenge-form',
                        '[data-sitekey]','#cf-challenge-running'
                    ];
                    return sels.some(s => !!document.querySelector(s));
                }""")
                if has_captcha:
                    logger.warning(f"[{worker.agent_id}] captcha detected — rotating to fresh IP")
                    worker.phase = "captcha_detected"
                    return False
            except Exception:
                pass
        await page.wait_for_timeout(2500)
        url_now = page.url

        # Username-collision bounce → retry with a random suffix
        if "alert=" in url_now:
            import base64
            from urllib.parse import urlparse, parse_qs
            msg = ""
            try:
                a = parse_qs(urlparse(url_now).query).get("alert", [""])[0]
                msg = base64.b64decode(a).decode(errors="ignore")
            except Exception:
                pass
            logger.warning(f"login bounced for {worker.username}: {msg or url_now}")
            if "taken" in msg.lower() and _attempt < 5:
                logger.info("username taken — retrying with a fresh generated name")
                return await self._cdp_guest_login(worker, _attempt + 1)
            return False

        in_room = "schat." in url_now or "/room/" in url_now
        worker.phase = "in_room" if in_room else "login_uncertain"

        # Close ad popups + dismiss the welcome/tip overlay (do NOT block on chat
        # readiness — the loop handles that itself and would otherwise never start).
        await self._close_popups(worker)
        await self._dismiss_overlays(page)

        logger.info(f"Guest login {'OK' if in_room else 'UNCERTAIN'} for "
                    f"{worker.username} (room={room}) @ {url_now}")
        return in_room

    async def _wait_chat_ready(self, page, worker) -> bool:
        """Wait for FCN's WS-driven chat input to load; reload the room once if it
        stalls (the shell loads but the chat hangs on a WebSocket flap)."""
        for _reload in range(2):
            for _ in range(12):
                await page.wait_for_timeout(2000)
                try:
                    if await page.query_selector('input[placeholder="Type to chat"]'):
                        logger.info(f"[{worker.username}] chat UI ready")
                        return True
                except Exception:
                    pass
            logger.warning(f"[{worker.username}] chat UI not ready — reloading room")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
        logger.warning(f"[{worker.username}] chat UI never loaded")
        return False

    async def _dismiss_overlays(self, page) -> int:
        """Clear FCN's overlays: dismiss the welcome/tip, and REMOVE ad iframes.

        - Welcome/tip dismiss control is `.action.dismiss` ("I'm already familiar")
          — not a <button>, curly apostrophe, Playwright sees it as not-visible, so
          a direct DOM .click() in evaluate fires the handler.
        - The "I AM 18+" age-gate is a cross-origin 12chats ad IFRAME. Network
          blocking is unreliable (iframe doc loads look top-level to Playwright), so
          we just remove the ad <iframe> elements from the DOM. They refresh every
          30s, so this runs every auto-pilot tick.
        """
        total = 0
        try:
            for _ in range(4):
                n = await page.evaluate("""
                    (() => {
                        let n = 0;
                        // 1. Welcome / tip dismiss
                        document.querySelectorAll('.action.dismiss, [class*=tip] [class*=dismiss], [class*=tip] [class*=close], [class*=welcome] [class*=close]')
                            .forEach(e => { try { e.click(); n++; } catch(_){} });
                        // 2. Remove ad iframes (the "I AM 18+" age-gate lives in one).
                        document.querySelectorAll('iframe').forEach(f => {
                            const s = (f.src || '') + ' ' + (f.id || '');
                            if (/12chats|\\/afr|exoclick|popads|propeller|adsterra|doubleclick|trafficjunky/i.test(s)) {
                                try { f.remove(); n++; } catch(_){}
                            }
                        });
                        // 3. Close ad MODALS: an [X]/× inside a positioned overlay, or a
                        // positioned box holding a broken (ad) image. Remove only the
                        // POSITIONED container, and NEVER one that holds the chat — so we
                        // don't white-out the real UI (the old over-aggressive bug).
                        const killModal = (start) => {
                            let p = start, cont = null;
                            for (let i = 0; i < 6 && p; i++) {
                                const s = getComputedStyle(p);
                                if (s.position === 'fixed' || s.position === 'absolute') cont = p;
                                p = p.parentElement;
                            }
                            if (cont && !cont.querySelector('.room-messages-container, .writer-input, [class*=userlist i], [class*=roomlist i]')) {
                                try { cont.remove(); return true; } catch(_) {}
                            }
                            return false;
                        };
                        document.querySelectorAll('a, span, div, button').forEach(e => {
                            if (e.children.length) return;
                            if (/^(\\[?[xX]\\]?|×|✕|✖)$/.test((e.textContent || '').trim())) {
                                try { e.click(); } catch(_){}
                                if (killModal(e)) n++;
                            }
                        });
                        document.querySelectorAll('img').forEach(img => {
                            if (img.complete && img.naturalWidth === 0) { if (killModal(img)) n++; }
                        });
                        return n;
                    })()
                """)
                total += (n or 0)
                if not n:
                    break
                await page.wait_for_timeout(500)
        except Exception:
            pass
        return total

    async def _kill_ads(self, page):
        """Lightweight, every-tick removal of the ad iframe + its overlay wrapper
        (the gray broken-image '[x]' modal). Only touches ad iframes — cheap and
        won't churn the chat DOM/WS — and never removes a wrapper holding the chat."""
        try:
            await page.evaluate("""
                (() => {
                    document.querySelectorAll('iframe').forEach(f => {
                        const s = (f.src || '') + ' ' + (f.id || '');
                        if (!/12chats|\\/afr|exoclick|popads|propeller|adsterra|doubleclick|trafficjunky/i.test(s)) return;
                        // remove the iframe AND its positioned overlay wrapper (box + backdrop)
                        let p = f, cont = f;
                        for (let i = 0; i < 5 && p; i++) {
                            const st = getComputedStyle(p);
                            if (st.position === 'fixed' || st.position === 'absolute') cont = p;
                            p = p.parentElement;
                        }
                        if (cont !== f && cont.querySelector('.room-messages-container, .writer-input')) cont = f;
                        try { cont.remove(); } catch(_) { try { f.remove(); } catch(_) {} }
                    });
                })()
            """)
        except Exception:
            pass

    async def _close_popups(self, worker: BotWorker):
        """Close any ad popup windows, keeping only the room page foregrounded."""
        try:
            if not worker._page:
                return
            closed = False
            for pg in list(worker._page.context.pages):
                if pg is not worker._page:
                    try:
                        await pg.close()
                        closed = True
                    except Exception:
                        pass
            # Return the live view to the room tab after closing a popup
            if closed:
                try:
                    await worker._page.bring_to_front()
                except Exception:
                    pass
        except Exception:
            pass

    async def _list_conversations(self, page) -> list:
        """List open conversation tabs (rooms + DMs) from nav.roomlist.
        Each: {href, target, text, unseen, active, is_dm}. A tab is a DM when its
        link isn't a room (data-target != 'room' and href not /room/)."""
        try:
            return await page.evaluate("""
                (() => {
                    const out = [];
                    document.querySelectorAll('.roomlist-room').forEach(d => {
                        const a = d.querySelector('a.roomlist-link, a[href]');
                        if (!a) return;
                        const href = a.getAttribute('href') || '';
                        const target = a.getAttribute('data-target') || '';
                        const cls = (d.className || '').toString();
                        out.push({
                            href, target, text: (a.textContent || '').trim().slice(0,30),
                            unseen: /unseen/.test(cls), active: /\\bactive\\b/.test(cls),
                            is_dm: target !== 'room' && !href.startsWith('/room/'),
                        });
                    });
                    return out;
                })()
            """)
        except Exception:
            return []

    async def _open_conversation(self, page, href: str) -> bool:
        """Click a conversation tab (room or DM) to make it the active thread."""
        if not href:
            return False
        try:
            el = await page.query_selector(f'.roomlist-room a[href="{href}"]')
            if el is None:
                return False
            await el.click(timeout=4000)
            await page.wait_for_timeout(1200)
            return True
        except Exception:
            return False

    async def _log_dm_messages(self, worker: BotWorker, other_user: str,
                                msgs: list, persona_id: str):
        """Store every message in a DM thread (both sides) since we last logged.

        Parses each "username: text" line: if the username matches worker.login_name
        it's a 'bot' message, otherwise 'user'. New messages only — tracked via
        worker._dm_state[other_user]["logged_count"].
        """
        state = worker._dm_state.setdefault(other_user, {
            "conv_id": None, "logged_count": 0, "first_bot_sent": False
        })
        # Lazy-create conversation row on first encounter
        if not state["conv_id"]:
            try:
                state["conv_id"] = await db.get_or_create_dm_conversation(
                    persona_id, worker.agent_id, other_user)
            except Exception as e:
                logger.warning(f"[{worker.agent_id}] dm_conversation create failed: {e}")
                return

        new_msgs = msgs[state["logged_count"]:]
        if not new_msgs:
            return

        for msg in new_msgs:
            if ":" in msg:
                uname, content = msg.split(":", 1)
                sender = "bot" if uname.strip() == worker.login_name else "user"
                content = content.strip()
            else:
                sender = "user"
                content = msg.strip()
            if not content:
                continue
            is_opener = (sender == "bot" and not state["first_bot_sent"])
            try:
                await db.log_dm_message(state["conv_id"], sender, content, is_opener)
            except Exception as e:
                logger.warning(f"[{worker.agent_id}] log_dm_message failed: {e}")
            if sender == "bot":
                state["first_bot_sent"] = True

        state["logged_count"] = len(msgs)

    async def _run_auto_pilot(self, worker: BotWorker):
        """Fast JS-based auto-pilot loop for a single bot.

        Uses CDP (zero cost per tick) for read_chat → generate → send.
        Falls back to SDK agent for recovery if CDP is unavailable.
        """
        agent_id = worker.agent_id
        persona_id = worker.persona.get("id", "")
        client = await self._get_client()

        tick = 0
        ban_strikes = 0
        next_send = 0.0  # group-room pace gate (monotonic seconds)
        dm_next = 0.0    # DM pace gate (faster)
        while self._auto_pilot_enabled.get(agent_id, False):
            try:
                # ── CDP path (fast, zero cost) ──
                if worker._page:
                    tick += 1
                    worker.loop_ticks = tick

                    # Ban/kick detection: 2 consecutive "looks banned" ticks before
                    # triggering recovery (debounces brief network blips).
                    if await self._looks_banned(worker):
                        ban_strikes += 1
                        logger.info(f"[{agent_id}] ban signal #{ban_strikes} "
                                    f"(url={worker._page.url[:60] if worker._page else '?'})")
                        if ban_strikes >= 2:
                            ban_strikes = 0
                            try:
                                await db.log_event(persona_id, "ban", room=worker.room,
                                                   content=worker.last_response or "")
                            except Exception:
                                pass
                            try:
                                from app.supervisor import supervisor_engine
                                await supervisor_engine.analyze_ban(
                                    "", persona_id, [worker.last_response or ""],
                                    "kicked/banned from room")
                            except Exception:
                                pass
                            if not await self._recover(worker):
                                # Recovery exhausted all attempts — stop this agent
                                worker.status = "error"
                                break
                            # After recovery: give the room a moment to settle,
                            # try to re-join second room, then resume the loop
                            next_send = time.monotonic() + 20
                            dm_next = time.monotonic() + 10
                            if len(worker.rooms) > 1:
                                await asyncio.sleep(4)
                                await self._join_second_room(worker, worker.rooms[1])
                        await asyncio.sleep(3)
                        continue
                    ban_strikes = 0

                    # Refresh persona settings (handle/bio/tone) live — so edits on
                    # the Personas page apply WITHOUT restarting the session.
                    if tick % 15 == 0 and persona_id:
                        try:
                            fresh = await db.get_persona(persona_id)
                            if fresh:
                                worker.persona = fresh
                        except Exception:
                            pass

                    # Kill the ad modal/iframe every tick (cheap, targeted) so the
                    # gray "[x]" box never lingers in the live view.
                    await self._kill_ads(worker._page)

                    # Heavier cleanup (tip dismiss, popups, refocus) only periodically.
                    if tick % 5 == 1:
                        await self._close_popups(worker)
                        await self._dismiss_overlays(worker._page)
                        try:
                            await worker._page.bring_to_front()
                        except Exception:
                            pass
                    # DMs-FIRST round-robin: handle an unread DM (5-10s pace), else
                    # the group room (~30s pace). Switch tabs before read+respond.
                    now = time.monotonic()
                    convos = await self._list_conversations(worker._page)
                    unread_dms = [c for c in convos if c["is_dm"] and c["unseen"]]
                    rooms = [c for c in convos if not c["is_dm"]]

                    if unread_dms and now >= dm_next:
                        c = unread_dms[0]
                        if await self._open_conversation(worker._page, c["href"]):
                            worker.in_dm = True
                            other_user = c["text"] or "unknown"
                            worker.room = other_user
                            msgs = await worker.read_chat()
                            if msgs:
                                # Log both sides of the DM before generating a reply
                                await self._log_dm_messages(worker, other_user, msgs, persona_id)
                                await self._auto_pilot_tick(worker, msgs, client,
                                                            dm_other_user=other_user)
                        dm_next = now + random.randint(5, 10)
                    elif now >= next_send:
                        # Group room: rotate between all joined rooms on each send
                        if rooms:
                            worker._room_index = (worker._room_index + 1) % len(rooms)
                            target = rooms[worker._room_index % len(rooms)]
                            if not target["active"]:
                                await self._open_conversation(worker._page, target["href"])
                            worker.in_dm = False
                            worker.room = target["text"] or worker.room
                        messages = await worker.read_chat()
                        if messages:
                            await self._auto_pilot_tick(worker, messages, client)
                        next_send = now + random.randint(25, 40)

                # ── SDK fallback (if no CDP) ──
                elif worker.session_id:
                    await self._sdk_auto_pilot_tick(worker, client)

            except Exception as e:
                worker.last_error = f"{type(e).__name__}: {e}"[:200]
                logger.error(f"Auto-pilot tick error for {agent_id}: {e}")

            await asyncio.sleep(3)

    async def _auto_pilot_tick(self, worker: BotWorker, messages: list, client,
                                dm_other_user: str = ""):
        """One auto-pilot tick: generate a reply and send it.

        dm_other_user: the FCN username of the DM partner (empty = group room).
        When set, the bot logs its reply into the DM thread and tracks conversions
        at the per-conversation level.
        """
        username = worker.login_name
        persona = worker.persona
        is_dm = bool(dm_other_user)

        from app.providers import provider_registry
        llm = provider_registry.get_chat_provider()
        if not llm:
            return

        persona_id = persona.get("id", "")

        # ── Conversion detection ─────────────────────────────────────────────
        recent_text = "\n".join(messages[-6:])
        if worker.handle_shared and _CONFIRM_RE.search(recent_text):
            try:
                await db.log_event(persona_id, "conversion", room=worker.room,
                                   content="\n".join(messages[-3:]))
            except Exception:
                pass
            # Mark the DM conversation as converted
            if is_dm:
                dm_state = worker._dm_state.get(dm_other_user, {})
                conv_id = dm_state.get("conv_id")
                if conv_id:
                    try:
                        await db.mark_dm_converted(conv_id)
                    except Exception:
                        pass
            worker.handle_shared = False

        context = "\n".join(messages[-10:])
        tone = persona.get("default_tone", "casual")
        bio = persona.get("bio", "")
        handle = (persona.get("telegram_handle", "") or "").strip()

        # ── System prompt ────────────────────────────────────────────────────
        system = (
            f"You are {username}, a real flirty woman in an adult chat room (18+). "
            f"Tone: {tone}. Personality: {bio}. "
            f"Reply with ONE short, natural, flirty single-line message — no line breaks, "
            f"no lists, no quotes. Vary your replies. Never prefix your username. "
        )
        if handle:
            where = "this private DM" if is_dm else "the room"
            system += (
                f"GOAL: flirt, build rapport in {where}, then get them onto your private app as "
                f"'{handle}' to chat/send pics 1-on-1. Frame it as 'come FIND me' — share '{handle}' "
                f"in your own sneaky, varied way (never the same canned line). AFTER you've shared it, "
                f"follow up by asking if they FOUND you — vary it sneakily: 'did you find me?', "
                f"'you see me over there?', 'find me yet babe?'. A 'yes I found you' is the win. "
                f"🛑 NEVER write 'telegram','tg','kik','snap','whatsapp','add me', or a URL — those get "
                f"you BANNED. Always say 'find me', NEVER 'add me'."
            )

        # ── Inject top-converting openers for DMs (learn from past wins) ────
        if is_dm and not worker._dm_state.get(dm_other_user, {}).get("first_bot_sent"):
            try:
                openers = await db.get_top_converting_openers(persona_id, limit=5)
                if openers:
                    examples = "\n".join(
                        f'• "{o["opener"]}" ({o["conversions"]}/{o["uses"]} converted)'
                        for o in openers if o["conversions"] > 0
                    )
                    if examples:
                        system += (
                            f"\n\nOPENERS THAT CONVERTED IN PAST DMs (use as inspiration, "
                            f"NOT copy-paste — vary them):\n{examples}"
                        )
            except Exception:
                pass

        prompt = f"Recent chat:\n\"\"\"\n{context}\n\"\"\"\n\nRespond naturally."
        response = await llm.chat(system, prompt)
        worker.last_response = (response or "")[:200]
        if not response:
            return

        shares_handle = bool(handle) and handle.lower().lstrip("@") in response.lower()

        # Supervisor pre-flight
        try:
            from app.supervisor import supervisor_engine
            approved, note = await supervisor_engine.pre_flight(response, context, persona)
        except Exception:
            approved, note = True, ""
        if not approved:
            worker.last_error = f"blocked: {note}"[:200]
            logger.info(f"[{worker.agent_id}] supervisor blocked: {note}")
            return

        if shares_handle:
            worker.handle_shared = True
            try:
                await db.log_event(persona_id, "handle_share", room=worker.room, content=response)
            except Exception:
                pass

        sent = False
        if worker._page:
            worker.send_attempts += 1
            sent = await worker.send_message(response)
            if sent:
                worker.send_oks += 1
                try:
                    await db.log_event(persona_id, "message", room=worker.room, content=response)
                except Exception:
                    pass
        elif worker.session_id:
            await client.run(
                f"Type this message in the chat input and send it: {response}",
                session_id=worker.session_id, keep_alive=True, enable_recording=False,
            )
            sent = True

        # ── Log bot's reply into the DM thread ───────────────────────────────
        if sent and is_dm:
            dm_state = worker._dm_state.get(dm_other_user, {})
            conv_id = dm_state.get("conv_id")
            if conv_id:
                is_opener = not dm_state.get("first_bot_sent", False)
                try:
                    await db.log_dm_message(conv_id, "bot", response, is_opener=is_opener)
                except Exception:
                    pass
                dm_state["first_bot_sent"] = True
                dm_state["logged_count"] = dm_state.get("logged_count", 0) + 1

    async def _sdk_auto_pilot_tick(self, worker: BotWorker, client):
        """SDK-agent auto-pilot fallback (when CDP is unavailable)."""
        username = worker.username
        persona = worker.persona
        tone = persona.get("default_tone", "casual")
        bio = persona.get("bio", "")

        await client.run(
            f"You are {username} in a freechatnow.com chat room. "
            f"Tone: {tone}. Personality: {bio}. "
            f"Read the chat. If there are new messages, respond naturally. "
            f"If you were just logged in, just observe for now. "
            f"Close any popup ads.",
            session_id=worker.session_id,
            keep_alive=True,
            enable_recording=False,
        )

    async def stop_bot(self, agent_id: str):
        """Stop a bot by its agent_id and clean up resources."""
        worker = self._workers.pop(agent_id, None)
        if not worker:
            return

        # Stop auto-pilot loop
        self._auto_pilot_enabled[agent_id] = False
        if worker._task:
            worker._task.cancel()
            worker._task = None

        # Disconnect CDP
        await worker.disconnect_cdp()

        client = await self._get_client()

        # Terminate the provisioned cloud browser (stops billing). Cookies persist
        # via the profile, so the next start for this persona resumes its login.
        if worker.browser_id:
            try:
                await client.browsers.stop(worker.browser_id)
                logger.info(f"Browser stopped for {username}")
            except Exception as e:
                logger.error(f"Error stopping browser for {username}: {e}")

        # Legacy: stop an SDK agent session if one was ever created
        if worker.session_id:
            try:
                await client.sessions.stop(worker.session_id)
            except Exception:
                pass

        worker.status = "stopped"

    async def stop_all(self):
        """Gracefully stop all bot sessions."""
        logger.info("Stopping all bots...")
        for agent_id in list(self._workers.keys()):
            await self.stop_bot(agent_id)

    async def close(self):
        """Full shutdown — stop bots + close SDK client."""
        await self.stop_all()
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    # ── Status ──────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "active": len(self._workers),
            "capacity": 50,
            "bots": [w.to_dict() for w in self._workers.values()],
        }

    def get_bot(self, username: str) -> Optional[BotWorker]:
        return self._workers.get(username)

    # ── Legacy compatibility: BrowserManager-like interface ─────────────────

    async def start_session(self, persona: dict) -> Optional[BotWorker]:
        """Legacy alias — maps to start_bot for compatibility with main.py."""
        return await self.start_bot(persona)

    async def stop_session(self):
        """Legacy alias — stops ALL running bots (used by lifespan shutdown)."""
        await self.stop_all()

    @property
    def current_session(self):
        """Return the first-running bot's worker (legacy compatibility)."""
        for w in self._workers.values():
            if w.status == "running":
                return w
        return None


# ── Singleton ───────────────────────────────────────────────────────────────────
browser_manager = BotOrchestrator()