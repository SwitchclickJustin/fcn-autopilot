"""Chat Avenue (adultchat.chat-avenue.com) broadcast adapter.

GROUP-ONLY, TEXT-ONLY: log in as a guest, join a high-traffic guest room, and broadcast
every 20-45s — the obfuscated handle rides in the TEXT every message (no photos on Chat
Avenue), reusing the FCN AI brain — BU Cloud provisioning, LLM, sanitizers, handle
obfuscation. No DM engine, no photos: the Chat Avenue funnel is broadcast -> Telegram add
(confirmed fast in manual tests).

Selectors mapped live 2026-06-20 (Chrome recon):
  entry         https://adultchat.chat-avenue.com/
  guest button  .intro_guest_btn
  guest form    #guest_username, #guest_gender, #date_day/#date_month/#date_year,
                Cloudflare Turnstile (auto-solved by BU Cloud residential)
  submit        .theme_btn.full_button
  room list     hamburger menu -> "Room list" -> click a room row (e.g. "Adult Chat" ~964)
  send input    #content  (placeholder "Type here...")
  send button   button#submit_button.send_btn   (paper-plane)

Each browser step is marked LIVE-TEST: validate against the running site (deploy +
watch the live_url) before trusting it. Built incrementally.
"""
import asyncio
import logging
import random
import time

from app import browser as fcn  # reuse helpers + BU Cloud provisioning

logger = logging.getLogger(__name__)

CHAT_URL = "https://adultchat.chat-avenue.com/"
# High-traffic, guest-allowed rooms (from recon). Avoid registered-only (DICE, Desktop).
GUEST_ROOMS = ["Adult Chat", "Taboo", "Seniors Room"]
BROADCAST_MIN_S, BROADCAST_MAX_S = 15, 25   # default cadence when the persona has none


def _broadcast_interval(persona: dict) -> tuple:
    """Seconds between broadcasts (min, max), read from the persona's cooldown_min/max so
    operators tune cadence per-persona. Falls back to 15-25s when unset/invalid, and never
    blasts faster than 5s."""
    try:
        lo = int(persona.get("cooldown_min") or BROADCAST_MIN_S)
        hi = int(persona.get("cooldown_max") or BROADCAST_MAX_S)
    except (TypeError, ValueError):
        lo, hi = BROADCAST_MIN_S, BROADCAST_MAX_S
    lo = max(5, lo)
    hi = max(lo, hi)
    return lo, hi


class ChatAvenueWorker:
    """One Chat Avenue broadcaster. Composes a BotWorker for the BU Cloud browser +
    send_photo + the shared `_page`, so we reuse FCN's provisioning untouched."""

    def __init__(self, persona: dict, agent_id: str, slot: int = 0, agent_total: int = 1):
        self.persona = persona
        self.agent_id = agent_id
        self.slot = slot
        self.agent_total = max(1, agent_total)
        self.login_name = ""
        self.status = "init"
        self.room = ""
        self.started_at = time.time()
        self.send_oks = 0
        self.send_attempts = 0
        self._recent_msgs: list = []          # no-repeat memory
        self._task = None
        self._stop = False
        # Cadence (seconds between blasts) from the persona's cooldown_min/max, 15-25s default.
        self.cd_min, self.cd_max = _broadcast_interval(persona)
        # Composition: a BotWorker carries the CDP page + send_photo + slicing slot.
        self._bw = fcn.BotWorker(persona)
        self._bw.agent_id = agent_id
        self._bw.slot = slot
        self._bw.agent_total = self.agent_total
        # Distinct US Decodo IP per agent (first 50 entries = us.decodo.com) so every guest
        # registration comes from a fresh, unused IP — avoids Chat Avenue's per-IP cap.
        self.custom_proxy = fcn.DECODA_PROXIES[slot % 50]

    @property
    def _page(self):
        return self._bw._page

    # ── login ────────────────────────────────────────────────────────────────
    async def login(self) -> bool:
        """Guest login. LIVE-TEST: Turnstile clears on BU Cloud residential; the gender/DOB
        selects are selectboxit-styled — select_option drives the underlying <select>, but
        confirm the form reads it (may need to also click the styled option)."""
        page = self._page
        if not page:
            return False
        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            await page.click(".intro_guest_btn", timeout=8000)
            await page.wait_for_timeout(1200)
            name = fcn.BotOrchestrator._unique_username.__func__(None) if False else _guest_name()
            self.login_name = name
            self._bw.login_name = name
            await page.fill("#guest_username", name, timeout=5000)
            # Gender/DOB are selectboxit widgets. The ONLY thing that updates both selectboxit's
            # display AND the native <select> the form submits is jQuery .val().trigger('change')
            # — verified in Chrome (anchor-click and vanilla dispatchEvent do NOT sync selectboxit).
            # Chat Avenue loads jQuery, so this is reliable.
            try:
                gset = await page.evaluate("""() => {
                    if (!window.jQuery) return 'no-jquery';
                    const setSB = (id, m) => { const s=document.getElementById(id); if(!s) return;
                        const o=[...s.options].find(m); if(o) jQuery(s).val(o.value).trigger('change'); };
                    setSB('guest_gender', o=>/female/i.test(o.text));
                    setSB('date_day',   o=>o.text.trim()==='15');
                    setSB('date_month', o=>/^jun/i.test(o.text.trim()));
                    setSB('date_year',  o=>/^(200[0-6])$/.test(o.text.trim()));
                    return (document.getElementById('guest_gender')||{}).value;  // expect '2' = Female
                }""")
                logger.info(f"[{self.agent_id}] CA gender set -> {gset}")
                await page.wait_for_timeout(400)
            except Exception as e:
                logger.warning(f"[{self.agent_id}] CA gender/DOB set issue: {e}")
            # Wait for Cloudflare Turnstile to auto-solve (token populated) before submitting.
            solved = False
            for _ in range(44):                                    # up to ~22s (was 12s)
                tok = await page.evaluate("() => { const e=document.querySelector('input[name=cf-turnstile-response]'); return e ? (e.value||'') : 'none'; }")
                if tok and tok != 'none':
                    solved = True
                    logger.info(f"[{self.agent_id}] CA turnstile solved")
                    break
                await page.wait_for_timeout(500)
            if not solved:
                logger.warning(f"[{self.agent_id}] CA turnstile NOT solved in 22s — submitting anyway")
            await page.click(".theme_btn.full_button", timeout=8000)
            await page.wait_for_timeout(4000)                       # lobby render
            # Per-IP guest-registration cap (distinct from a form failure).
            capped = await page.evaluate(
                "() => /maximum allowed registrations|try again later/i.test((document.body||{}).innerText||'')")
            if capped:
                self.status = "ip_capped"
                logger.warning(f"[{self.agent_id}] CA IP CAPPED (max guest registrations — needs a fresh IP)")
                return False
            ok = await page.evaluate("() => !document.getElementById('guest_username')")
            self.status = "lobby" if ok else "login_failed"
            if ok:
                logger.info(f"[{self.agent_id}] CA login OK as {name}")
            else:
                snip = await page.evaluate("() => ((document.body||{}).innerText||'').replace(/\\s+/g,' ').slice(0,160)")
                logger.warning(f"[{self.agent_id}] CA login FAILED (turnstile_solved={solved}) — page: {snip}")
            return bool(ok)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA login error: {e}")
            return False

    # ── join a room ──────────────────────────────────────────────────────────
    async def join_room(self) -> bool:
        """Open the room list and click a high-traffic guest room. Each agent picks a
        different room by slot to spread out. LIVE-TEST: the room-list control is the
        hamburger; rooms are clickable rows matched by name text."""
        page = self._page
        if not page:
            return False
        try:
            # After guest login Chat Avenue lands on the room-selection page: #container_rooms
            # with `.room_element` rows, each holding a `.room_count`. If we're already inside a
            # room instead, open the menu -> "Room list" to surface the same rows.
            await page.wait_for_timeout(900)
            has_list = await page.evaluate("() => !!document.querySelector('.room_element .room_count')")
            if not has_list:
                await page.evaluate("""() => {
                    const b = document.querySelector('.menu_toggle, [class*=burger i], #menu_button')
                        || document.querySelectorAll('.fa-bars, [class*=bars i]')[0];
                    if (b) (b.closest('[class*=click i]')||b).click();
                }""")
                await page.wait_for_timeout(800)
                await page.evaluate("""() => {
                    const rl = Array.from(document.querySelectorAll('*'))
                        .find(e => /^\\s*Room list\\s*$/i.test(e.textContent||'') && e.querySelectorAll('*').length<4);
                    if (rl) (rl.closest('[class*=click i]')||rl).click();
                }""")
                await page.wait_for_timeout(1400)
            # Pick the busiest GUEST-accessible room and click it. Rooms that say "no guests" /
            # "registered users only" (e.g. Adult Chat - Desktop Version) are skipped — a guest
            # can't post there. slot N takes the Nth-busiest so agents don't all stack one room.
            chosen = await page.evaluate("""(slot) => {
                const rooms = Array.from(document.querySelectorAll('.room_element, .blisting'))
                    .filter(r => r.offsetParent !== null && r.querySelector('.room_count'));
                const data = rooms.map(r => {
                    const count = parseInt((r.querySelector('.room_count').textContent||'').trim()) || 0;
                    const lines = (r.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
                    const name = lines[0] || '?';
                    const noGuests = /no guests|registered users only/i.test(r.textContent||'');
                    return {r, name, count, ok: !noGuests};
                }).filter(x => x.ok).sort((a,b) => b.count - a.count);
                if (!data.length) return null;
                const pick = data[Math.min(slot, data.length-1)];
                pick.r.click();
                return {name: pick.name, count: pick.count,
                        list: data.slice(0,8).map(d => ({n:d.name, c:d.count}))};
            }""", self.slot)
            if chosen:
                logger.info(f"[{self.agent_id}] CA rooms by traffic: {chosen['list']}")
            await page.wait_for_timeout(2500)
            in_room = await page.evaluate("() => !!document.getElementById('content')")
            if in_room:
                self.room = chosen["name"] if chosen else "?"
                self.status = "running"
                logger.info(f"[{self.agent_id}] CA joined room '{self.room}' ({chosen['count'] if chosen else '?'} online)")
            return bool(in_room)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA join_room error: {e}")
            return False

    # ── send ───────────────────────────────────────────────────────────────────
    async def send(self, text: str) -> bool:
        """Fill #content and submit. Reuses FCN's validated one-shot fill() approach."""
        page = self._page
        if not page or not text:
            return False
        text = " ".join(text.split())[:300].strip()
        try:
            inp = await page.query_selector("#content")
            if inp is None:
                return False
            self.send_attempts += 1
            await inp.fill(text, timeout=2500)
            # send: click the button, fall back to Enter
            try:
                await page.click("#submit_button", timeout=2000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            cleared = await page.evaluate("() => { const e=document.getElementById('content'); return e ? !e.value : false; }")
            if cleared:
                self.send_oks += 1
            return bool(cleared)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA send error: {e}")
            return False

    # ── broadcast generation (reuses FCN brain) ─────────────────────────────────
    async def _make_broadcast(self) -> str:
        from app.providers import provider_registry
        llm = provider_registry.get_chat_provider()
        if not llm:
            return ""
        bio = self.persona.get("bio", "")
        goals = (self.persona.get("goals", "") or "").strip()   # the "concepts" the operator gives
        handle = (self.persona.get("telegram_handle", "") or "").strip()
        handle_cap = handle.capitalize() if handle else ""
        no_repeat = ""
        if self._recent_msgs:
            no_repeat = ("Do NOT repeat or paraphrase any of these you just sent: "
                         + " | ".join(f'"{m}"' for m in self._recent_msgs[-5:]) + ". ")
        system = (
            f"You are {self.login_name}, posting in a busy public adult group chat (18+). "
            f"Personality: {bio}. "
            + (f"CONCEPTS — what to say / angles to rotate through: {goals}. " if goals else "")
            + f"Write ONE short broadcast to the WHOLE room (never a reply to one person, never "
            f"'where u from'). Work in a call to find you + your handle '{handle_cap}'. Refer to the "
            f"platform ONLY as 'TG' or 'Tela Grahm' — NEVER write 'telegram'. ONE short punchy line, "
            f"no emojis, no quotes, no stage directions, no [tags]. Vary it every time. {no_repeat}"
        )
        resp = await llm.chat(system, "Write the broadcast.")
        if not resp:
            return ""
        # reuse the FCN guard/sanitizer chain
        if handle:
            resp = fcn._scrub_retired_handles(resp, handle)
            resp = fcn._normalize_handle(resp, handle)
        tg = fcn._pick_tg_token(is_dm=False)
        send_text = fcn._obfuscate_handle(resp, handle, tg) if handle else fcn._sanitize_platforms(resp, tg)
        send_text = fcn._force_group_cta(send_text, handle, tg) if handle else send_text
        send_text = fcn._strip_ai_tells(send_text, strip_emoji=True)
        return send_text

    # ── run loop ─────────────────────────────────────────────────────────────────
    async def run(self):
        if not await self.login():
            self.status = "login_failed"; return
        if not await self.join_room():
            self.status = "join_failed"; return
        logger.info(f"[{self.agent_id}] CA broadcasting every {self.cd_min}-{self.cd_max}s in '{self.room}'")
        while not self._stop:
            try:
                text = await self._make_broadcast()
                if text:
                    sent = await self.send(text)
                    if sent:
                        self._recent_msgs.append(text)
                        self._recent_msgs = self._recent_msgs[-8:]
                        logger.info(f"[{self.agent_id}] CA BROADCAST room={self.room}: {text}")
            except Exception as e:
                logger.warning(f"[{self.agent_id}] CA loop error: {e}")
            await asyncio.sleep(random.uniform(self.cd_min, self.cd_max))

    async def stop(self):
        self._stop = True


_FEMALE = ["Mia", "Sofia", "Luna", "Lola", "Bella", "Nina", "Maya", "Jade", "Lexi", "Stella"]


def _guest_name() -> str:
    return f"{random.choice(_FEMALE)}{random.randint(10, 9999)}"
